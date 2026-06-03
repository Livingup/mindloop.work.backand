# -*- coding: utf-8 -*-
"""云端用户全局配置 PostgreSQL 通用 JSONB 存储。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_runtime.cloud_user_config.config import CloudUserConfigDatabaseConfig


_SCHEMA_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS cloud_user_config_items (
    namespace   TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, item_id)
);
"""

_SCHEMA_INDEX_SQL = """\
CREATE INDEX IF NOT EXISTS idx_cloud_user_config_items_namespace
    ON cloud_user_config_items(namespace);
"""


class PostgresUserConfigStore:
    """面向仓储实现的 PostgreSQL JSONB 存储网关。

    每个用户全局配置聚合根按 namespace + item_id 保存为一条 JSONB 记录；
    领域服务仍依赖各自仓储接口，不感知 PostgreSQL。
    """

    def __init__(self, config_accessor: Callable[[], CloudUserConfigDatabaseConfig]) -> None:
        self._config_accessor = config_accessor

    def list_payloads(self, namespace: str) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload
                FROM cloud_user_config_items
                WHERE namespace = %s
                ORDER BY item_id ASC
                """,
                (namespace,),
            ).fetchall()
        return [dict(row["payload"]) for row in rows]

    def get_payload(self, namespace: str, item_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload
                FROM cloud_user_config_items
                WHERE namespace = %s AND item_id = %s
                """,
                (namespace, item_id),
            ).fetchone()
        return dict(row["payload"]) if row is not None else None

    def save_payload(self, namespace: str, item_id: str, payload: dict[str, Any]) -> None:
        self.ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cloud_user_config_items (namespace, item_id, payload)
                VALUES (%s, %s, %s)
                ON CONFLICT(namespace, item_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = now()
                """,
                (namespace, item_id, Jsonb(payload)),
            )
            conn.commit()

    def delete_payload(self, namespace: str, item_id: str) -> bool:
        self.ensure_schema()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM cloud_user_config_items
                WHERE namespace = %s AND item_id = %s
                """,
                (namespace, item_id),
            )
            conn.commit()
            return (cursor.rowcount or 0) > 0

    def contains_payload(self, namespace: str, item_id: str) -> bool:
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM cloud_user_config_items
                WHERE namespace = %s AND item_id = %s
                LIMIT 1
                """,
                (namespace, item_id),
            ).fetchone()
        return row is not None

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_SCHEMA_TABLE_SQL)
            conn.execute(_SCHEMA_INDEX_SQL)
            conn.commit()

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        config = self._config_accessor()
        password = config.require_password()
        conn = psycopg.connect(
            host=config.host,
            port=config.port,
            dbname=config.database,
            user=config.username,
            password=password,
            sslmode=config.sslmode,
            connect_timeout=config.pool.connect_timeout_seconds,
            row_factory=dict_row,
        )
        try:
            yield conn
        finally:
            conn.close()
