# -*- coding: utf-8 -*-
"""LLM Registry SQLite 仓储实现。

该仓储是 LLM 模型元数据、服务商定义和执行策略组的运行期权威持久化实现。
历史 YAML 数据导入由仓库外部的一次性迁移脚本完成；本仓储只负责 DB schema 初始化和正常 CRUD，
不在应用启动或仓储初始化时读取 legacy YAML。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from agent_runtime.llm_model_registry.llm_execution_policy import (
    LlmExecutionPolicy,
    LlmExecutionPolicyCandidate,
    UNSUPPORTED_INPUT_MODALITY_STRATEGY_DEGRADE,
)
from agent_runtime.llm_model_registry.llm_model_metadata import LlmModelMetadata
from agent_runtime.llm_model_registry.llm_provider_definition import (
    LlmApiKey,
    LlmProviderDefinition,
    LlmProviderModelBinding,
    LlmRetryPolicy,
)
from agent_runtime.llm_model_registry.llm_registry_validation import validate_registry_id

logger = logging.getLogger(__name__)

LLM_MODEL_REGISTRY_DB_FILENAME = "llm_model_registry.sqlite3"

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS llm_model_metadata (
    model_id                            TEXT PRIMARY KEY,
    context_window                      INTEGER NOT NULL,
    max_output_tokens                   INTEGER NOT NULL,
    group_name                          TEXT NOT NULL,
    compression_threshold               INTEGER,
    supports_tool_calling               INTEGER NOT NULL,
    supports_structured_output          INTEGER NOT NULL,
    supported_input_modalities_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_model_metadata_group ON llm_model_metadata(group_name);

CREATE TABLE IF NOT EXISTS llm_provider_definitions (
    provider_id                         TEXT PRIMARY KEY,
    group_name                          TEXT NOT NULL,
    api_base_url                        TEXT NOT NULL,
    custom_headers_json                 TEXT NOT NULL,
    retry_json                          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_provider_definitions_group ON llm_provider_definitions(group_name);

CREATE TABLE IF NOT EXISTS llm_provider_api_keys (
    provider_id                         TEXT NOT NULL,
    position                            INTEGER NOT NULL,
    key                                 TEXT NOT NULL,
    name                                TEXT,
    PRIMARY KEY (provider_id, position),
    FOREIGN KEY (provider_id) REFERENCES llm_provider_definitions(provider_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS llm_provider_model_bindings (
    provider_id                         TEXT NOT NULL,
    binding_id                          TEXT NOT NULL,
    position                            INTEGER NOT NULL,
    model_id                            TEXT NOT NULL,
    model_alias                         TEXT NOT NULL,
    api_format                          TEXT NOT NULL,
    tool_choice                         TEXT,
    thinking                            TEXT,
    custom_request_params               TEXT,
    api_base_url                        TEXT,
    custom_headers_json                 TEXT NOT NULL,
    PRIMARY KEY (provider_id, binding_id),
    UNIQUE (provider_id, position),
    FOREIGN KEY (provider_id) REFERENCES llm_provider_definitions(provider_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_llm_provider_model_bindings_model ON llm_provider_model_bindings(model_id);

CREATE TABLE IF NOT EXISTS llm_provider_model_binding_api_keys (
    provider_id                         TEXT NOT NULL,
    binding_id                          TEXT NOT NULL,
    position                            INTEGER NOT NULL,
    key                                 TEXT NOT NULL,
    name                                TEXT,
    PRIMARY KEY (provider_id, binding_id, position),
    FOREIGN KEY (provider_id, binding_id)
        REFERENCES llm_provider_model_bindings(provider_id, binding_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS llm_execution_policies (
    policy_group_id                     TEXT PRIMARY KEY,
    name                                TEXT,
    description                         TEXT,
    group_name                          TEXT NOT NULL,
    enabled                             INTEGER NOT NULL,
    unsupported_input_modality_strategy TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_execution_policies_group ON llm_execution_policies(group_name);

CREATE TABLE IF NOT EXISTS llm_execution_policy_candidates (
    policy_group_id                     TEXT NOT NULL,
    position                            INTEGER NOT NULL,
    candidate_id                        TEXT NOT NULL,
    provider_id                         TEXT NOT NULL,
    model_binding_id                    TEXT NOT NULL,
    enabled                             INTEGER NOT NULL,
    PRIMARY KEY (policy_group_id, position),
    UNIQUE (policy_group_id, candidate_id),
    FOREIGN KEY (policy_group_id) REFERENCES llm_execution_policies(policy_group_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_llm_execution_policy_candidates_provider
    ON llm_execution_policy_candidates(provider_id, model_binding_id);
"""


