"""会话仓储混合持久化实现：SQLite 元数据 + JSONL 消息文件。

存储布局：
    {data_dir}/
    ├── conversations.db                  ← SQLite（会话元数据 + 派生状态）
    └── messages/
        └── {task_id}/
            └── {conversation_id}.jsonl   ← 每行一条消息

消息采用 JSONL 格式以获得最佳读写效率：
- append_message: 追加一行（O(1)）
- get（加载完整历史）: 顺序读取文件
- save（全量覆写）: 重写整个文件
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from agent_runtime.conversation_message.message.models import (
    Conversation,
    ConversationState,
    ImagePart,
    PdfPart,
    TextPart,
    ThinkingPart,
    ToolResultPart,
    ToolUsePart,
    UniMessage,
    UniPart,
)


# ── 序列化辅助函数 ──────────────────────────────────────────────


def _part_to_dict(part: UniPart) -> dict[str, Any]:
    """将统一消息内容片段序列化为字典，用于 JSONL 持久化。"""
    if isinstance(part, TextPart):
        return {"kind": "text", "text": part.text}
    if isinstance(part, ImagePart):
        return {"kind": "image", "resource_id": part.resource_id}
    if isinstance(part, PdfPart):
        return {"kind": "pdf", "resource_id": part.resource_id}
    if isinstance(part, ThinkingPart):
        return {"kind": "thinking", "text": part.text}
    if isinstance(part, ToolUsePart):
        data = {"kind": "tool_use", "name": part.name, "input": part.input}
        if part.tool_use_id is not None:
            data["tool_use_id"] = part.tool_use_id
        return data
    if isinstance(part, ToolResultPart):
        return {
            "kind": "tool_result",
            "tool_use_ref": part.tool_use_ref,
            "is_error": part.is_error,
            "output": part.output,
        }
    raise TypeError(f"Unknown part type: {type(part)}")


def _dict_to_part(d: dict[str, Any]) -> UniPart:
    """将字典反序列化为统一消息内容片段，用于从 JSONL 文件还原消息。"""
    kind = d["kind"]
    if kind == "text":
        return TextPart(text=d["text"])
    if kind == "image":
        return ImagePart(resource_id=d["resource_id"])
    if kind == "pdf":
        return PdfPart(resource_id=d["resource_id"])
    if kind == "thinking":
        return ThinkingPart(text=d.get("text"))
    if kind == "tool_use":
        return ToolUsePart(
            name=d["name"],
            input=d["input"],
            tool_use_id=d.get("tool_use_id"),
        )
    if kind == "tool_result":
        return ToolResultPart(
            tool_use_ref=d["tool_use_ref"],
            is_error=bool(d.get("is_error", False)),
            output=d["output"],
        )
    raise ValueError(f"Unknown part kind: {kind}")


def _message_to_json_line(msg: UniMessage) -> str:
    """将统一消息序列化为单行 JSON 字符串，用于 JSONL 文件追加写入。"""
    d = {
        "role": msg.role,
        "content": [_part_to_dict(p) for p in msg.content],
    }
    if msg.source_provider is not None:
        d["source_provider"] = msg.source_provider
    if msg.source_vendor is not None:
        d["source_vendor"] = msg.source_vendor
    if msg.provider_extras is not None:
        d["provider_extras"] = msg.provider_extras
    return json.dumps(d, ensure_ascii=False)


def _json_line_to_message(line: str) -> UniMessage:
    """将单行 JSON 字符串反序列化为统一消息对象，用于从 JSONL 文件还原消息。"""
    d = json.loads(line)
    return UniMessage(
        role=d["role"],
        content=[_dict_to_part(p) for p in d["content"]],
        source_provider=d.get("source_provider"),
        source_vendor=d.get("source_vendor"),
        provider_extras=d.get("provider_extras"),
    )


# ── SQLite Schema ────────────────────────────────────────────────

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS conversations (
    id                TEXT PRIMARY KEY,
    task_id           TEXT NOT NULL,
    thread_id         TEXT NOT NULL,
    agent_id          TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_task           ON conversations(task_id);
CREATE INDEX IF NOT EXISTS idx_conv_thread         ON conversations(thread_id);
CREATE INDEX IF NOT EXISTS idx_conv_agent          ON conversations(agent_id);
CREATE INDEX IF NOT EXISTS idx_conv_agent_instance ON conversations(agent_instance_id);

CREATE TABLE IF NOT EXISTS conversation_state (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    providers       TEXT    NOT NULL DEFAULT '[]',
    vendors         TEXT    NOT NULL DEFAULT '[]',
    modalities      TEXT    NOT NULL DEFAULT '[]',
    image_count     INTEGER NOT NULL DEFAULT 0,
    pdf_page_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS provider_state (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,
    state           TEXT NOT NULL,
    PRIMARY KEY (conversation_id, provider)
);
"""


