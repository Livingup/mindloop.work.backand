# -*- coding: utf-8 -*-
"""Agent Task Note 32 个专属维护工具的单元测试。"""
from __future__ import annotations

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
    DeleteMissionRequest,
    DeleteTaskArtifactOtherRequest,
    DeleteTaskArtifactRequest,
    DeleteTaskDiscoveryRequest,
    DeleteTaskTodoRequest,
    DeleteTaskWorkspaceContextRequest,
    LineRange,
    NoteUpdateEndRequest,
    PruneMissionRequest,
    ResolveTaskIssueRequest,
    SupersedeTaskDecisionRequest,
    SupersedeTaskSolutionRequest,
    TodoItemInput,
    UpdateMissionPromptsRequest,
    UpdateMissionRequirementRequest,
    UpdateMissionStatusRequest,
    UpdateMissionTitleRequest,
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
    delete_mission,
    delete_task_artifact,
    delete_task_artifact_other,
    delete_task_discovery,
    delete_task_todo,
    delete_task_workspace_context,
    note_update_end,
    prune_mission,
    resolve_task_issue,
    supersede_task_decision,
    supersede_task_solution,
    update_mission_prompts,
    update_mission_requirement,
    update_mission_status,
    update_mission_title,
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
from agent_runtime.tool._global_registry import get_global_repository
from agent_runtime.tool.infrastructure.openai_schema_generator import (
    OpenAIChatCompletionsSchemaGenerator,
)
from agent_runtime.tool.local_tool_factory import LocalToolFactory
from agent_runtime.tool.tool_execution_models import (
    BaseToolResult,
    ToolRuntimeContext,
)

ALL_NOTE_TOOL_NAMES = [
    "create_mission",
    "append_mission_prompt",
    "update_mission_prompts",
    "update_mission_title",
    "update_mission_requirement",
    "update_mission_status",
    "prune_mission",
    "delete_mission",
    "create_task_workspace_context",
    "update_task_workspace_context",
    "delete_task_workspace_context",
    "update_task_solution",
    "supersede_task_solution",
    "create_task_decision",
    "supersede_task_decision",
    "update_task_decision",
    "create_task_todo",
    "batch_create_task_todos",
    "update_task_todo_status",
    "update_task_todo_content",
    "delete_task_todo",
    "create_task_discovery",
    "update_task_discovery",
    "delete_task_discovery",
    "create_task_issue",
    "update_task_issue",
    "resolve_task_issue",
    "create_task_artifact_other",
    "delete_task_artifact_other",
    "delete_task_artifact",
    "create_task_validation",
    "update_task_validation",
    "create_task_external_state",
    "update_task_external_state",
    "update_task_progress",
    "bind_resources_to_mission",
    "note_update_end",
]


@pytest.fixture(autouse=True)
def init_factory():
    """初始化 LocalToolFactory，确保所有模块被导入。"""
    LocalToolFactory.create()


def test_all_note_tools_registered():
    """验证所有 37 个 Note 工具成功注册至全局工具仓储中。"""
    repo = get_global_repository()
    assert len(ALL_NOTE_TOOL_NAMES) == 37

    for name in ALL_NOTE_TOOL_NAMES:
        assert repo.contains(name), f"工具 {name} 未在仓储中注册"
        tool_def = repo.get_by_name(name)
        assert tool_def.group == "task_note"
        assert tool_def.category == "business"
        assert tool_def.public is True
        assert tool_def.execution_mode == "direct"
        assert tool_def.description
        assert callable(tool_def.fn)


def test_note_tools_schema_generation_without_ctx_leak():
    """验证 Schema 生成器生成工具参数时不会泄漏 ctx 上下文参数。"""
    repo = get_global_repository()
    generator = OpenAIChatCompletionsSchemaGenerator()

    for name in ALL_NOTE_TOOL_NAMES:
        tool_def = repo.get_by_name(name)
        schema = generator.generate(tool_def)

        # 检查结构格式
        assert schema["type"] == "function"
        function_info = schema["function"]
        assert function_info["name"] == name
        assert "description" in function_info

        parameters = function_info.get("parameters", {})
        properties = parameters.get("properties", {})

        # 严禁在给 LLM 的入参 Schema 中暴露上下文注入参数
        assert "ctx" not in properties
        assert "runtime_context" not in properties
        assert "task_id" not in properties
        assert "agent_instance_id" not in properties


