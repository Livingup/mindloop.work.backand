# -*- coding: utf-8 -*-
"""云端用户全局配置 PostgreSQL 通用 JSONB 存储。"""
from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_runtime.cloud_user_config.config import CloudUserConfigDatabaseConfig

logger = logging.getLogger("agent_runtime.cloud_user_config.postgres_store")

CONFIG_CENTER_POLL_INTERVAL_SECONDS = 5

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

_SCHEMA_REVISION_STATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS cloud_user_config_revision_state (
    id          BOOLEAN PRIMARY KEY DEFAULT TRUE,
    revision    BIGINT NOT NULL DEFAULT nextval('cloud_user_config_revision_seq'),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (id)
);
"""

_SCHEMA_REVISION_STATE_INIT_SQL = """\
INSERT INTO cloud_user_config_revision_state (id)
VALUES (TRUE)
ON CONFLICT (id) DO NOTHING;
"""

_SCHEMA_TOUCH_TRIGGER_FUNCTION_SQL = """\
CREATE OR REPLACE FUNCTION cloud_user_config_touch_revision()
RETURNS trigger AS $$
BEGIN
    UPDATE cloud_user_config_revision_state
    SET revision = nextval('cloud_user_config_revision_seq'),
        updated_at = now()
    WHERE id = TRUE;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_SCHEMA_ITEM_UPDATED_AT_FUNCTION_SQL = """\
CREATE OR REPLACE FUNCTION cloud_user_config_set_item_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_SCHEMA_ITEM_UPDATED_AT_TRIGGER_SQL = """\
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'trg_cloud_user_config_set_item_updated_at'
    ) THEN
        CREATE TRIGGER trg_cloud_user_config_set_item_updated_at
        BEFORE UPDATE ON cloud_user_config_items
        FOR EACH ROW
        EXECUTE FUNCTION cloud_user_config_set_item_updated_at();
    END IF;
END
$$;
"""

_SCHEMA_TOUCH_TRIGGER_SQL = """\
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'trg_cloud_user_config_touch_revision'
    ) THEN
        CREATE TRIGGER trg_cloud_user_config_touch_revision
        AFTER INSERT OR UPDATE OR DELETE ON cloud_user_config_items
        FOR EACH ROW
        EXECUTE FUNCTION cloud_user_config_touch_revision();
    END IF;
