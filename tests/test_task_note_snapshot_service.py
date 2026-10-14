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
    """验收 3.1 (D08): 未配置数据库连接或连接不可用时，build_snapshot 抛出 TaskNoteStorageUnavailableError。"""
    import pytest
    from agent_runtime.task_note.task_note_store import TaskNoteStorageUnavailableError

    service = TaskNoteSnapshotService(connection_factory=None)
    with pytest.raises(TaskNoteStorageUnavailableError) as excinfo:
        service.build_snapshot(task_id="task-001", agent_instance_id="inst-001")
    assert "Task Note 存储不可用" in str(excinfo.value)


def test_snapshot_service_single_table_query_failure_raises() -> None:
    """验收 3.1 #3: 单张表查询异常时 build_snapshot 抛错，不返回部分快照。"""
    import pytest
    from unittest.mock import MagicMock
    from contextlib import contextmanager
    from agent_runtime.task_note.task_note_store import TaskNoteStorageUnavailableError

    mock_cursor = MagicMock()
    # 模拟在执行某个表的查询时抛出数据库异常
    mock_cursor.execute.side_effect = Exception("psycopg2 connection reset by peer")

    @contextmanager
    def mock_conn():
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = mock_cursor
        conn.cursor.return_value.__exit__.return_value = False
        yield conn

    service = TaskNoteSnapshotService(connection_factory=mock_conn)
    with pytest.raises(TaskNoteStorageUnavailableError) as excinfo:
        service.build_snapshot(task_id="t-err", agent_instance_id="a-err")
    assert "构建 Task Note 快照失败" in str(excinfo.value) or "查询" in str(excinfo.value)



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


def test_snapshot_service_multi_mission_solutions_and_progresses() -> None:
    """验收 3.2 (D09): 同一 Agent 2 个 mission 各写一条 progress，snapshot.progresses 长度为 2，兼容字段 solution/progress 等于 created_at 最新的一条。"""
    from unittest.mock import MagicMock
    from contextlib import contextmanager

    mock_cursor = MagicMock()

    # 构造 solutions 和 progresses 查询结果
    raw_solutions = [
        {"solution_id": "sol-2", "task_id": "t1", "mission_id": "m-2", "agent_instance_id": "a1", "solution_content": "sol 2", "solution_status": "active", "created_at": "2026-10-13T02:00:00"},
        {"solution_id": "sol-1", "task_id": "t1", "mission_id": "m-1", "agent_instance_id": "a1", "solution_content": "sol 1", "solution_status": "active", "created_at": "2026-10-13T01:00:00"},
    ]
    raw_progresses = [
        {"progress_id": "p-2", "task_id": "t1", "mission_id": "m-2", "agent_instance_id": "a1", "next_step": "step 2", "created_at": "2026-10-13T02:00:00"},
        {"progress_id": "p-1", "task_id": "t1", "mission_id": "m-1", "agent_instance_id": "a1", "next_step": "step 1", "created_at": "2026-10-13T01:00:00"},
    ]

    def mock_execute(sql, params):
        if "task_solution_content" in sql:
            mock_cursor.fetchall.return_value = list(raw_solutions)
        elif "task_progress_content" in sql:
            mock_cursor.fetchall.return_value = list(raw_progresses)
        else:
            mock_cursor.fetchall.return_value = []

    mock_cursor.execute.side_effect = mock_execute

    @contextmanager
    def mock_conn():
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = mock_cursor
        conn.cursor.return_value.__exit__.return_value = False
        yield conn

    service = TaskNoteSnapshotService(connection_factory=mock_conn)
    snapshot = service.build_snapshot(task_id="t1", agent_instance_id="a1")

    # 1. solutions 列表长度为 2，包含两个 mission
    assert len(snapshot["solutions"]) == 2
    assert {s["mission_id"] for s in snapshot["solutions"]} == {"m-1", "m-2"}
    # 兼容字段等于最新的一条
    assert snapshot["solution"]["solution_id"] == "sol-2"

    # 2. progresses 列表长度为 2，包含两个 mission
    assert len(snapshot["progresses"]) == 2
    assert {p["mission_id"] for p in snapshot["progresses"]} == {"m-1", "m-2"}
    # 兼容字段等于最新的一条
    assert snapshot["progress"]["progress_id"] == "p-2"


def test_snapshot_service_artifacts_type_mapping() -> None:
    """验收 3.3 (D16): 库中预置 create/modify/delete/move 旧值的行在快照出参中映射为 file_*。"""
    from unittest.mock import MagicMock
    from contextlib import contextmanager

    mock_cursor = MagicMock()
    raw_artifacts = [
        {"artifact_id": "art-1", "type": "create", "file_path": "a.txt"},
        {"artifact_id": "art-2", "type": "modify", "file_path": "b.txt"},
        {"artifact_id": "art-3", "type": "delete", "file_path": "c.txt"},
        {"artifact_id": "art-4", "type": "move", "file_path": "d.txt"},
        {"artifact_id": "art-5", "type": "file_modify", "file_path": "e.txt"},
        {"artifact_id": "art-6", "type": "other", "file_path": "f.txt"},
    ]

    def mock_execute(sql, params):
        if "task_artifact_records" in sql:
            mock_cursor.fetchall.return_value = list(raw_artifacts)
        else:
            mock_cursor.fetchall.return_value = []

    mock_cursor.execute.side_effect = mock_execute

    @contextmanager
    def mock_conn():
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = mock_cursor
        conn.cursor.return_value.__exit__.return_value = False
        yield conn

    service = TaskNoteSnapshotService(connection_factory=mock_conn)
    snapshot = service.build_snapshot(task_id="t1", agent_instance_id="a1")

    types = [a["type"] for a in snapshot["artifacts"]]
    assert types == ["file_create", "file_modify", "file_delete", "file_move", "file_modify", "other"]


