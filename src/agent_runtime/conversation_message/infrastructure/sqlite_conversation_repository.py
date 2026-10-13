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
import logging
import os
import sqlite3
import uuid
from pathlib import Path
from threading import RLock
from typing import Any

logger = logging.getLogger(__name__)

from agent_runtime.conversation_message.message.models import (
    ConfigurationComponentEvent,
    Conversation,
    ConversationState,
    ImagePart,
    InstructionsSnapshot,
    PdfPart,
    TextPart,
    ThinkingPart,
    ToolResultPart,
    ToolUsePart,
    UniMessage,
    UniPart,
)
from agent_runtime.conversation_message.message.tool_result_output import normalize_tool_output_for_json


# ── 序列化辅助函数 ──────────────────────────────────────────────


def _configuration_event_to_dict(evt: ConfigurationComponentEvent) -> dict[str, Any]:
    """将配置事件序列化为字典，用于 SQLite 持久化。"""
    snapshot = evt.snapshot
    if isinstance(snapshot, tuple):
        snapshot = list(snapshot)
    return {
        "event_id": evt.event_id,
        "definition_id": evt.definition_id,
        "anchor": evt.anchor,
        "anchor_message_id": evt.anchor_message_id,
        "sequence": evt.sequence,
        "status": evt.status,
        "snapshot": snapshot,
        "revision": evt.revision,
        "component": evt.component,
        "supplementary_prompts": list(evt.supplementary_prompts),
    }


def _dict_to_configuration_event(d: dict[str, Any]) -> ConfigurationComponentEvent:
    """将字典反序列化为配置事件对象。"""
    snapshot = d.get("snapshot")
    if isinstance(snapshot, list):
        snapshot = tuple(snapshot)
    return ConfigurationComponentEvent(
        event_id=d["event_id"],
        definition_id=d["definition_id"],
        anchor=d.get("anchor", "start"),
        anchor_message_id=d.get("anchor_message_id"),
        sequence=d.get("sequence", 0),
        status=d.get("status", "historical"),
        snapshot=snapshot,
        revision=d.get("revision"),
        component=d.get("component"),
        supplementary_prompts=tuple(d.get("supplementary_prompts") or ()),
    )


def _snapshot_to_json(snapshot: InstructionsSnapshot | None) -> str | None:
    """将 InstructionsSnapshot 序列化为 JSON 字符串。"""
    if snapshot is None:
        return None
    return json.dumps(
        {
            "template_names": list(snapshot.template_names),
            "content": snapshot.content,
        },
        ensure_ascii=False,
    )


def _json_to_snapshot(raw: str | None) -> InstructionsSnapshot | None:
    """将 JSON 字符串反序列化为 InstructionsSnapshot 对象。"""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return InstructionsSnapshot(
            template_names=tuple(data.get("template_names") or ()),
            content=str(data.get("content") or ""),
        )
    except Exception:
        return None


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
            "output": _tool_result_output_to_json(part.output),
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
            output=_tool_result_output_from_json(d["output"]),
        )
    raise ValueError(f"Unknown part kind: {kind}")


def _tool_result_output_to_json(output: Any) -> Any:
    return normalize_tool_output_for_json(output)


def _tool_result_output_from_json(output: Any) -> Any:
    return normalize_tool_output_for_json(output)


