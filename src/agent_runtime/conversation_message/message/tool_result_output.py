from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any
import urllib.parse

from agent_runtime.conversation_message.message.models import ImagePart, PdfPart, TextPart
from agent_runtime.conversation_message.message.multimodal_url_markers import (
    multimodal_file_close_marker,
    multimodal_file_open_marker,
)
from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import (
    ResourceResolver,
    ResolvedResource,
    resolve_resource_access,
)

_TOOL_OUTPUT_TEXT = "text"
_TOOL_OUTPUT_IMAGE = "image"
_TOOL_OUTPUT_PDF = "pdf"
_TOOL_OUTPUT_KINDS = frozenset({_TOOL_OUTPUT_TEXT, _TOOL_OUTPUT_IMAGE, _TOOL_OUTPUT_PDF})


def normalize_tool_result_output(value: Any) -> list[dict[str, Any]]:
    if _is_tool_output_part_list(value):
        return [_normalize_tool_output_part(item) for item in value]
    if isinstance(value, str):
        return [_text_part(value)]
    if isinstance(value, TextPart):
        return [_text_part(value.text)]
    if isinstance(value, ImagePart):
        return [_resource_part(_TOOL_OUTPUT_IMAGE, value.resource_id)]
    if isinstance(value, PdfPart):
        return [_resource_part(_TOOL_OUTPUT_PDF, value.resource_id)]
    if isinstance(value, list) and all(isinstance(item, (TextPart, ImagePart, PdfPart)) for item in value):
        return [_part_object_to_output(item) for item in value]
    if isinstance(value, list) and all(_is_tool_output_part_like(item) for item in value):
        return [_normalize_tool_output_part(item) for item in value]
    return [_text_part(_stable_json_text(value))]


def is_tool_result_output(value: Any) -> bool:
    return _is_tool_output_part_list(value)


def normalize_tool_output_for_json(value: Any) -> list[dict[str, Any]]:
    return normalize_tool_result_output(value)


async def tool_result_output_to_text(value: Any, resolver: ResourceResolver) -> str:
    pieces: list[str] = []
    for item in normalize_tool_result_output(value):
        kind = item.get("kind")
        if kind == _TOOL_OUTPUT_TEXT:
            text = str(item.get("text") or "")
            if text:
                pieces.append(text)
            continue
        if kind in (_TOOL_OUTPUT_IMAGE, _TOOL_OUTPUT_PDF):
            resource_id = item.get("resource_id")
            if isinstance(resource_id, str) and resource_id:
                resource = await _try_resolve_resource(resolver, resource_id)
                if resource is None:
                    pieces.append(f"[{kind} file] resource_id={resource_id} unresolved")
                    continue
                locator = _resource_locator(resource)
                if not locator:
                    pieces.append(f"[{kind} file] resource_id={resource_id} resolved_without_url_or_path")
                    continue
                pieces.append(f"[{kind} file] resource_id={resource_id} locator={locator}")
                continue
            direct_locator = str(item.get("url") or item.get("path") or "").strip()
            if not direct_locator:
                pieces.append(f"[{kind} file] resource_id=<missing>")
                continue
            resource = await _try_resolve_output_resource(resolver, item)
            if resource is None:
                pieces.append(f"[{kind} file] locator={direct_locator} unresolved")
                continue
            pieces.append(f"[{kind} file] locator={_resource_locator(resource) or direct_locator}")
    return "\n".join(pieces)


async def tool_result_output_to_openai_responses_output(
    value: Any,
    resolver: ResourceResolver,
) -> str | list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in normalize_tool_result_output(value):
        kind = item.get("kind")
        if kind == _TOOL_OUTPUT_TEXT:
            text = str(item.get("text") or "")
            if text:
                items.append({"type": "input_text", "text": text})
            continue
        if kind == _TOOL_OUTPUT_IMAGE:
            resource = await _try_resolve_output_resource(resolver, item)
            if resource is None:
                continue
            image_url = resource.object_storage_url
            if not image_url:
                data = resource.base64_content
                if not data:
                    continue
                image_url = f"data:{resource.mime_type};base64,{data}"
            marker = _tool_output_open_marker("image", item, resource)
            if marker is not None:
                items.append({"type": "input_text", "text": marker})
            items.append({"type": "input_image", "image_url": image_url})
            if marker is not None:
                items.append({"type": "input_text", "text": multimodal_file_close_marker()})
            continue
        if kind == _TOOL_OUTPUT_PDF:
            resource = await _try_resolve_output_resource(resolver, item)
            if resource is None:
                continue
            if resource.object_storage_url:
                file_item: dict[str, Any] = {"type": "input_file", "file_url": resource.object_storage_url}
            else:
                data = resource.base64_content
                if not data:
                    continue
                file_item = {
                    "type": "input_file",
                    "file_data": f"data:{resource.mime_type or 'application/pdf'};base64,{data}",
                }
            marker = _tool_output_open_marker("pdf", item, resource)
            if marker is not None:
                items.append({"type": "input_text", "text": marker})
            items.append(file_item)
            if marker is not None:
                items.append({"type": "input_text", "text": multimodal_file_close_marker()})
    if not items:
        return ""
    if all(item.get("type") == "input_text" for item in items):
        return "\n".join(str(item.get("text") or "") for item in items if item.get("text"))
    return items


