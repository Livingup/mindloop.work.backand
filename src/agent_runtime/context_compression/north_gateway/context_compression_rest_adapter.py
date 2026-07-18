from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from agent_runtime.context_compression.archive_repository import ContextCompressionArchiveRepository
from agent_runtime.context_compression.config import ContextCompressionConfig
from agent_runtime.context_compression.models import SECTION_WORKERS, SECTION_WORKER_TITLES
from agent_runtime.context_compression.profiles import (
    DEFAULT_ACTIVE_PROFILE_ID,
    get_profile,
    list_profiles,
    normalize_profile_id,
    prompt_storage_keys_for_profile,
    worker_to_dict,
)
from agent_runtime.context_compression.default_prompts import (
    CONTEXT_COMPRESSION_WORKER_NAMES,
    get_default_worker_prompt,
)
from agent_runtime.context_compression.prompt_repository import (
    ContextCompressionWorkerPrompt,
    ContextCompressionWorkerPromptRepository,
)
from agent_runtime.context_compression.settings_repository import (
    ContextCompressionSettingsRepository,
)
from agent_runtime.conversation_message.conversation_repository import ConversationRepository
from agent_runtime.conversation_message.message.models import TextPart, UniMessage
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmModelRegistryGateway,
)


# Worker 元数据由代码内置，与设计文档 15.10.2 保持一致。
SUPPORTED_INPUT_MODES: tuple[str, ...] = ("native_multimodal_history",)
NATIVE_INPUT_MODE = "native_multimodal_history"


# Worker 分组：UI 列表按这个分组顺序展示。
WORKER_GROUP_SECTION = "section"
WORKER_GROUP_FINAL_ASSEMBLY = "final_assembly"
WORKER_GROUP_FINAL_VERIFICATION = "final_verification"

_WORKER_GROUP_LABELS: dict[str, str] = {
    WORKER_GROUP_SECTION: "分段汇编",
    WORKER_GROUP_FINAL_ASSEMBLY: "最终汇编",
    WORKER_GROUP_FINAL_VERIFICATION: "最终校验",
}

_WORKER_METADATA: tuple[dict[str, str], ...] = (
    {
        "name": "RequirementConstraintWorker",
        "title": "用户需求与硬约束",
        "description": "从对话历史中提取用户的核心需求、硬性约束、禁止事项以及验收标准，避免后续步骤遗漏关键限制。",
        "group": WORKER_GROUP_SECTION,
    },
    {
        "name": "ProgressPlanWorker",
        "title": "任务计划与执行进度",
        "description": "汇总当前任务的执行计划、已完成步骤、进行中步骤和后续待办，保留可恢复的工作流上下文。",
        "group": WORKER_GROUP_SECTION,
    },
    {
        "name": "CodeArchitectureWorker",
        "title": "代码结构与架构事实",
        "description": "提炼涉及到的代码结构、模块划分、关键类与函数、调用关系等技术事实，便于后续编码继续。",
        "group": WORKER_GROUP_SECTION,
    },
    {
        "name": "ObservationEvidenceWorker",
        "title": "工具观察与外部事实",
        "description": "梳理工具调用结果、文件读取、外部接口返回等观察证据，并保留可被 Agent 再次访问的资源定位信息。",
        "group": WORKER_GROUP_SECTION,
    },
    {
        "name": "DecisionRationaleWorker",
        "title": "决策与理由",
        "description": "汇总在对话过程中做出的关键决策、理由与权衡，方便后续延续相同的判断标准。",
        "group": WORKER_GROUP_SECTION,
    },
    {
        "name": "TodoRiskWorker",
        "title": "待办与风险",
        "description": "列出仍需处理的任务、潜在风险与未解决问题，确保压缩后 Agent 仍知道下一步该做什么、应该回避什么。",
        "group": WORKER_GROUP_SECTION,
    },
    {
        "name": "FinalAssemblerWorker",
        "title": "最终汇编（兼容）",
        "description": "保留旧版 LLM 最终汇编 Worker 的配置入口；当前默认链路由程序按固定章节确定性生成 Markdown 草稿。",
        "group": WORKER_GROUP_FINAL_ASSEMBLY,
    },
    {
        "name": "FinalVerifierWorker",
        "title": "最终校验与编辑",
        "description": "读取原始历史证据和程序化 Markdown 草稿，检查遗漏、错误或上下文断裂，并通过草稿行编辑工具做小范围最终修订。",
        "group": WORKER_GROUP_FINAL_VERIFICATION,
    },
)