END
$$;
"""


@dataclass(frozen=True, slots=True)
class CloudUserConfigCacheSnapshot:
    """PostgreSQL 配置表当前数据和数据库侧版本号快照。"""

    revision: int
    items: dict[tuple[str, str], dict[str, Any]]


def _config_key(config: CloudUserConfigDatabaseConfig) -> tuple[object, ...]:
    return (
        config.host,
        config.port,
        config.database,
        config.username,
        config.sslmode,
    )


class PostgresUserConfigStore:
    """面向仓储实现的 PostgreSQL JSONB 配置存储与本机缓存网关。

    PostgreSQL 的 cloud_user_config_items 是用户共享配置权威；数据库侧 trigger 维护
    cloud_user_config_revision_state.revision。Runtime 本机进程只缓存配置 payload 和
    已加载 revision，不生成业务版本号。前端和后端业务仓储统一通过本 store 获取配置，
    store 在启动和后台轮询中发现数据库 revision 变化后刷新本机内存缓存。
    """

    _cache_lock = threading.RLock()
    _cache_revision: int | None = None
    _cache_items: dict[tuple[str, str], dict[str, Any]] = {}
    _cache_config_key: tuple[object, ...] | None = None

    def __init__(self, config_accessor: Callable[[], CloudUserConfigDatabaseConfig]) -> None:
        self._config_accessor = config_accessor

    @classmethod
    def reset_process_cache_for_tests(cls) -> None:
        """清空进程级配置缓存，仅供测试隔离使用。"""

        with cls._cache_lock:
            cls._cache_revision = None
            cls._cache_items = {}
            cls._cache_config_key = None

    @classmethod
    def start_background_revision_polling(
        cls,
        config_accessor: Callable[[], CloudUserConfigDatabaseConfig],
        *,
        interval_seconds: int = CONFIG_CENTER_POLL_INTERVAL_SECONDS,
    ) -> asyncio.Task[None]:
        """启动 Runtime 后端配置缓存 revision 轮询任务。"""

        store = cls(config_accessor)
        return asyncio.create_task(
            store._poll_database_revision(max(int(interval_seconds), 1)),
            name="cloud-user-config-cache-revision-poller",
        )

    def list_payloads(self, namespace: str) -> list[dict[str, Any]]:
        self.ensure_cache_loaded()
        with self._cache_lock:
            rows = [
                (item_id, payload)
                for (row_namespace, item_id), payload in self._cache_items.items()
                if row_namespace == namespace
            ]
        return [dict(payload) for _, payload in sorted(rows, key=lambda item: item[0])]

    def get_payload(self, namespace: str, item_id: str) -> dict[str, Any] | None:
        self.ensure_cache_loaded()
        with self._cache_lock:
            payload = self._cache_items.get((namespace, item_id))
            return dict(payload) if payload is not None else None

    def save_payload(self, namespace: str, item_id: str, payload: dict[str, Any]) -> int:
        """保存配置 payload，并读取数据库侧 trigger 更新后的 revision 刷新本机缓存。"""

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
        return self.reload_cache_from_database().revision

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
            deleted = (cursor.rowcount or 0) > 0
            conn.commit()
        if deleted:
            self.reload_cache_from_database()
        return deleted

    def contains_payload(self, namespace: str, item_id: str) -> bool:
        self.ensure_cache_loaded()
        with self._cache_lock:
            return (namespace, item_id) in self._cache_items

    def ensure_cache_loaded(self) -> CloudUserConfigCacheSnapshot:
        """确保本机缓存已从 PostgreSQL 配置表加载。"""

        config_key = _config_key(self._config_accessor())
        with self._cache_lock:
            if self._cache_revision is not None and self._cache_config_key == config_key:
                return CloudUserConfigCacheSnapshot(
                    revision=self._cache_revision,
                    items={key: dict(value) for key, value in self._cache_items.items()},
                )
        return self.reload_cache_from_database()

    def reload_cache_from_database(self) -> CloudUserConfigCacheSnapshot:
        """从 PostgreSQL 全量拉取配置表和数据库侧 revision，覆盖本机内存缓存。"""

        snapshot = self._read_cache_snapshot_from_database()
        with self._cache_lock:
            self.__class__._cache_revision = snapshot.revision
            self.__class__._cache_items = {key: dict(value) for key, value in snapshot.items.items()}
            self.__class__._cache_config_key = _config_key(self._config_accessor())
        return snapshot

    def refresh_cache_if_revision_changed(self) -> CloudUserConfigCacheSnapshot:
        """后端轮询入口：版本不一致时才全量刷新本机配置缓存。"""

        config_key = _config_key(self._config_accessor())
        database_revision = self._read_database_revision()
        with self._cache_lock:
            local_revision = self._cache_revision
            cache_key = self._cache_config_key
        if local_revision != database_revision or cache_key != config_key:
            return self.reload_cache_from_database()
        with self._cache_lock:
            return CloudUserConfigCacheSnapshot(
                revision=self._cache_revision or 0,
                items={key: dict(value) for key, value in self._cache_items.items()},
            )

    def get_current_config_revision(self) -> int:
        """返回 PostgreSQL 数据库侧配置全局 revision。"""

        self.ensure_schema()
        with self._connect() as conn:
            return self._current_config_revision(conn)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_SCHEMA_TABLE_SQL)
            conn.execute(_SCHEMA_INDEX_SQL)
            conn.execute(_SCHEMA_REVISION_SEQUENCE_SQL)
            conn.execute(_SCHEMA_REVISION_STATE_TABLE_SQL)
            conn.execute(_SCHEMA_REVISION_STATE_INIT_SQL)
            conn.execute(_SCHEMA_TOUCH_TRIGGER_FUNCTION_SQL)
            conn.execute(_SCHEMA_ITEM_UPDATED_AT_FUNCTION_SQL)
            conn.execute(_SCHEMA_ITEM_UPDATED_AT_TRIGGER_SQL)
            conn.execute(_SCHEMA_TOUCH_TRIGGER_SQL)
            conn.commit()

    async def _poll_database_revision(self, interval_seconds: int) -> None:
        try:
            await asyncio.to_thread(self.reload_cache_from_database)
        except Exception:
            logger.exception("Runtime 后端配置缓存启动加载失败，将继续按轮询重试。")
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await asyncio.to_thread(self.refresh_cache_if_revision_changed)
            except Exception:
                logger.exception("Runtime 后端配置缓存 revision 轮询失败，将在下一轮继续。")

    def _read_cache_snapshot_from_database(self) -> CloudUserConfigCacheSnapshot:
        self.ensure_schema()
        with self._connect() as conn:
            return self._load_cache_snapshot(conn)

    def _read_database_revision(self) -> int:
        self.ensure_schema()
        with self._connect() as conn:
            return self._current_config_revision(conn)

    def _load_cache_snapshot(
        self,
        conn: psycopg.Connection[dict[str, Any]],
    ) -> CloudUserConfigCacheSnapshot:
        revision = self._current_config_revision(conn)
        rows = conn.execute(
            """
            SELECT namespace, item_id, payload
            FROM cloud_user_config_items
            ORDER BY namespace ASC, item_id ASC
            """
        ).fetchall()
        items = {
            (str(row["namespace"]), str(row["item_id"])): dict(row["payload"])
            for row in rows
        }
        return CloudUserConfigCacheSnapshot(revision=revision, items=items)

    def _current_config_revision(self, conn: psycopg.Connection[dict[str, Any]]) -> int:
        row = conn.execute(
            """
            SELECT revision
            FROM cloud_user_config_revision_state
            WHERE id = TRUE
            """
        ).fetchone()
        return int(row["revision"]) if row is not None else 0

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