async def tool_result_output_to_claude_content(
    value: Any,
    resolver: ResourceResolver,
) -> str | list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for item in normalize_tool_result_output(value):
        kind = item.get("kind")
        if kind == _TOOL_OUTPUT_TEXT:
            text = str(item.get("text") or "")
            if text:
                blocks.append({"type": "text", "text": text})
            continue
        if kind == _TOOL_OUTPUT_IMAGE:
            resource = await _try_resolve_output_resource(resolver, item)
            source = _try_claude_resource_source(resource) if resource is not None else None
            if source is not None and resource is not None:
                marker = _tool_output_open_marker("image", item, resource)
                if marker is not None:
                    blocks.append({"type": "text", "text": marker})
                blocks.append({"type": "image", "source": source})
                if marker is not None:
                    blocks.append({"type": "text", "text": multimodal_file_close_marker()})
            continue
        if kind == _TOOL_OUTPUT_PDF:
            resource = await _try_resolve_output_resource(resolver, item)
            source = _try_claude_resource_source(resource, default_mime="application/pdf") if resource is not None else None
            if source is not None and resource is not None:
                marker = _tool_output_open_marker("pdf", item, resource)
                if marker is not None:
                    blocks.append({"type": "text", "text": marker})
                blocks.append({"type": "document", "source": source})
                if marker is not None:
                    blocks.append({"type": "text", "text": multimodal_file_close_marker()})
    if not blocks:
        return ""
    if all(block.get("type") == "text" for block in blocks):
        return "\n".join(str(block.get("text") or "") for block in blocks if block.get("text"))
    return blocks


async def tool_result_output_to_gemini_response_parts(
    value: Any,
    resolver: ResourceResolver,
) -> tuple[str, list[dict[str, Any]]]:
    text_pieces: list[str] = []
    media_parts: list[dict[str, Any]] = []
    for item in normalize_tool_result_output(value):
        kind = item.get("kind")
        if kind == _TOOL_OUTPUT_TEXT:
            text = str(item.get("text") or "")
            if text:
                text_pieces.append(text)
            continue
        if kind == _TOOL_OUTPUT_IMAGE:
            resource = await _try_resolve_output_resource(resolver, item)
            media_part = _try_gemini_media_part(resource, default_mime=resource.mime_type) if resource is not None else None
            if media_part is not None and resource is not None:
                marker = _tool_output_open_marker("image", item, resource)
                if marker is not None:
                    text_pieces.append(f"{marker}{multimodal_file_close_marker()}")
                media_parts.append(media_part)
            continue
        if kind == _TOOL_OUTPUT_PDF:
            resource = await _try_resolve_output_resource(resolver, item)
            media_part = _try_gemini_media_part(resource, default_mime="application/pdf") if resource is not None else None
            if media_part is not None and resource is not None:
                marker = _tool_output_open_marker("pdf", item, resource)
                if marker is not None:
                    text_pieces.append(f"{marker}{multimodal_file_close_marker()}")
                media_parts.append(media_part)
    return "\n".join(text_pieces), media_parts


def prefix_tool_result_error_output(value: Any) -> list[dict[str, Any]]:
    output = normalize_tool_result_output(value)
    if not output:
        return [_text_part("[ERROR] 工具执行失败。")]
    for item in output:
        if item.get("kind") == _TOOL_OUTPUT_TEXT:
            text = str(item.get("text") or "")
            item["text"] = f"[ERROR] {text}" if text else "[ERROR] 工具执行失败。"
            return output
    return [_text_part("[ERROR] 工具执行失败。"), *output]


def _part_object_to_output(value: TextPart | ImagePart | PdfPart) -> dict[str, Any]:
    if isinstance(value, TextPart):
        return _text_part(value.text)
    if isinstance(value, ImagePart):
        return _resource_part(_TOOL_OUTPUT_IMAGE, value.resource_id)
    return _resource_part(_TOOL_OUTPUT_PDF, value.resource_id)


def _text_part(text: str) -> dict[str, Any]:
    return {"kind": _TOOL_OUTPUT_TEXT, "text": text}


def _resource_part(kind: str, resource_id: str) -> dict[str, Any]:
    return {"kind": kind, "resource_id": resource_id}


def _is_tool_output_part_list(value: Any) -> bool:
    return isinstance(value, list) and all(_is_tool_output_part_like(item) for item in value)


def _is_tool_output_part_like(value: Any) -> bool:
    return isinstance(value, dict) and value.get("kind") in _TOOL_OUTPUT_KINDS


