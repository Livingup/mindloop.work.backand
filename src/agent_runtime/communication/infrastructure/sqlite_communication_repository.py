# -*- coding: utf-8 -*-
"""通用通信模块 SQLite 仓储实现。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from agent_runtime.communication.communication_message import CommunicationMessage
from agent_runtime.communication.communication_stream import CommunicationStream

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS communication_streams (
    stream_id  TEXT PRIMARY KEY,
    topic      TEXT NOT NULL,
    owner_id   TEXT,
    status     TEXT NOT NULL,
    metadata   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_communication_streams_topic ON communication_streams(topic);
CREATE INDEX IF NOT EXISTS idx_communication_streams_owner ON communication_streams(owner_id);
CREATE INDEX IF NOT EXISTS idx_communication_streams_status ON communication_streams(status);

CREATE TABLE IF NOT EXISTS communication_messages (
    message_id        TEXT PRIMARY KEY,
    stream_id         TEXT NOT NULL,
    seq               INTEGER NOT NULL,
    type              TEXT NOT NULL,
    topic             TEXT NOT NULL,
    source            TEXT,
    actor_id          TEXT,
    correlation_id    TEXT,
    parent_message_id TEXT,
    payload           TEXT NOT NULL,
    metadata          TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    schema_version    TEXT NOT NULL,
    UNIQUE(stream_id, seq),
    FOREIGN KEY(stream_id) REFERENCES communication_streams(stream_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_communication_messages_stream_seq ON communication_messages(stream_id, seq);
CREATE INDEX IF NOT EXISTS idx_communication_messages_type ON communication_messages(type);
CREATE INDEX IF NOT EXISTS idx_communication_messages_correlation ON communication_messages(correlation_id);
"""


