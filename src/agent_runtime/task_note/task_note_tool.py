# -*- coding: utf-8 -*-
"""Agent Task Note 32 个专属维护工具定义。

实现 Note 更新 Agent 对 11 张表的增删改查、生命周期推进、延迟绑定与退出控制。
严格遵循项目 LocalTool 规范：
1. @tool 装饰器声明元数据（group="task_note", category="business"）；
2. 入参通过 BaseToolRequest Pydantic 模型传递（生成标准 JSON Schema）；
3. 运行环境通过 ToolRuntimeContext 隐式注入（在 Schema 中剔除）；
4. 出参遵循零冗余原则，封装在 BaseToolResult(output={...}) 中。
5. 数据操作真实持久化至 PostgreSQL 数据表（通过 TaskNoteStore）。
"""
from __future__ import annotations

import logging
from typing import Any

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
    NoteUpdateEndRequest,
    ResolveTaskIssueRequest,
    SupersedeTaskDecisionRequest,
    SupersedeTaskSolutionRequest,
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
from agent_runtime.task_note.task_note_store import get_task_note_store
from agent_runtime.tool.tool_decorator import tool
from agent_runtime.tool.tool_execution_models import (
    BaseToolResult,
    LoopControl,
    ToolRuntimeContext,
)

logger = logging.getLogger("agent_runtime.task_note.tool")


def _resolve_mission_id(mission_id: str | None, ctx: ToolRuntimeContext) -> str:
    """解析目标 mission_id；未显式传递时使用当前上下文默认任务。"""
    if mission_id and mission_id.strip():
        return mission_id.strip()
    return f"mission-default-{ctx.task_id or 'main'}"


def _normalize_line_ranges(line_ranges: Any) -> list[dict[str, Any]] | None:
    """将 LineRange 列表或混合行区间归一化为纯 dict 列表。"""
    if line_ranges is None:
        return None
    return [
        r.model_dump(mode="json") if hasattr(r, "model_dump") and callable(r.model_dump) else r
        for r in line_ranges
    ]


# ── 1. 业务任务与需求维护工具 (agent_user_mission) ───────────────────

