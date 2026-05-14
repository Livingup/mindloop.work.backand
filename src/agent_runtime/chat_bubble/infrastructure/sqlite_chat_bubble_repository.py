# -*- coding: utf-8 -*-
"""Chat Bubble 前端消息气泡 SQLite 仓储实现。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from agent_runtime.chat_bubble.chat_bubble_message import (
    ChatBubbleInteractionOption,
    ChatBubbleMessage,
)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS chat_bubble_messages (
    bubble_id                TEXT PRIMARY KEY,
    scene_id                 TEXT NOT NULL,
    agent_instance_id        TEXT,
    task_id                  TEXT,
    conversation_id          TEXT,
    role                     TEXT NOT NULL,
    status                   TEXT NOT NULL,
    text                     TEXT NOT NULL,
    text_format              TEXT NOT NULL,
    channel                  TEXT,
    client_message_id        TEXT,
    backend_message_id       TEXT,
    pending_message_id       TEXT,
    interaction_options_json TEXT NOT NULL DEFAULT '[]',
    selected_option_id       TEXT,
    sort_order               INTEGER NOT NULL,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    metadata_json            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_chat_bubble_scene_sort ON chat_bubble_messages(scene_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_chat_bubble_scene_created ON chat_bubble_messages(scene_id, created_at);
CREATE INDEX IF NOT EXISTS idx_chat_bubble_scene_agent ON chat_bubble_messages(scene_id, agent_instance_id);
CREATE INDEX IF NOT EXISTS idx_chat_bubble_scene_client_message ON chat_bubble_messages(scene_id, client_message_id);
CREATE INDEX IF NOT EXISTS idx_chat_bubble_scene_pending_message ON chat_bubble_messages(scene_id, pending_message_id);
"""


