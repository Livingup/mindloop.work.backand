from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from agent_runtime.conversation_message.conversation_repository import ConversationRepository
from agent_runtime.conversation_message.message.models import TextPart, UniMessage


class ContextCompressionMarkdownResponse(BaseModel):
    conversation_id: str
    message_index: int
    markdown: str
    text_format: str = "markdown"
    matched_reference: str
    summary_message_id: str | None = None
    archive_job_id: str | None = None


def create_context_compression_router(
    conversation_repository: ConversationRepository,
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
