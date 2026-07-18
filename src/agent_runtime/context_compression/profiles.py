from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from agent_runtime.context_compression.models import SECTION_WORKERS


StageMode = Literal["serial", "parallel"]
WorkerRole = Literal["section_worker", "final_assembler", "final_verifier"]
InputBuilder = Literal["native_history", "native_history_with_upstream_sections"]
AssemblyStrategy = Literal["structured_priority_sections", "ordered_section_concat"]

PROFILE_SECTION_PARALLEL_V1 = "section_parallel_v1"
PROFILE_TASK_RESUME_V2 = "task_resume_v2"
DEFAULT_ACTIVE_PROFILE_ID = PROFILE_SECTION_PARALLEL_V1
NATIVE_INPUT_MODE = "native_multimodal_history"

# 旧 prompt 存储键（input_mode 字段）兼容到 Profile A。
LEGACY_PROMPT_STORAGE_KEY = NATIVE_INPUT_MODE


@dataclass(frozen=True, slots=True)
class CompressionStage:
    stage_id: str
    title: str
    mode: StageMode
    worker_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompressionWorkerDef:
    worker_name: str
    title: str
    description: str
    role: WorkerRole
    output_section: str | None
    stage_id: str
    group: str
    group_label: str
    input_builder: InputBuilder = "native_history"
    upstream_section_ids: tuple[str, ...] = ()
    required: bool = True


@dataclass(frozen=True, slots=True)
class AssemblySpec:
    strategy: AssemblyStrategy
    section_order: tuple[str, ...]
    section_headings: dict[str, str]
    include_resource_reference_appendix: bool = False


@dataclass(frozen=True, slots=True)
class VerifierSpec:
    enabled_default: bool
    worker_name: str | None = None


@dataclass(frozen=True, slots=True)
class CompressionPipelineProfile:
    profile_id: str
    title: str
    description: str
    version: str
    stages: tuple[CompressionStage, ...]
    workers: tuple[CompressionWorkerDef, ...]
    assembly: AssemblySpec
    verifier: VerifierSpec
    input_mode: str = NATIVE_INPUT_MODE

    def worker_by_name(self, worker_name: str) -> CompressionWorkerDef:
        for worker in self.workers:
            if worker.worker_name == worker_name:
                return worker
        raise KeyError(f"profile {self.profile_id} 未知 worker: {worker_name}")

    def section_workers(self) -> tuple[CompressionWorkerDef, ...]:
        return tuple(worker for worker in self.workers if worker.role == "section_worker")

    def to_summary_dict(self, *, is_active: bool = False) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "stage_count": len(self.stages),
            "worker_count": len([w for w in self.workers if w.role == "section_worker"]),
            "is_active": is_active,
            "stage_summary": [
                {
                    "stage_id": stage.stage_id,
                    "title": stage.title,
                    "mode": stage.mode,
                    "worker_count": len(stage.worker_names),
                }
                for stage in self.stages
            ],
        }

    def to_detail_dict(self, *, is_active: bool = False) -> dict[str, Any]:
        payload = self.to_summary_dict(is_active=is_active)
        payload["stages"] = [
            {
                "stage_id": stage.stage_id,
                "title": stage.title,
                "mode": stage.mode,
                "worker_names": list(stage.worker_names),
            }
            for stage in self.stages
        ]
        payload["workers"] = [worker_to_dict(worker) for worker in self.workers]
        payload["assembly"] = {
            "strategy": self.assembly.strategy,
            "section_order": list(self.assembly.section_order),
            "section_headings": dict(self.assembly.section_headings),
            "include_resource_reference_appendix": self.assembly.include_resource_reference_appendix,
        }
        payload["verifier"] = {
            "enabled_default": self.verifier.enabled_default,
            "worker_name": self.verifier.worker_name,
        }
        return payload


def worker_to_dict(worker: CompressionWorkerDef) -> dict[str, Any]:
    return {
        "name": worker.worker_name,
        "worker_name": worker.worker_name,
        "title": worker.title,
        "description": worker.description,
        "role": worker.role,
        "output_section": worker.output_section,
        "stage_id": worker.stage_id,
        "group": worker.group,
        "group_label": worker.group_label,
        "input_builder": worker.input_builder,
        "upstream_section_ids": list(worker.upstream_section_ids),
        "required": worker.required,
    }