def llm_model_registry_db_path(registry_dir: str) -> Path:
    """根据 registry_dir 解析 LLM Registry SQLite 文件路径。"""
    return Path(registry_dir).expanduser() / LLM_MODEL_REGISTRY_DB_FILENAME


class SqliteLlmModelRegistryRepository:
    """基于 SQLite 的 LLM Registry 仓储。

    模型元数据、服务商定义和执行策略组都以领域聚合根为读写单位；API Keys、Provider
    模型接入项和执行策略候选项的数组顺序通过 position 列显式保存，避免运行时轮换优先级
    或外层候选切换顺序被 SQLite 行顺序打乱。
    """

    def __init__(self, registry_dir_accessor: Callable[[], str]) -> None:
        self._registry_dir_accessor = registry_dir_accessor
        self._lock = RLock()
        self._conn: sqlite3.Connection | None = None
        self._db_path: Path | None = None

    def close(self) -> None:
        """关闭 SQLite 连接；下次访问会按当前 registry_dir 重新打开。"""
        with self._lock:
            self._close_locked()

    def ensure_initialized(self) -> None:
        """确保 SQLite 文件和 schema 已创建。"""
        self._connection()

    # ── 模型元数据集合 ──────────────────────────────────────

    def find_all_model_metadata(self) -> list[LlmModelMetadata]:
        with self._lock:
            rows = self._connection().execute(
                """
                SELECT model_id, context_window, max_output_tokens, group_name,
                       compression_threshold, supports_tool_calling,
                       supports_structured_output, supported_input_modalities_json
                FROM llm_model_metadata
                ORDER BY model_id ASC
                """
            ).fetchall()
            return [_row_to_model_metadata(row) for row in rows]

    def find_model_metadata_by_id(self, model_id: str) -> LlmModelMetadata | None:
        validate_registry_id(model_id, "model_id")
        with self._lock:
            row = self._connection().execute(
                """
                SELECT model_id, context_window, max_output_tokens, group_name,
                       compression_threshold, supports_tool_calling,
                       supports_structured_output, supported_input_modalities_json
                FROM llm_model_metadata
                WHERE model_id = ?
                """,
                (model_id,),
            ).fetchone()
            return _row_to_model_metadata(row) if row else None

    def save_model_metadata(self, metadata: LlmModelMetadata) -> None:
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT INTO llm_model_metadata (
                    model_id, context_window, max_output_tokens, group_name,
                    compression_threshold, supports_tool_calling,
                    supports_structured_output, supported_input_modalities_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(model_id) DO UPDATE SET
                    context_window = excluded.context_window,
                    max_output_tokens = excluded.max_output_tokens,
                    group_name = excluded.group_name,
                    compression_threshold = excluded.compression_threshold,
                    supports_tool_calling = excluded.supports_tool_calling,
                    supports_structured_output = excluded.supports_structured_output,
                    supported_input_modalities_json = excluded.supported_input_modalities_json
                """,
                _model_metadata_to_row(metadata),
            )
            conn.commit()

    def delete_model_metadata(self, model_id: str) -> bool:
        validate_registry_id(model_id, "model_id")
        with self._lock:
            conn = self._connection()
            cursor = conn.execute("DELETE FROM llm_model_metadata WHERE model_id = ?", (model_id,))
            conn.commit()
            return cursor.rowcount > 0

    def contains_model_metadata(self, model_id: str) -> bool:
        validate_registry_id(model_id, "model_id")
        with self._lock:
            row = self._connection().execute(
                "SELECT 1 FROM llm_model_metadata WHERE model_id = ? LIMIT 1",
                (model_id,),
            ).fetchone()
            return row is not None

    # ── 服务商定义集合 ──────────────────────────────────────

    def find_all_providers(self) -> list[LlmProviderDefinition]:
        with self._lock:
            rows = self._connection().execute(
                """
                SELECT provider_id, group_name, api_base_url, custom_headers_json, retry_json
                FROM llm_provider_definitions
                ORDER BY provider_id ASC
                """
            ).fetchall()
            return [self._row_to_provider(row) for row in rows]

    def find_provider_by_id(self, provider_id: str) -> LlmProviderDefinition | None:
        validate_registry_id(provider_id, "provider_id")
        with self._lock:
            row = self._connection().execute(
                """
                SELECT provider_id, group_name, api_base_url, custom_headers_json, retry_json
                FROM llm_provider_definitions
                WHERE provider_id = ?
                """,
                (provider_id,),
            ).fetchone()
            return self._row_to_provider(row) if row else None

    def save_provider(self, provider: LlmProviderDefinition) -> None:
        with self._lock:
            conn = self._connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO llm_provider_definitions (
                        provider_id, group_name, api_base_url, custom_headers_json, retry_json
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(provider_id) DO UPDATE SET
                        group_name = excluded.group_name,
                        api_base_url = excluded.api_base_url,
                        custom_headers_json = excluded.custom_headers_json,
                        retry_json = excluded.retry_json
                    """,
                    (
                        provider.provider_id,
                        provider.group,
                        provider.api_base_url,
                        _json_dumps(provider.custom_headers),
                        _json_dumps(provider.retry.to_dict()),
                    ),
                )
                conn.execute("DELETE FROM llm_provider_api_keys WHERE provider_id = ?", (provider.provider_id,))
                conn.execute("DELETE FROM llm_provider_model_bindings WHERE provider_id = ?", (provider.provider_id,))
                conn.executemany(
                    """
                    INSERT INTO llm_provider_api_keys (provider_id, position, key, name)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        (provider.provider_id, index, api_key.key, api_key.name)
                        for index, api_key in enumerate(provider.api_keys)
                    ),
                )
                for index, binding in enumerate(provider.models):
                    conn.execute(
                        """
                        INSERT INTO llm_provider_model_bindings (
                            provider_id, binding_id, position, model_id, model_alias, api_format,
                            tool_choice, thinking, custom_request_params, api_base_url, custom_headers_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            provider.provider_id,
                            binding.id,
                            index,
                            binding.model_id,
                            binding.model_alias,
                            binding.api_format,
                            binding.tool_choice,
                            binding.thinking,
                            binding.custom_request_params,
                            binding.api_base_url,
                            _json_dumps(binding.custom_headers),
                        ),
                    )
                    conn.executemany(
                        """
                        INSERT INTO llm_provider_model_binding_api_keys (
                            provider_id, binding_id, position, key, name
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            (provider.provider_id, binding.id, key_index, api_key.key, api_key.name)
                            for key_index, api_key in enumerate(binding.api_keys)
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def delete_provider(self, provider_id: str) -> bool:
        validate_registry_id(provider_id, "provider_id")
        with self._lock:
            conn = self._connection()
            cursor = conn.execute("DELETE FROM llm_provider_definitions WHERE provider_id = ?", (provider_id,))
            conn.commit()
            return cursor.rowcount > 0

    def contains_provider(self, provider_id: str) -> bool:
        validate_registry_id(provider_id, "provider_id")
        with self._lock:
            row = self._connection().execute(
                "SELECT 1 FROM llm_provider_definitions WHERE provider_id = ? LIMIT 1",
                (provider_id,),
            ).fetchone()
            return row is not None

    # ── 执行策略组集合 ──────────────────────────────────────

    def find_all_execution_policies(self) -> list[LlmExecutionPolicy]:
        with self._lock:
            rows = self._connection().execute(
                """
                SELECT policy_group_id, name, description, group_name, enabled,
                       unsupported_input_modality_strategy
                FROM llm_execution_policies
                ORDER BY policy_group_id ASC
                """
            ).fetchall()
            return [self._row_to_execution_policy(row) for row in rows]

    def find_execution_policy_by_id(self, policy_group_id: str) -> LlmExecutionPolicy | None:
        validate_registry_id(policy_group_id, "policy_group_id")
        with self._lock:
            row = self._connection().execute(
                """
                SELECT policy_group_id, name, description, group_name, enabled,
                       unsupported_input_modality_strategy
                FROM llm_execution_policies
                WHERE policy_group_id = ?
                """,
                (policy_group_id,),
            ).fetchone()
            return self._row_to_execution_policy(row) if row else None

    def save_execution_policy(self, policy: LlmExecutionPolicy) -> None:
        with self._lock:
            conn = self._connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO llm_execution_policies (
                        policy_group_id, name, description, group_name, enabled,
                        unsupported_input_modality_strategy
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(policy_group_id) DO UPDATE SET
                        name = excluded.name,
                        description = excluded.description,
                        group_name = excluded.group_name,
                        enabled = excluded.enabled,
                        unsupported_input_modality_strategy = excluded.unsupported_input_modality_strategy
                    """,
                    (
                        policy.policy_group_id,
                        policy.name,
                        policy.description,
                        policy.group,
                        _bool_to_int(policy.enabled),
                        policy.unsupported_input_modality_strategy,
                    ),
                )
                conn.execute(
                    "DELETE FROM llm_execution_policy_candidates WHERE policy_group_id = ?",
                    (policy.policy_group_id,),
                )
                conn.executemany(
                    """
                    INSERT INTO llm_execution_policy_candidates (
                        policy_group_id, position, candidate_id, provider_id, model_binding_id, enabled
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            policy.policy_group_id,
                            index,
                            candidate.id,
                            candidate.provider_id,
                            candidate.model_binding_id,
                            _bool_to_int(candidate.enabled),
                        )
                        for index, candidate in enumerate(policy.candidates)
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def delete_execution_policy(self, policy_group_id: str) -> bool:
        validate_registry_id(policy_group_id, "policy_group_id")
        with self._lock:
            conn = self._connection()
            cursor = conn.execute(
                "DELETE FROM llm_execution_policies WHERE policy_group_id = ?",
                (policy_group_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def contains_execution_policy(self, policy_group_id: str) -> bool:
        validate_registry_id(policy_group_id, "policy_group_id")
        with self._lock:
            row = self._connection().execute(
                "SELECT 1 FROM llm_execution_policies WHERE policy_group_id = ? LIMIT 1",
                (policy_group_id,),
            ).fetchone()
            return row is not None

    # ── 内部连接管理 ──────────────────────────────────────────────

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            db_path = llm_model_registry_db_path(self._registry_dir_accessor())
            if self._conn is not None and self._db_path == db_path:
                return self._conn
            if self._conn is not None:
                self._close_locked()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
            self._db_path = db_path
            logger.info("已打开 LLM Registry SQLite 数据库 path=%s", db_path)
            return conn

    def _close_locked(self) -> None:
        if self._conn is None:
            self._db_path = None
            return
        try:
            self._conn.close()
        except sqlite3.Error as exc:
            logger.warning("关闭 LLM Registry SQLite 连接失败 err=%s", exc)
        finally:
            self._conn = None
            self._db_path = None

    # ── 聚合重建 ────────────────────────────────────────────────

    def _row_to_provider(self, row: tuple[Any, ...]) -> LlmProviderDefinition:
        provider_id = str(row[0])
        api_keys = tuple(
            LlmApiKey(key=key_row[2], name=key_row[3])
            for key_row in self._connection().execute(
                """
                SELECT provider_id, position, key, name
                FROM llm_provider_api_keys
                WHERE provider_id = ?
                ORDER BY position ASC
                """,
                (provider_id,),
            ).fetchall()
        )
        binding_rows = self._connection().execute(
            """
            SELECT provider_id, binding_id, position, model_id, model_alias, api_format,
                   tool_choice, thinking, custom_request_params, api_base_url, custom_headers_json
            FROM llm_provider_model_bindings
            WHERE provider_id = ?
            ORDER BY position ASC
            """,
            (provider_id,),
        ).fetchall()
        models = tuple(self._row_to_binding(binding_row) for binding_row in binding_rows)
        return LlmProviderDefinition(
            provider_id=provider_id,
            group=str(row[1]),
            api_base_url=str(row[2]),
            custom_headers=_json_loads_dict(row[3]),
            api_keys=api_keys,
            retry=LlmRetryPolicy.from_dict(_json_loads_dict(row[4])),
            models=models,
        )

    def _row_to_binding(self, row: tuple[Any, ...]) -> LlmProviderModelBinding:
        provider_id = str(row[0])
        binding_id = str(row[1])
        api_keys = tuple(
            LlmApiKey(key=key_row[3], name=key_row[4])
            for key_row in self._connection().execute(
                """
                SELECT provider_id, binding_id, position, key, name
                FROM llm_provider_model_binding_api_keys
                WHERE provider_id = ? AND binding_id = ?
                ORDER BY position ASC
                """,
                (provider_id, binding_id),
            ).fetchall()
        )
        return LlmProviderModelBinding(
            id=binding_id,
            model_id=str(row[3]),
            model_alias=str(row[4]),
            api_format=str(row[5]),
            tool_choice=row[6],
            thinking=row[7],
            custom_request_params=row[8],
            api_base_url=row[9],
            custom_headers=_json_loads_dict(row[10]),
            api_keys=api_keys,
        )

    def _row_to_execution_policy(self, row: tuple[Any, ...]) -> LlmExecutionPolicy:
        policy_group_id = str(row[0])
        candidate_rows = self._connection().execute(
            """
            SELECT policy_group_id, position, candidate_id, provider_id, model_binding_id, enabled
            FROM llm_execution_policy_candidates
            WHERE policy_group_id = ?
            ORDER BY position ASC
            """,
            (policy_group_id,),
        ).fetchall()
        candidates = tuple(
            LlmExecutionPolicyCandidate(
                id=str(candidate_row[2]),
                provider_id=str(candidate_row[3]),
                model_binding_id=str(candidate_row[4]),
                enabled=bool(candidate_row[5]),
            )
            for candidate_row in candidate_rows
        )
        return LlmExecutionPolicy(
            policy_group_id=policy_group_id,
            name=row[1],
            description=row[2],
            group=str(row[3]),
            enabled=bool(row[4]),
            unsupported_input_modality_strategy=(
                str(row[5]) if row[5] else UNSUPPORTED_INPUT_MODALITY_STRATEGY_DEGRADE
            ),
            candidates=candidates,
        )


# ── 行转换辅助 ─────────────────────────────────────────────────────


def _model_metadata_to_row(metadata: LlmModelMetadata) -> tuple[Any, ...]:
    return (
        metadata.model_id,
        metadata.context_window,
        metadata.max_output_tokens,
        metadata.group,
        metadata.compression_threshold,
        _bool_to_int(metadata.supports_tool_calling),
        _bool_to_int(metadata.supports_structured_output),
        _json_dumps(list(metadata.supported_input_modalities)),
    )


def _row_to_model_metadata(row: tuple[Any, ...]) -> LlmModelMetadata:
    return LlmModelMetadata(
        model_id=str(row[0]),
        context_window=int(row[1]),
        max_output_tokens=int(row[2]),
        group=str(row[3]),
        compression_threshold=int(row[4]) if row[4] is not None else None,
        supports_tool_calling=bool(row[5]),
        supports_structured_output=bool(row[6]),
        supported_input_modalities=tuple(str(item) for item in _json_loads_list(row[7])),
    )


def _bool_to_int(value: bool) -> int:
    return 1 if value else 0


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError("LLM Registry SQLite JSON 字段应为对象。")
    return data


def _json_loads_list(value: str | None) -> list[Any]:
    if not value:
        return []
    data = json.loads(value)
    if not isinstance(data, list):
        raise ValueError("LLM Registry SQLite JSON 字段应为数组。")
    return data
