# -*- coding: utf-8 -*-
"""Agent 执行循环内部工具调度器。"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from agent_runtime.agent.execution_loop_logger import (
    AgentExecutionLoopLogger,
    NoopAgentExecutionLoopLogger,
)
from agent_runtime.agent_definition.agent_definition import AgentDefinition, McpToolBinding
from agent_runtime.communication.runtime_event_publisher import (
    NoopRuntimeEventPublisher,
    RuntimeCommunicationContext,
    RuntimeEventPublisher,
)
from agent_runtime.conversation_message.message.models import ImagePart, PdfPart, TextPart, ToolResultPart, ToolUsePart
from agent_runtime.conversation_message.message.tool_result_output import normalize_tool_result_output
from agent_runtime.llm_api_protocol import (
    API_FORMAT_ANTHROPIC_MESSAGES,
    API_FORMAT_GEMINI_GENERATE_CONTENT,
    API_FORMAT_OPENAI_CHAT_COMPLETIONS,
    API_FORMAT_OPENAI_RESPONSES,
    API_FORMAT_ZHIPU_CHAT_COMPLETIONS,
)
from agent_runtime.llm_client.llm_request_models import LlmToolDefinition
from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_contracts import McpClientGateway
from agent_runtime.mcp.mcp_client.tool_result_normalizer import normalize_mcp_tool_content_for_llm
from agent_runtime.tool.local_tool_definition import is_tool_default_enabled_category
from agent_runtime.tool.north_gateway.local_tool_contracts import (
    LocalToolGateway,
    ToolExecuteRequest,
)
from agent_runtime.task_note.task_note_prompt_provider import TASK_NOTE_TOOL_NAMES
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

    调度器只暴露当前 AgentDefinition 明确允许的本地工具和 always-enabled 本地工具。
    AgentDefinition.mcp_tools 只用于系统提示词中的 MCP 服务与工具摘要，不会被展开为 LLM tools；
    远端 MCP 服务通过 mcp_get_service_detail / mcp_get_tool_detail / mcp_call_tool 等本地代理工具访问。
    """

    def __init__(
        self,
        *,
        local_tool_gateway: LocalToolGateway,
        mcp_client_gateway: McpClientGateway | None = None,
        event_publisher: RuntimeEventPublisher | None = None,
        execution_loop_logger: AgentExecutionLoopLogger | NoopAgentExecutionLoopLogger | None = None,
    ) -> None:
        # 本地工具北向网关。用于读取本地工具 schema 和执行本地工具函数。
        self._local_tool_gateway = local_tool_gateway

        # MCP 客户端北向网关。MCP 元工具和兼容性 MCP 分发路径通过该网关访问远端服务。
        self._mcp_client_gateway = mcp_client_gateway

        # 运行时事件发布器。用于把工具调用生命周期推送到通用通信流。
        self._event_publisher = event_publisher or NoopRuntimeEventPublisher()
        self._execution_loop_logger = execution_loop_logger or NoopAgentExecutionLoopLogger()

    async def build_llm_tools(
        self,
        *,
        agent_definition: AgentDefinition,
        api_format: str,
    ) -> tuple[LlmToolDefinition, ...]:
        """根据 AgentDefinition 本地工具能力边界构建本轮 LLM 可见工具定义。"""
        tools: list[LlmToolDefinition] = []
        for tool_name in self._visible_local_tool_names(agent_definition):
            detail = self._local_tool_gateway.get_tool(tool_name)
            schema = detail.schemas.get(api_format)
            if schema is None:
                raise ValueError(f"本地工具 {tool_name} 缺少 {api_format} 协议格式 schema。")
            tools.append(LlmToolDefinition(name=tool_name, schema=schema, source="local"))

        # AgentDefinition.mcp_tools 只用于系统提示词中的 MCP 服务/工具摘要。
        # 为防止 LLM 请求参数 tools 膨胀，远端 MCP 工具不再直接展开为 LlmToolDefinition；
        # Agent 调用 MCP 服务时应使用本地 MCP 代理工具 mcp_get_service_detail /
        # mcp_get_tool_detail / mcp_call_tool 等。
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
        metadata = communication_context.metadata if communication_context is not None else {}
        instance_id = str(metadata.get("agent_instance_id") or "")
        if instance_id:
            self._execution_loop_logger.record_step(
                instance_id=instance_id,
                stage="tool_dispatch.routing",
                status="started",
                summary="工具调度器已收到一次 LLM 工具调用，准备按来源路由到 local 或 MCP。",
                details={
                    "task_id": metadata.get("task_id"),
                    "conversation_id": metadata.get("conversation_id"),
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_use.name,
                    "source": source,
                    "arguments_preview": _preview_arguments(tool_use.input),
                },
                expected_next="tool_dispatch.execute",
            )
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
                result = await self._dispatch_mcp_tool(agent_definition, tool_use, communication_context)
            else:
                result = await self._dispatch_local_tool(agent_definition, tool_use, communication_context)
        except Exception as exc:
            if instance_id:
                self._execution_loop_logger.record_step(
                    instance_id=instance_id,
                    stage="tool_dispatch.execute",
                    status="failed",
                    summary="工具调度器执行工具调用失败。",
                    details={
                        "task_id": metadata.get("task_id"),
                        "conversation_id": metadata.get("conversation_id"),
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_use.name,
                        "source": source,
                        "error_type": exc.__class__.__name__,
                        "error_message": str(exc),
                        "duration_ms": int((time.perf_counter() - started_at) * 1000),
                    },
                    expected_next="engine_build_exception_tool_result",
                )
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
                        "error_type": exc.__class__.__name__,
                        "error_message": str(exc),
                    },
                },
                source="tool_dispatcher",
                correlation_id=tool_call_id,
            )
            raise
        if instance_id:
            self._execution_loop_logger.record_step(
                instance_id=instance_id,
                stage="tool_dispatch.execute",
                status="completed",
                summary="工具调度器已完成工具调用并得到统一工具结果。",
                details={
                    "task_id": metadata.get("task_id"),
                    "conversation_id": metadata.get("conversation_id"),
                    "tool_call_id": tool_call_id,
                    "tool_name": result.tool_name,
                    "source": result.source,
                    "success": result.tool_result.success,
                    "execution_status": result.tool_result.execution_status,
                    "loop_decision": result.tool_result.loop_decision,
                    "duration_ms": int((time.perf_counter() - started_at) * 1000),
                    "result_preview": _preview_tool_result(result.tool_result),
                },
                expected_next="tool_result_part.persist_pending",
            )
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
                "ui_preview": _preview_tool_ui(result.tool_result),
            },
            source="tool_dispatcher",
            correlation_id=tool_call_id,
        )
        is_task_note_mutating = (
            result.tool_name in TASK_NOTE_TOOL_NAMES
            or result.tool_name in (
                "file_create",
                "file_modify",
                "file_delete",
                "file_move",
                "terminal_execute_command",
                "create_subagent",
                "send_agent_message",
                "end_subagent",
            )
        )
        if result.tool_result.success and is_task_note_mutating and communication_context is not None:
            try:
                task_id = metadata.get("task_id")
                caller_instance_id = metadata.get("agent_instance_id") or instance_id
                scene_id = metadata.get("scene_id")
                target_instances: list[str] = [str(caller_instance_id)] if caller_instance_id else []

                tool_output = result.tool_result.output if isinstance(result.tool_result.output, dict) else {}
                if result.tool_name == "create_subagent":
                    child_id = tool_output.get("child_agent_instance_id")
                    if child_id and str(child_id) not in target_instances:
                        target_instances.append(str(child_id))
                elif result.tool_name == "send_agent_message":
                    target_id = tool_output.get("target_agent_instance_id")
                    if target_id and str(target_id) not in target_instances:
                        target_instances.append(str(target_id))
                elif result.tool_name == "end_subagent":
                    released_ids = tool_output.get("released_agent_instance_ids")
                    if isinstance(released_ids, (list, tuple)):
                        for rid in released_ids:
                            if rid and str(rid) not in target_instances:
                                target_instances.append(str(rid))

                now_iso = datetime.now(timezone.utc).isoformat()
                for inst_id in target_instances:
                    await self._event_publisher.publish(
                        communication_context,
                        "task_note.updated",
                        {
                            "task_id": task_id,
                            "agent_instance_id": inst_id,
                            "scene_id": scene_id,
                            "tool_name": result.tool_name,
                            "updated_at": now_iso,
                        },
                        source="task_note",
                        correlation_id=tool_call_id,
                    )
            except Exception as e:
                logger.warning("发布 task_note.updated 事件失败 (已忽略): %s", e)

        return result

    async def _dispatch_local_tool(
        self,
        agent_definition: AgentDefinition,
        tool_use: ToolUsePart,
        communication_context: RuntimeCommunicationContext | None,
    ) -> ToolDispatchResult:
        """执行本地工具调用。"""
        detail = self._local_tool_gateway.get_tool(tool_use.name)
        metadata = communication_context.metadata if communication_context is not None else {}
        instance_id = str(metadata.get("agent_instance_id") or "")
        default_enabled = is_tool_default_enabled_category(detail.category)
        is_allowed = tool_use.name in agent_definition.tool_ids or default_enabled
        if instance_id:
            self._execution_loop_logger.record_step(
                instance_id=instance_id,
                stage="tool_dispatch.local.allowlist",
                status="allowed" if is_allowed else "denied",
                summary="本地工具 allowlist 校验完成。",
                details={
                    "task_id": metadata.get("task_id"),
                    "conversation_id": metadata.get("conversation_id"),
                    "tool_call_id": _tool_use_ref(tool_use),
                    "tool_name": tool_use.name,
                    "agent_definition_id": agent_definition.agent_id,
                    "declared_in_agent_tool_ids": tool_use.name in agent_definition.tool_ids,
                    "category": detail.category,
                    "default_enabled_by_category": default_enabled,
                    "always_enabled": detail.always_enabled,
                },
                expected_next="local_tool_gateway.execute" if is_allowed else "tool_dispatch.denied",
            )
        if not is_allowed:
            raise PermissionError(f"Agent {agent_definition.agent_id} 无权调用本地工具: {tool_use.name}")
        params = _ensure_dict_arguments(tool_use.input)
        result = await self._local_tool_gateway.execute_tool(
            ToolExecuteRequest(
                tool_name=tool_use.name,
                params=params,
                request_id=_tool_use_ref(tool_use),
                task_id=metadata.get("task_id"),
                agent_instance_id=metadata.get("agent_instance_id"),
                scene_id=metadata.get("scene_id"),
                conversation_id=metadata.get("conversation_id"),
                conversation_history_copy_message_count=metadata.get("conversation_history_copy_message_count"),
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
        communication_context: RuntimeCommunicationContext | None,
    ) -> ToolDispatchResult:
        """执行 MCP 工具调用。"""
        if self._mcp_client_gateway is None:
            raise RuntimeError("当前执行器未配置 McpClientGateway，不能执行 MCP 工具。")
        service_id, tool_name = parse_mcp_exposed_tool_name(tool_use.name)
        metadata = communication_context.metadata if communication_context is not None else {}
        instance_id = str(metadata.get("agent_instance_id") or "")
        try:
            _ensure_mcp_tool_allowed(agent_definition.mcp_tools, service_id, tool_name)
        except Exception as exc:
            if instance_id:
                self._execution_loop_logger.record_step(
                    instance_id=instance_id,
                    stage="tool_dispatch.mcp.allowlist",
                    status="denied",
                    summary="MCP 工具 allowlist 校验失败。",
                    details={
                        "task_id": metadata.get("task_id"),
                        "conversation_id": metadata.get("conversation_id"),
                        "tool_call_id": _tool_use_ref(tool_use),
                        "exposed_tool_name": tool_use.name,
                        "service_id": service_id,
                        "tool_name": tool_name,
                        "agent_definition_id": agent_definition.agent_id,
                        "error_type": exc.__class__.__name__,
                        "error_message": str(exc),
                    },
                    expected_next="tool_dispatch.denied",
                )
            raise
        if instance_id:
            self._execution_loop_logger.record_step(
                instance_id=instance_id,
                stage="tool_dispatch.mcp.allowlist",
                status="allowed",
                summary="MCP 工具 allowlist 校验通过，准备调用 MCP 客户端网关。",
                details={
                    "task_id": metadata.get("task_id"),
                    "conversation_id": metadata.get("conversation_id"),
                    "tool_call_id": _tool_use_ref(tool_use),
                    "exposed_tool_name": tool_use.name,
                    "service_id": service_id,
                    "tool_name": tool_name,
                    "agent_definition_id": agent_definition.agent_id,
                },
                expected_next="mcp_client.call_tool",
            )
        params = _ensure_dict_arguments(tool_use.input)
        result = await self._mcp_client_gateway.call_tool(service_id, tool_name, params)
        output = normalize_mcp_tool_content_for_llm(result.content_blocks, fallback_text=result.content)
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
            if is_tool_default_enabled_category(item.category) and item.tool_name not in visible_tool_names:
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
        return normalize_tool_result_output(result.output)
    error_text = f"工具执行失败：{result.error_message or 'Tool execution failed.'}"
    if result.output is None:
        return normalize_tool_result_output(error_text)
    if isinstance(result.output, str):
        return normalize_tool_result_output(f"{error_text}\n\n{result.output}")
    if _is_unipart_list(result.output):
        return normalize_tool_result_output([TextPart(text=error_text), *result.output])
    return normalize_tool_result_output(f"{error_text}\n\n{_stable_json_text(result.output)}")


def _is_unipart_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, (TextPart, ImagePart, PdfPart)) for item in value)


