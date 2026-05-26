# -*- coding: utf-8 -*-
"""Agent 执行循环内部工具调度器。"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from agent_runtime.agent_definition.agent_definition import AgentDefinition, McpToolBinding
from agent_runtime.communication.runtime_event_publisher import (
    NoopRuntimeEventPublisher,
    RuntimeCommunicationContext,
    RuntimeEventPublisher,
)
from agent_runtime.conversation_message.message.models import ImagePart, PdfPart, TextPart, ToolResultPart, ToolUsePart
from agent_runtime.file_resource.north_gateway.resource_create_contracts import Base64CreateRequest
from agent_runtime.file_resource.shared_service import get_file_resource_service
from agent_runtime.llm_api_protocol import (
    API_FORMAT_ANTHROPIC_MESSAGES,
    API_FORMAT_GEMINI_GENERATE_CONTENT,
    API_FORMAT_OPENAI_CHAT_COMPLETIONS,
    API_FORMAT_OPENAI_RESPONSES,
    API_FORMAT_ZHIPU_CHAT_COMPLETIONS,
)
from agent_runtime.llm_client.llm_request_models import LlmToolDefinition
from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_contracts import McpClientGateway
from agent_runtime.tool.north_gateway.local_tool_contracts import (
    LocalToolGateway,
    ToolExecuteRequest,
)
from agent_runtime.tool.tool_execution_models import (
    BaseToolResult,
    LoopControl,
    build_tool_error_result,
)


AGENT_MCP_TOOL_NAME_PREFIX = "mcp__"
logger = logging.getLogger(__name__)
TOOL_RESULT_PREVIEW_MAX_STRING_LENGTH = 4000
TOOL_RESULT_PREVIEW_MAX_LIST_ITEMS = 20
TOOL_RESULT_PREVIEW_MAX_DICT_ITEMS = 40
TOOL_RESULT_PREVIEW_MAX_DEPTH = 4
_MCP_TEXT_BLOCK = "text"
_MCP_IMAGE_BLOCK = "image"
_MCP_AUDIO_BLOCK = "audio"
_MCP_RESOURCE_LINK_BLOCK = "resource_link"
_MCP_EMBEDDED_RESOURCE_BLOCK = "resource"


@dataclass(frozen=True, slots=True)
class ToolDispatchResult:
    """单次工具调用调度结果。"""

    # LLM 返回的工具名称。
    tool_name: str

    # 工具来源：local 或 mcp。
    source: str

    # 写回 conversation_message 的工具结果内容块。
    result_part: ToolResultPart

    # 工具返回的统一执行结果，Runtime 根据其中的 loop_control 控制下一轮循环。
    tool_result: BaseToolResult


class AgentToolDispatcher:
    """Agent 内部工具调度器。

    调度器只暴露当前 AgentDefinition 明确允许的本地工具和 MCP 工具。LLM 请求前，
    build_llm_tools() 生成可传给对应 SDK 平台的工具 schema；LLM 返回工具调用后，
    dispatch_tool_call() 再次执行 allowlist 校验并调用本地工具或 MCP 工具。
    """

    def __init__(
        self,
        *,
        local_tool_gateway: LocalToolGateway,
        mcp_client_gateway: McpClientGateway | None = None,
        event_publisher: RuntimeEventPublisher | None = None,
    ) -> None:
        # 本地工具北向网关。用于读取本地工具 schema 和执行本地工具函数。
        self._local_tool_gateway = local_tool_gateway

        # MCP 客户端北向网关。AgentDefinition 声明 mcp_tools 时必须配置该网关。
        self._mcp_client_gateway = mcp_client_gateway

        # 运行时事件发布器。用于把工具调用生命周期推送到通用通信流。
        self._event_publisher = event_publisher or NoopRuntimeEventPublisher()

    async def build_llm_tools(
        self,
        *,
        agent_definition: AgentDefinition,
        api_format: str,
    ) -> tuple[LlmToolDefinition, ...]:
        """根据 AgentDefinition 能力边界构建本轮 LLM 可见工具定义。"""
        tools: list[LlmToolDefinition] = []
        for tool_name in self._visible_local_tool_names(agent_definition):
            detail = self._local_tool_gateway.get_tool(tool_name)
            schema = detail.schemas.get(api_format)
            if schema is None:
                raise ValueError(f"本地工具 {tool_name} 缺少 {api_format} 协议格式 schema。")
            tools.append(LlmToolDefinition(name=tool_name, schema=schema, source="local"))

        if agent_definition.mcp_tools:
            if self._mcp_client_gateway is None:
                raise RuntimeError("当前 Agent 定义了 MCP 工具，但执行器未配置 McpClientGateway。")
            for binding in agent_definition.mcp_tools:
                details = await self._mcp_client_gateway.get_tool_detail(
                    binding.service_id,
                    list(binding.tool_ids),
                )
                detail_map = {detail.name: detail for detail in details}
                for tool_name in binding.tool_ids:
                    detail = detail_map.get(tool_name)
                    if detail is None:
                        raise KeyError(f"MCP 工具不存在: {binding.service_id}/{tool_name}")
                    exposed_name = build_mcp_exposed_tool_name(binding.service_id, tool_name)
                    schema = _build_mcp_tool_schema(
                        api_format=api_format,
                        exposed_name=exposed_name,
                        description=detail.description,
                        input_schema=detail.input_schema,
                    )
                    tools.append(
                        LlmToolDefinition(
                            name=exposed_name,
                            schema=schema,
                            source="mcp",
                            mcp_service_id=binding.service_id,
                            mcp_tool_name=tool_name,
                        )
                    )
        return tuple(tools)

    async def dispatch_tool_call(
        self,
        *,
        agent_definition: AgentDefinition,
        tool_use: ToolUsePart,
        communication_context: RuntimeCommunicationContext | None = None,
    ) -> ToolDispatchResult:
        """执行 LLM 返回的一次工具调用，并返回可写入会话的 ToolResultPart。"""
        source = "mcp" if tool_use.name.startswith(AGENT_MCP_TOOL_NAME_PREFIX) else "local"
        tool_call_id = _tool_use_ref(tool_use)
        started_at = time.perf_counter()
        logger.info(
            "Agent 工具调用开始: tool=%s, source=%s, tool_call_id=%s",
            tool_use.name,
            source,
            tool_call_id,
        )
        await self._event_publisher.publish(
            communication_context,
            "tool.call.started",
            {
                "tool_call_id": tool_call_id,
                "tool_name": tool_use.name,
                "title": tool_use.name,
                "source": source,
                "tool_source": source,
                "input": tool_use.input,
                "arguments_preview": _preview_arguments(tool_use.input),
            },
            source="tool_dispatcher",
            correlation_id=tool_call_id,
        )
        try:
            logger.info(
                "Agent 工具调用进入执行: tool=%s, source=%s, tool_call_id=%s",
                tool_use.name,
                source,
                tool_call_id,
            )
            if source == "mcp":
                result = await self._dispatch_mcp_tool(agent_definition, tool_use)
            else:
                result = await self._dispatch_local_tool(agent_definition, tool_use, communication_context)
        except Exception as exc:
            logger.exception(
                "Agent 工具调用失败: tool=%s, source=%s, tool_call_id=%s, 耗时=%sms",
                tool_use.name,
                source,
                tool_call_id,
                int((time.perf_counter() - started_at) * 1000),
            )
            await self._event_publisher.publish(
                communication_context,
                "tool.call.failed",
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_use.name,
                    "source": source,
                    "tool_source": source,
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                    "duration_ms": int((time.perf_counter() - started_at) * 1000),
                    "result_preview": {
                        "success": False,
                        "execution_status": "failed",
                        "error_type": exc.__class__.__name__,
                        "error_message": str(exc),
                    },
                },
                source="tool_dispatcher",
                correlation_id=tool_call_id,
            )
            raise
        logger.info(
            "Agent 工具调用完成: tool=%s, source=%s, tool_call_id=%s, success=%s, execution_status=%s, loop_decision=%s, 耗时=%sms",
            result.tool_name,
            result.source,
            tool_call_id,
            result.tool_result.success,
            result.tool_result.execution_status,
            result.tool_result.loop_decision,
            int((time.perf_counter() - started_at) * 1000),
        )
        await self._event_publisher.publish(
            communication_context,
            "tool.call.completed",
            {
                "tool_call_id": tool_call_id,
                "tool_name": result.tool_name,
                "source": result.source,
                "tool_source": result.source,
                "success": result.tool_result.success,
                "execution_status": result.tool_result.execution_status,
                "loop_decision": result.tool_result.loop_decision,
                "error_code": result.tool_result.error_code,
                "error_message": result.tool_result.error_message,
                "duration_ms": int((time.perf_counter() - started_at) * 1000),
                "result_preview": _preview_tool_result(result.tool_result),
            },
            source="tool_dispatcher",
            correlation_id=tool_call_id,
        )
        return result

    async def _dispatch_local_tool(
        self,
        agent_definition: AgentDefinition,
        tool_use: ToolUsePart,
        communication_context: RuntimeCommunicationContext | None,
    ) -> ToolDispatchResult:
        """执行本地工具调用。"""
        detail = self._local_tool_gateway.get_tool(tool_use.name)
        if tool_use.name not in agent_definition.tool_ids and not detail.always_enabled:
            raise PermissionError(f"Agent {agent_definition.agent_id} 无权调用本地工具: {tool_use.name}")
        params = _ensure_dict_arguments(tool_use.input)
        metadata = communication_context.metadata if communication_context is not None else {}
        result = await self._local_tool_gateway.execute_tool(
            ToolExecuteRequest(
                tool_name=tool_use.name,
                params=params,
                request_id=_tool_use_ref(tool_use),
                task_id=metadata.get("task_id"),
                agent_instance_id=metadata.get("agent_instance_id"),
                trace_id=communication_context.trace_id if communication_context is not None else None,
                communication_stream_id=(
                    communication_context.stream_id if communication_context is not None else None
                ),
            ),
        )
        return ToolDispatchResult(
            tool_name=tool_use.name,
            source="local",
            result_part=ToolResultPart(
                tool_use_ref=_tool_use_ref(tool_use),
                is_error=not result.success,
                output=_extract_llm_visible_content(result),
            ),
            tool_result=result,
        )

    async def _dispatch_mcp_tool(
        self,
        agent_definition: AgentDefinition,
        tool_use: ToolUsePart,
    ) -> ToolDispatchResult:
        """执行 MCP 工具调用。"""
        if self._mcp_client_gateway is None:
            raise RuntimeError("当前执行器未配置 McpClientGateway，不能执行 MCP 工具。")
        service_id, tool_name = parse_mcp_exposed_tool_name(tool_use.name)
        _ensure_mcp_tool_allowed(agent_definition.mcp_tools, service_id, tool_name)
        params = _ensure_dict_arguments(tool_use.input)
        result = await self._mcp_client_gateway.call_tool(service_id, tool_name, params)
        output = _normalize_mcp_tool_content_for_llm(result.content_blocks, fallback_text=result.content)
        if result.is_error:
            tool_result = build_tool_error_result(
                error_code="McpToolError",
                error_message=result.content,
                output=output,
            )
        else:
            tool_result = BaseToolResult(
                success=True,
                execution_status="success",
                output=output,
                loop_control=LoopControl(decision="continue"),
            )
        return ToolDispatchResult(
            tool_name=tool_use.name,
            source="mcp",
            result_part=ToolResultPart(
                tool_use_ref=_tool_use_ref(tool_use),
                is_error=not tool_result.success,
                output=_extract_llm_visible_content(tool_result),
            ),
            tool_result=tool_result,
        )

    def _visible_local_tool_names(self, agent_definition: AgentDefinition) -> tuple[str, ...]:
        """返回当前 Agent 可见的本地工具名，包含显式授权和默认启用工具。"""
        explicit_tool_names = tuple(agent_definition.tool_ids)
        visible_tool_names = list(explicit_tool_names)
        all_tools = self._local_tool_gateway.list_tools(public_only=False, group_by=False)
        for item in all_tools:
            if item.always_enabled and item.tool_name not in visible_tool_names:
                visible_tool_names.append(item.tool_name)
        return tuple(visible_tool_names)


def build_mcp_exposed_tool_name(service_id: str, tool_name: str) -> str:
    """构造暴露给 LLM 的 MCP 工具名，避免和本地工具重名。"""
    return f"{AGENT_MCP_TOOL_NAME_PREFIX}{service_id}__{tool_name}"


def parse_mcp_exposed_tool_name(exposed_name: str) -> tuple[str, str]:
    """从 LLM 返回的 MCP 暴露工具名还原 service_id 与 tool_name。"""
    if not exposed_name.startswith(AGENT_MCP_TOOL_NAME_PREFIX):
        raise ValueError(f"不是 MCP 暴露工具名: {exposed_name}")
    payload = exposed_name[len(AGENT_MCP_TOOL_NAME_PREFIX):]
    service_id, separator, tool_name = payload.partition("__")
    if not separator or not service_id or not tool_name:
        raise ValueError(f"MCP 暴露工具名格式错误: {exposed_name}")
    return service_id, tool_name


def _build_mcp_tool_schema(
    *,
    api_format: str,
    exposed_name: str,
    description: str | None,
    input_schema: dict[str, Any],
) -> dict[str, Any]:
    """把 MCP inputSchema 转为目标协议格式的工具 schema。"""
    parameters = input_schema or {"type": "object", "properties": {}}
    if api_format == API_FORMAT_OPENAI_RESPONSES:
        return {
            "type": "function",
            "name": exposed_name,
            "description": description or "",
            "parameters": parameters,
            "strict": False,
        }
    if api_format == API_FORMAT_OPENAI_CHAT_COMPLETIONS:
        return {
            "type": "function",
            "function": {
                "name": exposed_name,
                "description": description or "",
                "parameters": parameters,
            },
        }
    if api_format == API_FORMAT_ANTHROPIC_MESSAGES:
        return {
            "name": exposed_name,
            "description": description or "",
            "input_schema": parameters,
        }
    if api_format == API_FORMAT_GEMINI_GENERATE_CONTENT:
        return {
            "name": exposed_name,
            "description": description or "",
            "parameters": parameters,
        }
    if api_format == API_FORMAT_ZHIPU_CHAT_COMPLETIONS:
        return {
            "type": "function",
            "function": {
                "name": exposed_name,
                "description": description or "",
                "parameters": parameters,
            },
        }
    raise ValueError(f"当前工具调度器不支持 api_format={api_format} 的工具 schema。")


def _ensure_mcp_tool_allowed(bindings: tuple[McpToolBinding, ...], service_id: str, tool_name: str) -> None:
    """校验当前 AgentDefinition 是否允许调用指定 MCP 工具。"""
    for binding in bindings:
        if binding.service_id == service_id and tool_name in binding.tool_ids:
            return
    raise PermissionError(f"当前 Agent 无权调用 MCP 工具: {service_id}/{tool_name}")


def _ensure_dict_arguments(value: Any) -> dict[str, Any]:
    """把 LLM 返回的工具参数规范为字典。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"input": value}
        if isinstance(parsed, dict):
            return parsed
        return {"input": parsed}
    if value is None:
        return {}
    return {"input": value}


