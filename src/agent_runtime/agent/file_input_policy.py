# -*- coding: utf-8 -*-
"""Agent 用户消息文件输入限制共享策略。

该模块把前端查询展示、REST 消息入口、本地子 Agent 协作工具和未来远程 Agent
文件消息入口都需要遵守的 LLM 文件输入限制收敛到同一套后端策略中。
前端可以继续做发送前预校验，但真正的入队前强制校验必须由本模块完成。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_runtime.agent.agent_instance import PendingUserMessageAttachment
from agent_runtime.input_modality import INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF


MAX_IMAGE_INPUTS_PER_LLM_REQUEST = 1500
MAX_TOTAL_IMAGE_BYTES_PER_LLM_REQUEST = 512 * 1024 * 1024
MAX_SINGLE_INPUT_FILE_BYTES_PER_LLM_REQUEST = 50 * 1024 * 1024
MAX_TOTAL_INPUT_FILE_BYTES_PER_LLM_REQUEST = 50 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FileInputLimits:
    max_image_inputs_per_request: int = MAX_IMAGE_INPUTS_PER_LLM_REQUEST
    max_total_image_bytes_per_request: int = MAX_TOTAL_IMAGE_BYTES_PER_LLM_REQUEST
    max_single_input_file_bytes: int = MAX_SINGLE_INPUT_FILE_BYTES_PER_LLM_REQUEST
    max_total_input_file_bytes_per_request: int = MAX_TOTAL_INPUT_FILE_BYTES_PER_LLM_REQUEST


@dataclass(slots=True)
class FileInputStats:
    file_count: int = 0
    total_file_bytes: int = 0
    pdf_file_count: int = 0
    image_file_count: int = 0
    total_pdf_pages: int = 0
    total_image_bytes: int = 0
    total_input_file_bytes: int = 0


@dataclass(frozen=True, slots=True)
class FileInputCandidate:
    kind: str
    size_in_bytes: int
    page_count: int = 0
    name: str | None = None


class FileInputLimitError(ValueError):
    """文件输入超出统一 LLM 请求限制。"""


def default_file_input_limits() -> FileInputLimits:
    return FileInputLimits()


def file_input_limits_payload(limits: FileInputLimits | None = None) -> dict[str, int]:
    actual = limits or default_file_input_limits()
    return {
        "max_image_inputs_per_request": actual.max_image_inputs_per_request,
        "max_total_image_bytes_per_request": actual.max_total_image_bytes_per_request,
        "max_single_input_file_bytes": actual.max_single_input_file_bytes,
        "max_total_input_file_bytes_per_request": actual.max_total_input_file_bytes_per_request,
    }


def file_input_stats_payload(stats: FileInputStats) -> dict[str, int]:
    return {
        "file_count": stats.file_count,
        "total_file_bytes": stats.total_file_bytes,
        "pdf_file_count": stats.pdf_file_count,
        "image_file_count": stats.image_file_count,
        "total_pdf_pages": stats.total_pdf_pages,
        "total_image_bytes": stats.total_image_bytes,
        "total_input_file_bytes": stats.total_input_file_bytes,
    }


async def collect_next_request_file_input_stats(
    instance: Any,
    conversation_repository: Any | None,
    file_resource_service: Any | None,
) -> FileInputStats:
    """统计下一次 LLM 请求中已经存在的历史和待处理文件输入。"""

    stats = FileInputStats()
    if conversation_repository is not None and getattr(instance, "conversation_id", None):
        conversation = await conversation_repository.get(instance.conversation_id)
        _add_resource_reference_stats(
            stats,
            list(_iter_file_resource_references(getattr(conversation, "messages", []))),
            file_resource_service,
        )
    for message in getattr(instance, "pending_user_messages", ()):
        _add_attachment_reference_stats(stats, tuple(message.attachments), file_resource_service)
    return stats


async def validate_agent_instance_file_input_allowed(
    *,
    instance: Any,
    new_attachments: tuple[PendingUserMessageAttachment, ...],
    conversation_repository: Any | None,
    file_resource_service: Any | None,
    limits: FileInputLimits | None = None,
) -> None:
    """在用户消息入队前校验本次新增附件不会让下一次 LLM 请求超限。"""

    if not new_attachments:
        return
    if file_resource_service is None:
        raise FileInputLimitError("文件资源服务未启用，无法校验文件输入限制。")
    actual_limits = limits or default_file_input_limits()
    stats = await collect_next_request_file_input_stats(
        instance,
        conversation_repository,
        file_resource_service,
    )
    candidates = _attachment_candidates(new_attachments, file_resource_service)
    for candidate in candidates:
        add_file_input_stats(
            stats,
            candidate.kind,
            candidate.size_in_bytes,
            candidate.page_count,
        )
    validate_file_input_stats(actual_limits, stats, candidates)


def validate_file_input_candidates(
    candidates: tuple[FileInputCandidate, ...],
    limits: FileInputLimits | None = None,
) -> None:
    """只校验一组即将发送的新文件，适用于还没有目标实例的新建子 Agent 初始消息。"""

    stats = FileInputStats()
    for candidate in candidates:
        add_file_input_stats(
            stats,
            candidate.kind,
            candidate.size_in_bytes,
            candidate.page_count,
        )
    validate_file_input_stats(limits or default_file_input_limits(), stats, candidates)


def validate_file_input_stats(
    limits: FileInputLimits,
    predicted_stats: FileInputStats,
    new_candidates: tuple[FileInputCandidate, ...] = (),
) -> None:
    for candidate in new_candidates:
        if (
            candidate.kind == INPUT_MODALITY_PDF
            and candidate.size_in_bytes >= limits.max_single_input_file_bytes
        ):
            raise FileInputLimitError(
                f"文件输入超出限制：{candidate.name or 'PDF/文件'} 大小为 {candidate.size_in_bytes} bytes，"
                f"单个 PDF/文件必须小于 {limits.max_single_input_file_bytes} bytes。"
            )
    if predicted_stats.image_file_count > limits.max_image_inputs_per_request:
        raise FileInputLimitError(
            f"文件输入超出限制：当前请求将包含 {predicted_stats.image_file_count} 张图片，"
            f"超过上限 {limits.max_image_inputs_per_request} 张。"
        )
    if predicted_stats.total_image_bytes > limits.max_total_image_bytes_per_request:
        raise FileInputLimitError(
            f"文件输入超出限制：当前请求图片总大小 {predicted_stats.total_image_bytes} bytes，"
            f"超过上限 {limits.max_total_image_bytes_per_request} bytes。"
        )
    if predicted_stats.total_input_file_bytes > limits.max_total_input_file_bytes_per_request:
        raise FileInputLimitError(
            f"文件输入超出限制：当前请求 PDF/文件总大小 {predicted_stats.total_input_file_bytes} bytes，"
            f"超过上限 {limits.max_total_input_file_bytes_per_request} bytes。"
        )


def add_file_input_stats(
    stats: FileInputStats,
    kind: str,
    size_in_bytes: int,
    page_count: int = 0,
) -> None:
    safe_size = max(0, size_in_bytes)
    stats.file_count += 1
    stats.total_file_bytes += safe_size
    if kind == INPUT_MODALITY_PDF:
        stats.pdf_file_count += 1
        stats.total_pdf_pages += max(0, page_count)
        stats.total_input_file_bytes += safe_size
        return
    if kind == INPUT_MODALITY_IMAGE:
        stats.image_file_count += 1
        stats.total_image_bytes += safe_size


def _attachment_candidates(
    attachments: tuple[PendingUserMessageAttachment, ...],
    file_resource_service: Any,
) -> tuple[FileInputCandidate, ...]:
    resources = file_resource_service.get_resources([attachment.resource_id for attachment in attachments])
    candidates: list[FileInputCandidate] = []
    for index, attachment in enumerate(attachments):
        resource = resources[index] if index < len(resources) else None
        if resource is None:
            raise FileInputLimitError(f"文件资源不存在，无法校验文件输入限制: {attachment.resource_id}")
        candidates.append(_resource_candidate(resource, fallback_kind=attachment.kind, fallback_name=attachment.name))
    return tuple(candidates)


def _add_attachment_reference_stats(
    stats: FileInputStats,
    attachments: tuple[PendingUserMessageAttachment, ...],
    file_resource_service: Any | None,
) -> None:
    if not attachments:
        return
    if file_resource_service is None:
        raise FileInputLimitError("文件资源服务未启用，无法统计待处理文件输入。")
    for candidate in _attachment_candidates(attachments, file_resource_service):
        add_file_input_stats(stats, candidate.kind, candidate.size_in_bytes, candidate.page_count)


def _iter_file_resource_references(messages: list[Any]):
    for message in messages:
        yield from _iter_file_resource_references_from_parts(getattr(message, "content", []))


def _iter_file_resource_references_from_parts(parts: list[Any]):
    for part in parts:
        kind = getattr(part, "kind", None)
        if kind in {INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF}:
            resource_id = getattr(part, "resource_id", None)
            if resource_id:
                yield kind, str(resource_id)
        elif kind == "tool_result":
            output = getattr(part, "output", None)
            if isinstance(output, list):
                yield from _iter_file_resource_references_from_parts(output)


def _add_resource_reference_stats(
    stats: FileInputStats,
    references: list[tuple[str, str]],
    file_resource_service: Any | None,
) -> None:
    if not references:
        return
    if file_resource_service is None:
        raise FileInputLimitError("文件资源服务未启用，无法统计历史文件输入。")
    resources = file_resource_service.get_resources([resource_id for _, resource_id in references])
    for index, (kind, resource_id) in enumerate(references):
        resource = resources[index] if index < len(resources) else None
        if resource is None:
            raise FileInputLimitError(f"历史文件资源不存在，无法统计文件输入限制: {resource_id}")
        candidate = _resource_candidate(resource, fallback_kind=kind, fallback_name=None)
        add_file_input_stats(stats, candidate.kind, candidate.size_in_bytes, candidate.page_count)


def _resource_candidate(resource: Any, *, fallback_kind: str, fallback_name: str | None) -> FileInputCandidate:
    metadata = getattr(resource, "resource_metadata", None)
    category = getattr(metadata, "resource_category", None)
    actual_kind = (
        INPUT_MODALITY_PDF
        if category == INPUT_MODALITY_PDF
        else INPUT_MODALITY_IMAGE
        if category == INPUT_MODALITY_IMAGE
        else fallback_kind
    )
    return FileInputCandidate(
        kind=actual_kind,
        size_in_bytes=_resource_size(resource),
        page_count=getattr(metadata, "pdf_page_count", 0) if actual_kind == INPUT_MODALITY_PDF else 0,
        name=fallback_name,
    )


def _resource_size(resource: Any) -> int:
    try:
        return max(0, int(getattr(resource, "resource_size_in_bytes", 0) or 0))
    except (TypeError, ValueError):
        return 0