_SECTION_TO_WORKER_NAME: dict[str, str] = {
    section: worker_name
    for worker_name, section in SECTION_WORKERS
}


class ContextCompressionMarkdownResponse(BaseModel):
    conversation_id: str
    message_index: int
    markdown: str
    text_format: str = "markdown"
    matched_reference: str
    summary_message_id: str | None = None
    archive_job_id: str | None = None


class ContextCompressionWorkerReadableContentResponse(BaseModel):
    worker_name: str
    title: str
    content_markdown: str


class ContextCompressionWorkerContentsResponse(BaseModel):
    job_id: str
    workers: list[ContextCompressionWorkerReadableContentResponse]


class ContextCompressionPromptStatusResponse(BaseModel):
    use_default: bool
    updated_at: str | None = None


class ContextCompressionWorkerResponse(BaseModel):
    name: str
    title: str
    description: str
    group: str
    group_label: str
    stage_id: str | None = None
    stage_mode: str | None = None
    input_builder: str | None = None
    output_section: str | None = None
    profile_id: str | None = None
    prompt_status: dict[str, ContextCompressionPromptStatusResponse]


class ContextCompressionSettingsResponse(BaseModel):
    enabled: bool
    input_mode: str
    active_profile_id: str
    llm_policy_group_id: str | None
    llm_policy_group_status: str  # ok | unset | missing | disabled | no_candidates
    llm_policy_group_status_message: str | None = None


class ContextCompressionSettingsUpdateBody(BaseModel):
    llm_policy_group_id: str | None = None
    active_profile_id: str | None = None
    input_mode: str | None = None


class ContextCompressionProfileSummaryResponse(BaseModel):
    profile_id: str
    title: str
    description: str
    version: str
    stage_count: int
    worker_count: int
    is_active: bool
    stage_summary: list[dict]


class ContextCompressionProfileDetailResponse(BaseModel):
    profile_id: str
    title: str
    description: str
    version: str
    stage_count: int
    worker_count: int
    is_active: bool
    stage_summary: list[dict]
    stages: list[dict]
    workers: list[dict]
    assembly: dict
    verifier: dict


class ContextCompressionWorkerPromptDetailResponse(BaseModel):
    worker_name: str
    profile_id: str
    input_mode: str
    use_default: bool
    default_text: str
    override_text: str | None
    effective_text: str
    updated_at: str | None = None


class ContextCompressionWorkerPromptUpdateBody(BaseModel):
    override_text: str