def _message_to_json_line(msg: UniMessage) -> str:
    """将统一消息序列化为单行 JSON 字符串，用于 JSONL 文件追加写入。"""
    d = {
        "message_id": msg.message_id,
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


def _json_line_to_message(
    line: str,
    *,
    legacy_message_id: str | None = None,
) -> UniMessage:
    """将单行 JSON 字符串反序列化为统一消息对象，用于从 JSONL 文件还原消息。"""
    d = json.loads(line)
    return UniMessage(
        role=d["role"],
        content=[_dict_to_part(p) for p in d["content"]],
        message_id=d.get("message_id") or legacy_message_id or f"msg_{uuid.uuid4().hex}",
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
    pdf_page_count  INTEGER NOT NULL DEFAULT 0,
    last_context_usage TEXT,
    task_note_tracking TEXT,
    task_note_tracking_version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS provider_state (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,
    state           TEXT NOT NULL,
    PRIMARY KEY (conversation_id, provider)
);

CREATE TABLE IF NOT EXISTS pending_tool_results (
    conversation_id          TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    assistant_message_index  INTEGER NOT NULL,
    tool_use_ref             TEXT NOT NULL,
    result_part              TEXT NOT NULL,
    PRIMARY KEY (conversation_id, assistant_message_index, tool_use_ref)
);

CREATE TABLE IF NOT EXISTS conversation_configuration (
    conversation_id       TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    timeline              TEXT NOT NULL DEFAULT '[]',
    system_prompt         TEXT,
    tools                 TEXT NOT NULL DEFAULT '[]',
    instructions_snapshot TEXT
);
"""


_TIMELINE_CACHE: dict[str, list[Any]] = {}
_SYSTEM_PROMPT_CACHE: dict[str, str | None] = {}
_TOOLS_CACHE: dict[str, tuple[dict[str, Any], ...]] = {}
_INSTRUCTIONS_SNAPSHOT_CACHE: dict[str, InstructionsSnapshot | None] = {}


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
        # Agent 执行会在 FastAPI 请求线程与后台执行线程之间切换；同一仓储连接允许跨线程复用，
        # 但所有 SQLite 与对应 JSONL 文件访问必须由仓储锁串行化，避免同一连接并发操作。
        self._lock = RLock()
        self._conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate_schema()
        self._conn.commit()

    def close(self) -> None:
        """关闭底层数据库连接。"""
        with self._lock:
            self._conn.close()

    # ── 核心增删改查 ─────────────────────────────────────────────

    async def create(
        self, conv: Conversation, *, task_id: str, thread_id: str,
        agent_id: str, agent_instance_id: str,
    ) -> None:
        with self._lock:
            c = self._conn
            c.execute(
                "INSERT INTO conversations (id, task_id, thread_id, agent_id, agent_instance_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (conv.id, task_id, thread_id, agent_id, agent_instance_id),
            )
            self._upsert_state(conv.id, conv.state)
            instructions_snapshot = getattr(conv, "instructions_snapshot", None)
            self._upsert_configuration(
                conv.id,
                conv.timeline,
                getattr(conv, "system_prompt", None),
                getattr(conv, "tools", ()),
                instructions_snapshot,
            )
            c.commit()
            _TIMELINE_CACHE[conv.id] = list(conv.timeline)
            _SYSTEM_PROMPT_CACHE[conv.id] = getattr(conv, "system_prompt", None)
            _TOOLS_CACHE[conv.id] = tuple(getattr(conv, "tools", ()))
            _INSTRUCTIONS_SNAPSHOT_CACHE[conv.id] = instructions_snapshot
            # 写入已有消息（创建时通常为空）。
            if conv.messages:
                self._write_messages_file(task_id, conv.id, conv.messages)
            else:
                # 确保目录和空文件存在。
                path = self._msg_path(task_id, conv.id)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()

    async def get(self, conversation_id: str) -> Conversation:
        with self._lock:
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
                "SELECT providers, vendors, modalities, image_count, pdf_page_count, last_context_usage, task_note_tracking "
                "FROM conversation_state WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            state = ConversationState(
                providers=json.loads(sr[0]),
                vendors=json.loads(sr[1]),
                modalities=json.loads(sr[2]),
                image_count=sr[3],
                pdf_page_count=sr[4],
                last_context_usage=json.loads(sr[5]) if sr[5] else None,
                task_note_tracking=json.loads(sr[6]) if len(sr) > 6 and sr[6] else None,
            ) if sr else ConversationState()

            # 从 SQLite 读取各 LLM 供应商的会话状态。
            ps_rows = c.execute(
                "SELECT provider, state FROM provider_state WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchall()
            provider_state: dict[str, Any] = {r[0]: json.loads(r[1]) for r in ps_rows}

            # 从 SQLite 读取会话配置与时间线（优先读取已持久化配置，保障重启后完整恢复）。
            cfg_row = c.execute(
                "SELECT timeline, system_prompt, tools, instructions_snapshot FROM conversation_configuration WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            instructions_snapshot: InstructionsSnapshot | None = None
            if cfg_row is not None:
                try:
                    timeline = [
                        _dict_to_configuration_event(item)
                        for item in json.loads(cfg_row[0])
                    ]
                except Exception:
                    timeline = list(_TIMELINE_CACHE.get(conversation_id, []))
                system_prompt = cfg_row[1]
                try:
                    tools = tuple(json.loads(cfg_row[2])) if cfg_row[2] else ()
                except Exception:
                    tools = _TOOLS_CACHE.get(conversation_id, ())
                try:
                    instructions_snapshot = _json_to_snapshot(cfg_row[3]) if len(cfg_row) > 3 else None
                except Exception:
                    instructions_snapshot = _INSTRUCTIONS_SNAPSHOT_CACHE.get(conversation_id)
            else:
                timeline = list(_TIMELINE_CACHE.get(conversation_id, []))
                system_prompt = _SYSTEM_PROMPT_CACHE.get(conversation_id)
                tools = _TOOLS_CACHE.get(conversation_id, ())
                instructions_snapshot = _INSTRUCTIONS_SNAPSHOT_CACHE.get(conversation_id)

            _TIMELINE_CACHE[conversation_id] = list(timeline)
            _SYSTEM_PROMPT_CACHE[conversation_id] = system_prompt
            _TOOLS_CACHE[conversation_id] = tuple(tools)
            _INSTRUCTIONS_SNAPSHOT_CACHE[conversation_id] = instructions_snapshot

            return Conversation(
                id=conversation_id,
                messages=messages,
                state=state,
                provider_state=provider_state,
                timeline=list(timeline),
                system_prompt=system_prompt,
                tools=tools,
                instructions_snapshot=instructions_snapshot,
            )

    async def save(self, conv: Conversation) -> None:
        with self._lock:
            c = self._conn
            cid = conv.id
            task_id = self._get_task_id(cid)
            if not task_id:
                raise KeyError(f"Conversation not found: {cid}")

            # 裁剪孤儿时间线事件：若非 start 锚点引用的 message_id 在当前会话中已被删除，自动剔除
            known_message_ids = {m.message_id for m in conv.messages}
            valid_timeline = [
                ev for ev in conv.timeline
                if ev.anchor == "start" or ev.anchor_message_id in known_message_ids
            ]
            if len(valid_timeline) != len(conv.timeline):
                conv.timeline = valid_timeline

            instructions_snapshot = getattr(conv, "instructions_snapshot", None)
            # 若时间线及配置均为空，物理删除配置记录，避免残留无效数据
            if (
                not conv.timeline
                and not getattr(conv, "system_prompt", None)
                and not getattr(conv, "tools", ())
                and instructions_snapshot is None
            ):
                self._conn.execute(
                    "DELETE FROM conversation_configuration WHERE conversation_id = ?",
                    (cid,),
                )
            else:
                self._upsert_configuration(
                    cid,
                    conv.timeline,
                    getattr(conv, "system_prompt", None),
                    getattr(conv, "tools", ()),
                    instructions_snapshot,
                )

            _TIMELINE_CACHE[cid] = list(conv.timeline)
            _SYSTEM_PROMPT_CACHE[cid] = getattr(conv, "system_prompt", None)
            _TOOLS_CACHE[cid] = tuple(getattr(conv, "tools", ()))
            _INSTRUCTIONS_SNAPSHOT_CACHE[cid] = instructions_snapshot
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
        with self._lock:
            _TIMELINE_CACHE.pop(conversation_id, None)
            _SYSTEM_PROMPT_CACHE.pop(conversation_id, None)
            _TOOLS_CACHE.pop(conversation_id, None)
            task_id = self._get_task_id(conversation_id)
            # 删除 JSONL 消息文件。
            if task_id:
                path = self._msg_path(task_id, conversation_id)
                path.unlink(missing_ok=True)
            # 物理删除配置记录。
            self._conn.execute(
                "DELETE FROM conversation_configuration WHERE conversation_id = ?",
                (conversation_id,),
            )
            # SQLite 外键 CASCADE 自动删除关联的状态行。
            self._conn.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,),
            )
            self._conn.commit()

    # ── 增量操作 ─────────────────────────────────────────────────

    async def append_message(self, conversation_id: str, msg: UniMessage) -> None:
        with self._lock:
            task_id = self._get_task_id(conversation_id)
            if not task_id:
                raise KeyError(f"Conversation not found: {conversation_id}")
            path = self._msg_path(task_id, conversation_id)
            with open(path, "a", encoding="utf-8") as f:
                f.write(_message_to_json_line(msg) + "\n")

    async def save_state(self, conversation_id: str, state: ConversationState) -> None:
        with self._lock:
            self._upsert_state(conversation_id, state)
            self._conn.commit()

    async def save_provider_state(
        self, conversation_id: str, provider: str, state: Any,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO provider_state "
                "(conversation_id, provider, state) VALUES (?, ?, ?)",
                (conversation_id, provider, json.dumps(state, ensure_ascii=False)),
            )
            self._conn.commit()

    async def save_pending_tool_result(
        self,
        conversation_id: str,
        assistant_message_index: int,
        result_part: ToolResultPart,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO pending_tool_results "
                "(conversation_id, assistant_message_index, tool_use_ref, result_part) "
                "VALUES (?, ?, ?, ?)",
                (
                    conversation_id,
                    assistant_message_index,
                    result_part.tool_use_ref,
                    json.dumps(_part_to_dict(result_part), ensure_ascii=False),
                ),
            )
            self._conn.commit()

    async def list_pending_tool_results(
        self,
        conversation_id: str,
        assistant_message_index: int,
    ) -> list[ToolResultPart]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT result_part FROM pending_tool_results "
                "WHERE conversation_id = ? AND assistant_message_index = ?",
                (conversation_id, assistant_message_index),
            ).fetchall()
            results: list[ToolResultPart] = []
            for row in rows:
                part = _dict_to_part(json.loads(row[0]))
                if isinstance(part, ToolResultPart):
                    results.append(part)
            return results

    async def clear_pending_tool_results(
        self,
        conversation_id: str,
        assistant_message_index: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM pending_tool_results "
                "WHERE conversation_id = ? AND assistant_message_index = ?",
                (conversation_id, assistant_message_index),
            )
            self._conn.commit()

    # ── 层级查询 ─────────────────────────────────────────────────

    async def list_by_task(self, task_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM conversations WHERE task_id = ?", (task_id,),
            ).fetchall()
            return [r[0] for r in rows]

    async def list_by_thread(self, thread_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM conversations WHERE thread_id = ?", (thread_id,),
            ).fetchall()
            return [r[0] for r in rows]

    async def list_by_agent(self, agent_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM conversations WHERE agent_id = ?", (agent_id,),
            ).fetchall()
            return [r[0] for r in rows]

    async def list_by_agent_instance(self, agent_instance_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM conversations WHERE agent_instance_id = ?",
                (agent_instance_id,),
            ).fetchall()
            return [r[0] for r in rows]

    # ── Task Note 增量跟踪状态 (CAS 乐观锁) ──────────────────────────

    async def get_task_note_tracking(self, conversation_id: str) -> tuple[dict | None, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT task_note_tracking, task_note_tracking_version FROM conversation_state WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if row is None:
                return None, 0
            tracking_json, version = row[0], int(row[1] or 0)
            tracking_dict = json.loads(tracking_json) if tracking_json else None
            return tracking_dict, version

    async def cas_task_note_tracking(
        self, conversation_id: str, expected_version: int, tracking: dict
    ) -> bool:
        with self._lock:
            new_version = expected_version + 1
            tracking_to_save = dict(tracking)
            tracking_to_save["version"] = new_version
            tracking_json = json.dumps(tracking_to_save, ensure_ascii=False)

            cur = self._conn.execute(
                """
                UPDATE conversation_state
                SET task_note_tracking = ?, task_note_tracking_version = ?
                WHERE conversation_id = ? AND task_note_tracking_version = ?
                """,
                (tracking_json, new_version, conversation_id, expected_version),
            )
            self._conn.commit()
            if cur.rowcount == 1:
                return True

            row = self._conn.execute(
                "SELECT task_note_tracking_version FROM conversation_state WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if row is None:
                logger.warning("cas_task_note_tracking 失败: conversation_state 不存在 (cid=%s)", conversation_id)
            else:
                logger.debug(
                    "cas_task_note_tracking 乐观锁版本冲突 (cid=%s): 期望版本 %d, 当前版本 %d",
                    conversation_id, expected_version, row[0]
                )
            return False

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
        needs_message_id_migration = False
        with open(path, "r", encoding="utf-8") as f:
            for line_index, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                raw_message = json.loads(line)
                if not raw_message.get("message_id"):
                    needs_message_id_migration = True
                legacy_message_id = f"msg_{uuid.uuid5(uuid.NAMESPACE_URL, f'{conversation_id}:{line_index}').hex}"
                messages.append(
                    _json_line_to_message(line, legacy_message_id=legacy_message_id)
                )
        if needs_message_id_migration:
            self._write_messages_file(task_id, conversation_id, messages)
        return messages

    def _write_messages_file(
        self, task_id: str, conversation_id: str, messages: list[UniMessage],
    ) -> None:
        """全量覆写指定会话的 JSONL 消息文件。"""
        path = self._msg_path(task_id, conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(_message_to_json_line(msg) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)

    def _upsert_state(self, cid: str, state: ConversationState) -> None:
        tracking_json = (
            json.dumps(state.task_note_tracking, ensure_ascii=False)
            if state.task_note_tracking is not None
            else None
        )
        self._conn.execute(
            "INSERT INTO conversation_state "
            "(conversation_id, providers, vendors, modalities, image_count, pdf_page_count, last_context_usage, task_note_tracking, task_note_tracking_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(conversation_id) DO UPDATE SET "
            "providers = excluded.providers, "
            "vendors = excluded.vendors, "
            "modalities = excluded.modalities, "
            "image_count = excluded.image_count, "
            "pdf_page_count = excluded.pdf_page_count, "
            "last_context_usage = excluded.last_context_usage",
            (
                cid,
                json.dumps(state.providers, ensure_ascii=False),
                json.dumps(state.vendors, ensure_ascii=False),
                json.dumps(state.modalities, ensure_ascii=False),
                state.image_count,
                state.pdf_page_count,
                json.dumps(state.last_context_usage, ensure_ascii=False)
                if state.last_context_usage is not None
                else None,
                tracking_json,
            ),
        )

    def _upsert_configuration(
        self,
        cid: str,
        timeline: list[ConfigurationComponentEvent],
        system_prompt: str | None,
        tools: tuple[dict[str, Any], ...] | list[dict[str, Any]],
        instructions_snapshot: InstructionsSnapshot | None = None,
    ) -> None:
        timeline_data = [_configuration_event_to_dict(evt) for evt in timeline]
        tools_data = list(tools) if tools else []
        self._conn.execute(
            "INSERT OR REPLACE INTO conversation_configuration "
            "(conversation_id, timeline, system_prompt, tools, instructions_snapshot) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                cid,
                json.dumps(timeline_data, ensure_ascii=False),
                system_prompt,
                json.dumps(tools_data, ensure_ascii=False),
                _snapshot_to_json(instructions_snapshot),
            ),
        )

    def _migrate_schema(self) -> None:
        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(conversation_state)").fetchall()
        }
        if "last_context_usage" not in columns:
            self._conn.execute("ALTER TABLE conversation_state ADD COLUMN last_context_usage TEXT")
        if "task_note_tracking" not in columns:
            self._conn.execute("ALTER TABLE conversation_state ADD COLUMN task_note_tracking TEXT")
        if "task_note_tracking_version" not in columns:
            self._conn.execute("ALTER TABLE conversation_state ADD COLUMN task_note_tracking_version INTEGER NOT NULL DEFAULT 0")
        tables = {
            row[0]
            for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "conversation_configuration" not in tables:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS conversation_configuration ("
                "conversation_id       TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE, "
                "timeline              TEXT NOT NULL DEFAULT '[]', "
                "system_prompt         TEXT, "
                "tools                 TEXT NOT NULL DEFAULT '[]', "
                "instructions_snapshot TEXT"
                ")"
            )
        else:
            cfg_columns = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(conversation_configuration)").fetchall()
            }
            if "instructions_snapshot" not in cfg_columns:
                self._conn.execute("ALTER TABLE conversation_configuration ADD COLUMN instructions_snapshot TEXT")
