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
from agent_runtime.cloud_user_config.local_state import CloudUserConfigLocalStateRepository
from agent_runtime.infrastructure.asyncio_diagnostics import BlockingPool, monitored_to_thread
from agent_runtime.infrastructure.runtime_task_registry import get_runtime_task_registry

logger = logging.getLogger("agent_runtime.cloud_user_config.postgres_store")

CONFIG_CENTER_POLL_INTERVAL_SECONDS = 5
# PostgreSQL advisory locks are scoped to the current database. 该常量只用于串行化
# cloud_user_config schema 初始化，避免多台 Runtime release 同时 CREATE OR REPLACE
# FUNCTION / CREATE TRIGGER 时并发更新 PostgreSQL 系统 catalog。
_SCHEMA_INITIALIZATION_ADVISORY_LOCK_ID = 732145908367451201

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
    """共享配置表当前数据和数据库侧版本号快照。"""

    revision: int
    items: dict[tuple[str, str], dict[str, Any]]
    config_key: tuple[object, ...] | None = None


class CloudUserConfigWriteUnavailableError(RuntimeError):
    """PostgreSQL 写权威暂不可用，禁止把共享配置写入本地快照伪造成功。"""


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

    PostgreSQL 的 cloud_user_config_items 是用户共享配置写权威；数据库侧 trigger 维护
    cloud_user_config_revision_state.revision。Runtime 本机通过机器本地持久化快照支持
    离线冷启动读取，再把快照加载为进程内热缓存。写入必须先成功落到 PostgreSQL，随后
    重新拉取权威快照并同步更新进程缓存与本地持久化快照，禁止离线写入本地快照形成双权威。
    """

    _cache_lock = threading.RLock()
    _cache_revision: int | None = None
    _cache_items: dict[tuple[str, str], dict[str, Any]] = {}
    _cache_config_key: tuple[object, ...] | None = None
    _schema_lock = threading.RLock()
    _schema_initialized_config_keys: set[tuple[object, ...]] = set()

    def __init__(
        self,
        config_accessor: Callable[[], CloudUserConfigDatabaseConfig],
        *,
        local_state_repository: CloudUserConfigLocalStateRepository | None = None,
    ) -> None:
        self._config_accessor = config_accessor
        self._local_state_repository = local_state_repository

    @classmethod
    def reset_process_cache_for_tests(cls) -> None:
        """清空进程级配置缓存，仅供测试隔离使用。"""

        with cls._cache_lock:
            cls._cache_revision = None
            cls._cache_items = {}
            cls._cache_config_key = None
        with cls._schema_lock:
            cls._schema_initialized_config_keys = set()

    @classmethod
    def start_background_revision_polling(
        cls,
        config_accessor: Callable[[], CloudUserConfigDatabaseConfig],
        *,
        interval_seconds: int = CONFIG_CENTER_POLL_INTERVAL_SECONDS,
    ) -> asyncio.Task[None]:
        """启动 Runtime 后端配置缓存 revision 轮询任务。"""

        interval = max(int(interval_seconds), 1)
        store = cls(config_accessor)
        return get_runtime_task_registry().create_task(
            store._poll_database_revision(interval),
            task_name="cloud-user-config-cache-revision-poller",
            owner_type="cloud_user_config",
            owner_id="postgres",
            task_class="revision_poller",
            diagnostic_context={"poll_interval_seconds": interval},
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
        """按 write-through 语义保存共享配置。

        PostgreSQL 是唯一写权威；写入成功后重新拉取权威快照，更新进程热缓存和机器本地
        持久化快照，使设置页保存成功后本机立即生效。PG 不可用时不写本地快照，避免双权威。
        """

        try:
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
        except CloudUserConfigWriteUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - 写路径需要统一暴露临时不可用语义
            raise CloudUserConfigWriteUnavailableError(
                "云端共享配置 PostgreSQL 暂不可用，配置修改未保存；请稍后重试。"
            ) from exc

    def delete_payload(self, namespace: str, item_id: str) -> bool:
        try:
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
        except CloudUserConfigWriteUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 - 写路径需要统一暴露临时不可用语义
            raise CloudUserConfigWriteUnavailableError(
                "云端共享配置 PostgreSQL 暂不可用，配置删除未保存；请稍后重试。"
            ) from exc

    def contains_payload(self, namespace: str, item_id: str) -> bool:
        self.ensure_cache_loaded()
        with self._cache_lock:
            return (namespace, item_id) in self._cache_items

    def ensure_cache_loaded(self) -> CloudUserConfigCacheSnapshot:
        """确保进程缓存已优先从机器本地持久化快照加载。"""

        config_key = _config_key(self._config_accessor())
        with self._cache_lock:
            if self._cache_revision is not None and self._cache_config_key == config_key:
                return CloudUserConfigCacheSnapshot(
                    revision=self._cache_revision,
                    items={key: dict(value) for key, value in self._cache_items.items()},
                    config_key=config_key,
                )
        snapshot = self._load_cache_snapshot_from_local_state(config_key)
        self._apply_process_cache(snapshot)
        return snapshot

    def reload_cache_from_database(self) -> CloudUserConfigCacheSnapshot:
        """从 PostgreSQL 全量拉取配置表和 revision，并同步进程缓存与本机持久化快照。"""

        snapshot = self._read_cache_snapshot_from_database()
        self._apply_process_cache(snapshot)
        self._save_cache_snapshot_to_local_state(snapshot)
        return snapshot

    def read_authoritative_snapshot_without_process_side_effects(self) -> CloudUserConfigCacheSnapshot:
        """连接目标库并读取权威快照，但不改写进程缓存或本机 shared snapshot。

        Settings 重配置在当前进程仍绑定旧 PostgreSQL 目标时，需要先验证候选连接；
        不能调用 reload_cache_from_database，否则会污染运行中的进程 cache，并把唯一
        本机 snapshot 提前覆盖成新 key，导致“已保存待重启”窗口内 readiness 失配。
        durable snapshot 只能由 local_state.atomic_commit_database_reconfigure 原子写入。
        """

        return self._read_cache_snapshot_from_database()

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
                config_key=config_key,
            )

    def get_current_config_revision(self) -> int:
        """返回 PostgreSQL 数据库侧配置全局 revision。"""

        self.ensure_schema()
        with self._connect() as conn:
            return self._current_config_revision(conn)

    def ensure_schema(self) -> None:
        """确保共享配置 PostgreSQL schema 已初始化。

        Runtime 正常运行期会高频轮询 revision；该路径不能每轮重复执行 DDL。schema
        初始化按 PostgreSQL 连接配置 key 在进程内去重，并通过 PostgreSQL advisory
        transaction lock 在多台 release / 多进程同时启动时串行化，避免并发 CREATE OR
        REPLACE FUNCTION / CREATE TRIGGER 更新系统 catalog 导致 tuple concurrently updated。
        """

        config_key = _config_key(self._config_accessor())
        with self._schema_lock:
            if config_key in self._schema_initialized_config_keys:
                return
            with self._connect() as conn:
                self._lock_schema_initialization(conn)
                self._execute_schema_initialization(conn)
                conn.commit()
            self.__class__._schema_initialized_config_keys = {
                *self._schema_initialized_config_keys,
                config_key,
            }

    def _lock_schema_initialization(self, conn: psycopg.Connection[dict[str, Any]]) -> None:
        """获取数据库级 schema 初始化事务锁。"""

        conn.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_SCHEMA_INITIALIZATION_ADVISORY_LOCK_ID,),
        )

    def _execute_schema_initialization(self, conn: psycopg.Connection[dict[str, Any]]) -> None:
        """执行 cloud_user_config 共享配置 schema DDL。"""

        conn.execute(_SCHEMA_TABLE_SQL)
        conn.execute(_SCHEMA_INDEX_SQL)
        conn.execute(_SCHEMA_REVISION_SEQUENCE_SQL)
        conn.execute(_SCHEMA_REVISION_STATE_TABLE_SQL)
        conn.execute(_SCHEMA_REVISION_STATE_INIT_SQL)
        conn.execute(_SCHEMA_TOUCH_TRIGGER_FUNCTION_SQL)
        conn.execute(_SCHEMA_ITEM_UPDATED_AT_FUNCTION_SQL)
        conn.execute(_SCHEMA_ITEM_UPDATED_AT_TRIGGER_SQL)
        conn.execute(_SCHEMA_TOUCH_TRIGGER_SQL)

    async def _poll_database_revision(self, interval_seconds: int) -> None:
        try:
            await monitored_to_thread(
                self.reload_cache_from_database,
                operation="cloud_user_config.postgres.reload_cache_from_database",
                caller="PostgresCloudUserConfigStore._poll_database_revision",
                pool=BlockingPool.DB_BLOCKING,
                log=logger,
                diagnostic_context={"poll_interval_seconds": interval_seconds, "poll_phase": "startup_reload"},
            )
        except Exception:
            logger.exception("Runtime 后端配置缓存启动加载失败，将继续按轮询重试。")
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await monitored_to_thread(
                    self.refresh_cache_if_revision_changed,
                    operation="cloud_user_config.postgres.refresh_cache_if_revision_changed",
                    caller="PostgresCloudUserConfigStore._poll_database_revision",
                    pool=BlockingPool.DB_BLOCKING,
                    log=logger,
                    diagnostic_context={"poll_interval_seconds": interval_seconds, "poll_phase": "revision_poll"},
                )
            except Exception:
                logger.exception("Runtime 后端配置缓存 revision 轮询失败，将在下一轮继续。")

    def _read_cache_snapshot_from_database(self) -> CloudUserConfigCacheSnapshot:
        self.ensure_schema()
        with self._connect() as conn:
            return self._load_cache_snapshot(conn)

    def _load_cache_snapshot_from_local_state(
        self,
        config_key: tuple[object, ...],
    ) -> CloudUserConfigCacheSnapshot:
        local_snapshot = self._local_state().get_snapshot(expected_config_key=config_key)
        if local_snapshot is None:
            raise RuntimeError(
                "本机共享配置快照不存在或与当前 PostgreSQL 配置不匹配；无法离线读取共享配置。"
            )
        return CloudUserConfigCacheSnapshot(
            revision=local_snapshot.revision,
            items={key: dict(value) for key, value in local_snapshot.items.items()},
            config_key=local_snapshot.config_key,
        )

    def _apply_process_cache(self, snapshot: CloudUserConfigCacheSnapshot) -> None:
        config_key = snapshot.config_key or _config_key(self._config_accessor())
        with self._cache_lock:
            self.__class__._cache_revision = snapshot.revision
            self.__class__._cache_items = {key: dict(value) for key, value in snapshot.items.items()}
            self.__class__._cache_config_key = config_key

    def _save_cache_snapshot_to_local_state(self, snapshot: CloudUserConfigCacheSnapshot) -> None:
        config_key = snapshot.config_key or _config_key(self._config_accessor())
        self._local_state().save_snapshot(
            config_key=config_key,
            revision=snapshot.revision,
            items={key: dict(value) for key, value in snapshot.items.items()},
        )

    def _local_state(self) -> CloudUserConfigLocalStateRepository:
        if self._local_state_repository is not None:
            return self._local_state_repository
        self._local_state_repository = CloudUserConfigLocalStateRepository(
            lambda: self._config_accessor().bootstrap_data_dir,
        )
        return self._local_state_repository

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
        return CloudUserConfigCacheSnapshot(
            revision=revision,
            items=items,
            config_key=_config_key(self._config_accessor()),
        )

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
