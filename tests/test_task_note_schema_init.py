# -*- coding: utf-8 -*-
"""Phase 1: Task Note Schema 初始化与迁移单测 (D01)。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent_runtime.task_note.infrastructure.task_note_schema import (
    REQUIRED_MIGRATION_COLUMNS,
    ensure_task_note_schema,
    init_task_note_schema,
)
from agent_runtime.task_note.task_note_store import (
    is_task_note_available,
    set_task_note_available,
)


def test_ensure_task_note_schema_when_pg_disabled():
    """验证：PG 未启用时返回 False，不抛出异常。"""
    mock_cfg = MagicMock()
    mock_cfg.cloud_user_config_database.is_postgresql_enabled = False

    set_task_note_available(False)
    ok = ensure_task_note_schema(mock_cfg)
    assert ok is False
    assert is_task_note_available() is False


def test_ensure_task_note_schema_when_ddl_raises():
    """验证：DDL 执行发生异常时记日志并安全返回 False，不向上抛出阻断服务启动。"""
    mock_cfg = MagicMock()
    mock_cfg.cloud_user_config_database.is_postgresql_enabled = True

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.execute.side_effect = RuntimeError("Database network drop")
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    mock_store = MagicMock()
    mock_store._connect.return_value.__enter__.return_value = mock_conn

    with patch("agent_runtime.cloud_user_config.postgres_store.PostgresUserConfigStore", return_value=mock_store):
        set_task_note_available(False)
        ok = ensure_task_note_schema(mock_cfg)
        assert ok is False
        assert is_task_note_available() is False


def test_ensure_task_note_schema_when_columns_missing():
    """验证：DDL 执行后若缺少必需迁移列，断言失败返回 False。"""
    mock_cfg = MagicMock()
    mock_cfg.cloud_user_config_database.is_postgresql_enabled = True

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    # 模拟 information_schema 只查出部分列，缺失 pruned_at
    mock_cur.fetchall.return_value = [
        ("agent_user_mission", "outcome_summary"),
        ("task_agent_relationships", "delegation_content"),
        ("task_agent_relationships", "fork_message_id"),
    ]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    mock_store = MagicMock()
    mock_store._connect.return_value.__enter__.return_value = mock_conn

    with patch("agent_runtime.cloud_user_config.postgres_store.PostgresUserConfigStore", return_value=mock_store):
        set_task_note_available(False)
        ok = ensure_task_note_schema(mock_cfg)
        assert ok is False


def test_ensure_task_note_schema_success():
    """验证：DDL 执行成功且 4 列全在时返回 True，并置 available 为 True。"""
    mock_cfg = MagicMock()
    mock_cfg.cloud_user_config_database.is_postgresql_enabled = True

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    # 模拟 information_schema 查出全部 4 列
    mock_cur.fetchall.return_value = list(REQUIRED_MIGRATION_COLUMNS)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur

    mock_store = MagicMock()
    mock_store._connect.return_value.__enter__.return_value = mock_conn

    with patch("agent_runtime.cloud_user_config.postgres_store.PostgresUserConfigStore", return_value=mock_store):
        ok = ensure_task_note_schema(mock_cfg)
        assert ok is True
        set_task_note_available(ok)
        assert is_task_note_available() is True