def _profile_a() -> CompressionPipelineProfile:
    section_workers = tuple(
        CompressionWorkerDef(
            worker_name=worker_name,
            title=title,
            description=description,
            role="section_worker",
            output_section=section,
            stage_id="section_extract",
            group="section",
            group_label="分段提取",
            input_builder="native_history",
        )
        for (worker_name, section), (title, description) in zip(
            SECTION_WORKERS,
            (
                ("用户需求与硬约束", "从对话历史中提取用户的核心需求、硬性约束、禁止事项以及验收标准。"),
                ("任务计划与执行进度", "汇总当前任务的执行计划、已完成步骤、进行中步骤和后续待办。"),
                ("代码结构与架构事实", "提炼涉及到的代码结构、模块划分、关键类与函数、调用关系等技术事实。"),
                ("工具观察与外部事实", "梳理工具调用结果、文件读取、外部接口返回等观察证据。"),
                ("决策与理由", "汇总关键决策、理由与权衡，方便后续延续相同的判断标准。"),
                ("待办与风险", "列出仍需处理的任务、潜在风险与未解决问题。"),
            ),
        )
    )
    final_workers = (
        CompressionWorkerDef(
            worker_name="FinalAssemblerWorker",
            title="最终汇编（兼容）",
            description="保留旧版 LLM 最终汇编 Worker 的配置入口；当前默认链路由程序确定性生成 Markdown 草稿。",
            role="final_assembler",
            output_section=None,
            stage_id="final_assembly",
            group="final_assembly",
            group_label="最终汇编",
            required=False,
        ),
        CompressionWorkerDef(
            worker_name="FinalVerifierWorker",
            title="最终校验与编辑",
            description="读取原始历史证据和程序化 Markdown 草稿，检查遗漏并通过草稿行编辑工具做小范围修订。",
            role="final_verifier",
            output_section=None,
            stage_id="final_verification",
            group="final_verification",
            group_label="最终校验",
            required=False,
        ),
    )
    section_order = tuple(section for _name, section in SECTION_WORKERS)
    return CompressionPipelineProfile(
        profile_id=PROFILE_SECTION_PARALLEL_V1,
        title="结构化切片并行压缩",
        description="六维结构化并行提取，再按固定章节与优先级渲染最终 Markdown。",
        version="1.0",
        stages=(
            CompressionStage(
                stage_id="section_extract",
                title="分段提取",
                mode="parallel",
                worker_names=tuple(worker.worker_name for worker in section_workers),
            ),
        ),
        workers=section_workers + final_workers,
        assembly=AssemblySpec(
            strategy="structured_priority_sections",
            section_order=section_order,
            section_headings={
                "requirements_constraints": "User Requirements And Hard Constraints",
                "progress_plan": "Execution Plan And Progress",
                "code_architecture": "Codebase And Architecture Facts",
                "observations_evidence": "Observations, Commands, Tool Results",
                "decisions_rationale": "Decisions And Rationale",
                "todos_risks": "Pending Work, Risks, Unknowns, And Conflicts",
            },
            include_resource_reference_appendix=True,
        ),
        verifier=VerifierSpec(enabled_default=True, worker_name="FinalVerifierWorker"),
    )