def _tool_use_ref(tool_use: ToolUsePart) -> str:
    """确定工具结果要关联的工具调用引用。"""
    return tool_use.tool_use_id or tool_use.name


def _extract_llm_visible_content(result: BaseToolResult) -> Any:
    """从 BaseToolResult 中提取 LLM 可见的业务输出。

    Runtime 内部控制字段（loop_control / frontend_message / metadata 等）不暴露给 LLM。
    成功时返回业务输出 result.output；失败时返回错误信息文本（必要时附带 partial output）。
    """
    if result.success:
        return result.output
    error_text = f"工具执行失败：{result.error_message or 'Tool execution failed.'}"
    if result.output is None:
        return [TextPart(text=error_text)]
    if isinstance(result.output, str):
        return [TextPart(text=f"{error_text}\n\n{result.output}")]
    if _is_unipart_list(result.output):
        return [TextPart(text=error_text), *result.output]
    return [TextPart(text=f"{error_text}\n\n{_stable_json_text(result.output)}")]


def _normalize_mcp_tool_content_for_llm(
    content_blocks: tuple[dict[str, Any], ...],
    *,
    fallback_text: str,
) -> list[TextPart | ImagePart | PdfPart]:
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
        return parts
    return [TextPart(text=fallback_text or "MCP 工具执行完成，但没有返回可见内容。")]


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