def test_note_tools_execution_zero_redundancy():
    """验证工具执行并返回零冗余输出结构（无 output.success 冗余，且入参不机械复述）。"""
    ctx = ToolRuntimeContext(task_id="task-test-01", agent_instance_id="agent-worker-01")

    # 1. create_mission
    res1 = create_mission(CreateMissionRequest(title="重构登录模块"), ctx)
    assert res1.success is True
    assert "mission_id" in res1.output
    assert res1.output["title"] == "重构登录模块"
    assert "success" not in res1.output

    # 2. append_mission_prompt
    res2 = append_mission_prompt(AppendMissionPromptRequest(prompt="用户说增加手机号验证码登录"), ctx)
    assert "mission_id" in res2.output
    assert res2.output["raw_prompts_count"] >= 1

    # 3. update_mission_requirement
    res3 = update_mission_requirement(UpdateMissionRequirementRequest(refined_requirement="## 需求规格书\n1. 支持手机号验证码"), ctx)
    assert "mission_id" in res3.output

    # 4. update_mission_status
    res4 = update_mission_status(UpdateMissionStatusRequest(status="in_progress"), ctx)
    assert "in_progress" in res4.output["message"]

    # 5. create_task_workspace_context
    res5 = create_task_workspace_context(
        CreateTaskWorkspaceContextRequest(
            source_type="file",
            path="src/auth.py",
            line_ranges=[LineRange(start_line=10, end_line=30)],
        ),
        ctx,
    )
    assert "context_id" in res5.output

    # 6. update_task_workspace_context
    res6 = update_task_workspace_context(
        UpdateTaskWorkspaceContextRequest(context_id=res5.output["context_id"], description="修改说明"),
        ctx,
    )
    assert res6.output["context_id"] == res5.output["context_id"]

    # 7. delete_task_workspace_context
    res7 = delete_task_workspace_context(DeleteTaskWorkspaceContextRequest(context_id=res5.output["context_id"]), ctx)
    assert res7.output["deleted_context_id"] == res5.output["context_id"]

    # 8. update_task_solution
    res8 = update_task_solution(UpdateTaskSolutionRequest(solution_content="整体技术架构方案"), ctx)
    assert "solution_id" in res8.output

    # 9. supersede_task_solution
    res9 = supersede_task_solution(SupersedeTaskSolutionRequest(), ctx)
    assert "solution_id" in res9.output

    # 10. create_task_decision
    res10 = create_task_decision(CreateTaskDecisionRequest(decision_content="采用 Redis 缓存 Session", rationale="高并发读需求"), ctx)
    assert "decision_id" in res10.output

    # 11. supersede_task_decision
    res11 = supersede_task_decision(SupersedeTaskDecisionRequest(decision_id=res10.output["decision_id"], superseded_reason="性能瓶颈，重构成 JWT"), ctx)
    assert res11.output["decision_id"] == res10.output["decision_id"]
    assert res11.output["superseded_reason"] == "性能瓶颈，重构成 JWT"

    # 12. update_task_decision
    res12 = update_task_decision(UpdateTaskDecisionRequest(decision_id=res10.output["decision_id"], rationale="修正理由"), ctx)
    assert res12.output["decision_id"] == res10.output["decision_id"]

    # 13. create_task_todo
    res13 = create_task_todo(CreateTaskTodoRequest(todo_content="编写单元测试"), ctx)
    assert "todo_id" in res13.output

    # 14. batch_create_task_todos
    res14 = batch_create_task_todos(
        BatchCreateTaskTodosRequest(todos=[
            TodoItemInput(todo_content="任务1", priority=1),
            TodoItemInput(todo_content="任务2", priority=2),
        ]),
        ctx,
    )
    assert len(res14.output["created_todos"]) == 2

    # 15. update_task_todo_status
    res15 = update_task_todo_status(UpdateTaskTodoStatusRequest(todo_id=res13.output["todo_id"], status="已完成"), ctx)
    assert res15.output["todo_id"] == res13.output["todo_id"]

    # 16. update_task_todo_content
    res16 = update_task_todo_content(UpdateTaskTodoContentRequest(todo_id=res13.output["todo_id"], todo_content="编写集成测试"), ctx)
    assert res16.output["todo_id"] == res13.output["todo_id"]

    # 17. delete_task_todo
    res17 = delete_task_todo(DeleteTaskTodoRequest(todo_id=res13.output["todo_id"]), ctx)
    assert res17.output["deleted_todo_id"] == res13.output["todo_id"]

    # 18. create_task_discovery
    res18 = create_task_discovery(CreateTaskDiscoveryRequest(discovery_content="发现Windows符号链接限制", impact="使用复制代替软链"), ctx)
    assert "discovery_id" in res18.output

    # 19. update_task_discovery
    res19 = update_task_discovery(UpdateTaskDiscoveryRequest(discovery_id=res18.output["discovery_id"], impact="修改影响"), ctx)
    assert res19.output["discovery_id"] == res18.output["discovery_id"]

    # 20. delete_task_discovery
    res20 = delete_task_discovery(DeleteTaskDiscoveryRequest(discovery_id=res18.output["discovery_id"]), ctx)
    assert res20.output["deleted_discovery_id"] == res18.output["discovery_id"]

    # 21. create_task_issue
    res21 = create_task_issue(CreateTaskIssueRequest(issue_content="连接超时", impact="阻塞调试"), ctx)
    assert "issue_id" in res21.output

    # 22. update_task_issue
    res22 = update_task_issue(UpdateTaskIssueRequest(issue_id=res21.output["issue_id"], status="in_progress"), ctx)
    assert res22.output["issue_id"] == res21.output["issue_id"]

    # 23. resolve_task_issue
    res23 = resolve_task_issue(ResolveTaskIssueRequest(issue_id=res21.output["issue_id"], resolution="增大连接池与重试参数"), ctx)
    assert res23.output["issue_id"] == res21.output["issue_id"]

    # 24. create_task_artifact_other
    res24 = create_task_artifact_other(CreateTaskArtifactOtherRequest(content="性能评测报告摘要"), ctx)
    assert "artifact_id" in res24.output

    # 25. delete_task_artifact_other
    res25 = delete_task_artifact_other(DeleteTaskArtifactOtherRequest(artifact_id=res24.output["artifact_id"]), ctx)
    assert res25.output["deleted_artifact_id"] == res24.output["artifact_id"]

    # 26. create_task_validation
    res26 = create_task_validation(
        CreateTaskValidationRequest(
            target_type="requirement",
            validation_method="automated_test",
            validation_scope="登录接口回归测试",
            conclusion="passed",
            validation_content="12个用例全通",
        ),
        ctx,
    )
    assert "validation_id" in res26.output

    # 27. update_task_validation
    res27 = update_task_validation(UpdateTaskValidationRequest(validation_id=res26.output["validation_id"], uncovered_scope="边界压测"), ctx)
    assert res27.output["validation_id"] == res26.output["validation_id"]

    # 28. create_task_external_state
    res28 = create_task_external_state(
        CreateTaskExternalStateRequest(
            target_type="database",
            target_identifier="pg_dev_db",
            change_content="执行了迁移脚本 002",
            current_state="迁移完成",
        ),
        ctx,
    )
    assert "external_state_id" in res28.output

    # 29. update_task_external_state
    res29 = update_task_external_state(UpdateTaskExternalStateRequest(external_state_id=res28.output["external_state_id"], current_state="运行中"), ctx)
    assert res29.output["external_state_id"] == res28.output["external_state_id"]

    # 30. update_task_progress
    res30 = update_task_progress(
        UpdateTaskProgressRequest(
            completed_summary="完成认证重构",
            current_work="准备集成测试",
            next_step="运行 pytest tests/test_auth.py",
        ),
        ctx,
    )
    assert "progress_id" in res30.output

    # 31. bind_resources_to_mission
    res31 = bind_resources_to_mission(
        BindResourcesToMissionRequest(
            resource_type="artifact",
            resource_ids=["art-01", "art-02"],
            mission_id="mission-01",
        ),
        ctx,
    )
    assert res31.output["bound_count"] == 2
    assert res31.output["resource_type"] == "artifact"

    res31_cmd = bind_resources_to_mission(
        BindResourcesToMissionRequest(
            resource_type="command",
            resource_ids=["cmd-01", "cmd-02"],
            mission_id="mission-01",
        ),
        ctx,
    )
    assert res31_cmd.output["bound_count"] == 2
    assert res31_cmd.output["resource_type"] == "command"

    # 33. update_mission_title
    m_id = res1.output["mission_id"]
    res33 = update_mission_title(UpdateMissionTitleRequest(mission_id=m_id, title="重构登录模块（包含手机验证码）"), ctx)
    assert res33.output["title"] == "重构登录模块（包含手机验证码）"

    # 34. update_mission_prompts
    res34 = update_mission_prompts(UpdateMissionPromptsRequest(mission_id=m_id, raw_prompts=["合并后的完整用户需求"]), ctx)
    assert res34.output["prompt_count"] == 1

    # 35. delete_task_artifact
    res35 = delete_task_artifact(DeleteTaskArtifactRequest(artifact_ids=["art-01"]), ctx)
    assert res35.output["deleted_count"] >= 1

    # 36. prune_mission
    res36 = prune_mission(PruneMissionRequest(mission_id=m_id, outcome_summary="已完成手机号验证码登录并全量回归"), ctx)
    assert "outcome_summary" in res36.output

    # 37. delete_mission
    res_temp_mission = create_mission(CreateMissionRequest(title="待删除临时任务"), ctx)
    temp_m_id = res_temp_mission.output["mission_id"]
    res37 = delete_mission(DeleteMissionRequest(mission_id=temp_m_id, reason="测试删除"), ctx)
    assert res37.output["mission_id"] == temp_m_id

    # 38. note_update_end
    res38 = note_update_end(NoteUpdateEndRequest(covered_until_seq=15), ctx)
    assert res38.loop_control.decision == "complete"
    assert "安全退出" in res38.output["message"]
    assert res38.output["covered_until_seq"] == 15