def create_context_compression_router(
    conversation_repository: ConversationRepository,
    *,
    prompt_repository: ContextCompressionWorkerPromptRepository | None = None,
    settings_repository: ContextCompressionSettingsRepository | None = None,
    llm_registry_gateway: LlmModelRegistryGateway | None = None,
    archive_repository: ContextCompressionArchiveRepository | None = None,
    config_accessor: Callable[[], ContextCompressionConfig] | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/conversations/{conversation_id}/compressed-markdown",
        response_model=ContextCompressionMarkdownResponse,
    )
    async def get_compressed_markdown(
        conversation_id: str,
        summary_message_id: str | None = Query(default=None),
        summary_message_index: int | None = Query(default=None, ge=0),
        archive_job_id: str | None = Query(default=None),
    ) -> ContextCompressionMarkdownResponse:
        if not summary_message_id and summary_message_index is None and not archive_job_id:
            raise HTTPException(status_code=400, detail="必须提供 summary_message_id、summary_message_index 或 archive_job_id。")
        try:
            conversation = await conversation_repository.get(conversation_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"会话不存在: {conversation_id}")

        match = _select_summary_message(
            conversation.messages,
            summary_message_id=summary_message_id,
            summary_message_index=summary_message_index,
            archive_job_id=archive_job_id,
        )
        if match is None:
            raise HTTPException(status_code=404, detail="未找到上下文压缩摘要消息。")
        index, message, matched_reference = match
        markdown = _message_markdown(message)
        if not markdown.strip():
            raise HTTPException(status_code=404, detail="上下文压缩摘要消息为空。")
        return ContextCompressionMarkdownResponse(
            conversation_id=conversation_id,
            message_index=index,
            markdown=markdown,
            matched_reference=matched_reference,
            summary_message_id=summary_message_id,
            archive_job_id=archive_job_id,
        )

    @router.get(
        "/jobs/{job_id}/worker-contents",
        response_model=ContextCompressionWorkerContentsResponse,
    )
    async def get_worker_contents(job_id: str) -> ContextCompressionWorkerContentsResponse:
        repository = _require_archive_repository(archive_repository)
        try:
            section_items = repository.get_section_items(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"上下文压缩 job 不存在: {job_id}")
        return ContextCompressionWorkerContentsResponse(
            job_id=job_id,
            workers=_worker_content_responses(section_items),
        )

    @router.get("/settings", response_model=ContextCompressionSettingsResponse)
    async def get_settings() -> ContextCompressionSettingsResponse:
        config = _require_config(config_accessor)
        repo = _require_settings_repository(settings_repository)
        settings = repo.get_settings()
        policy_id = settings.llm_policy_group_id
        status, status_message = _evaluate_policy_status(llm_registry_gateway, policy_id)
        active_profile_id = settings.active_profile_id or DEFAULT_ACTIVE_PROFILE_ID
        try:
            active_profile_id = normalize_profile_id(active_profile_id)
        except ValueError:
            active_profile_id = DEFAULT_ACTIVE_PROFILE_ID
        return ContextCompressionSettingsResponse(
            enabled=config.enabled,
            input_mode=NATIVE_INPUT_MODE,
            active_profile_id=active_profile_id,
            llm_policy_group_id=policy_id,
            llm_policy_group_status=status,
            llm_policy_group_status_message=status_message,
        )

    @router.put("/settings", response_model=ContextCompressionSettingsResponse)
    async def update_settings(
        body: ContextCompressionSettingsUpdateBody,
    ) -> ContextCompressionSettingsResponse:
        _require_config(config_accessor)
        repo = _require_settings_repository(settings_repository)
        current_settings = repo.get_settings()
        new_policy_id = body.llm_policy_group_id
        if new_policy_id is not None:
            new_policy_id = new_policy_id.strip() or None
        if new_policy_id is not None:
            _require_valid_execution_policy(llm_registry_gateway, new_policy_id)
        # 产品不再暴露输入模式切换；持久化固定 native，忽略 body.input_mode。
        active_profile_id = body.active_profile_id
        if active_profile_id is None:
            active_profile_id = current_settings.active_profile_id or DEFAULT_ACTIVE_PROFILE_ID
        try:
            active_profile_id = normalize_profile_id(active_profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if new_policy_id is None and body.llm_policy_group_id is None:
            # allow profile-only updates; keep current policy when field omitted via null explicit?
            new_policy_id = current_settings.llm_policy_group_id
        # body.llm_policy_group_id may be explicitly null to clear; if attribute missing handled by default None
        if "llm_policy_group_id" not in body.model_fields_set:
            new_policy_id = current_settings.llm_policy_group_id
        repo.set_settings(
            llm_policy_group_id=new_policy_id,
            input_mode=NATIVE_INPUT_MODE,
            active_profile_id=active_profile_id,
        )
        return await get_settings()

    @router.get("/profiles", response_model=list[ContextCompressionProfileSummaryResponse])
    async def list_profile_summaries() -> list[ContextCompressionProfileSummaryResponse]:
        repo = _require_settings_repository(settings_repository)
        active = repo.get_active_profile_id() or DEFAULT_ACTIVE_PROFILE_ID
        try:
            active = normalize_profile_id(active)
        except ValueError:
            active = DEFAULT_ACTIVE_PROFILE_ID
        return [
            ContextCompressionProfileSummaryResponse(**profile.to_summary_dict(is_active=profile.profile_id == active))
            for profile in list_profiles()
        ]

    @router.get("/profiles/{profile_id}", response_model=ContextCompressionProfileDetailResponse)
    async def get_profile_detail(profile_id: str) -> ContextCompressionProfileDetailResponse:
        repo = _require_settings_repository(settings_repository)
        active = repo.get_active_profile_id() or DEFAULT_ACTIVE_PROFILE_ID
        try:
            active = normalize_profile_id(active)
            profile = get_profile(profile_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return ContextCompressionProfileDetailResponse(**profile.to_detail_dict(is_active=profile.profile_id == active))

    @router.get("/profiles/{profile_id}/workers", response_model=list[ContextCompressionWorkerResponse])
    async def list_profile_workers(profile_id: str) -> list[ContextCompressionWorkerResponse]:
        try:
            profile = get_profile(profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        repository = _require_prompt_repository(prompt_repository)
        prompt_index = {(p.input_mode, p.worker_name): p for p in repository.list_prompts()}
        stage_mode = {stage.stage_id: stage.mode for stage in profile.stages}
        workers: list[ContextCompressionWorkerResponse] = []
        for worker in profile.workers:
            storage_keys = prompt_storage_keys_for_profile(profile.profile_id)
            override = None
            for key in storage_keys:
                override = prompt_index.get((key, worker.worker_name))
                if override is not None:
                    break
            prompt_status = {
                profile.profile_id: ContextCompressionPromptStatusResponse(
                    use_default=override is None,
                    updated_at=override.updated_at if override is not None else None,
                )
            }
            workers.append(
                ContextCompressionWorkerResponse(
                    name=worker.worker_name,
                    title=worker.title,
                    description=worker.description,
                    group=worker.group,
                    group_label=worker.group_label,
                    stage_id=worker.stage_id,
                    stage_mode=stage_mode.get(worker.stage_id),
                    input_builder=worker.input_builder,
                    output_section=worker.output_section,
                    profile_id=profile.profile_id,
                    prompt_status=prompt_status,
                )
            )
        return workers

    @router.get(
        "/profiles/{profile_id}/workers/{worker_name}/prompt",
        response_model=ContextCompressionWorkerPromptDetailResponse,
    )
    async def get_profile_worker_prompt(profile_id: str, worker_name: str) -> ContextCompressionWorkerPromptDetailResponse:
        profile = _require_profile(profile_id)
        _validate_profile_worker(profile_id, worker_name)
        repository = _require_prompt_repository(prompt_repository)
        default_text = get_default_worker_prompt(profile_id=profile_id, worker_name=worker_name)
        override = None
        for key in prompt_storage_keys_for_profile(profile_id):
            override = repository.get_prompt(input_mode=key, worker_name=worker_name)
            if override is not None:
                break
        return _prompt_detail_response(
            worker_name=worker_name,
            profile_id=profile_id,
            input_mode=NATIVE_INPUT_MODE,
            default_text=default_text,
            override=override,
        )

    @router.put(
        "/profiles/{profile_id}/workers/{worker_name}/prompt",
        response_model=ContextCompressionWorkerPromptDetailResponse,
    )
    async def update_profile_worker_prompt(
        profile_id: str,
        worker_name: str,
        body: ContextCompressionWorkerPromptUpdateBody,
    ) -> ContextCompressionWorkerPromptDetailResponse:
        _require_profile(profile_id)
        _validate_profile_worker(profile_id, worker_name)
        repository = _require_prompt_repository(prompt_repository)
        default_text = get_default_worker_prompt(profile_id=profile_id, worker_name=worker_name)
        try:
            prompt = repository.update_prompt(
                input_mode=profile_id,
                worker_name=worker_name,
                prompt_text=body.override_text,
            )
        except KeyError:
            # sqlite legacy path may require insert; postgres upserts
            if hasattr(repository, "save_prompt_snapshot"):
                from agent_runtime.context_compression.prompt_repository import ContextCompressionWorkerPrompt
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).isoformat()
                prompt = ContextCompressionWorkerPrompt(
                    input_mode=profile_id,
                    worker_name=worker_name,
                    prompt_text=body.override_text,
                    default_prompt_text="",
                    is_custom=True,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
                repository.save_prompt_snapshot(prompt)
            else:
                raise HTTPException(status_code=404, detail=f"上下文压缩 Worker prompt 不存在: {profile_id}/{worker_name}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _prompt_detail_response(
            worker_name=worker_name,
            profile_id=profile_id,
            input_mode=NATIVE_INPUT_MODE,
            default_text=default_text,
            override=prompt,
        )

    @router.post(
        "/profiles/{profile_id}/workers/{worker_name}/prompt/reset",
        response_model=ContextCompressionWorkerPromptDetailResponse,
    )
    async def reset_profile_worker_prompt(
        profile_id: str,
        worker_name: str,
    ) -> ContextCompressionWorkerPromptDetailResponse:
        _require_profile(profile_id)
        _validate_profile_worker(profile_id, worker_name)
        repository = _require_prompt_repository(prompt_repository)
        default_text = get_default_worker_prompt(profile_id=profile_id, worker_name=worker_name)
        for key in prompt_storage_keys_for_profile(profile_id):
            try:
                repository.reset_prompt(input_mode=key, worker_name=worker_name)
            except KeyError:
                pass
        return _prompt_detail_response(
            worker_name=worker_name,
            profile_id=profile_id,
            input_mode=NATIVE_INPUT_MODE,
            default_text=default_text,
            override=None,
        )

    @router.get("/workers", response_model=list[ContextCompressionWorkerResponse])
    async def list_workers() -> list[ContextCompressionWorkerResponse]:
        repo = _require_settings_repository(settings_repository)
        active = repo.get_active_profile_id() or DEFAULT_ACTIVE_PROFILE_ID
        try:
            active = normalize_profile_id(active)
        except ValueError:
            active = DEFAULT_ACTIVE_PROFILE_ID
        return await list_profile_workers(active)

    @router.get(
        "/workers/{worker_name}/prompts/{input_mode}",
        response_model=ContextCompressionWorkerPromptDetailResponse,
    )
    async def get_worker_prompt(
        worker_name: str,
        input_mode: str,
    ) -> ContextCompressionWorkerPromptDetailResponse:
        _validate_worker_name(worker_name)
        _validate_input_mode(input_mode)
        repository = _require_prompt_repository(prompt_repository)
        default_text = get_default_worker_prompt(input_mode=input_mode, worker_name=worker_name)
        override = repository.get_prompt(
            input_mode=input_mode,
            worker_name=worker_name,
        )
        return _prompt_detail_response(
            worker_name=worker_name,
            profile_id=DEFAULT_ACTIVE_PROFILE_ID,
            input_mode=input_mode,
            default_text=default_text,
            override=override,
        )

    @router.put(
        "/workers/{worker_name}/prompts/{input_mode}",
        response_model=ContextCompressionWorkerPromptDetailResponse,
    )
    async def update_worker_prompt(
        worker_name: str,
        input_mode: str,
        body: ContextCompressionWorkerPromptUpdateBody,
    ) -> ContextCompressionWorkerPromptDetailResponse:
        _validate_worker_name(worker_name)
        _validate_input_mode(input_mode)
        repository = _require_prompt_repository(prompt_repository)
        default_text = get_default_worker_prompt(input_mode=input_mode, worker_name=worker_name)
        try:
            prompt = repository.update_prompt(
                input_mode=input_mode,
                worker_name=worker_name,
                prompt_text=body.override_text,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _prompt_detail_response(
            worker_name=worker_name,
            profile_id=DEFAULT_ACTIVE_PROFILE_ID,
            input_mode=input_mode,
            default_text=default_text,
            override=prompt,
        )

    @router.post(
        "/workers/{worker_name}/prompts/{input_mode}/reset",
        response_model=ContextCompressionWorkerPromptDetailResponse,
    )
    async def reset_worker_prompt(
        worker_name: str,
        input_mode: str,
    ) -> ContextCompressionWorkerPromptDetailResponse:
        _validate_worker_name(worker_name)
        _validate_input_mode(input_mode)
        repository = _require_prompt_repository(prompt_repository)
        default_text = get_default_worker_prompt(input_mode=input_mode, worker_name=worker_name)
        try:
            repository.reset_prompt(input_mode=input_mode, worker_name=worker_name)
        except KeyError:
            pass
        return _prompt_detail_response(
            worker_name=worker_name,
            profile_id=DEFAULT_ACTIVE_PROFILE_ID,
            input_mode=input_mode,
            default_text=default_text,
            override=None,
        )

    return router


def _select_summary_message(
    messages: list[UniMessage],
    *,
    summary_message_id: str | None,
    summary_message_index: int | None,
    archive_job_id: str | None,
) -> tuple[int, UniMessage, str] | None:
    if summary_message_index is not None:
        if summary_message_index >= len(messages):
            return None
        return summary_message_index, messages[summary_message_index], "summary_message_index"

    if summary_message_id:
        for index, message in enumerate(messages):
            if _message_reference_matches(message, summary_message_id):
                return index, message, "summary_message_id"

    if archive_job_id:
        for index, message in enumerate(messages):
            if _message_reference_matches(message, archive_job_id):
                return index, message, "archive_job_id"

    return None


def _message_markdown(message: UniMessage) -> str:
    parts: list[str] = []
    for part in message.content:
        if isinstance(part, TextPart):
            parts.append(part.text)
    return "\n".join(parts)


def _message_reference_matches(message: UniMessage, value: str) -> bool:
    provider_extras = message.provider_extras or {}
    return _contains_reference(provider_extras, value)


def _contains_reference(candidate: object, value: str) -> bool:
    if candidate == value:
        return True
    if isinstance(candidate, dict):
        return any(_contains_reference(item, value) for item in candidate.values())
    if isinstance(candidate, list | tuple):
        return any(_contains_reference(item, value) for item in candidate)
    return False


def _require_archive_repository(
    archive_repository: ContextCompressionArchiveRepository | None,
) -> ContextCompressionArchiveRepository:
    if archive_repository is None:
        raise HTTPException(status_code=503, detail="上下文压缩归档仓储未配置。")
    return archive_repository


def _worker_content_responses(
    section_items: list[dict[str, object]],
) -> list[ContextCompressionWorkerReadableContentResponse]:
    grouped_contents: dict[str, list[str]] = {worker_name: [] for worker_name, _section in SECTION_WORKERS}
    for item in section_items:
        section = str(item.get("section") or "").strip()
        worker_name = str(item.get("worker_name") or "").strip()
        if worker_name not in grouped_contents:
            worker_name = _SECTION_TO_WORKER_NAME.get(section, "")
        if worker_name not in grouped_contents:
            continue
        raw_content = item.get("content")
        content = raw_content.strip() if isinstance(raw_content, str) else ""
        if content:
            grouped_contents[worker_name].append(content)

    responses: list[ContextCompressionWorkerReadableContentResponse] = []
    for worker_name, section in SECTION_WORKERS:
        responses.append(
            ContextCompressionWorkerReadableContentResponse(
                worker_name=worker_name,
                title=SECTION_WORKER_TITLES.get(section, worker_name),
                content_markdown=_join_readable_contents(grouped_contents.get(worker_name, [])),
            )
        )
    return responses


def _join_readable_contents(contents: list[str]) -> str:
    deduped: list[str] = []
    seen: set[str] = set()
    for content in contents:
        normalized = "\n".join(line.rstrip() for line in content.strip().splitlines()).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return "\n\n".join(deduped)


def _require_prompt_repository(
    prompt_repository: ContextCompressionWorkerPromptRepository | None,
) -> ContextCompressionWorkerPromptRepository:
    if prompt_repository is None:
        raise HTTPException(status_code=503, detail="上下文压缩 Worker prompt 仓储未配置。")
    return prompt_repository


def _require_config(
    config_accessor: Callable[[], ContextCompressionConfig] | None,
) -> ContextCompressionConfig:
    if config_accessor is None:
        raise HTTPException(status_code=503, detail="上下文压缩配置未注入。")
    return config_accessor()


def _require_settings_repository(
    settings_repository: ContextCompressionSettingsRepository | None,
) -> ContextCompressionSettingsRepository:
    if settings_repository is None:
        raise HTTPException(status_code=503, detail="上下文压缩设置仓储未配置。")
    return settings_repository


def _evaluate_policy_status(
    llm_registry_gateway: LlmModelRegistryGateway | None,
    policy_group_id: str | None,
) -> tuple[str, str | None]:
    """返回 (status, message)。status ∈ unset|missing|disabled|no_candidates|ok|unknown。"""
    if not policy_group_id:
        return "unset", None
    if llm_registry_gateway is None:
        return "unknown", "LLM 注册网关未注入。"
    try:
        policy = llm_registry_gateway.get_execution_policy(policy_group_id)
    except KeyError:
        return "missing", f"LLM 执行策略组不存在或已被删除: {policy_group_id}"
    if not policy.enabled:
        return "disabled", f"LLM 执行策略组已禁用: {policy_group_id}"
    if not policy.enabled_candidates():
        return "no_candidates", f"LLM 执行策略组没有任何启用的候选项: {policy_group_id}"
    return "ok", None


def _require_valid_execution_policy(
    llm_registry_gateway: LlmModelRegistryGateway | None,
    policy_group_id: str,
) -> None:
    if llm_registry_gateway is None:
        raise HTTPException(status_code=503, detail="LLM 注册网关未注入。")
    try:
        policy = llm_registry_gateway.get_execution_policy(policy_group_id)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"LLM 执行策略组不存在: {policy_group_id}")
    if not policy.enabled:
        raise HTTPException(status_code=400, detail=f"LLM 执行策略组已禁用: {policy_group_id}")
    if not policy.enabled_candidates():
        raise HTTPException(
            status_code=400,
            detail=f"LLM 执行策略组没有任何启用的候选项: {policy_group_id}",
        )


def _validate_worker_name(worker_name: str) -> None:
    if worker_name not in CONTEXT_COMPRESSION_WORKER_NAMES:
        raise HTTPException(status_code=404, detail=f"未知的上下文压缩 Worker: {worker_name}")


def _validate_input_mode(input_mode: str) -> None:
    if input_mode not in SUPPORTED_INPUT_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"input_mode 必须是 {' 或 '.join(SUPPORTED_INPUT_MODES)}。",
        )



def _require_profile(profile_id: str):
    try:
        return get_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _validate_profile_worker(profile_id: str, worker_name: str) -> None:
    profile = _require_profile(profile_id)
    names = {worker.worker_name for worker in profile.workers}
    if worker_name not in names:
        raise HTTPException(status_code=404, detail=f"未知的上下文压缩 Worker: {profile_id}/{worker_name}")

def _prompt_detail_response(
    *,
    worker_name: str,
    profile_id: str = DEFAULT_ACTIVE_PROFILE_ID,
    input_mode: str,
    default_text: str,
    override: ContextCompressionWorkerPrompt | None,
) -> ContextCompressionWorkerPromptDetailResponse:
    override_text = override.prompt_text if override is not None else None
    return ContextCompressionWorkerPromptDetailResponse(
        worker_name=worker_name,
        profile_id=profile_id,
        input_mode=input_mode,
        use_default=override is None,
        default_text=default_text,
        override_text=override_text,
        effective_text=override_text if override_text is not None else default_text,
        updated_at=override.updated_at if override is not None else None,
    )