def _is_unipart_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, (TextPart, ImagePart, PdfPart)) for item in value)


def _stable_json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        return str(value)


def _preview_tool_result(result: BaseToolResult) -> dict[str, Any]:
    preview: dict[str, Any] = {
        "success": result.success,
        "execution_status": result.execution_status,
        "loop_decision": result.loop_decision,
        "output": _preview_value(result.output),
    }
    ui_kind = result.metadata.get("ui_kind")
    if ui_kind:
        preview["ui_kind"] = ui_kind
    if "preview_output" in result.metadata:
        preview["output"] = _preview_value(result.metadata["preview_output"])
    if result.frontend_message is not None:
        preview["frontend_message"] = _preview_value(result.frontend_message.model_dump(mode="json"))
    if result.error_code:
        preview["error_code"] = result.error_code
    if result.error_message:
        preview["error_message"] = result.error_message
    return preview


def _preview_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= TOOL_RESULT_PREVIEW_MAX_DEPTH:
        return "<truncated: max depth reached>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= TOOL_RESULT_PREVIEW_MAX_STRING_LENGTH:
            return value
        return f"{value[:TOOL_RESULT_PREVIEW_MAX_STRING_LENGTH]}...<truncated {len(value) - TOOL_RESULT_PREVIEW_MAX_STRING_LENGTH} chars>"
    if isinstance(value, dict):
        items = list(value.items())
        preview = {
            str(key): _preview_value(item_value, depth=depth + 1)
            for key, item_value in items[:TOOL_RESULT_PREVIEW_MAX_DICT_ITEMS]
        }
        if len(items) > TOOL_RESULT_PREVIEW_MAX_DICT_ITEMS:
            preview["<truncated>"] = f"{len(items) - TOOL_RESULT_PREVIEW_MAX_DICT_ITEMS} more keys"
        return preview
    if isinstance(value, (list, tuple)):
        preview_items = [_preview_value(item, depth=depth + 1) for item in value[:TOOL_RESULT_PREVIEW_MAX_LIST_ITEMS]]
        if len(value) > TOOL_RESULT_PREVIEW_MAX_LIST_ITEMS:
            preview_items.append(f"<truncated {len(value) - TOOL_RESULT_PREVIEW_MAX_LIST_ITEMS} more items>")
        return preview_items
    if isinstance(value, BaseToolResult):
        return _preview_value(value.to_serializable_dict(), depth=depth + 1)
    if hasattr(value, "model_dump"):
        try:
            return _preview_value(value.model_dump(mode="json"), depth=depth + 1)
        except Exception:
            return str(value)
    return str(value)


def _preview_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {"input": value}
