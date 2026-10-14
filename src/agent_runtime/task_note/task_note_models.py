# -*- coding: utf-8 -*-
"""Agent Task Note 维护工具统一 Pydantic 请求模型。

定义 32 个 Note 维护工具的入参契约。
严格遵循 BaseToolRequest 设计规范：
1. 仅承载 LLM 可见的业务入参与字段 Prompt 描述；
2. 不包含 task_id、agent_instance_id 等系统调度元数据（由 ToolRuntimeContext 隐式注入）；
3. 严格定义字段类型、校验边界与枚举。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.tool.tool_execution_models import BaseToolRequest

# ── 基础枚举与公用复合结构 ──────────────────────────────────────────

MissionStatus = Literal["pending", "in_progress", "completed", "abandoned"]
ContextSourceType = Literal["file", "directory", "web_search", "url_context", "terminal", "other"]
DecisionStatus = Literal["active", "superseded"]
TodoStatus = Literal["未开始", "执行中", "已完成"]
IssueStatus = Literal["open", "in_progress", "blocked", "resolved", "wont_fix"]
ValidationTargetType = Literal["requirement", "todo", "artifact", "external_state", "other"]
ValidationMethod = Literal["automated_test", "build", "static_check", "manual_check", "user_acceptance"]
ValidationConclusion = Literal["passed", "failed", "partial", "not_completed"]
ExternalStateTargetType = Literal["environment", "service", "database", "external_api", "process", "other"]
ExternalStateReversibility = Literal["reversible", "irreversible", "unknown"]
BindResourceType = Literal["artifact", "agent_relationship"]


class LineRange(BaseModel):
    """代码或文本行号区间。"""

    model_config = ConfigDict(extra="forbid")

    start_line: int = Field(ge=1, description="1-based 起始行号；必须大于等于 1。")
    end_line: int = Field(ge=1, description="1-based 结束行号；必须大于等于 start_line。")


class TodoItemInput(BaseModel):
    """批量创建待办时的单个待办项输入。"""

    model_config = ConfigDict(extra="forbid")

    todo_content: str = Field(min_length=1, description="计划执行的具体待办工作内容。")
    priority: int = Field(default=1, ge=1, le=100, description="优先级序号；越小越优先，默认为 1。")


# ── 1. 业务任务与需求维护工具模型 (agent_user_mission) ─────────────

class CreateMissionRequest(BaseToolRequest):
    """创建新的独立业务任务单元请求。"""

    title: str = Field(min_length=1, description="任务简明标题，清晰表达业务目标。")
    initial_prompt: str | None = Field(default=None, description="可选的用户初始发言或委派指令（支持在保持核心含义与需求不变的前提下优化措辞，使需求描述更清晰明确）。")
    refined_requirement: str | None = Field(default=None, description="经思考提炼后的正式需求规格书、架构规范与验收标准。")
    sort_order: int = Field(default=1, ge=1, description="当前会期内的任务顺序号，默认为 1。")


class AppendMissionPromptRequest(BaseToolRequest):
    """向任务增量追加用户发言或委派指令请求。"""

    mission_id: str = Field(min_length=1, description="目标任务 ID。")
    prompt: str = Field(min_length=1, description="新增的用户发言或委派指令（支持在保持核心含义与需求不变的前提下优化措辞，使需求描述更清晰明确）。")


class UpdateMissionRequirementRequest(BaseToolRequest):
    """全量覆盖更新任务正式需求规格书请求。"""

    mission_id: str = Field(min_length=1, description="目标任务 ID。")
    refined_requirement: str = Field(min_length=1, description="经提炼后的最新正式需求规格、架构硬约束与验收标准全量文本。")


class UpdateMissionStatusRequest(BaseToolRequest):
    """推进任务生命周期状态请求。"""

    mission_id: str = Field(min_length=1, description="目标任务 ID。")
    status: MissionStatus = Field(description="目标任务状态；可选值仅限 pending、in_progress、completed、abandoned。")


class UpdateMissionPromptsRequest(BaseToolRequest):
    """整组覆盖替换任务原始诉求列表请求（支持在保持核心含义与需求不变的前提下优化措辞）。"""

    mission_id: str = Field(min_length=1, description="目标任务 ID。")
    raw_prompts: list[str] = Field(min_length=1, description="优化措辞后的完整用户原始诉求字符串数组。")


class UpdateMissionTitleRequest(BaseToolRequest):
    """更新业务任务简明标题请求。"""

    mission_id: str = Field(min_length=1, description="目标任务 ID。")
    title: str = Field(min_length=1, description="更新后的任务简明标题。")


class PruneMissionRequest(BaseToolRequest):
    """闭环任务卡片化裁剪请求（已完成或已废弃任务沉淀为极简结果卡片）。"""

    mission_id: str = Field(min_length=1, description="目标任务 ID。")
    outcome_summary: str = Field(min_length=1, description="核心交付结果摘要或废弃原因复盘（不超过200字）。")
    keep_record_ids: list[str] | None = Field(default=None, description="可选白名单：保留的关键产物/决策/发现 ID 数组，其余级联删除。")


class DeleteMissionRequest(BaseToolRequest):
    """物理删除误创或无效的业务任务请求。"""

    mission_id: str = Field(min_length=1, description="目标任务 ID。")
    reason: str = Field(min_length=1, description="删除该任务的业务理由。")
    reassign_to_mission_id: str | None = Field(default=None, description="可选的目标任务 ID；若指定，则将该任务下挂载的记录安全转移，否则直接级联物理删除。")


# ── 2. 任务背景素材维护工具模型 (task_workspace_context) ──────────

class CreateTaskWorkspaceContextRequest(BaseToolRequest):
    """增量创建任务背景素材记录请求。"""

    mission_id: str = Field(min_length=1, description="归属任务 ID。")
    source_type: ContextSourceType = Field(description="素材来源类型；可选值仅限 file、directory、web_search、url_context、terminal、other。")
    description: str | None = Field(default=None, description="说明该素材为何与当前任务强相关。")
    path: str | None = Field(default=None, description="文件或目录的绝对路径（source_type 为 file/directory 时使用）。")
    line_ranges: list[LineRange] = Field(default_factory=list, description="核心行区间列表，如 [{'start_line': 10, 'end_line': 35}]。")
    directory_structure: str | None = Field(default=None, description="提炼的关键目录树文本。")
    search_content: str | None = Field(default=None, description="实际网络搜索词。")
    core_result_content: str | None = Field(default=None, description="提炼出的核心搜索事实。")
    reference_links: list[str] = Field(default_factory=list, description="参考 URL 数组。")
    url_list: list[str] = Field(default_factory=list, description="目标 URL 数组。")
    core_content: str | None = Field(default=None, description="网页提取的核心内容。")
    command_id: str | None = Field(default=None, description="关联终端命令执行记录 ID。")
    important_content: str | None = Field(default=None, description="命令行输出提炼出的核心重要结论。")
    content: str | None = Field(default=None, description="通用文本素材内容。")


class UpdateTaskWorkspaceContextRequest(BaseToolRequest):
    """根据 context_id 覆盖更新背景素材请求。"""

    context_id: str = Field(min_length=1, description="待更新背景素材的目标主键 ID。")
    description: str | None = Field(default=None, description="说明该素材为何与当前任务强相关。")
    path: str | None = Field(default=None, description="文件或目录的绝对路径。")
    line_ranges: list[LineRange] | None = Field(default=None, description="核心行区间列表。")
    directory_structure: str | None = Field(default=None, description="提炼的关键目录树文本。")
    search_content: str | None = Field(default=None, description="实际网络搜索词。")
    core_result_content: str | None = Field(default=None, description="提炼出的核心搜索事实。")
    reference_links: list[str] | None = Field(default=None, description="参考 URL 数组。")
    url_list: list[str] | None = Field(default=None, description="目标 URL 数组。")
    core_content: str | None = Field(default=None, description="网页提取的核心内容。")
    important_content: str | None = Field(default=None, description="命令行输出提炼出的核心重要结论。")
    content: str | None = Field(default=None, description="通用文本素材内容。")


class DeleteTaskWorkspaceContextRequest(BaseToolRequest):
    """物理删除失效背景素材请求。"""

    context_id: str = Field(min_length=1, description="待删除素材的目标主键 ID。")


# ── 3. 当前宏观方案维护工具模型 (task_solution_content) ───────────

class UpdateTaskSolutionRequest(BaseToolRequest):
    """全量覆盖当前工人宏观方案快照请求。"""

    mission_id: str = Field(min_length=1, description="归属任务 ID。")
    solution_content: str = Field(min_length=1, description="实施思路、技术架构与主要步骤全量文本。")


class SupersedeTaskSolutionRequest(BaseToolRequest):
    """标记软废弃当前工人宏观方案快照请求。"""

    mission_id: str = Field(min_length=1, description="归属任务 ID。")


# ── 4. 关键决策记录维护工具模型 (task_decision_records) ────────────

class CreateTaskDecisionRequest(BaseToolRequest):
    """增量记录技术决策与架构取舍请求。"""

    mission_id: str = Field(min_length=1, description="归属任务 ID。")
    decision_content: str = Field(min_length=1, description="采纳的技术选型、架构取舍或锁定决定。")
    rationale: str = Field(min_length=1, description="采纳该选择的核心技术理由与收益。")
    alternatives: str | None = Field(default=None, description="权衡并被放弃的备选方案说明。")
    impact: str | None = Field(default=None, description="该决策对后续开发或系统行为的硬约束。")


class SupersedeTaskDecisionRequest(BaseToolRequest):
    """软废弃已有决策请求（留痕防倒退，禁物理删除）。"""

    decision_id: str = Field(min_length=1, description="待废弃决策的目标主键 ID。")
    superseded_reason: str | None = Field(default=None, description="被废弃或被新决策替代的业务理由。")


class UpdateTaskDecisionRequest(BaseToolRequest):
    """修正决策描述或补充约束细节请求。"""

    decision_id: str = Field(min_length=1, description="待修改决策的目标主键 ID。")
    decision_content: str | None = Field(default=None, description="修正后的决策内容。")
    rationale: str | None = Field(default=None, description="修正后的决策理由。")
    alternatives: str | None = Field(default=None, description="修正后的备选方案。")
    impact: str | None = Field(default=None, description="修正后的后续约束影响。")


# ── 5. 计划工作项维护工具模型 (task_todo_items) ───────────────────

class CreateTaskTodoRequest(BaseToolRequest):
    """增量创建单条待办工作项请求。"""

    mission_id: str = Field(min_length=1, description="归属任务 ID。")
    todo_content: str = Field(min_length=1, description="计划执行的工作内容。")
    priority: int = Field(default=1, ge=1, le=100, description="优先级序号；越小越优先，默认为 1。")


class BatchCreateTaskTodosRequest(BaseToolRequest):
    """批量创建多条待办工作项请求。"""

    mission_id: str = Field(min_length=1, description="归属任务 ID。")
    todos: list[TodoItemInput] = Field(min_length=1, max_length=50, description="待创建的工作项列表，每项包含 todo_content 和 priority。")


class UpdateTaskTodoStatusRequest(BaseToolRequest):
    """高频极轻量推进待办生命周期状态请求。"""

    todo_id: str = Field(min_length=1, description="目标待办项主键 ID。")
    status: TodoStatus = Field(description="目标状态；可选值仅限 未开始、执行中、已完成。")


class UpdateTaskTodoContentRequest(BaseToolRequest):
    """修改待办内容描述或调整优先级请求。"""

    todo_id: str = Field(min_length=1, description="目标待办项主键 ID。")
    todo_content: str | None = Field(default=None, description="修改后的工作内容。")
    priority: int | None = Field(default=None, ge=1, le=100, description="调整后的优先级序号。")


class DeleteTaskTodoRequest(BaseToolRequest):
    """物理删除不再执行的待办项请求。"""

    todo_id: str = Field(min_length=1, description="待删除的目标待办项主键 ID。")


# ── 6. 客观认知发现维护工具模型 (task_discovery_records) ─────────

class CreateTaskDiscoveryRequest(BaseToolRequest):
    """增量沉淀客观认知真相与避坑经验请求。"""

    mission_id: str = Field(min_length=1, description="归属任务 ID。")
    discovery_content: str = Field(min_length=1, description="探索中证实的新认知事实、系统限制、失败原因或反模式。")
    impact: str = Field(min_length=1, description="该发现对后续方案设计与代码实现的影响。")
    source_type: str | None = Field(default=None, description="证据来源类型（如 file, terminal, web_search 等）。")
    source_path: str | None = Field(default=None, description="证据文件绝对路径。")
    line_ranges: list[LineRange] = Field(default_factory=list, description="证据所在行区间列表。")
    command_id: str | None = Field(default=None, description="关联命令执行记录 ID。")
    reference_url: str | None = Field(default=None, description="外部参考文档或 issue 链接。")


class UpdateTaskDiscoveryRequest(BaseToolRequest):
    """修正已有认知或补充关联凭据请求。"""

    discovery_id: str = Field(min_length=1, description="目标认知记录主键 ID。")
    discovery_content: str | None = Field(default=None, description="修正后的认知内容。")
    impact: str | None = Field(default=None, description="修正后的影响说明。")
    source_type: str | None = Field(default=None, description="修正后的证据类型。")
    source_path: str | None = Field(default=None, description="修正后的证据文件绝对路径。")
    line_ranges: list[LineRange] | None = Field(default=None, description="修正后的证据行区间。")
    command_id: str | None = Field(default=None, description="关联命令执行记录 ID。")
    reference_url: str | None = Field(default=None, description="外部参考链接。")


class DeleteTaskDiscoveryRequest(BaseToolRequest):
    """物理删除证实为假阳性的认知噪音请求。"""

    discovery_id: str = Field(min_length=1, description="待删除的认知主键 ID。")


# ── 7. 问题与阻塞跟踪维护工具模型 (task_issue_records) ───────────

class CreateTaskIssueRequest(BaseToolRequest):
    """增量登记故障报错、未闭环阻塞事项请求。"""

    mission_id: str = Field(min_length=1, description="归属任务 ID。")
    issue_content: str = Field(min_length=1, description="未闭环的具体故障报错、依赖缺失或阻碍事项。")
    impact: str = Field(min_length=1, description="该问题造成的影响。")
    status: Literal["open", "in_progress", "blocked"] = Field(default="open", description="初始问题状态，默认为 open。")
    next_action: str | None = Field(default=None, description="下一步推进排查动作。")
    closure_condition: str | None = Field(default=None, description="问题关闭或解决的验收条件。")
    source_type: str | None = Field(default=None, description="证据来源类型。")
    command_id: str | None = Field(default=None, description="关联报错命令 ID。")


class UpdateTaskIssueRequest(BaseToolRequest):
    """推进排查进展或更新问题状态请求。"""

    issue_id: str = Field(min_length=1, description="目标问题记录主键 ID。")
    status: Literal["open", "in_progress", "blocked", "wont_fix"] | None = Field(default=None, description="推进状态。")
    next_action: str | None = Field(default=None, description="更新后的下一步行动。")
    closure_condition: str | None = Field(default=None, description="更新后的关闭条件。")
    impact: str | None = Field(default=None, description="更新后的影响说明。")


class ResolveTaskIssueRequest(BaseToolRequest):
    """核心闭环动作：解决问题并沉淀避坑经验请求（严禁物理删除）。"""

    issue_id: str = Field(min_length=1, description="已解决的目标问题主键 ID。")
    resolution: str = Field(min_length=1, description="解决方案与根本原因复盘（强约束必填，用于长效避坑）。")


# ── 8. 交付产物维护工具模型 (task_artifact_records) ───────────────

class CreateTaskArtifactOtherRequest(BaseToolRequest):
    """登记非文件类通用交付成果与计算结论请求。"""

    mission_id: str = Field(min_length=1, description="归属任务 ID。")
    content: str = Field(min_length=1, description="通用交付成果内容、计算报告摘要或数据引用。")


class DeleteTaskArtifactRequest(BaseToolRequest):
    """物理批量删除失效的产物记录请求。"""

    artifact_ids: list[str] = Field(min_length=1, description="待删除的产物记录 ID 数组。")


class DeleteTaskArtifactOtherRequest(BaseToolRequest):
    """物理删除失效的非文件通用交付成果请求（兼容老接口）。"""

    artifact_id: str = Field(min_length=1, description="目标通用产物主键 ID（严格仅限 type='other'，底层杜绝删除任何 file_* 文件物理产物）。")


# ── 9. 验证与检查结论维护工具模型 (task_validation_records) ───────

class CreateTaskValidationRequest(BaseToolRequest):
    """增量记录测试、构建、静态扫描或人工验收结论请求。"""

    mission_id: str = Field(min_length=1, description="归属任务 ID。")
    target_type: ValidationTargetType = Field(description="验证目标类型；可选值仅限 requirement、todo、artifact、external_state、other。")
    target_record_id: str | None = Field(default=None, description="关联的具体目标记录 ID（如 todo_id、artifact_id）。")
    validation_method: ValidationMethod = Field(description="验证方式；可选值仅限 automated_test、build、static_check、manual_check、user_acceptance。")
    validation_scope: str = Field(min_length=1, description="验证覆盖的功能点或范围。")
    conclusion: ValidationConclusion = Field(description="验证结论；可选值仅限 passed、failed、partial、not_completed。")
    validation_content: str = Field(min_length=1, description="结论详细说明与关键异常错误摘要。")
    uncovered_scope: str | None = Field(default=None, description="未覆盖的范围说明。")
    source_type: str | None = Field(default=None, description="证据来源类型（如 terminal, file, manual）。")
    command_id: str | None = Field(default=None, description="关联执行测试或构建的命令记录 ID。")
    source_path: str | None = Field(default=None, description="测试文件或报告绝对路径。")
    line_ranges: list[LineRange] = Field(default_factory=list, description="测试用例所在行区间。")


class UpdateTaskValidationRequest(BaseToolRequest):
    """微调验证说明或补充异常细节请求。"""

    validation_id: str = Field(min_length=1, description="目标验证记录主键 ID。")
    validation_content: str | None = Field(default=None, description="更新后的结论详细说明与关键异常摘要。")
    conclusion: ValidationConclusion | None = Field(default=None, description="更新后的结论。")
    uncovered_scope: str | None = Field(default=None, description="更新后的未覆盖范围说明。")


# ── 10. 外部持久状态维护工具模型 (task_external_state_records) ────

class CreateTaskExternalStateRequest(BaseToolRequest):
    """登记对外部持久化环境真实改变请求。"""

    mission_id: str = Field(min_length=1, description="归属任务 ID。")
    target_type: ExternalStateTargetType = Field(description="外部目标类型；可选值仅限 environment、service、database、external_api、process、other。")
    target_identifier: str = Field(min_length=1, description="目标标识（如数据库名、容器名、服务名）。")
    change_content: str = Field(min_length=1, description="发生的具体持久改变内容。")
    current_state: str = Field(min_length=1, description="变更后的当前状态。")
    reversibility: ExternalStateReversibility = Field(default="reversible", description="可逆性；可选值仅限 reversible、irreversible、unknown。")
    rollback_info: str | None = Field(default=None, description="明确的回滚方案与指引。")
    command_id: str | None = Field(default=None, description="关联变更命令 ID。")


class UpdateTaskExternalStateRequest(BaseToolRequest):
    """更新外部环境最新状态或补充回滚指引请求。"""

    external_state_id: str = Field(min_length=1, description="目标外部状态记录主键 ID。")
    current_state: str | None = Field(default=None, description="更新后的当前状态。")
    rollback_info: str | None = Field(default=None, description="更新后的回滚指引。")
    reversibility: ExternalStateReversibility | None = Field(default=None, description="更新后的可逆性。")


# ── 11. 任务恢复快照维护工具模型 (task_progress_content) ──────────

class UpdateTaskProgressRequest(BaseToolRequest):
    """覆盖生成唯一恢复快照与即刻下一步（PC 指针）请求。"""

    mission_id: str = Field(min_length=1, description="归属任务 ID。")
    completed_summary: str = Field(min_length=1, description="有证据支持的已完成工作高密度总结。")
    current_work: str = Field(min_length=1, description="当前正在微观处理的具体动作/断点指针。")
    next_step: str = Field(min_length=1, description="唤醒后执行的第一个具体动作，必须明确拟调用的工具与关键入参。")
    current_blockers: str | None = Field(default=None, description="当前阻塞的 Issue 引用或阻碍摘要。")
    recent_validation: str | None = Field(default=None, description="最近的重要验证结论。")
    handoff_notes: str | None = Field(default=None, description="交接风险与避坑提示。")


# ── 12. 物理资源延迟归属与状态生命周期工具模型 ───────────────────────

class BindResourcesToMissionRequest(BaseToolRequest):
    """批量延迟绑定底层物理事实到业务任务请求。"""

    resource_type: BindResourceType = Field(
        description=(
            "待绑定的底层资源类型；可选值仅限：\n"
            "• artifact: 文件产出记录（对应 task_artifact_records 中文件工具自动生成的文件项）\n"
            "• command: 关键终端执行记录（对应 task_command_records）\n"
            "• agent_relationship: 子 Agent 协作拓扑关系（对应 task_agent_relationships）"
        )
    )
    resource_ids: list[str] = Field(min_length=1, description="待绑定的目标资源 ID 数组（如 artifact_ids, command_ids, relationship_ids）。")
    mission_id: str = Field(min_length=1, description="目标业务任务 ID。")


class NoteUpdateEndRequest(BaseToolRequest):
    """完成当前批次 Note 观察并推进检查点请求。"""

    covered_until_seq: int = Field(
        default=0,
        ge=0,
        description="当前观察批次覆盖到的最后一个合法断点 seq (含)；Runtime 将校验该 seq 的有效性并原子推进断点。",
    )


# ── 13. 系统状态与断点数据模型 ───────────────────────────────────────

class TaskNoteWatermark(BaseModel):
    """业务 Agent 观察断点与快照状态实体。"""

    model_config = ConfigDict(extra="ignore")

    agent_instance_id: str = Field(description="被观察的业务 Agent 实例 ID。")
    task_id: str = Field(description="所属 Task ID。")
    covered_until_seq: int = Field(default=0, description="快照覆盖到的合法断点 seq (含)。")
    covered_until_message_id: str | None = Field(default=None, description="该断点对应的业务 Agent 消息 ID。")
    note_version: int = Field(default=0, description="快照版本号，每次成功观察递增。")
    inherited_processed: bool = Field(default=False, description="子 Agent 继承段是否已在首次观察中处理。")
    updated_at: str | None = Field(default=None, description="最后更新时间戳。")


class TaskNoteUnit(BaseModel):
    """业务消息合法断点原子切片单元。"""

    model_config = ConfigDict(extra="ignore")

    seq: int = Field(description="全局断点序号，单调递增。")
    unit_type: str = Field(description="单元类型: user_turn | tool_interaction | direct_response | snapshot | system。")
    messages: list[Any] = Field(default_factory=list, description="该单元包含的连续消息列表。")
    token_count: int = Field(default=0, description="该单元预估 Token 数量。")
    end_message_id: str = Field(description="该单元末尾消息的主键 ID。")
    is_snapshot: bool = Field(default=False, description="是否为前缀替换产生的手记快照（不计入增量统计）。")


class SliceResult(BaseModel):
    """切片器切片与增量分析结果。"""

    model_config = ConfigDict(extra="ignore")

    all_units: list[TaskNoteUnit] = Field(default_factory=list, description="完整 Unit 列表。")
    inherited_units: list[TaskNoteUnit] = Field(default_factory=list, description="子 Agent 首次观察继承的历史 Unit 列表。")
    incremental_units: list[TaskNoteUnit] = Field(default_factory=list, description="水位之后的增量 Unit 列表。")
    incremental_tokens: int = Field(default=0, description="增量 Token 总数（排除快照手记）。")
    should_trigger: bool = Field(default=False, description="是否达到观察触发阈值。")
    max_seq: int = Field(default=0, description="本次可覆盖的最大合法断点 seq。")
    watermark_covered_until_seq: int = Field(default=0, description="切片计算时采用的当前水位 seq。")


TaskNoteUnit.model_rebuild()
SliceResult.model_rebuild()


class ObservingRun(BaseModel):
    """当前正在后台运行的观察者会话元数据。"""

    model_config = ConfigDict(extra="ignore")

    run_id: str = Field(description="观察运行全局唯一标识。")
    start_after_message_id: str | None = Field(default=None, description="本次增量切片起点的消息 ID。")
    end_message_id: str = Field(description="本次增量切片终点的消息 ID。")
    started_at: str = Field(description="观察启动时间戳 (ISO 8601)。")


class TaskNoteTracking(BaseModel):
    """会话级 Task Note 增量跟踪状态唯一权威实体 (ConversationState.task_note_tracking)。"""

    model_config = ConfigDict(extra="ignore")

    version: int = Field(default=0, description="乐观锁版本号，每次 CAS 写入 +1。")
    prefix_end_message_id: str | None = Field(default=None, description="前缀边界消息 ID (子 Agent 边界或快照消息 ID)。")
    snapshot_message_id: str | None = Field(default=None, description="当前快照消息 ID。")
    covered_until_message_id: str | None = Field(default=None, description="上一次观察实际覆盖到的最后一条消息 ID。")
    note_version: int = Field(default=0, description="成功观察的次数。")
    inherited_processed: bool = Field(default=True, description="子 Agent 继承段是否已处理；根 Agent 恒为 True。")
    observing: ObservingRun | None = Field(default=None, description="当前运行中的观察；无运行中观察时为 None。")

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, *, is_subagent: bool = False) -> TaskNoteTracking:
        """从字典还原 TaskNoteTracking，d 为空时返回根据角色初始化的默认值。"""
        if not d:
            return cls(
                version=0,
                prefix_end_message_id=None,
                snapshot_message_id=None,
                covered_until_message_id=None,
                note_version=0,
                inherited_processed=not is_subagent,
                observing=None,
            )

        obs_val = d.get("observing")
        obs_obj: ObservingRun | None = None
        if isinstance(obs_val, dict):
            obs_obj = ObservingRun(**obs_val)
        elif isinstance(obs_val, ObservingRun):
            obs_obj = obs_val

        return cls(
            version=int(d.get("version", 0)),
            prefix_end_message_id=d.get("prefix_end_message_id"),
            snapshot_message_id=d.get("snapshot_message_id"),
            covered_until_message_id=d.get("covered_until_message_id"),
            note_version=int(d.get("note_version", 0)),
            inherited_processed=bool(d.get("inherited_processed", not is_subagent)),
            observing=obs_obj,
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为存储字典。"""
        obs_dict: dict[str, Any] | None = None
        if self.observing is not None:
            obs_dict = {
                "run_id": self.observing.run_id,
                "start_after_message_id": self.observing.start_after_message_id,
                "end_message_id": self.observing.end_message_id,
                "started_at": self.observing.started_at,
            }

        return {
            "version": self.version,
            "prefix_end_message_id": self.prefix_end_message_id,
            "snapshot_message_id": self.snapshot_message_id,
            "covered_until_message_id": self.covered_until_message_id,
            "note_version": self.note_version,
            "inherited_processed": self.inherited_processed,
            "observing": obs_dict,
        }


ObservingRun.model_rebuild()
TaskNoteTracking.model_rebuild()