@tool(
    name="create_mission",
    description="在当前工作会期（task_id）内创建新的独立业务任务单元（任务间平铺无嵌套）。系统自动生成 mission_id 并返回。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### create_mission\n在班次内创建业务任务单元，保存任务标题、用户/上级委派指令（支持在保持原意前提下优化措辞使描述更明确）与提炼后的正式需求规格书。",
)
def create_mission(request: CreateMissionRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    scene_id = getattr(ctx, "scene_id", None) or f"scene-{task_id}"
    try:
        mission_id = store.create_mission(
            task_id=task_id,
            scene_id=scene_id,
            agent_instance_id=agent_instance_id,
            title=request.title,
            raw_prompt=request.initial_prompt,
            refined_requirement=request.refined_requirement,
            status="in_progress",
            sort_order=request.sort_order,
        )
        output = {
            "mission_id": mission_id,
            "title": request.title,
            "status": "in_progress",
            "message": "业务任务单元创建成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("create_mission 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"创建业务任务单元失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="append_mission_prompt",
    description="向目标业务任务追加用户最新发言文本或委派指令。支持在保持原始含义和需求不变的前提下优化措辞，使需求描述更清晰明确。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### append_mission_prompt\n增量追加用户发言或委派指令（支持优化措辞使需求更清晰，保持核心含义与需求不变），不修改已提炼的需求规格。",
)
def append_mission_prompt(request: AppendMissionPromptRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.append_mission_prompt(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            mission_id=target_mission_id,
            prompt=request.prompt,
        )
        output = {
            "mission_id": target_mission_id,
            "raw_prompts_count": 1,
            "message": "用户发言/委派指令已追加",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("append_mission_prompt 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"追加用户发言失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="update_mission_requirement",
    description="全量覆盖更新经 LLM 思考提炼后的正式需求规格书（功能边界、架构硬约束、验收标准）。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### update_mission_requirement\n更新需求规格与技术规范，新共识全量覆盖旧共识。",
)
def update_mission_requirement(request: UpdateMissionRequirementRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.update_mission_requirement(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            mission_id=target_mission_id,
            refined_requirement=request.refined_requirement,
        )
        output = {
            "mission_id": target_mission_id,
            "message": "任务需求规格已覆盖更新",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("update_mission_requirement 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"更新需求规格失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="update_mission_status",
    description="推进业务任务生命周期状态；可选值: pending, in_progress, completed, abandoned。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### update_mission_status\n按 ID 推进任务的生命周期状态。",
)
def update_mission_status(request: UpdateMissionStatusRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.update_mission_status(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            mission_id=target_mission_id,
            status=request.status,
        )
        output = {
            "mission_id": target_mission_id,
            "status": request.status,
            "message": f"任务状态已推进为 {request.status}",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("update_mission_status 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"推进任务状态失败: {exc}",
            output={"error": str(exc)},
        )


# ── 2. 任务背景素材维护工具 (task_workspace_context) ────────────────

@tool(
    name="create_task_workspace_context",
    description="新增一条重要背景素材记录（文件、目录、搜索、URL、终端输出摘要等）。主键 context_id 由系统自动生成返回。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### create_task_workspace_context\n登记与当前任务强相关的背景素材，支持行区间与搜索结论提炼。",
)
def create_task_workspace_context(request: CreateTaskWorkspaceContextRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        context_id = store.create_workspace_context(
            task_id=task_id,
            mission_id=target_mission_id,
            agent_instance_id=agent_instance_id,
            source_type=request.source_type,
            description=request.description,
            path=request.path,
            line_ranges=_normalize_line_ranges(request.line_ranges),
            directory_structure=request.directory_structure,
            search_content=request.search_content,
            core_result_content=request.core_result_content,
            reference_links=request.reference_links,
            url_list=request.url_list,
            core_content=request.core_content,
            command_id=request.command_id,
            important_content=request.important_content,
            content=request.content,
        )
        output = {
            "context_id": context_id,
            "description": request.description or request.path or "背景素材",
            "message": "背景素材记录创建成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("create_task_workspace_context 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"创建背景素材失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="update_task_workspace_context",
    description="根据 context_id 覆盖更新已有背景素材的内容、说明或修正核心行区间。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### update_task_workspace_context\n按 context_id 精准修正背景素材。",
)
def update_task_workspace_context(request: UpdateTaskWorkspaceContextRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.update_workspace_context(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            context_id=request.context_id,
            description=request.description,
            path=request.path,
            line_ranges=_normalize_line_ranges(request.line_ranges),
            directory_structure=request.directory_structure,
            search_content=request.search_content,
            core_result_content=request.core_result_content,
            reference_links=request.reference_links,
            url_list=request.url_list,
            core_content=request.core_content,
            important_content=request.important_content,
            content=request.content,
        )
        output = {
            "context_id": request.context_id,
            "message": "背景素材更新成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("update_task_workspace_context 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"更新背景素材失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="delete_task_workspace_context",
    description="根据 context_id 物理移除失效或证实无关的背景素材，保持 Working Memory 极简。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### delete_task_workspace_context\n物理删除失效素材，防止工作记忆膨胀。",
)
def delete_task_workspace_context(request: DeleteTaskWorkspaceContextRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.delete_workspace_context(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            context_id=request.context_id,
        )
        output = {
            "deleted_context_id": request.context_id,
            "message": "背景素材删除成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("delete_task_workspace_context 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"删除背景素材失败: {exc}",
            output={"error": str(exc)},
        )


# ── 3. 当前宏观方案维护工具 (task_solution_content) ─────────────────

@tool(
    name="update_task_solution",
    description="全量覆盖更新当前工人在当前任务下的宏观技术方案快照（基于任务与工人唯一快照覆盖，无需传入主键 ID）。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### update_task_solution\n全量覆盖更新宏观技术架构方案，保持连贯思维。",
)
def update_task_solution(request: UpdateTaskSolutionRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        solution_id = store.upsert_solution(
            task_id=task_id,
            mission_id=target_mission_id,
            agent_instance_id=agent_instance_id,
            solution_content=request.solution_content,
        )
        output = {
            "solution_id": solution_id,
            "mission_id": target_mission_id,
            "message": "当前宏观方案快照覆盖更新成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("update_task_solution 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"更新宏观方案失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="supersede_task_solution",
    description="标记软废弃当前工人的宏观技术方案（将 solution_status 置为 superseded，留痕备查）。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### supersede_task_solution\n废弃宏观方案，留痕不物理删除。",
)
def supersede_task_solution(request: SupersedeTaskSolutionRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        solution_id = store.supersede_solution(
            task_id=task_id,
            mission_id=target_mission_id,
            agent_instance_id=agent_instance_id,
        )
        output = {
            "solution_id": solution_id or f"sol-{target_mission_id}",
            "mission_id": target_mission_id,
            "message": "当前方案已标记为软废弃留痕",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("supersede_task_solution 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"废弃宏观方案失败: {exc}",
            output={"error": str(exc)},
        )


# ── 4. 关键决策记录维护工具 (task_decision_records) ──────────────────

@tool(
    name="create_task_decision",
    description="增量记录一项锁定的技术选型、架构取舍或规范决策。主键 decision_id 由系统自动生成返回。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### create_task_decision\n沉淀技术决策与被放弃的替代方案，防反复摇摆。",
)
def create_task_decision(request: CreateTaskDecisionRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        decision_id = store.create_decision(
            task_id=task_id,
            mission_id=target_mission_id,
            agent_instance_id=agent_instance_id,
            decision_content=request.decision_content,
            rationale=request.rationale,
            alternatives=request.alternatives,
            impact=request.impact,
            decision_status="active",
        )
        output = {
            "decision_id": decision_id,
            "decision_content": request.decision_content,
            "message": "关键决策记录创建成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("create_task_decision 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"创建决策记录失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="supersede_task_decision",
    description="软废弃已有决策（严禁物理删除，状态置为 superseded，保留长效留痕防倒退）。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### supersede_task_decision\n软废弃决策留痕，必须指定 decision_id。",
)
def supersede_task_decision(request: SupersedeTaskDecisionRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.supersede_decision(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            decision_id=request.decision_id,
        )
        output = {
            "decision_id": request.decision_id,
            "message": "决策已标记为软废弃留痕",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("supersede_task_decision 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"废弃决策失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="update_task_decision",
    description="修正决策描述、补充采纳理由或微调后续约束影响。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### update_task_decision\n根据 decision_id 修正决策细节。",
)
def update_task_decision(request: UpdateTaskDecisionRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.update_decision(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            decision_id=request.decision_id,
            decision_content=request.decision_content,
            rationale=request.rationale,
            alternatives=request.alternatives,
            impact=request.impact,
        )
        output = {
            "decision_id": request.decision_id,
            "message": "决策内容更新成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("update_task_decision 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"更新决策失败: {exc}",
            output={"error": str(exc)},
        )


# ── 5. 计划工作项维护工具 (task_todo_items) ─────────────────────────

@tool(
    name="create_task_todo",
    description="增量创建单条计划待办工作项。主键 todo_id 由系统自动生成返回。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### create_task_todo\n创建单条工作待办，支持设置优先级。",
)
def create_task_todo(request: CreateTaskTodoRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        todo_id = store.create_todo(
            task_id=task_id,
            mission_id=target_mission_id,
            agent_instance_id=agent_instance_id,
            todo_content=request.todo_content,
            priority=request.priority,
            status="未开始",
        )
        output = {
            "todo_id": todo_id,
            "todo_content": request.todo_content,
            "status": "未开始",
            "message": "待办工作项创建成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("create_task_todo 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"创建待办失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="batch_create_task_todos",
    description="批量创建多条计划待办工作项。主键 todo_ids 由系统批量生成返回。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### batch_create_task_todos\n一次性批量创建多条待办清单，返回生成的 todo_id 数组。",
)
def batch_create_task_todos(request: BatchCreateTaskTodosRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        todos_payload = [
            {
                "todo_content": item.todo_content,
                "priority": item.priority,
                "status": "未开始",
            }
            for item in request.todos
        ]
        created = store.batch_create_todos(
            task_id=task_id,
            mission_id=target_mission_id,
            agent_instance_id=agent_instance_id,
            todos=todos_payload,
        )
        output = {
            "created_todos": created,
            "message": f"批量创建 {len(created)} 条待办成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("batch_create_task_todos 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"批量创建待办失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="update_task_todo_status",
    description="高频极轻量推进单个待办的状态；可选值仅限 未开始、执行中、已完成。极省 Token，仅传 todo_id 和 status。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### update_task_todo_status\n单字段推进待办状态，避免重传完整待办内容。",
)
def update_task_todo_status(request: UpdateTaskTodoStatusRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.update_todo_status(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            todo_id=request.todo_id,
            status=request.status,
        )
        output = {
            "todo_id": request.todo_id,
            "status": request.status,
            "message": "待办状态推进成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("update_task_todo_status 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"推进待办状态失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="update_task_todo_content",
    description="修改待办内容描述或调整其优先级顺序。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### update_task_todo_content\n修改待办内容或优先级。",
)
def update_task_todo_content(request: UpdateTaskTodoContentRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.update_todo_content(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            todo_id=request.todo_id,
            todo_content=request.todo_content,
            priority=request.priority,
        )
        output = {
            "todo_id": request.todo_id,
            "message": "待办内容/优先级修改成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("update_task_todo_content 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"修改待办内容失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="delete_task_todo",
    description="物理删除被放弃或不再执行的待办项。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### delete_task_todo\n物理删除无效待办项。",
)
def delete_task_todo(request: DeleteTaskTodoRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.delete_todo(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            todo_id=request.todo_id,
        )
        output = {
            "deleted_todo_id": request.todo_id,
            "message": "待办项删除成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("delete_task_todo 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"删除待办失败: {exc}",
            output={"error": str(exc)},
        )


# ── 6. 客观认知发现维护工具 (task_discovery_records) ───────────────

@tool(
    name="create_task_discovery",
    description="增量沉淀探索中证实的新认知事实、系统限制、失败原因或避坑反模式。主键 discovery_id 由系统自动生成返回。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### create_task_discovery\n沉淀客观认知与避坑事实，附带证据来源与代码行区间。",
)
def create_task_discovery(request: CreateTaskDiscoveryRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        discovery_id = store.create_discovery(
            task_id=task_id,
            mission_id=target_mission_id,
            agent_instance_id=agent_instance_id,
            discovery_content=request.discovery_content,
            impact=request.impact,
            source_type=request.source_type,
            source_path=request.source_path,
            line_ranges=_normalize_line_ranges(request.line_ranges),
            command_id=request.command_id,
            reference_url=request.reference_url,
        )
        output = {
            "discovery_id": discovery_id,
            "discovery_content": request.discovery_content,
            "message": "客观认知记录创建成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("create_task_discovery 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"创建认知记录失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="update_task_discovery",
    description="修正已有认知描述或补充关联证据凭据句柄。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### update_task_discovery\n修正已有认知或补充证据。",
)
def update_task_discovery(request: UpdateTaskDiscoveryRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.update_discovery(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            discovery_id=request.discovery_id,
            discovery_content=request.discovery_content,
            impact=request.impact,
            source_type=request.source_type,
            source_path=request.source_path,
            line_ranges=_normalize_line_ranges(request.line_ranges),
            command_id=request.command_id,
            reference_url=request.reference_url,
        )
        output = {
            "discovery_id": request.discovery_id,
            "message": "客观认知更新成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("update_task_discovery 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"更新认知记录失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="delete_task_discovery",
    description="物理删除被后续事实彻底证伪为假阳性（False Positive）的错误探索噪音。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### delete_task_discovery\n删除被证伪的认知噪音。",
)
def delete_task_discovery(request: DeleteTaskDiscoveryRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.delete_discovery(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            discovery_id=request.discovery_id,
        )
        output = {
            "deleted_discovery_id": request.discovery_id,
            "message": "认知记录删除成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("delete_task_discovery 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"删除认知记录失败: {exc}",
            output={"error": str(exc)},
        )


# ── 7. 问题与阻塞跟踪维护工具 (task_issue_records) ─────────────────

@tool(
    name="create_task_issue",
    description="增量登记遇到的故障报错、依赖缺失或未闭环阻塞事项。主键 issue_id 由系统自动生成返回。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### create_task_issue\n登记阻塞与异常问题，必须描述阻碍与后续行动。",
)
def create_task_issue(request: CreateTaskIssueRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        issue_id = store.create_issue(
            task_id=task_id,
            mission_id=target_mission_id,
            agent_instance_id=agent_instance_id,
            issue_content=request.issue_content,
            impact=request.impact,
            status=request.status,
            next_action=request.next_action,
            closure_condition=request.closure_condition,
            resolution=None,
            source_type=request.source_type,
            command_id=request.command_id,
        )
        output = {
            "issue_id": issue_id,
            "issue_content": request.issue_content,
            "status": request.status,
            "message": "问题与阻塞记录创建成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("create_task_issue 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"创建问题记录失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="update_task_issue",
    description="更新问题影响范围、修正后续行动方案或变更推进状态。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### update_task_issue\n更新问题的排查进展或闭环条件。",
)
def update_task_issue(request: UpdateTaskIssueRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.update_issue(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            issue_id=request.issue_id,
            impact=request.impact,
            status=request.status,
            next_action=request.next_action,
            closure_condition=request.closure_condition,
        )
        output = {
            "issue_id": request.issue_id,
            "message": "问题记录更新成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("update_task_issue 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"更新问题记录失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="resolve_task_issue",
    description="闭环标记解决问题（status 置为 resolved，必须填写 resolution 沉淀避坑方案）。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### resolve_task_issue\n闭环解决问题，沉淀有效解决方案。",
)
def resolve_task_issue(request: ResolveTaskIssueRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.resolve_issue(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            issue_id=request.issue_id,
            resolution=request.resolution,
        )
        output = {
            "issue_id": request.issue_id,
            "status": "resolved",
            "message": "问题已标记为闭环解决",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("resolve_task_issue 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"解决问题记录失败: {exc}",
            output={"error": str(exc)},
        )


# ── 8. 通用成果交付产物维护工具 (task_artifact_records) ─────────────

@tool(
    name="create_task_artifact_other",
    description="登记非代码文件类的通用业务交付产物（设计规范文档、架构草案、业务数据报告等）。主键 artifact_id 由系统自动生成返回。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### create_task_artifact_other\n登记通用成果产物，仅限非代码文件类。",
)
def create_task_artifact_other(request: CreateTaskArtifactOtherRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        artifact_id = store.create_artifact_other(
            task_id=task_id,
            mission_id=target_mission_id,
            agent_instance_id=agent_instance_id,
            artifact_type="other",
            content=request.content,
        )
        output = {
            "artifact_id": artifact_id,
            "type": "other",
            "message": "通用交付产物登记成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("create_task_artifact_other 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"登记通用产物失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="delete_task_artifact_other",
    description="物理删除非代码文件的通用交付产物（严格仅限 type='other'，底层绝对防止删除文件物理产物）。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### delete_task_artifact_other\n物理删除无效通用成果，仅限 type='other'，底层绝对保护 file_* 文件物理产物。",
)
def delete_task_artifact_other(request: DeleteTaskArtifactOtherRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.delete_artifact_other(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            artifact_id=request.artifact_id,
        )
        output = {
            "deleted_artifact_id": request.artifact_id,
            "message": "通用交付物删除成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("delete_task_artifact_other 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"删除通用产物失败: {exc}",
            output={"error": str(exc)},
        )


# ── 9. 测试与验证结论维护工具 (task_validation_records) ─────────────

@tool(
    name="create_task_validation",
    description="增量登记一项测试、构建、自测或人工验收的确定性结论。主键 validation_id 由系统自动生成返回。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### create_task_validation\n沉淀验证结论与测试证据，包含通过/失败与未覆盖范围。",
)
def create_task_validation(request: CreateTaskValidationRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        validation_id = store.create_validation(
            task_id=task_id,
            mission_id=target_mission_id,
            agent_instance_id=agent_instance_id,
            target_type=request.target_type,
            target_record_id=request.target_record_id,
            validation_method=request.validation_method,
            validation_scope=request.validation_scope,
            conclusion=request.conclusion,
            uncovered_scope=request.uncovered_scope,
            validation_content=request.validation_content,
            source_type=request.source_type or "manual",
            command_id=request.command_id,
            source_path=request.source_path,
            line_ranges=_normalize_line_ranges(request.line_ranges),
        )
        output = {
            "validation_id": validation_id,
            "conclusion": request.conclusion,
            "message": "验证结论记录创建成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("create_task_validation 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"创建验证结论失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="update_task_validation",
    description="修正已有验证结论，补充未覆盖范围或修正验证描述。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### update_task_validation\n根据 validation_id 精准修正验证结论。",
)
def update_task_validation(request: UpdateTaskValidationRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.update_validation(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            validation_id=request.validation_id,
            conclusion=request.conclusion,
            uncovered_scope=request.uncovered_scope,
            validation_content=request.validation_content,
        )
        output = {
            "validation_id": request.validation_id,
            "message": "验证结论更新成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("update_task_validation 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"更新验证结论失败: {exc}",
            output={"error": str(exc)},
        )


# ── 10. 外部持久状态变更工具 (task_external_state_records) ─────────

@tool(
    name="create_task_external_state",
    description="登记不可逆或关键的环境状态变更（Docker 容器、数据库、远程主机端口、系统配置等），提供精确回滚方案。主键 external_state_id 由系统自动生成返回。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### create_task_external_state\n记录外部环境变更与回滚命令，确保系统安全可逆。",
)
def create_task_external_state(request: CreateTaskExternalStateRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        external_state_id = store.create_external_state(
            task_id=task_id,
            mission_id=target_mission_id,
            agent_instance_id=agent_instance_id,
            target_type=request.target_type,
            target_identifier=request.target_identifier,
            change_content=request.change_content,
            current_state=request.current_state,
            reversibility=request.reversibility,
            rollback_info=request.rollback_info,
            command_id=request.command_id,
        )
        output = {
            "external_state_id": external_state_id,
            "message": "外部状态变更记录创建成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("create_task_external_state 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"创建外部状态记录失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="update_task_external_state",
    description="更新外部环境的当前状态或补充完善回滚方案。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### update_task_external_state\n更新外部状态或回滚方案。",
)
def update_task_external_state(request: UpdateTaskExternalStateRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        store.update_external_state(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            external_state_id=request.external_state_id,
            current_state=request.current_state,
            reversibility=request.reversibility,
            rollback_info=request.rollback_info,
        )
        output = {
            "external_state_id": request.external_state_id,
            "message": "外部状态更新成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("update_task_external_state 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"更新外部状态记录失败: {exc}",
            output={"error": str(exc)},
        )


# ── 11. 任务恢复快照维护工具 (task_progress_content) ────────────────

@tool(
    name="update_task_progress",
    description="必须在每次 Note 更新的最后一步调用，全量覆盖生成当前唯一的任务恢复快照与即刻下一步（PC 指针）。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### update_task_progress\nNote 更新最后一步必调，重写唯一步骤断点与唤醒后的第一个动作。",
)
def update_task_progress(request: UpdateTaskProgressRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        progress_id = store.upsert_progress(
            task_id=task_id,
            mission_id=target_mission_id,
            agent_instance_id=agent_instance_id,
            completed_summary=request.completed_summary,
            current_work=request.current_work,
            current_blockers=request.current_blockers,
            recent_validation=request.recent_validation,
            next_step=request.next_step,
            handoff_notes=request.handoff_notes,
        )
        output = {
            "progress_id": progress_id,
            "mission_id": target_mission_id,
            "next_step": request.next_step,
            "message": "唯一恢复快照与即刻动量生成成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("update_task_progress 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"更新任务恢复快照失败: {exc}",
            output={"error": str(exc)},
        )


# ── 12. 物理资源延迟归属与状态生命周期工具 ─────────────────────────

@tool(
    name="bind_resources_to_mission",
    description="将底层程序自动记录、且 mission_id 为空的物理事实记录（文件产物、终端命令、子 Agent 协作拓扑）批量延迟绑定到指定的业务任务。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### bind_resources_to_mission\n按资源类型批量关联底层物理事实到业务任务，专线通信随关系绑定自动级联。",
)
def bind_resources_to_mission(request: BindResourcesToMissionRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    store = get_task_note_store()
    task_id = ctx.task_id or "task-default"
    agent_instance_id = ctx.agent_instance_id or "inst-default"
    try:
        bound_count = store.bind_resources_to_mission(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            mission_id=request.mission_id,
            resource_type=request.resource_type,
            resource_ids=request.resource_ids,
        )
        output = {
            "mission_id": request.mission_id,
            "resource_type": request.resource_type,
            "bound_count": bound_count,
            "message": "底层物理资源延迟归属绑定成功",
        }
        return BaseToolResult(output=output)
    except Exception as exc:
        logger.error("bind_resources_to_mission 失败: %s", exc)
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DatabaseError",
            error_message=f"绑定物理资源失败: {exc}",
            output={"error": str(exc)},
        )


@tool(
    name="note_update_end",
    description="Note 更新流程全部完成后调用，通知系统所有 Note 主题更新完毕，安全退出 Note 更新状态，切换回业务 Agent 运行态。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    allowed_loop_decisions=("continue", "complete"),
    wiki="### note_update_end\n结束 Note 更新流程，向系统发出完成信号并切回业务运行态。",
)
def note_update_end(request: NoteUpdateEndRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    output = {
        "message": "Note 更新流程已结束，系统安全退出 Note 状态并切回业务运行态",
    }
    return BaseToolResult(
        output=output,
        loop_control=LoopControl(decision="complete", reason="Note 更新流程完成"),
    )
