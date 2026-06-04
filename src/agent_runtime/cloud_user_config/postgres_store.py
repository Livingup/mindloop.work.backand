# -*- coding: utf-8 -*-
"""云端用户全局配置 PostgreSQL 通用 JSONB 存储。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_runtime.cloud_user_config.config import CloudUserConfigDatabaseConfig


VERSIONED_CONFIG_NAMESPACES = frozenset({
    "agent_definition",
    "context_compression.runtime_settings",
    "context_compression.worker_prompt",
    "file_resource.object_storage_settings",
    "image_generation.runtime_settings",
    "image_generation_llm_execution_policy",
    "image_generation_llm_model_metadata",
    "image_generation_llm_provider_definition",
    "lacp.registry_service_settings",
    "llm_execution_policy",
    "llm_model_metadata",
    "llm_provider_definition",
    "mcp.streamable_http_service_definition",
    "prompt_snippet",
    "web_tools.runtime_settings",
})

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

_SCHEMA_REVISION_SEQUENCE_SQL = """\
CREATE SEQUENCE IF NOT EXISTS cloud_user_config_revision_seq;
"""

_SCHEMA_VERSION_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS cloud_user_config_item_versions (
    namespace   TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    revision    BIGINT NOT NULL,
    deleted     BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, item_id)
);
"""

_SCHEMA_VERSION_REVISION_INDEX_SQL = """\
CREATE INDEX IF NOT EXISTS idx_cloud_user_config_item_versions_revision
    ON cloud_user_config_item_versions(revision);
"""

_SCHEMA_VERSION_NAMESPACE_REVISION_INDEX_SQL = """\
CREATE INDEX IF NOT EXISTS idx_cloud_user_config_item_versions_namespace_revision
    ON cloud_user_config_item_versions(namespace, revision);
"""


@dataclass(frozen=True, slots=True)
class CloudUserConfigChange:
    """配置中心变更视图中的单个配置项当前状态。"""

    namespace: str
    item_id: str
    revision: int
    deleted: bool
    payload: dict[str, Any] | None
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class CloudUserConfigChangeSet:
    """配置中心基于 last_seen_revision 的增量变更结果。"""

    current_revision: int
    changes: tuple[CloudUserConfigChange, ...]


def is_versioned_config_namespace(namespace: str) -> bool:
    """判断 namespace 是否属于用户共享配置中心版本同步范围。"""

    return namespace in VERSIONED_CONFIG_NAMESPACES


def versioned_config_namespaces() -> tuple[str, ...]:
    """返回当前 PostgreSQL 云端配置表中需要 revision 同步的配置 namespace。"""

    return tuple(sorted(VERSIONED_CONFIG_NAMESPACES))