def _normalize_tool_output_part(value: dict[str, Any]) -> dict[str, Any]:
    kind = value.get("kind")
    if kind == _TOOL_OUTPUT_TEXT:
        return _text_part(str(value.get("text") or ""))
    if kind in (_TOOL_OUTPUT_IMAGE, _TOOL_OUTPUT_PDF):
        resource_id = str(value.get("resource_id") or "").strip()
        url = str(value.get("url") or "").strip()
        path = str(value.get("path") or "").strip()
        mime_type = str(value.get("mime_type") or "").strip()
        if resource_id:
            part = _resource_part(str(kind), resource_id)
        elif url:
            part = {"kind": str(kind), "url": url}
        elif path:
            part = {"kind": str(kind), "path": path}
        else:
            part = _resource_part(str(kind), "")
        if mime_type:
            part["mime_type"] = mime_type
        return part
    return _text_part(_stable_json_text(value))


def _stable_json_text(value: Any) -> str:
    try:
        return json.dumps(_jsonable_value(value), ensure_ascii=False, indent=2)
    except TypeError:
        return str(value)


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, (TextPart, ImagePart, PdfPart)):
        return _part_object_to_output(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable_value(item_value) for key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_value(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:
            return str(value)
    return value


def _tool_output_open_marker(
    kind: str,
    item: dict[str, Any],
    resource: ResolvedResource,
) -> str | None:
    if resource.object_storage_url:
        return multimodal_file_open_marker(kind, resource.object_storage_url)
    raw_path = item.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        return multimodal_file_open_marker(kind, path=raw_path.strip())
    return None


async def _try_resolve_output_resource(resolver: ResourceResolver, item: dict[str, Any]) -> ResolvedResource | None:
    resource_id = item.get("resource_id")
    if isinstance(resource_id, str) and resource_id.strip():
        return await _try_resolve_resource(resolver, resource_id.strip())
    kind = str(item.get("kind") or _TOOL_OUTPUT_IMAGE)
    raw_url = item.get("url")
    if isinstance(raw_url, str) and raw_url.strip():
        url = raw_url.strip()
        mime_type = _infer_mime_type(
            kind=kind,
            declared_mime_type=item.get("mime_type"),
            url_or_path=urllib.parse.urlparse(url).path,
            content=None,
        )
        return ResolvedResource(
            resource_id="",
            mime_type=mime_type,
            resource_category=kind,
            object_storage_url=url,
            base64_content=None,
            local_storage_path=None,
        )
    raw_path = item.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        return _try_resolve_local_file_resource(
            path_str=raw_path.strip(),
            kind=kind,
            declared_mime_type=item.get("mime_type"),
        )
    return None


def _try_resolve_local_file_resource(
    *,
    path_str: str,
    kind: str,
    declared_mime_type: Any,
) -> ResolvedResource | None:
    try:
        file_path = Path(path_str).expanduser()
        if not file_path.is_file():
            return None
        content = file_path.read_bytes()
        if not content:
            return None
        mime_type = _infer_mime_type(
            kind=kind,
            declared_mime_type=declared_mime_type,
            url_or_path=str(file_path),
            content=content,
        )
        return ResolvedResource(
            resource_id="",
            mime_type=mime_type,
            resource_category=kind,
            object_storage_url=None,
            base64_content=base64.b64encode(content).decode("ascii"),
            local_storage_path=str(file_path.resolve()),
        )
    except Exception:
        return None


def _infer_mime_type(
    *,
    kind: str,
    declared_mime_type: Any,
    url_or_path: str | None,
    content: bytes | None,
) -> str:
    if isinstance(declared_mime_type, str) and declared_mime_type.strip():
        return declared_mime_type.strip().lower()
    if content:
        magic_mime = _detect_magic_mime_type(content)
        if magic_mime:
            return magic_mime
    if url_or_path:
        guessed, _ = mimetypes.guess_type(url_or_path)
        if guessed:
            return guessed.lower()
    return "application/pdf" if kind == _TOOL_OUTPUT_PDF else "image/png"


def _detect_magic_mime_type(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    return None


async def _try_resolve_resource(resolver: ResourceResolver, resource_id: str) -> ResolvedResource | None:
    try:
        return await resolve_resource_access(resolver, resource_id)
    except Exception:
        return None


def _resource_locator(resource: ResolvedResource) -> str | None:
    return resource.object_storage_url or resource.local_storage_path


def _try_claude_resource_source(resource: ResolvedResource, *, default_mime: str | None = None) -> dict[str, Any] | None:
    if resource.object_storage_url:
        return {"type": "url", "url": resource.object_storage_url}
    data = resource.base64_content
    if not data:
        return None
    return {
        "type": "base64",
        "media_type": resource.mime_type or default_mime,
        "data": data,
    }


def _try_gemini_media_part(resource: ResolvedResource, *, default_mime: str | None = None) -> dict[str, Any] | None:
    mime_type = resource.mime_type or default_mime
    if resource.object_storage_url:
        return {
            "fileData": {
                "mimeType": mime_type,
                "fileUri": resource.object_storage_url,
            }
        }
    data = resource.base64_content
    if not data:
        return None
    return {
        "inlineData": {
            "mimeType": mime_type,
            "data": data,
        }
    }

