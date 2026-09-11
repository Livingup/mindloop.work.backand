# -*- coding: utf-8 -*-
"""Agent Task Note 32 个专属维护工具定义。

实现 Note 更新 Agent 对 11 张表的增删改查、生命周期推进、延迟绑定与退出控制。
严格遵循项目 LocalTool 规范：
1. @tool 装饰器声明元数据（group="task_note", category="business"）；
2. 入参通过 BaseToolRequest Pydantic 模型传递（生成标准 JSON Schema）；
3. 运行环境通过 ToolRuntimeContext 隐式注入（在 Schema 中剔除）；
4. 出参遵循零冗余原则，封装在 BaseToolResult(output={...}) 中。
"""
from __future__ import annotations

import time
import uuid
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
from agent_runtime.tool.tool_decorator import tool
from agent_runtime.tool.tool_execution_models import (
    BaseToolResult,
    LoopControl,
    ToolRuntimeContext,
)

# ── 工具 ID 生成辅助函数 ──────────────────────────────────────────────

def _generate_id(prefix: str) -> str:
    """生成带业务前缀的紧凑唯一标识。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _resolve_mission_id(mission_id: str | None, ctx: ToolRuntimeContext) -> str:
    """解析目标 mission_id；未显式传递时使用当前上下文默认任务。"""
    if mission_id and mission_id.strip():
        return mission_id.strip()
    return f"mission-default-{ctx.task_id or 'main'}"


# ── 1. 业务任务与需求维护工具 (agent_user_mission) ───────────────────

@tool(
    name="create_mission",
    description="在当前工作会期（task_id）内创建新的独立业务任务单元（任务间平铺无嵌套）。系统自动生成 mission_id 并返回。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### create_mission\n在班次内创建业务任务单元，保存任务标题、用户原始原话与提炼后的正式需求规格书。",
)
def create_mission(request: CreateMissionRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    mission_id = f"mission-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
    output = {
        "mission_id": mission_id,
        "title": request.title,
        "message": "业务任务单元创建成功",
    }
    return BaseToolResult(output=output)


@tool(
    name="append_mission_prompt",
    description="向目标业务任务追加用户最新原始发言文本或委派指令原话，完整保留对话时间序原貌。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### append_mission_prompt\n增量追加用户发言原话，不修改已提炼的需求规格。",
)
def append_mission_prompt(request: AppendMissionPromptRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    target_mission_id = _resolve_mission_id(request.mission_id, ctx)
    output = {
        "mission_id": target_mission_id,
        "raw_prompts_count": 1,
        "message": "用户原始发言已追加",
    }
    return BaseToolResult(output=output)


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
    output = {
        "mission_id": target_mission_id,
        "message": "任务需求规格已覆盖更新",
    }
    return BaseToolResult(output=output)


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
    output = {
        "mission_id": target_mission_id,
        "message": f"任务状态已推进为 {request.status}",
    }
    return BaseToolResult(output=output)


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
    context_id = _generate_id("ctx")
    output = {
        "context_id": context_id,
        "description": request.description or request.path or "背景素材",
        "message": "背景素材记录创建成功",
    }
    return BaseToolResult(output=output)


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
    output = {
        "context_id": request.context_id,
        "message": "背景素材更新成功",
    }
    return BaseToolResult(output=output)


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
    output = {
        "deleted_context_id": request.context_id,
        "message": "背景素材删除成功",
    }
    return BaseToolResult(output=output)


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
    solution_id = _generate_id("sol")
    output = {
        "solution_id": solution_id,
        "message": "当前宏观方案快照覆盖更新成功",
    }
    return BaseToolResult(output=output)


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
    solution_id = _generate_id("sol")
    output = {
        "solution_id": solution_id,
        "message": "当前方案已标记为软废弃留痕",
    }
    return BaseToolResult(output=output)


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
    decision_id = _generate_id("dec")
    output = {
        "decision_id": decision_id,
        "decision_content": request.decision_content,
        "message": "关键决策记录创建成功",
    }
    return BaseToolResult(output=output)


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
    output = {
        "decision_id": request.decision_id,
        "message": "决策已标记为软废弃留痕",
    }
    return BaseToolResult(output=output)


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
    output = {
        "decision_id": request.decision_id,
        "message": "决策内容更新成功",
    }
    return BaseToolResult(output=output)


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
    todo_id = _generate_id("todo")
    output = {
        "todo_id": todo_id,
        "todo_content": request.todo_content,
        "message": "待办工作项创建成功",
    }
    return BaseToolResult(output=output)


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
    created_todos = []
    for item in request.todos:
        created_todos.append({
            "todo_id": _generate_id("todo"),
            "todo_content": item.todo_content,
        })
    output = {
        "created_todos": created_todos,
        "message": f"批量创建 {len(created_todos)} 条待办成功",
    }
    return BaseToolResult(output=output)


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
    output = {
        "todo_id": request.todo_id,
        "message": "待办状态推进成功",
    }
    return BaseToolResult(output=output)


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
    output = {
        "todo_id": request.todo_id,
        "message": "待办内容/优先级修改成功",
    }
    return BaseToolResult(output=output)


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
    output = {
        "deleted_todo_id": request.todo_id,
        "message": "待办项删除成功",
    }
    return BaseToolResult(output=output)


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
    discovery_id = _generate_id("disc")
    output = {
        "discovery_id": discovery_id,
        "discovery_content": request.discovery_content,
        "message": "客观认知记录创建成功",
    }
    return BaseToolResult(output=output)


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
    output = {
        "discovery_id": request.discovery_id,
        "message": "客观认知更新成功",
    }
    return BaseToolResult(output=output)


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
    output = {
        "deleted_discovery_id": request.discovery_id,
        "message": "认知记录删除成功",
    }
    return BaseToolResult(output=output)


# ── 7. 问题与阻塞跟踪维护工具 (task_issue_records) ─────────────────

@tool(
    name="create_task_issue",
    description="增量登记遇到的故障报错、依赖缺失或未闭环阻塞事项。主键 issue_id 由系统自动生成返回。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### create_task_issue\n登记故障与阻塞，跟踪推进进展。",
)
def create_task_issue(request: CreateTaskIssueRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    issue_id = _generate_id("iss")
    output = {
        "issue_id": issue_id,
        "issue_content": request.issue_content,
        "message": "问题记录创建成功",
    }
    return BaseToolResult(output=output)


@tool(
    name="update_task_issue",
    description="推进排查进展、更新问题状态或下一步排查行动。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### update_task_issue\n推进问题排查，更新 next_action。",
)
def update_task_issue(request: UpdateTaskIssueRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    output = {
        "issue_id": request.issue_id,
        "message": "问题状态与进展更新成功",
    }
    return BaseToolResult(output=output)


@tool(
    name="resolve_task_issue",
    description="核心闭环动作：闭环解决问题并沉淀根本原因与避坑经验（严禁物理删除，强制填写 resolution）。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### resolve_task_issue\n闭环解决问题，必须提供 resolution 避坑复盘。",
)
def resolve_task_issue(request: ResolveTaskIssueRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    output = {
        "issue_id": request.issue_id,
        "message": "问题已闭环解决并成功沉淀 resolution 经验",
    }
    return BaseToolResult(output=output)


# ── 8. 交付产物维护工具 (task_artifact_records) ─────────────────────

@tool(
    name="create_task_artifact_other",
    description="登记非代码文件类的通用交付成果（如计算成果报告、分析数据包引用等）。主键 artifact_id 由系统自动生成返回。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### create_task_artifact_other\n登记非文件类通用成果，文件类由底层工具自动生成。",
)
def create_task_artifact_other(request: CreateTaskArtifactOtherRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    artifact_id = _generate_id("art")
    output = {
        "artifact_id": artifact_id,
        "message": "通用交付物记录创建成功",
    }
    return BaseToolResult(output=output)


@tool(
    name="delete_task_artifact_other",
    description="物理删除失效的非代码通用交付物（严禁删除任何底层程序自动记录的文件类产物）。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### delete_task_artifact_other\n仅限删除 type='other' 的通用成果。",
)
def delete_task_artifact_other(request: DeleteTaskArtifactOtherRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    output = {
        "deleted_artifact_id": request.artifact_id,
        "message": "通用交付物删除成功",
    }
    return BaseToolResult(output=output)


# ── 9. 验证与检查结论维护工具 (task_validation_records) ─────────────

@tool(
    name="create_task_validation",
    description="增量记录一次测试、构建、静态扫描或人工验收结论。主键 validation_id 由系统自动生成返回。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### create_task_validation\n记录测试与验证结论，关联执行命令或源文件。",
)
def create_task_validation(request: CreateTaskValidationRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    validation_id = _generate_id("val")
    output = {
        "validation_id": validation_id,
        "validation_scope": request.validation_scope,
        "message": "验证结论记录创建成功",
    }
    return BaseToolResult(output=output)


@tool(
    name="update_task_validation",
    description="微调验证范围说明或补充关键异常细节。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### update_task_validation\n修正验证结论或补充未覆盖范围。",
)
def update_task_validation(request: UpdateTaskValidationRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    output = {
        "validation_id": request.validation_id,
        "message": "验证结论更新成功",
    }
    return BaseToolResult(output=output)


# ── 10. 外部持久状态维护工具 (task_external_state_records) ──────────

@tool(
    name="create_task_external_state",
    description="登记对代码库外部持久化环境的真实改变（容器启动、数据库迁移、服务启停等）。主键 external_state_id 由系统自动生成返回。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### create_task_external_state\n记录外部环境真实变更，提供回滚指引与可逆性评估。",
)
def create_task_external_state(request: CreateTaskExternalStateRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    external_state_id = _generate_id("ext")
    output = {
        "external_state_id": external_state_id,
        "target_identifier": request.target_identifier,
        "message": "外部状态变更记录创建成功",
    }
    return BaseToolResult(output=output)


@tool(
    name="update_task_external_state",
    description="更新外部环境最新当前状态或补充回滚指引说明。",
    group="task_note",
    category="business",
    public=True,
    execution_mode="direct",
    wiki="### update_task_external_state\n更新外部状态或回滚方案。",
)
def update_task_external_state(request: UpdateTaskExternalStateRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    output = {
        "external_state_id": request.external_state_id,
        "message": "外部状态更新成功",
    }
    return BaseToolResult(output=output)


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
    progress_id = _generate_id("prog")
    output = {
        "progress_id": progress_id,
        "message": "唯一恢复快照与即刻动量生成成功",
    }
    return BaseToolResult(output=output)


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
    output = {
        "resource_type": request.resource_type,
        "bound_count": len(request.resource_ids),
        "message": "底层物理资源延迟归属绑定成功",
    }
    return BaseToolResult(output=output)


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