class PostgresUserConfigStore:
    """面向仓储实现的 PostgreSQL JSONB 存储网关。

    每个用户全局配置聚合根按 namespace + item_id 保存为一条 JSONB 记录；
    领域服务仍依赖各自仓储接口，不感知 PostgreSQL。配置中心只为明确的
    用户共享配置 namespace 维护全局 revision；未来若 PostgreSQL 中增加业务
    数据表或非配置 namespace，不会自动混入配置中心增量同步。
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

    def save_payload(self, namespace: str, item_id: str, payload: dict[str, Any]) -> int | None:
        """保存配置 payload。

        属于配置中心同步范围的 namespace 会同步更新
        cloud_user_config_item_versions 并返回新的全局 revision；其他 namespace
        只保存 payload，不产生配置中心 revision。
        """

        self.ensure_schema()
        with self._connect() as conn:
            revision = self._next_config_revision(conn) if is_versioned_config_namespace(namespace) else None
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
            if revision is not None:
                self._upsert_config_item_version(
                    conn,
                    namespace=namespace,
                    item_id=item_id,
                    revision=revision,
                    deleted=False,
                )
            conn.commit()
        return revision

    def delete_payload(self, namespace: str, item_id: str) -> bool:
        self.ensure_schema()
        with self._connect() as conn:
            revision = self._next_config_revision(conn) if is_versioned_config_namespace(namespace) else None
            cursor = conn.execute(
                """
                DELETE FROM cloud_user_config_items
                WHERE namespace = %s AND item_id = %s
                """,
                (namespace, item_id),
            )
            deleted = (cursor.rowcount or 0) > 0
            if deleted and revision is not None:
                self._upsert_config_item_version(
                    conn,
                    namespace=namespace,
                    item_id=item_id,
                    revision=revision,
                    deleted=True,
                )
            conn.commit()
            return deleted

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

    def get_current_config_revision(self) -> int:
        """返回当前用户共享配置中心的最大全局 revision。"""

        self.ensure_schema()
        with self._connect() as conn:
            return self._current_config_revision(conn)

    def list_config_changes_since(self, last_seen_revision: int) -> CloudUserConfigChangeSet:
        """按 last_seen_revision 返回配置项最新状态变化。

        该查询只覆盖 VERSIONED_CONFIG_NAMESPACES 中定义的配置 namespace。每个配置项
        只返回当前最新状态：如果同一 item 在客户端轮询间隔内多次变更，客户端只需
        应用最新 payload 或 tombstone 即可达到最终一致。
        """

        self.ensure_schema()
        since = max(int(last_seen_revision), 0)
        namespaces = versioned_config_namespaces()
        if not namespaces:
            return CloudUserConfigChangeSet(current_revision=0, changes=())
        placeholders = ", ".join(["%s"] * len(namespaces))
        with self._connect() as conn:
            current_revision = self._current_config_revision(conn)
            rows = conn.execute(
                f"""
                SELECT v.namespace, v.item_id, v.revision, v.deleted, v.updated_at, i.payload
                FROM cloud_user_config_item_versions v
                LEFT JOIN cloud_user_config_items i
                    ON i.namespace = v.namespace AND i.item_id = v.item_id
                WHERE v.revision > %s
                  AND v.namespace IN ({placeholders})
                ORDER BY v.revision ASC, v.namespace ASC, v.item_id ASC
                """,
                (since, *namespaces),
            ).fetchall()
        changes: list[CloudUserConfigChange] = []
        for row in rows:
            payload = row.get("payload")
            deleted = bool(row["deleted"]) or payload is None
            changes.append(
                CloudUserConfigChange(
                    namespace=str(row["namespace"]),
                    item_id=str(row["item_id"]),
                    revision=int(row["revision"]),
                    deleted=deleted,
                    payload=None if deleted else dict(payload),
                    updated_at=_datetime_text(row.get("updated_at")),
                )
            )
        return CloudUserConfigChangeSet(
            current_revision=current_revision,
            changes=tuple(changes),
        )

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_SCHEMA_TABLE_SQL)
            conn.execute(_SCHEMA_INDEX_SQL)
            conn.execute(_SCHEMA_REVISION_SEQUENCE_SQL)
            conn.execute(_SCHEMA_VERSION_TABLE_SQL)
            conn.execute(_SCHEMA_VERSION_REVISION_INDEX_SQL)
            conn.execute(_SCHEMA_VERSION_NAMESPACE_REVISION_INDEX_SQL)
            self._backfill_config_item_versions(conn)
            conn.commit()

    def _backfill_config_item_versions(self, conn: psycopg.Connection[dict[str, Any]]) -> None:
        namespaces = versioned_config_namespaces()
        if not namespaces:
            return
        placeholders = ", ".join(["%s"] * len(namespaces))
        conn.execute(
            f"""
            INSERT INTO cloud_user_config_item_versions (namespace, item_id, revision, deleted, updated_at)
            SELECT i.namespace, i.item_id, nextval('cloud_user_config_revision_seq'), FALSE, i.updated_at
            FROM cloud_user_config_items i
            LEFT JOIN cloud_user_config_item_versions v
                ON v.namespace = i.namespace AND v.item_id = i.item_id
            WHERE i.namespace IN ({placeholders})
              AND v.namespace IS NULL
            ORDER BY i.namespace ASC, i.item_id ASC
            """,
            namespaces,
        )

    def _next_config_revision(self, conn: psycopg.Connection[dict[str, Any]]) -> int:
        row = conn.execute(
            "SELECT nextval('cloud_user_config_revision_seq') AS revision"
        ).fetchone()
        if row is None:
            raise RuntimeError("无法生成用户共享配置 revision。")
        return int(row["revision"])

    def _current_config_revision(self, conn: psycopg.Connection[dict[str, Any]]) -> int:
        namespaces = versioned_config_namespaces()
        if not namespaces:
            return 0
        placeholders = ", ".join(["%s"] * len(namespaces))
        row = conn.execute(
            f"""
            SELECT COALESCE(MAX(revision), 0) AS current_revision
            FROM cloud_user_config_item_versions
            WHERE namespace IN ({placeholders})
            """,
            namespaces,
        ).fetchone()
        return int(row["current_revision"]) if row is not None else 0

    def _upsert_config_item_version(
        self,
        conn: psycopg.Connection[dict[str, Any]],
        *,
        namespace: str,
        item_id: str,
        revision: int,
        deleted: bool,
    ) -> None:
        conn.execute(
            """
            INSERT INTO cloud_user_config_item_versions (namespace, item_id, revision, deleted, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT(namespace, item_id) DO UPDATE SET
                revision = excluded.revision,
                deleted = excluded.deleted,
                updated_at = now()
            """,
            (namespace, item_id, revision, deleted),
        )

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


def _datetime_text(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)