def _stable_json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        return str(value)


def _preview_tool_result(result: BaseToolResult) -> Any:
    if result.output is not None:
        return _preview_value(result.output)
    if result.error_message:
        return {"error_message": result.error_message}
    return {}


def _preview_tool_ui(result: BaseToolResult) -> dict[str, Any] | None:
    preview: dict[str, Any] = {}
    ui_kind = result.metadata.get("ui_kind")
    if ui_kind:
        preview["ui_kind"] = ui_kind
    if "preview_output" in result.metadata:
        preview["output"] = _preview_value(result.metadata["preview_output"])
    if isinstance(result.metadata.get("html_content"), str):
        preview["html_content"] = result.metadata["html_content"]
    if result.frontend_message is not None:
        preview["frontend_message"] = _preview_value(result.frontend_message.model_dump(mode="json"))
    return preview or None


def _preview_value(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= TOOL_RESULT_PREVIEW_MAX_STRING_LENGTH:
            return value
        return f"{value[:TOOL_RESULT_PREVIEW_MAX_STRING_LENGTH]}...<truncated {len(value) - TOOL_RESULT_PREVIEW_MAX_STRING_LENGTH} chars>"
    if depth >= TOOL_RESULT_PREVIEW_MAX_DEPTH:
        return "<truncated: max depth reached>"
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
    if is_dataclass(value) and not isinstance(value, type):
        return _preview_value(asdict(value), depth=depth + 1)
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