class SqliteConversationRepository:
    """会话仓储混合持久化实现：SQLite 元数据 + JSONL 消息文件。

    存储布局：
        {data_dir}/
        ├── conversations.db                  ← SQLite（会话元数据 + 派生状态）
        └── messages/
            └── {task_id}/
                └── {conversation_id}.jsonl   ← 每行一条消息

    消息采用 JSONL 格式以获得最佳读写效率：
    - append_message: 追加一行（O(1)）
    - get（加载完整历史）: 顺序读取文件
    - save（全量覆写）: 重写整个文件
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        db_path = self._data_dir / "conversations.db"
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """关闭底层数据库连接。"""
        self._conn.close()

    # ── 核心增删改查 ─────────────────────────────────────────────

    async def create(
        self, conv: Conversation, *, task_id: str, thread_id: str,
        agent_id: str, agent_instance_id: str,
    ) -> None:
        c = self._conn
        c.execute(
            "INSERT INTO conversations (id, task_id, thread_id, agent_id, agent_instance_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (conv.id, task_id, thread_id, agent_id, agent_instance_id),
        )
        self._upsert_state(conv.id, conv.state)
        c.commit()
        # 写入已有消息（创建时通常为空）。
        if conv.messages:
            self._write_messages_file(task_id, conv.id, conv.messages)
        else:
            # 确保目录和空文件存在。
            path = self._msg_path(task_id, conv.id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    async def get(self, conversation_id: str) -> Conversation:
        c = self._conn

        row = c.execute(
            "SELECT id, task_id FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Conversation not found: {conversation_id}")
        task_id = row[1]

        # 从 JSONL 文件读取消息列表。
        messages = self._read_messages_file(task_id, conversation_id)

        # 从 SQLite 读取会话派生状态。
        sr = c.execute(
            "SELECT providers, vendors, modalities, image_count, pdf_page_count "
            "FROM conversation_state WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        state = ConversationState(
            providers=json.loads(sr[0]),
            vendors=json.loads(sr[1]),
            modalities=json.loads(sr[2]),
            image_count=sr[3],
            pdf_page_count=sr[4],
        ) if sr else ConversationState()

        # 从 SQLite 读取各 LLM 供应商的会话状态。
        ps_rows = c.execute(
            "SELECT provider, state FROM provider_state WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchall()
        provider_state: dict[str, Any] = {r[0]: json.loads(r[1]) for r in ps_rows}

        return Conversation(
            id=conversation_id,
            messages=messages,
            state=state,
            provider_state=provider_state,
        )

    async def save(self, conv: Conversation) -> None:
        c = self._conn
        cid = conv.id
        task_id = self._get_task_id(cid)
        # 覆写 JSONL 消息文件。
        self._write_messages_file(task_id, cid, conv.messages)
        # 覆写会话派生状态。
        self._upsert_state(cid, conv.state)
        c.execute("DELETE FROM provider_state WHERE conversation_id = ?", (cid,))
        for provider, ps in conv.provider_state.items():
            c.execute(
                "INSERT OR REPLACE INTO provider_state "
                "(conversation_id, provider, state) VALUES (?, ?, ?)",
                (cid, provider, json.dumps(ps, ensure_ascii=False)),
            )
        c.commit()

    async def delete(self, conversation_id: str) -> None:
        task_id = self._get_task_id(conversation_id)
        # 删除 JSONL 消息文件。
        if task_id:
            path = self._msg_path(task_id, conversation_id)
            path.unlink(missing_ok=True)
        # SQLite 外键 CASCADE 自动删除关联的状态行。
        self._conn.execute(
            "DELETE FROM conversations WHERE id = ?", (conversation_id,),
        )
        self._conn.commit()

    # ── 增量操作 ─────────────────────────────────────────────────

    async def append_message(self, conversation_id: str, msg: UniMessage) -> None:
        task_id = self._get_task_id(conversation_id)
        if not task_id:
            raise KeyError(f"Conversation not found: {conversation_id}")
        path = self._msg_path(task_id, conversation_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(_message_to_json_line(msg) + "\n")

    async def save_state(self, conversation_id: str, state: ConversationState) -> None:
        self._upsert_state(conversation_id, state)
        self._conn.commit()

    async def save_provider_state(
        self, conversation_id: str, provider: str, state: Any,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO provider_state "
            "(conversation_id, provider, state) VALUES (?, ?, ?)",
            (conversation_id, provider, json.dumps(state, ensure_ascii=False)),
        )
        self._conn.commit()

    # ── 层级查询 ─────────────────────────────────────────────────

    async def list_by_task(self, task_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT id FROM conversations WHERE task_id = ?", (task_id,),
        ).fetchall()
        return [r[0] for r in rows]

    async def list_by_thread(self, thread_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT id FROM conversations WHERE thread_id = ?", (thread_id,),
        ).fetchall()
        return [r[0] for r in rows]

    async def list_by_agent(self, agent_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT id FROM conversations WHERE agent_id = ?", (agent_id,),
        ).fetchall()
        return [r[0] for r in rows]

    async def list_by_agent_instance(self, agent_instance_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT id FROM conversations WHERE agent_instance_id = ?",
            (agent_instance_id,),
        ).fetchall()
        return [r[0] for r in rows]

    # ── 内部辅助方法 ─────────────────────────────────────────────

    def _msg_path(self, task_id: str, conversation_id: str) -> Path:
        """返回指定会话的 JSONL 消息文件路径。"""
        return self._data_dir / "messages" / task_id / f"{conversation_id}.jsonl"

    def _get_task_id(self, conversation_id: str) -> str | None:
        """根据会话 ID 查找所属任务 ID。未找到时返回 None。"""
        row = self._conn.execute(
            "SELECT task_id FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        return row[0] if row else None

    def _read_messages_file(self, task_id: str, conversation_id: str) -> list[UniMessage]:
        """从 JSONL 文件读取指定会话的全部消息。"""
        path = self._msg_path(task_id, conversation_id)
        if not path.exists():
            return []
        messages: list[UniMessage] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    messages.append(_json_line_to_message(line))
        return messages

    def _write_messages_file(
        self, task_id: str, conversation_id: str, messages: list[UniMessage],
    ) -> None:
        """全量覆写指定会话的 JSONL 消息文件。"""
        path = self._msg_path(task_id, conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(_message_to_json_line(msg) + "\n")

    def _upsert_state(self, cid: str, state: ConversationState) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO conversation_state "
            "(conversation_id, providers, vendors, modalities, image_count, pdf_page_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                cid,
                json.dumps(state.providers, ensure_ascii=False),
                json.dumps(state.vendors, ensure_ascii=False),
                json.dumps(state.modalities, ensure_ascii=False),
                state.image_count,
                state.pdf_page_count,
            ),
        )