class SqliteChatBubbleRepository:
    """Chat Bubble SQLite 仓储。

    仓储直接读写本机 SQLite 数据库，负责把前端消息气泡展示状态持久化为
    scene_id + sort_order 可分页读取的数据。该仓储只保存 UI 气泡表，不读取或回放
    Communication Event Log，避免把运行事件日志和前端展示状态混为一类数据。
    """

    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        # 运行态数据目录访问器；支持 ConfigRegistry 热更新后切换数据目录。
        self._data_dir_accessor = data_dir_accessor
        self._conn: sqlite3.Connection | None = None
        self._db_path: Path | None = None
        self._lock = RLock()

    def close(self) -> None:
        """关闭底层 SQLite 连接。"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._db_path = None

    def save_message(self, message: ChatBubbleMessage) -> ChatBubbleMessage:
        """保存一条前端消息气泡，已存在时覆盖展示状态。"""
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT INTO chat_bubble_messages (
                    bubble_id, scene_id, agent_instance_id, task_id, conversation_id,
                    role, status, text, text_format, channel, client_message_id,
                    backend_message_id, pending_message_id, interaction_options_json,
                    selected_option_id, sort_order, created_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bubble_id) DO UPDATE SET
                    scene_id = excluded.scene_id,
                    agent_instance_id = excluded.agent_instance_id,
                    task_id = excluded.task_id,
                    conversation_id = excluded.conversation_id,
                    role = excluded.role,
                    status = excluded.status,
                    text = excluded.text,
                    text_format = excluded.text_format,
                    channel = excluded.channel,
                    client_message_id = excluded.client_message_id,
                    backend_message_id = excluded.backend_message_id,
                    pending_message_id = excluded.pending_message_id,
                    interaction_options_json = excluded.interaction_options_json,
                    selected_option_id = excluded.selected_option_id,
                    sort_order = excluded.sort_order,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json
                """,
                _message_to_row(message),
            )
            conn.commit()
            return message

    def get_message(self, bubble_id: str) -> ChatBubbleMessage | None:
        """按 bubble_id 查询前端消息气泡。"""
        with self._lock:
            row = self._connection().execute(
                """
                SELECT bubble_id, scene_id, agent_instance_id, task_id, conversation_id,
                       role, status, text, text_format, channel, client_message_id,
                       backend_message_id, pending_message_id, interaction_options_json,
                       selected_option_id, sort_order, created_at, updated_at, metadata_json
                FROM chat_bubble_messages
                WHERE bubble_id = ?
                """,
                (bubble_id,),
            ).fetchone()
            return _row_to_message(row) if row else None

    def list_scene_messages(
        self,
        scene_id: str,
        *,
        limit: int = 100,
        before_sort_order: int | None = None,
        agent_instance_id: str | None = None,
    ) -> list[ChatBubbleMessage]:
        """读取 Scene 最新一段消息气泡，并按 sort_order 升序返回。"""
        with self._lock:
            filters = ["scene_id = ?"]
            params: list[Any] = [scene_id]
            if before_sort_order is not None:
                filters.append("sort_order < ?")
                params.append(before_sort_order)
            if agent_instance_id is not None:
                filters.append("agent_instance_id = ?")
                params.append(agent_instance_id)
            where_clause = " AND ".join(filters)
            rows = self._connection().execute(
                f"""
                SELECT bubble_id, scene_id, agent_instance_id, task_id, conversation_id,
                       role, status, text, text_format, channel, client_message_id,
                       backend_message_id, pending_message_id, interaction_options_json,
                       selected_option_id, sort_order, created_at, updated_at, metadata_json
                FROM (
                    SELECT bubble_id, scene_id, agent_instance_id, task_id, conversation_id,
                           role, status, text, text_format, channel, client_message_id,
                           backend_message_id, pending_message_id, interaction_options_json,
                           selected_option_id, sort_order, created_at, updated_at, metadata_json
                    FROM chat_bubble_messages
                    WHERE {where_clause}
                    ORDER BY sort_order DESC
                    LIMIT ?
                ) AS latest_bubbles
                ORDER BY sort_order ASC
                """,
                (*params, limit),
            ).fetchall()
            return [_row_to_message(row) for row in rows]

    def delete_message(self, bubble_id: str) -> bool:
        """删除一条前端消息气泡。"""
        with self._lock:
            conn = self._connection()
            cursor = conn.execute(
                "DELETE FROM chat_bubble_messages WHERE bubble_id = ?",
                (bubble_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def next_sort_order(self, scene_id: str) -> int:
        """为 Scene 分配下一条消息气泡排序号。"""
        with self._lock:
            row = self._connection().execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM chat_bubble_messages WHERE scene_id = ?",
                (scene_id,),
            ).fetchone()
            return int(row[0])

    def _connection(self) -> sqlite3.Connection:
        """获取当前配置目录对应的 SQLite 连接，配置路径变化时自动重建连接。"""
        data_dir = Path(self._data_dir_accessor()).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "chat_bubbles.db"
        if self._conn is not None and self._db_path == db_path:
            return self._conn
        if self._conn is not None:
            self._conn.close()
        self._conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._db_path = db_path
        return self._conn


def _message_to_row(message: ChatBubbleMessage) -> tuple[Any, ...]:
    """把消息气泡领域对象转换为 SQLite 行参数。"""
    return (
        message.bubble_id,
        message.scene_id,
        message.agent_instance_id,
        message.task_id,
        message.conversation_id,
        message.role,
        message.status,
        message.text,
        message.text_format,
        message.channel,
        message.client_message_id,
        message.backend_message_id,
        message.pending_message_id,
        json.dumps([option.to_json() for option in message.interaction_options], ensure_ascii=False),
        message.selected_option_id,
        message.sort_order,
        _datetime_to_text(message.created_at),
        _datetime_to_text(message.updated_at),
        json.dumps(message.metadata, ensure_ascii=False),
    )


def _row_to_message(row: tuple[Any, ...]) -> ChatBubbleMessage:
    """把 SQLite 行还原为消息气泡领域对象。"""
    return ChatBubbleMessage(
        bubble_id=row[0],
        scene_id=row[1],
        agent_instance_id=row[2],
        task_id=row[3],
        conversation_id=row[4],
        role=row[5],
        status=row[6],
        text=row[7],
        text_format=row[8],
        channel=row[9],
        client_message_id=row[10],
        backend_message_id=row[11],
        pending_message_id=row[12],
        interaction_options=tuple(
            ChatBubbleInteractionOption.from_json(option)
            for option in json.loads(row[13] or "[]")
        ),
        selected_option_id=row[14],
        sort_order=int(row[15]),
        created_at=_text_to_datetime(row[16]),
        updated_at=_text_to_datetime(row[17]),
        metadata=json.loads(row[18] or "{}"),
    )


def _datetime_to_text(value: datetime) -> str:
    """将 datetime 转为 ISO 文本。"""
    return value.isoformat()


def _text_to_datetime(value: str) -> datetime:
    """将 ISO 文本还原为 datetime。"""
    return datetime.fromisoformat(value)
