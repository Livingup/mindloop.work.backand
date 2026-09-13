# -*- coding: utf-8 -*-
"""TaskNoteStore 数据库持久化层与 31 个 Note 维护工具的单元与集成测试。"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_runtime.task_note.task_note_models import (
    AppendMissionPromptRequest,
    BatchCreateTaskTodosRequest,
    BindResourcesToMissionRequest,
    CreateMissionRequest,
    CreateTaskArtifactOtherRequest,
    CreateTaskDecisionRequest,
    CreateTaskDiscoveryRequest,
    CreateTaskExternalStateRequest,
    CreateTaskIssueRequest,
    CreateTaskTodoRequest,
    CreateTaskValidationRequest,
    CreateTaskWorkspaceContextRequest,
    DeleteTaskArtifactOtherRequest,
    DeleteTaskDiscoveryRequest,
    DeleteTaskTodoRequest,
    DeleteTaskWorkspaceContextRequest,
    LineRange,
    NoteUpdateEndRequest,
    ResolveTaskIssueRequest,
    SupersedeTaskDecisionRequest,
    SupersedeTaskSolutionRequest,
    TodoItemInput,
    UpdateMissionRequirementRequest,
    UpdateMissionStatusRequest,
    UpdateTaskDecisionRequest,
    UpdateTaskDiscoveryRequest,
    UpdateTaskExternalStateRequest,
    UpdateTaskIssueRequest,
    UpdateTaskProgressRequest,
    UpdateTaskSolutionRequest,
    UpdateTaskTodoContentRequest,
    UpdateTaskTodoStatusRequest,
    UpdateTaskValidationRequest,
    UpdateTaskWorkspaceContextRequest,
)
from agent_runtime.task_note.task_note_snapshot_service import TaskNoteSnapshotService
from agent_runtime.task_note.task_note_store import (
    TaskNoteStore,
    get_task_note_store,
    set_task_note_store,
)
from agent_runtime.task_note.task_note_tool import (
    append_mission_prompt,
    batch_create_task_todos,
    bind_resources_to_mission,
    create_mission,
    create_task_artifact_other,
    create_task_decision,
    create_task_discovery,
    create_task_external_state,
    create_task_issue,
    create_task_todo,
    create_task_validation,
    create_task_workspace_context,
    delete_task_artifact_other,
    delete_task_discovery,
    delete_task_todo,
    delete_task_workspace_context,
    note_update_end,
    resolve_task_issue,
    supersede_task_decision,
    supersede_task_solution,
    update_mission_requirement,
    update_mission_status,
    update_task_decision,
    update_task_discovery,
    update_task_external_state,
    update_task_issue,
    update_task_progress,
    update_task_solution,
    update_task_todo_content,
    update_task_todo_status,
    update_task_validation,
    update_task_workspace_context,
)
from agent_runtime.tool.tool_execution_models import ToolRuntimeContext


class MockCursor:
    """模拟数据库游标。"""

    def __init__(self, executed_statements: list[tuple[str, tuple[Any, ...]]]):
        self.executed_statements = executed_statements
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.executed_statements.append((sql.strip(), params or ()))

    def fetchone(self):
        return ("sol_test_123",)

    def fetchall(self):
        return []


class MockConnectionContext:
    """模拟数据库连接上下文管理器，捕获执行的 SQL 与参数。"""

    def __init__(self, executed_statements: list[tuple[str, tuple[Any, ...]]]):
        self.executed_statements = executed_statements

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def cursor(self):
        return MockCursor(self.executed_statements)

    def commit(self):
        pass


@pytest.fixture
def mock_db():
    executed: list[tuple[str, tuple[Any, ...]]] = []

    def factory():
        return MockConnectionContext(executed)

    store = TaskNoteStore(connection_factory=factory)
    return store, executed


def test_task_note_store_no_db_fallback():
    """测试在无数据库配置时的降级与安全缺省行为。"""
    store = TaskNoteStore(connection_factory=None)

    mid = store.create_mission(task_id="t1", scene_id="s1", agent_instance_id="a1", title="任务标题")
    assert mid.startswith("mission-")

    assert store.append_mission_prompt(task_id="t1", agent_instance_id="a1", mission_id=mid, prompt="原话") is True
    assert store.update_mission_requirement(task_id="t1", agent_instance_id="a1", mission_id=mid, refined_requirement="规格") is True
    assert store.update_mission_status(task_id="t1", agent_instance_id="a1", mission_id=mid, status="completed") is True

    cid = store.create_workspace_context(task_id="t1", mission_id=mid, agent_instance_id="a1", source_type="file", path="test.py")
    assert cid.startswith("ctx_")
    assert store.update_workspace_context(task_id="t1", agent_instance_id="a1", context_id=cid, description="说明") is True
    assert store.delete_workspace_context(task_id="t1", agent_instance_id="a1", context_id=cid) is True

    sid = store.upsert_solution(task_id="t1", mission_id=mid, agent_instance_id="a1", solution_content="方案内容")
    assert sid.startswith("sol_")
    assert store.supersede_solution(task_id="t1", mission_id=mid, agent_instance_id="a1") == "sol-superseded-t1"

    did = store.create_decision(task_id="t1", mission_id=mid, agent_instance_id="a1", decision_content="决策", rationale="理由")
    assert did.startswith("dec_")
    assert store.supersede_decision(task_id="t1", agent_instance_id="a1", decision_id=did) is True
    assert store.update_decision(task_id="t1", agent_instance_id="a1", decision_id=did, rationale="新理由") is True

    tid = store.create_todo(task_id="t1", mission_id=mid, agent_instance_id="a1", todo_content="工作内容")
    assert tid.startswith("todo_")
    batch = store.batch_create_todos(task_id="t1", mission_id=mid, agent_instance_id="a1", todos=[{"todo_content": "item1"}])
    assert len(batch) == 1
    assert store.update_todo_status(task_id="t1", agent_instance_id="a1", todo_id=tid, status="已完成") is True
    assert store.update_todo_content(task_id="t1", agent_instance_id="a1", todo_id=tid, todo_content="新内容") is True
    assert store.delete_todo(task_id="t1", agent_instance_id="a1", todo_id=tid) is True

    disc_id = store.create_discovery(task_id="t1", mission_id=mid, agent_instance_id="a1", discovery_content="发现", impact="影响")
    assert disc_id.startswith("disc_")
    assert store.update_discovery(task_id="t1", agent_instance_id="a1", discovery_id=disc_id, impact="新影响") is True
    assert store.delete_discovery(task_id="t1", agent_instance_id="a1", discovery_id=disc_id) is True

    iid = store.create_issue(task_id="t1", mission_id=mid, agent_instance_id="a1", issue_content="故障", impact="影响")
    assert iid.startswith("issue_")
    assert store.update_issue(task_id="t1", agent_instance_id="a1", issue_id=iid, status="in_progress") is True
    assert store.resolve_issue(task_id="t1", agent_instance_id="a1", issue_id=iid, resolution="解决方案") is True

    art_id = store.create_artifact_other(task_id="t1", mission_id=mid, agent_instance_id="a1", artifact_type="other", content="产物内容")
    assert art_id.startswith("art_")
    assert store.delete_artifact_other(task_id="t1", agent_instance_id="a1", artifact_id=art_id) is True

    vid = store.create_validation(
        task_id="t1",
        mission_id=mid,
        agent_instance_id="a1",
        target_type="requirement",
        validation_method="automated_test",
        validation_scope="范围",
        conclusion="passed",
        validation_content="用例通过",
        source_type="manual",
    )
    assert vid.startswith("val_")
    assert store.update_validation(task_id="t1", agent_instance_id="a1", validation_id=vid, conclusion="failed") is True

    ext_id = store.create_external_state(
        task_id="t1",
        mission_id=mid,
        agent_instance_id="a1",
        target_type="database",
        target_identifier="pg_db",
        change_content="迁移脚本",
        current_state="已迁移",
    )
    assert ext_id.startswith("ext_")
    assert store.update_external_state(task_id="t1", agent_instance_id="a1", external_state_id=ext_id, current_state="运行中") is True

    pid = store.upsert_progress(
        task_id="t1",
        mission_id=mid,
        agent_instance_id="a1",
        completed_summary="完成总结",
        current_work="当前动作",
        next_step="下一步工具",
    )
    assert pid.startswith("prog_")

    assert store.bind_resources_to_mission(task_id="t1", agent_instance_id="a1", mission_id=mid, resource_type="artifact", resource_ids=["r1", "r2"]) == 2


def test_task_note_store_crud_with_mock_db(mock_db):
    """测试 TaskNoteStore 在有 DB 连接时正确生成并执行 SQL。"""
    store, executed = mock_db

    # 1. create_mission
    mid = store.create_mission(
        task_id="t1",
        scene_id="s1",
        agent_instance_id="a1",
        title="测试任务",
        raw_prompt="用户原话",
        refined_requirement="提炼需求",
    )
    assert mid is not None
    assert len(executed) == 1
    sql, params = executed[-1]
    assert "INSERT INTO agent_user_mission" in sql
    assert params[0] == mid
    assert params[1] == "t1"
    assert params[2] == "s1"
    assert params[4] == "测试任务"

    # 2. append_mission_prompt
    store.append_mission_prompt(task_id="t1", agent_instance_id="a1", mission_id=mid, prompt="追加原话")
    sql, params = executed[-1]
    assert "UPDATE agent_user_mission" in sql
    assert "raw_prompts = raw_prompts ||" in sql

    # 3. bind_resources_to_mission (artifact, command, agent_relationship)
    store.bind_resources_to_mission(task_id="t1", agent_instance_id="a1", mission_id=mid, resource_type="artifact", resource_ids=["art-1", "art-2"])
    sql, params = executed[-1]
    assert "UPDATE task_artifact_records" in sql
    assert "mission_id = %s" in sql

    store.bind_resources_to_mission(task_id="t1", agent_instance_id="a1", mission_id=mid, resource_type="agent_relationship", resource_ids=["rel-1"])
    assert len(executed) >= 2
    # 验证级联更新 task_agent_messages
    assert any("UPDATE task_agent_relationships" in s for s, _ in executed)
    assert any("UPDATE task_agent_messages" in s for s, _ in executed)

    # 4. get_command_lines_by_ids & get_all_command_lines_for_tenant
    store.get_command_lines_by_ids(task_id="t1", command_ids=["cmd-1", "cmd-2"])
    sql, params = executed[-1]
    assert "FROM task_command_records" in sql
    assert "command_id = ANY(%s)" in sql


def test_task_note_tool_database_error_handling():
    """测试当底层数据库抛出异常时，Tool 稳健返回 BaseToolResult(success=False, error_code='DatabaseError')。"""
    mock_store = MagicMock(spec=TaskNoteStore)
    mock_store.create_mission.side_effect = RuntimeError("PG connection failed")

    old_store = get_task_note_store()
    set_task_note_store(mock_store)
    try:
        ctx = ToolRuntimeContext(task_id="t-err", agent_instance_id="a-err", scene_id="scene-01")
        res = create_mission(CreateMissionRequest(title="报错任务"), ctx)
        assert res.success is False
        assert res.execution_status == "failed"
        assert res.error_code == "DatabaseError"
        assert "PG connection failed" in (res.error_message or "")
    finally:
        set_task_note_store(old_store)


def test_task_note_tool_scene_id_passthrough():
    """测试 ToolRuntimeContext 中的 scene_id 穿透至 create_mission。"""
    mock_store = MagicMock(spec=TaskNoteStore)
    mock_store.create_mission.return_value = "mission-mock-123"

    old_store = get_task_note_store()
    set_task_note_store(mock_store)
    try:
        ctx = ToolRuntimeContext(task_id="task-custom-01", agent_instance_id="agent-custom-01", scene_id="scene-custom-999")
        res = create_mission(CreateMissionRequest(title="场景穿透测试"), ctx)
        assert res.success is True
        assert res.output["mission_id"] == "mission-mock-123"
        mock_store.create_mission.assert_called_once()
        kwargs = mock_store.create_mission.call_args.kwargs
        assert kwargs["scene_id"] == "scene-custom-999"
        assert kwargs["task_id"] == "task-custom-01"
        assert kwargs["agent_instance_id"] == "agent-custom-01"
    finally:
        set_task_note_store(old_store)


def test_snapshot_service_read_after_store_contract_alignment():
    """测试 TaskNoteSnapshotService 读取结构与 TaskNoteStore 写入契约对齐。"""
    service = TaskNoteSnapshotService(connection_factory=None)
    snapshot = service.build_snapshot(task_id="task-align-01", agent_instance_id="inst-align-01")

    # 11 个业务主题对应字段必须齐全且结构合规
    assert "missions" in snapshot
    assert "workspace_context" in snapshot
    assert "solution" in snapshot
    assert "decisions" in snapshot
    assert "todos" in snapshot
    assert "discoveries" in snapshot
    assert "issues" in snapshot
    assert "artifacts" in snapshot
    assert "validations" in snapshot
    assert "external_states" in snapshot
    assert "progress" in snapshot
    assert "commands" in snapshot
    assert "agent_relationships" in snapshot
    assert "agent_messages" in snapshot
    assert "remote_agent_messages" in snapshot


def test_cleanup_agent_instance_note(mock_db: Any):
    """测试非 Root Agent 调用 end_task 时物理删除该实例的 15 张表 Note 记录（含 task_command_records）。"""
    store, executed = mock_db
    deleted = store.cleanup_agent_instance_note(task_id="task-cleanup-01", agent_instance_id="subagent-worker-01")

    # 15 张表每个表执行一条 DELETE 语句，总共 15 条
    assert len(executed) == 15
    assert deleted == 15

    executed_tables = []
    for sql, params in executed:
        assert sql.startswith("DELETE FROM ")
        assert "WHERE task_id = %s AND agent_instance_id = %s" in sql
        assert params == ("task-cleanup-01", "subagent-worker-01")
        # 提取表名
        table_name = sql.split()[2]
        executed_tables.append(table_name)

    # 重点验证：task_command_records 必须在内
    assert "task_command_records" in executed_tables
    assert "agent_user_mission" in executed_tables
    assert "task_solution_content" in executed_tables
    assert "task_todo_items" in executed_tables
    assert "task_artifact_records" in executed_tables
    assert "task_agent_relationships" in executed_tables
    assert "task_agent_messages" in executed_tables


def test_cleanup_task_note(mock_db: Any):
    """测试 Root Agent 调用 end_task 时物理删除该 Task 下所有实例的 15 张表 Note 记录（含 task_command_records）。"""
    store, executed = mock_db
    deleted = store.cleanup_task_note(task_id="task-cleanup-02")

    assert len(executed) == 15
    assert deleted == 15

    executed_tables = []
    for sql, params in executed:
        assert sql.startswith("DELETE FROM ")
        assert sql.endswith("WHERE task_id = %s")
        assert params == ("task-cleanup-02",)
        table_name = sql.split()[2]
        executed_tables.append(table_name)

    # 重点验证：task_command_records 必须在内
    assert "task_command_records" in executed_tables
    assert "agent_user_mission" in executed_tables
    assert "task_solution_content" in executed_tables
    assert "task_progress_content" in executed_tables


@pytest.mark.asyncio
async def test_snapshot_service_async_cleanup(mock_db: Any):
    """测试 TaskNoteSnapshotService 的异步封装清理调用。"""
    _store, executed = mock_db
    # 构建连接工厂
    def factory():
        return MockConnectionContext(executed)

    service = TaskNoteSnapshotService(connection_factory=factory)

    # 1. 异步清理子 Agent 实例 (支持关键字与位置传参)
    del_inst = await service.async_cleanup_agent_instance_note(task_id="t1", agent_instance_id="sub1")
    assert del_inst == 15
    del_inst_pos = await service.async_cleanup_agent_instance_note("t1", "sub1")
    assert del_inst_pos == 15

    # 2. 异步清理 Task 全量 (支持关键字与位置传参)
    del_task = await service.async_cleanup_task_note(task_id="t1")
    assert del_task == 15
    del_task_pos = await service.async_cleanup_task_note("t1")
    assert del_task_pos == 15


def test_dump_json_serializes_pydantic_line_ranges():
    """测试 _dump_json 能够安全将包含 Pydantic LineRange 的对象序列化为原生可序列化格式。"""
    import json
    from agent_runtime.task_note.task_note_models import LineRange
    from agent_runtime.task_note.task_note_store import _dump_json, _to_json_compatible

    ranges = [LineRange(start_line=10, end_line=30), LineRange(start_line=50, end_line=100)]
    compat = _to_json_compatible(ranges)
    assert compat == [{"start_line": 10, "end_line": 30}, {"start_line": 50, "end_line": 100}]

    dumped = _dump_json(ranges)
    # 不论是 Jsonb 还是 fallback json str，都应可被标准 json.dumps 处理
    inner_val = getattr(dumped, "obj", dumped)
    serialized = json.dumps(inner_val)
    assert '{"start_line": 10, "end_line": 30}' in serialized
    assert '{"start_line": 50, "end_line": 100}' in serialized


def test_create_workspace_context_with_pydantic_line_ranges(mock_db: Any):
    """测试 create_task_workspace_context 传入 Pydantic LineRange 实例时正常执行入库。"""
    import json
    from agent_runtime.task_note.task_note_models import CreateTaskWorkspaceContextRequest, LineRange
    from agent_runtime.task_note.task_note_store import set_task_note_store
    _store, executed = mock_db
    set_task_note_store(_store)
    try:
        ctx = ToolRuntimeContext(task_id="t_real", agent_instance_id="inst_real")

        req = CreateTaskWorkspaceContextRequest(
            mission_id="m1",
            source_type="file",
            path="src/engine.py",
            line_ranges=[LineRange(start_line=3300, end_line=3479)],
            description="引擎门禁",
        )
        res = create_task_workspace_context(req, ctx)
        assert res.success is True
        assert "context_id" in res.output

        # 验证执行的 SQL 参数中 line_ranges 已正确转换为原生 dict，可被 json.dumps 序列化
        insert_stmt = [stmt for stmt in executed if "INSERT INTO task_workspace_context" in stmt[0]][0]
        line_ranges_param = insert_stmt[1][7]
        inner = getattr(line_ranges_param, "obj", line_ranges_param)
        dumped = json.dumps(inner)
        assert '{"start_line": 3300, "end_line": 3479}' in dumped
    finally:
        set_task_note_store(None)

