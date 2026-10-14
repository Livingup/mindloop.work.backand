from __future__ import annotations

import json
import logging
from typing import Any

from agent_runtime.conversation_message.message.models import ImagePart, PdfPart, TextPart
from agent_runtime.conversation_message.message.tool_result_output import normalize_tool_result_output
from agent_runtime.file_resource.north_gateway.resource_create_contracts import Base64CreateRequest
from agent_runtime.file_resource.shared_service import get_file_resource_service

logger = logging.getLogger(__name__)

_MCP_TEXT_BLOCK = "text"
_MCP_IMAGE_BLOCK = "image"
_MCP_AUDIO_BLOCK = "audio"
_MCP_RESOURCE_LINK_BLOCK = "resource_link"
_MCP_EMBEDDED_RESOURCE_BLOCK = "resource"


def normalize_mcp_tool_content_for_llm(
    content_blocks: tuple[dict[str, Any], ...],
    *,
    fallback_text: str,
) -> list[dict[str, Any]]:
    parts: list[TextPart | ImagePart | PdfPart] = []
    for block in content_blocks:
        block_type = block.get("type")
        if block_type == _MCP_TEXT_BLOCK:
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(TextPart(text=text))
            continue
        if block_type == _MCP_IMAGE_BLOCK:
            part = _mcp_image_block_to_part(block)
            parts.append(part if part is not None else TextPart(text=_describe_unsupported_mcp_block(block)))
            continue
        if block_type == _MCP_AUDIO_BLOCK:
            parts.append(TextPart(text=_describe_unsupported_mcp_block(block)))
            continue
        if block_type == _MCP_RESOURCE_LINK_BLOCK:
            parts.append(TextPart(text=_describe_mcp_resource_link(block)))
            continue
        if block_type == _MCP_EMBEDDED_RESOURCE_BLOCK:
            parts.extend(_mcp_embedded_resource_to_parts(block))
            continue
        parts.append(TextPart(text=_describe_unsupported_mcp_block(block)))
    if parts:
        return normalize_tool_result_output(parts)
    return normalize_tool_result_output(fallback_text or "MCP 工具执行完成，但没有返回可见内容。")


def _mcp_image_block_to_part(block: dict[str, Any]) -> ImagePart | None:
    data = block.get("data")
    mime_type = block.get("mimeType") or block.get("mime_type")
    if not isinstance(data, str) or not data:
        return None
    if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
        return None
    resource_id = _create_file_resource_from_base64(data, mime_type)
    return ImagePart(resource_id=resource_id) if resource_id else None


def _mcp_embedded_resource_to_parts(block: dict[str, Any]) -> list[TextPart | ImagePart | PdfPart]:
    resource = block.get("resource")
    if not isinstance(resource, dict):
        return [TextPart(text=_describe_unsupported_mcp_block(block))]
    uri = str(resource.get("uri") or "")
    mime_type = resource.get("mimeType") or resource.get("mime_type")
    if not isinstance(mime_type, str):
        mime_type = None
    text = resource.get("text")
    if isinstance(text, str):
        label = _resource_label(uri=uri, mime_type=mime_type)
        return [TextPart(text=f"{label}\n{text}" if label else text)]
    blob = resource.get("blob")
    if isinstance(blob, str) and mime_type:
        if mime_type.startswith("image/"):
            resource_id = _create_file_resource_from_base64(blob, mime_type)
            return [ImagePart(resource_id=resource_id)] if resource_id else [TextPart(text=_describe_unsupported_mcp_block(block))]
        if mime_type == "application/pdf":
            resource_id = _create_file_resource_from_base64(blob, mime_type)
            return [PdfPart(resource_id=resource_id)] if resource_id else [TextPart(text=_describe_unsupported_mcp_block(block))]
    return [TextPart(text=_describe_mcp_embedded_resource_reference(resource))]


def _create_file_resource_from_base64(data: str, mime_type: str) -> str | None:
    try:
        created = get_file_resource_service().create_resources_from_base64_contents([
            Base64CreateRequest(resource_base64_content=data, resource_mime_type=mime_type)
        ])
    except Exception:
        logger.exception("MCP 工具返回资源写入统一文件资源服务失败，已降级为文本。")
        return None
    if not created:
        return None
    return created[0].resource_id


def _describe_mcp_resource_link(block: dict[str, Any]) -> str:
    name = block.get("name")
    uri = block.get("uri")
    mime_type = block.get("mimeType") or block.get("mime_type")
    lines = ["[resource_link] 当前 LLM 协议不支持原生资源链接，已降级为文本引用。"]
    if name:
        lines.append(f"name: {name}")
    if uri:
        lines.append(f"uri: {uri}")
    if mime_type:
        lines.append(f"mime_type: {mime_type}")
    return "\n".join(lines)


def _describe_mcp_embedded_resource_reference(resource: dict[str, Any]) -> str:
    uri = resource.get("uri")
    mime_type = resource.get("mimeType") or resource.get("mime_type")
    lines = ["[unsupported_file] MCP 工具返回了一个文件资源，但当前运行时无法将该文件模态转换为 LLM 可直接读取的 content，已降级为文本引用。"]
    if uri:
        lines.append(f"uri: {uri}")
    if mime_type:
        lines.append(f"mime_type: {mime_type}")
    return "\n".join(lines)


def _describe_unsupported_mcp_block(block: dict[str, Any]) -> str:
    block_type = block.get("type") or "unknown"
    mime_type = block.get("mimeType") or block.get("mime_type")
    lines = [f"[unsupported_content] MCP 工具返回了不支持的内容类型 {block_type}，已降级为文本。"]
    if mime_type:
        lines.append(f"mime_type: {mime_type}")
    lines.append(f"raw: {_stable_json_text(block)}")
    return "\n".join(lines)


def _resource_label(*, uri: str, mime_type: str | None) -> str:
    fields = []
    if uri:
        fields.append(f"uri: {uri}")
    if mime_type:
        fields.append(f"mime_type: {mime_type}")
    return "\n".join(fields)


def _stable_json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        return str(value)