class SqliteCommunicationRepository:
    """通用通信 SQLite 仓储。

    仓储直接读写 SQLite，不维护应用层缓存；SQLite 开启 WAL 以支持本机多进程读写。
    流内消息 seq 在追加时由仓储事务分配，保证断线恢复读取顺序稳定。
    """

    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        # 通信模块数据目录访问器。仓储每次使用前检查路径，支持 ConfigRegistry 热更新后切换目录。
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

    def create_stream(self, stream: CommunicationStream) -> CommunicationStream:
        """创建通信流。"""
        with self._lock:
            if self.get_stream(stream.stream_id) is not None:
                raise ValueError(f"通信流已存在: {stream.stream_id}")
            conn = self._connection()
            try:
                conn.execute(
                    """
                    INSERT INTO communication_streams (
                        stream_id, topic, owner_id, status, metadata, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    _stream_to_row(stream),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise ValueError(f"通信流已存在: {stream.stream_id}") from exc
            return stream

    def save_stream(self, stream: CommunicationStream) -> CommunicationStream:
        """保存通信流当前状态。"""
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT INTO communication_streams (
                    stream_id, topic, owner_id, status, metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stream_id) DO UPDATE SET
                    topic = excluded.topic,
                    owner_id = excluded.owner_id,
                    status = excluded.status,
                    metadata = excluded.metadata,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                _stream_to_row(stream),
            )
            conn.commit()
            return stream

    def get_stream(self, stream_id: str) -> CommunicationStream | None:
        """按 stream_id 获取通信流。"""
        with self._lock:
            row = self._connection().execute(
                """
                SELECT stream_id, topic, owner_id, status, metadata, created_at, updated_at
                FROM communication_streams WHERE stream_id = ?
                """,
                (stream_id,),
            ).fetchone()
            return _row_to_stream(row) if row else None

    def append_message(self, message: CommunicationMessage) -> CommunicationMessage:
        """追加通信消息，并在事务内分配流内递增 seq。"""
        with self._lock:
            conn = self._connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM communication_messages WHERE stream_id = ?",
                    (message.stream_id,),
                ).fetchone()
                next_seq = int(row[0])
                saved = CommunicationMessage(
                    message_id=message.message_id,
                    stream_id=message.stream_id,
                    seq=next_seq,
                    type=message.type,
                    topic=message.topic,
                    source=message.source,
                    actor_id=message.actor_id,
                    correlation_id=message.correlation_id,
                    parent_message_id=message.parent_message_id,
                    payload=dict(message.payload),
                    metadata=dict(message.metadata),
                    created_at=message.created_at,
                    schema_version=message.schema_version,
                )
                conn.execute(
                    """
                    INSERT INTO communication_messages (
                        message_id, stream_id, seq, type, topic, source, actor_id,
                        correlation_id, parent_message_id, payload, metadata, created_at, schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _message_to_row(saved),
                )
                conn.commit()
                return saved
            except Exception:
                conn.rollback()
                raise

    def list_messages(
        self,
        stream_id: str,
        *,
        after_seq: int = 0,
        limit: int = 500,
    ) -> list[CommunicationMessage]:
        """按通信流和 seq 增量读取消息。"""
        with self._lock:
            rows = self._connection().execute(
                """
                SELECT message_id, stream_id, seq, type, topic, source, actor_id,
                       correlation_id, parent_message_id, payload, metadata, created_at, schema_version
                FROM communication_messages
                WHERE stream_id = ? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (stream_id, after_seq, limit),
            ).fetchall()
            return [_row_to_message(row) for row in rows]

    def list_latest_messages(
        self,
        stream_id: str,
        *,
        limit: int = 500,
    ) -> list[CommunicationMessage]:
        """读取通信流最新一段历史消息，并按 seq 升序返回。"""
        with self._lock:
            rows = self._connection().execute(
                """
                SELECT message_id, stream_id, seq, type, topic, source, actor_id,
                       correlation_id, parent_message_id, payload, metadata, created_at, schema_version
                FROM (
                    SELECT message_id, stream_id, seq, type, topic, source, actor_id,
                           correlation_id, parent_message_id, payload, metadata, created_at, schema_version
                    FROM communication_messages
                    WHERE stream_id = ?
                    ORDER BY seq DESC
                    LIMIT ?
                ) AS latest_messages
                ORDER BY seq ASC
                """,
                (stream_id, limit),
            ).fetchall()
            return [_row_to_message(row) for row in rows]

    def _connection(self) -> sqlite3.Connection:
        """获取当前配置目录对应的 SQLite 连接，配置路径变化时自动重建连接。"""
        data_dir = Path(self._data_dir_accessor()).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "communication.db"
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


def _stream_to_row(stream: CommunicationStream) -> tuple[Any, ...]:
    """把通信流实体转换为 SQLite 行参数。"""
    return (
        stream.stream_id,
        stream.topic,
        stream.owner_id,
        stream.status,
        json.dumps(stream.metadata, ensure_ascii=False),
        _datetime_to_text(stream.created_at),
        _datetime_to_text(stream.updated_at),
    )


def _row_to_stream(row: tuple[Any, ...]) -> CommunicationStream:
    """把 SQLite 行还原为通信流实体。"""
    return CommunicationStream(
        stream_id=row[0],
        topic=row[1],
        owner_id=row[2],
        status=row[3],
        metadata=json.loads(row[4]),
        created_at=_text_to_datetime(row[5]),
        updated_at=_text_to_datetime(row[6]),
    )


def _message_to_row(message: CommunicationMessage) -> tuple[Any, ...]:
    """把通信消息实体转换为 SQLite 行参数。"""
    return (
        message.message_id,
        message.stream_id,
        message.seq,
        message.type,
        message.topic,
        message.source,
        message.actor_id,
        message.correlation_id,
        message.parent_message_id,
        json.dumps(message.payload, ensure_ascii=False),
        json.dumps(message.metadata, ensure_ascii=False),
        _datetime_to_text(message.created_at),
        message.schema_version,
    )


def _row_to_message(row: tuple[Any, ...]) -> CommunicationMessage:
    """把 SQLite 行还原为通信消息实体。"""
    return CommunicationMessage(
        message_id=row[0],
        stream_id=row[1],
        seq=int(row[2]),
        type=row[3],
        topic=row[4],
        source=row[5],
        actor_id=row[6],
        correlation_id=row[7],
        parent_message_id=row[8],
        payload=json.loads(row[9]),
        metadata=json.loads(row[10]),
        created_at=_text_to_datetime(row[11]),
        schema_version=row[12],
    )


def _datetime_to_text(value: datetime) -> str:
    """将 datetime 转为 ISO 文本。"""
    return value.isoformat()


def _text_to_datetime(value: str) -> datetime:
    """将 ISO 文本还原为 datetime。"""
    return datetime.fromisoformat(value)
