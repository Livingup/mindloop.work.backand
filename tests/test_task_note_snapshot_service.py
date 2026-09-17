# -*- coding: utf-8 -*-
from agent_runtime.task_note.task_note_snapshot_service import (
    TaskNoteSnapshotService,
    get_empty_task_note_snapshot,
)


def test_empty_snapshot_structure() -> None:
    snapshot = get_empty_task_note_snapshot("task-001", "inst-001")
    assert snapshot["task_id"] == "task-001"
    assert snapshot["agent_instance_id"] == "inst-001"
    assert snapshot["missions"] == []
    assert snapshot["workspace_context"] == []
    assert snapshot["solution"] is None
    assert snapshot["decisions"] == []
    assert snapshot["todos"] == []
    assert snapshot["progress"] is None
    assert snapshot["discoveries"] == []
    assert snapshot["issues"] == []
    assert snapshot["artifacts"] == []
    assert snapshot["validations"] == []
    assert snapshot["external_states"] == []
    assert snapshot["commands"] == []
    assert snapshot["agent_relationships"] == []
    assert snapshot["agent_messages"] == []
    assert snapshot["remote_agent_messages"] == []


def test_snapshot_service_without_db() -> None:
    service = TaskNoteSnapshotService(connection_factory=None)
    snapshot = service.build_snapshot(task_id="task-001", agent_instance_id="inst-001")
    assert snapshot["task_id"] == "task-001"
    assert snapshot["agent_instance_id"] == "inst-001"
    assert isinstance(snapshot["todos"], list)


def test_snapshot_service_singleton() -> None:
    from agent_runtime.task_note.task_note_snapshot_service import (
        get_task_note_snapshot_service,
        set_task_note_snapshot_service,
    )

    original = get_task_note_snapshot_service()
    assert original is not None

    custom = TaskNoteSnapshotService(connection_factory=None)
    set_task_note_snapshot_service(custom)
    assert get_task_note_snapshot_service() is custom

    set_task_note_snapshot_service(None)
    reloaded = get_task_note_snapshot_service()
    assert reloaded is not None
    assert reloaded is not custom


def test_snapshot_service_mock_db() -> None:
    from unittest.mock import MagicMock
    from contextlib import contextmanager

    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_cursor.fetchone.return_value = None

    @contextmanager
    def mock_conn():
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = mock_cursor
        conn.cursor.return_value.__exit__.return_value = False
        yield conn

    service = TaskNoteSnapshotService(connection_factory=mock_conn)
    snapshot = service.build_snapshot(task_id="t-1", agent_instance_id="a-1")
    assert snapshot["task_id"] == "t-1"
    assert snapshot["agent_instance_id"] == "a-1"
    assert mock_cursor.execute.called

    # 验证第一条 agent_user_mission 查询必须严格包含双 ID
    first_call_sql, first_call_params = mock_cursor.execute.call_args_list[0][0]
    assert "FROM agent_user_mission" in first_call_sql
    assert "WHERE task_id = %s AND agent_instance_id = %s" in first_call_sql
    assert first_call_params == ("t-1", "a-1")


def test_snapshot_service_all_queries_enforce_dual_ids() -> None:
    """验证 build_snapshot 对所有 15 张业务子表的查询均 100% 携带 task_id 与 agent_instance_id。"""
    from unittest.mock import MagicMock
    from contextlib import contextmanager

    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_cursor.fetchone.return_value = None

    @contextmanager
    def mock_conn():
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = mock_cursor
        conn.cursor.return_value.__exit__.return_value = False
        yield conn

    service = TaskNoteSnapshotService(connection_factory=mock_conn)
    service.build_snapshot(task_id="task-env-01", agent_instance_id="agent-worker-01")

    # 快照涵盖 15 个查询
    assert mock_cursor.execute.call_count >= 15
    for call_item in mock_cursor.execute.call_args_list:
        sql, params = call_item[0]
        assert "WHERE task_id = %s AND agent_instance_id = %s" in sql
        assert params == ("task-env-01", "agent-worker-01")