def _profile_b() -> CompressionPipelineProfile:
    upstream = CompressionWorkerDef(
        worker_name="TaskRequirementsRemainingGoalsWorker",
        title="任务与剩余目标",
        description="提取尚未完成的独立任务、有效需求、规则约束与剩余目标，作为后续 Worker 的权威任务范围。",
        role="section_worker",
        output_section="task_requirements_remaining_goals",
        stage_id="scope_extract",
        group="upstream",
        group_label="前置范围",
        input_builder="native_history",
    )
    dependents = (
        CompressionWorkerDef(
            worker_name="TaskStateExecutionPlanWorker",
            title="任务状态与执行计划",
            description="提取当前有效执行计划、步骤状态与恢复位置。",
            role="section_worker",
            output_section="task_state_execution_plan",
            stage_id="dependent_extract",
            group="section",
            group_label="依赖提取",
            input_builder="native_history_with_upstream_sections",
            upstream_section_ids=("task_requirements_remaining_goals",),
        ),
        CompressionWorkerDef(
            worker_name="SolutionDesignDecisionsWorker",
            title="方案设计与关键决策",
            description="提取当前有效方案、执行策略与关键已定决策。",
            role="section_worker",
            output_section="solution_design_decisions",
            stage_id="dependent_extract",
            group="section",
            group_label="依赖提取",
            input_builder="native_history_with_upstream_sections",
            upstream_section_ids=("task_requirements_remaining_goals",),
        ),
        CompressionWorkerDef(
            worker_name="KeyKnowledgeFindingsWorker",
            title="关键知识与发现",
            description="提取继续完成剩余任务所需的任务事实知识。",
            role="section_worker",
            output_section="key_knowledge_findings",
            stage_id="dependent_extract",
            group="section",
            group_label="依赖提取",
            input_builder="native_history_with_upstream_sections",
            upstream_section_ids=("task_requirements_remaining_goals",),
        ),
        CompressionWorkerDef(
            worker_name="AgentCollabStatefulResourcesWorker",
            title="协作与有状态资源",
            description="提取父子/远程 Agent 协作关系与可复用终端等有状态资源。",
            role="section_worker",
            output_section="agent_collab_stateful_resources",
            stage_id="dependent_extract",
            group="section",
            group_label="依赖提取",
            input_builder="native_history_with_upstream_sections",
            upstream_section_ids=("task_requirements_remaining_goals",),
        ),
        CompressionWorkerDef(
            worker_name="ExistingWorkArtifactsWorker",
            title="已有工作成果与产物",
            description="提取已真实形成、后续可继续使用的内容成果与产物。",
            role="section_worker",
            output_section="existing_work_artifacts",
            stage_id="dependent_extract",
            group="section",
            group_label="依赖提取",
            input_builder="native_history_with_upstream_sections",
            upstream_section_ids=("task_requirements_remaining_goals",),
        ),
    )
    section_order = (
        "task_requirements_remaining_goals",
        "task_state_execution_plan",
        "solution_design_decisions",
        "key_knowledge_findings",
        "agent_collab_stateful_resources",
        "existing_work_artifacts",
    )
    headings = {
        "task_requirements_remaining_goals": "用户任务、要求与剩余目标",
        "task_state_execution_plan": "任务状态与执行计划",
        "solution_design_decisions": "方案设计与关键决策",
        "key_knowledge_findings": "关键知识与发现",
        "agent_collab_stateful_resources": "Agent 协作关系与有状态资源",
        "existing_work_artifacts": "已有工作成果与产物",
    }
    return CompressionPipelineProfile(
        profile_id=PROFILE_TASK_RESUME_V2,
        title="任务续跑职责压缩",
        description="先串行提取任务与剩余目标，再并发提取计划、方案、知识、协作与成果，由系统按固定顺序拼接。",
        version="1.0",
        stages=(
            CompressionStage(
                stage_id="scope_extract",
                title="前置范围",
                mode="serial",
                worker_names=(upstream.worker_name,),
            ),
            CompressionStage(
                stage_id="dependent_extract",
                title="依赖提取",
                mode="parallel",
                worker_names=tuple(worker.worker_name for worker in dependents),
            ),
        ),
        workers=(upstream, *dependents),
        assembly=AssemblySpec(
            strategy="ordered_section_concat",
            section_order=section_order,
            section_headings=headings,
            include_resource_reference_appendix=False,
        ),
        verifier=VerifierSpec(enabled_default=False, worker_name=None),
    )


_PROFILES: dict[str, CompressionPipelineProfile] = {
    PROFILE_SECTION_PARALLEL_V1: _profile_a(),
    PROFILE_TASK_RESUME_V2: _profile_b(),
}


def list_profiles() -> tuple[CompressionPipelineProfile, ...]:
    return tuple(_PROFILES[profile_id] for profile_id in (PROFILE_SECTION_PARALLEL_V1, PROFILE_TASK_RESUME_V2))


def get_profile(profile_id: str | None) -> CompressionPipelineProfile:
    resolved = normalize_profile_id(profile_id)
    try:
        return _PROFILES[resolved]
    except KeyError as exc:
        raise KeyError(f"未知上下文压缩 profile: {profile_id}") from exc


def normalize_profile_id(profile_id: str | None) -> str:
    if profile_id is None:
        return DEFAULT_ACTIVE_PROFILE_ID
    text = str(profile_id).strip()
    if not text:
        return DEFAULT_ACTIVE_PROFILE_ID
    if text not in _PROFILES:
        raise ValueError(f"active_profile_id 必须是已注册的内置 profile: {', '.join(_PROFILES)}")
    return text


def prompt_storage_keys_for_profile(profile_id: str) -> tuple[str, ...]:
    """返回查找 prompt override 时按优先级尝试的存储键。"""
    if profile_id == PROFILE_SECTION_PARALLEL_V1:
        return (PROFILE_SECTION_PARALLEL_V1, LEGACY_PROMPT_STORAGE_KEY)
    return (profile_id,)


def all_section_titles() -> dict[str, str]:
    titles: dict[str, str] = {}
    for profile in list_profiles():
        for worker in profile.section_workers():
            if worker.output_section:
                titles[worker.output_section] = worker.title
        titles.update(profile.assembly.section_headings)
    return titles


def all_worker_names() -> frozenset[str]:
    names: set[str] = set()
    for profile in list_profiles():
        for worker in profile.workers:
            names.add(worker.worker_name)
    return frozenset(names)

