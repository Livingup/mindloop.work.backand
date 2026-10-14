"""OpenAI Responses API 协议适配器。

该模块实现 OpenAI Responses API 的 ProviderAdapter，负责：
- 归一化：将 OpenAI 原始响应 → UniMessage（公共语义层 + providerExtras.raw）
- 渲染：将 Conversation.messages[] → OpenAI Responses stateless input
- 状态更新：API 成功后记录 stateless 模式和 prompt_cache_key

渲染决策树：
- user 消息：通过 ResourceResolver 将 resourceId → data URI / input_file
- assistant 消息（同平台）：展开 output items 到顶层 input（含 reasoning / message / function_call）
- assistant 消息（跨平台）：合成一条 assistant message item（降级）
- tool 消息：展开为顶层 function_call_output item

OpenAI Responses 采用 stateless 路径，不依赖 previous_response_id / store=true。
每次请求都全量展开历史消息，默认用 conversation id 作为 prompt_cache_key；特殊调用方可显式覆盖。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.conversation_message.message.assistant_projection import (
    AssistantTextSegment,
    AssistantToolUseSegment,
    clone_raw_value,
    iter_assistant_semantic_segments,
    serialize_tool_arguments,
    should_include_cross_protocol_thinking_text,
)
from agent_runtime.conversation_message.message.models import (
    PROVIDER_OPENAI,
    ROLE_ASSISTANT,
    ConfigurationAnchor,
    ConfigurationComponentEvent,
    CurrentPromptConfiguration,
    CurrentToolsConfiguration,
    RequestConfigurationView,
    build_current_request_configuration_view,
    build_request_configuration_view_from_timeline,
    Conversation,
    ImagePart,
    PdfPart,
    TextPart,
    ThinkingPart,
    ToolUsePart,
    UniMessage,
    UniPart,
)
from agent_runtime.conversation_message.message.tool_result_output import (
    prefix_tool_result_error_output,
    tool_result_output_to_openai_responses_output,
)
from agent_runtime.conversation_message.message.multimodal_url_markers import (
    multimodal_file_close_marker,
    multimodal_file_open_marker,
)
from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import (
    ResourceResolver,
    resolve_resource_access,
)


_OPENAI_REPLAYABLE_OUTPUT_ITEM_TYPES = frozenset({
    "reasoning",
    "message",
    "function_call",
})

OPENAI_HOSTED_TOOL_TYPES: frozenset[str] = frozenset({
    "web_search",
    "web_search_preview",
    "file_search",
    "computer",
    "computer_20241022",
    "bash",
    "bash_20241022",
    "code_interpreter",
    "image_generation",
})


def is_openai_hosted_tool(tool: Any) -> bool:
    """判断是否为 OpenAI Responses 模型内置/托管工具（非客户端自定义函数工具）。"""
    if not isinstance(tool, dict):
        return False
    tool_type = tool.get("type")
    if tool_type in OPENAI_HOSTED_TOOL_TYPES:
        return True
    if tool_type == "function":
        return False
    if "function" in tool or "parameters" in tool:
        return False
    if isinstance(tool_type, str) and tool_type != "function" and "name" not in tool:
        return True
    return False


@dataclass(frozen=True, slots=True)
class OpenAIRenderResult:
    """OpenAI 渲染结果。

    包含消息与配置事件组成的 input、缓存路由 key 和缓存选项。
    调用方仅合并 model、reasoning 等请求参数；系统提示词与自定义函数工具位于 input。
    请求客户端强制 store=false，自定义函数工具由配置视图投影为 input.additional_tools，
    平台内置托管工具（如 web_search）由客户端在顶层 tools 发送，未指定 instructions 时回退为固定默认指令。
    """

    # OpenAI Responses API 的 input 数组。
    input: list[Any]

    # OpenAI Responses stateless 路径不使用 previous_response_id。
    previous_response_id: str | None = None

    # 稳定缓存路由 key，默认使用 conversation id。
    prompt_cache_key: str | None = None

    # 始终包含，确保 raw 里有 encrypted_content。
    include: list[str] = field(
        default_factory=lambda: ["reasoning.encrypted_content"]
    )

    # 配置时间线投影启用时，客户端合并到请求的缓存选项。
    prompt_cache_options: dict[str, Any] | None = None

    # 标识 system prompt/tools 已在 input developer 事件中表达。
    uses_input_configuration: bool = False


class OpenAIAdapter:
    """OpenAI Responses API 协议适配器。

    实现 ProviderAdapter 接口，负责 OpenAI 协议的归一化、渲染和状态更新。
    内置预注册到 adapter 注册表中。
    """

    @property
    def provider(self) -> str:
        """协议标识。"""
        return PROVIDER_OPENAI

    # ── 归一化 ───────────────────────────────────────────────────

    def normalize_response(self, raw_response: Any) -> UniMessage:
        """将 OpenAI Responses API 原始响应归一化为 UniMessage。

        从响应的 output 数组中提取公共语义 parts（reasoning → thinking，
        message → text，function_call → tool_use），同时将完整的 output 数组
        存入 provider_extras["openai_responses"]["raw"]。

        OpenAI 响应结构示例：
        {
            "id": "resp_xxx",
            "output": [
                {"type": "reasoning", "id": "rs_xxx", "encrypted_content": "...", "summary": [...]},
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "..."}]}
            ]
        }
        """
        content: list[UniPart] = []

        for item in raw_response.get("output") or []:
            item_type = item.get("type")
            if item_type == "reasoning":
                # reasoning 通常无明文，可从 summary 提取摘要供 UI
                summary_parts = item.get("summary") or []
                summary_text = "\n".join(
                    s.get("text", "") for s in summary_parts if s.get("text")
                )
                content.append(
                    ThinkingPart(text=summary_text if summary_text else None)
                )
            elif item_type == "message":
                for c in item.get("content") or []:
                    if c.get("type") == "output_text":
                        content.append(TextPart(text=c.get("text", "")))
            elif item_type == "function_call":
                arguments = item.get("arguments", "{}")
                try:
                    parsed_input = json.loads(arguments)
                except (json.JSONDecodeError, TypeError):
                    parsed_input = arguments
                content.append(
                    ToolUsePart(
                        name=item.get("name", ""),
                        input=parsed_input,
                        tool_use_id=item.get("call_id") or item.get("id"),
                    )
                )

        return UniMessage(
            role=ROLE_ASSISTANT,
            content=content,
            source_provider=PROVIDER_OPENAI,
            provider_extras={
                PROVIDER_OPENAI: {
                    "raw": _filter_replayable_output_items(raw_response.get("output")),
                },
            },
        )

    # ── 渲染 ─────────────────────────────────────────────────────

    async def render(
        self,
        conv: Conversation,
        resolver: ResourceResolver,
        options: dict[str, Any] | None = None,
    ) -> OpenAIRenderResult:
        """将会话消息渲染为 OpenAI Responses API 的请求体。

        配置视图和消息共同投影为 input，附带缓存选项、prompt_cache_key 和 include。
        模型名、reasoning 等由调用方合并；请求客户端会强制 store=false。
        """
        message_slice = conv.messages
        configuration_view = _get_openai_configuration_view(conv, options)
        prompt_cache_breakpoint = (options or {}).get("prompt_cache_breakpoint", None)
        prompt_cache_options = (options or {}).get("prompt_cache_options", None)
        if prompt_cache_breakpoint is not None and not isinstance(prompt_cache_breakpoint, dict):
            raise TypeError("prompt_cache_breakpoint 必须是对象或 None。")
        if prompt_cache_options is not None and not isinstance(prompt_cache_options, dict):
            raise TypeError("prompt_cache_options 必须是对象或 None。")
        start_configuration, before_configuration, after_configuration = (
            _render_openai_configuration_timeline(
                configuration_view,
                conv,
                prompt_cache_breakpoint=prompt_cache_breakpoint,
            )
            if configuration_view is not None
            else ([], {}, {})
        )

        input_items: list[Any] = list(start_configuration)
        for m in message_slice:
            input_items.extend(before_configuration.get(m.message_id, ()))
            if m.role == "user":
                input_items.append(await _build_openai_user(m, resolver))
            elif m.role == "tool":
                # tool 消息展开为顶层 function_call_output items
                for p in m.content:
                    if p.kind == "tool_result":
                        input_items.append({
                            "type": "function_call_output",
                            "call_id": p.tool_use_ref,
                            "output": await _build_openai_tool_result_output(
                                p.output, p.is_error, resolver,
                            ),
                        })
            else:
                # assistant 消息
                input_items.extend(_build_openai_assistant(m, options))
            input_items.extend(after_configuration.get(m.message_id, ()))

        return OpenAIRenderResult(
            input=input_items,
            previous_response_id=None,
            prompt_cache_key=_prompt_cache_key(conv, options),
            prompt_cache_options=clone_raw_value(prompt_cache_options) if configuration_view is not None else None,
            uses_input_configuration=configuration_view is not None,
        )

    # ── 预检 ─────────────────────────────────────────────────────

    def preflight(self, conv: Conversation) -> None:
        """OpenAI 当前无特殊预检约束，跳过。"""

    # ── 状态更新 ─────────────────────────────────────────────────

    def update_state(
        self,
        conv: Conversation,
        raw_response: Any,
        options: dict[str, Any] | None = None,
    ) -> None:
        """记录 OpenAI Responses stateless 模式运行态。"""
        provider_state = dict(conv.provider_state.get(PROVIDER_OPENAI) or {})
        provider_state.pop("last_response_id", None)
        provider_state.pop("last_message_index", None)
        provider_state["prompt_cache_key"] = _prompt_cache_key(conv, options)
        provider_state["stateless"] = True
        conv.provider_state[PROVIDER_OPENAI] = provider_state


def _prompt_cache_key(conv: Conversation, options: dict[str, Any] | None = None) -> str:
    opts = options or {}
    option_cache_key = opts.get("prompt_cache_key")
    if isinstance(option_cache_key, str) and option_cache_key.strip():
        return option_cache_key

    provider_state = conv.provider_state.get(PROVIDER_OPENAI)
    if isinstance(provider_state, dict):
        state_cache_key = provider_state.get("prompt_cache_key")
        if isinstance(state_cache_key, str) and state_cache_key.strip():
            return state_cache_key
    return conv.id


_OPENAI_RESPONSES_CONFIGURATION_VIEW_OPTION = "openai_responses_configuration_view"


def _get_openai_configuration_view(
    conv: Conversation,
    options: dict[str, Any] | None,
) -> RequestConfigurationView | None:
    value = (options or {}).get(_OPENAI_RESPONSES_CONFIGURATION_VIEW_OPTION)
    if value is not None:
        if not isinstance(value, RequestConfigurationView):
            raise TypeError(
                f"{_OPENAI_RESPONSES_CONFIGURATION_VIEW_OPTION} 必须是 RequestConfigurationView。"
            )
        if value.conversation_id != conv.id:
            raise ValueError(
                "Responses 配置视图的 conversation_id 必须与待渲染 Conversation 一致。"
            )
        return value
    if getattr(conv, "timeline", None):
        return build_request_configuration_view_from_timeline(
            conversation_id=conv.id,
            timeline=conv.timeline,
            system_prompt=getattr(conv, "system_prompt", None),
            tools=getattr(conv, "tools", ()),
        )
    if getattr(conv, "system_prompt", None) is not None or getattr(conv, "tools", None):
        return build_current_request_configuration_view(
            conversation_id=conv.id,
            system_prompt=getattr(conv, "system_prompt", None),
            tools=getattr(conv, "tools", ()),
        )
    return None


def _render_openai_configuration_timeline(
    view: RequestConfigurationView,
    conv: Conversation,
    *,
    prompt_cache_breakpoint: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """按稳定消息锚点把上游配置事件投影为 OpenAI Responses input 项。"""
    known_message_ids = {message.message_id for message in conv.messages}
    if len(known_message_ids) != len(conv.messages):
        raise ValueError("Responses 配置锚点要求 Conversation.message_id 唯一。")
    tool_catalog: dict[str, dict[str, Any]] = {}
    if view.current_tools and view.current_tools.ordered_definitions:
        for t in view.current_tools.ordered_definitions:
            if isinstance(t, dict):
                name = t.get("name") or t.get("function", {}).get("name")
                if name:
                    tool_catalog[str(name)] = t
    if getattr(conv, "tools", None):
        for t in conv.tools:
            if isinstance(t, dict):
                name = t.get("name") or t.get("function", {}).get("name")
                if name and str(name) not in tool_catalog:
                    tool_catalog[str(name)] = t
    grouped: dict[tuple[str, str | None], list[tuple[ConfigurationComponentEvent, str]]] = {}
    for component, events in (
        ("tool", view.tool_events),
        ("prompt", view.prompt_events),
    ):
        for event in events:
            if event.anchor != "start" and event.anchor_message_id not in known_message_ids:
                raise ValueError(
                    f"配置事件 {event.event_id} 引用了不存在的 message_id: "
                    f"{event.anchor_message_id}"
                )
            grouped.setdefault((event.anchor, event.anchor_message_id), []).append(
                (event, component)
            )

    def render_group(
        anchor: ConfigurationAnchor,
        anchor_message_id: str | None,
    ) -> list[dict[str, Any]]:
        entries = grouped.get((anchor, anchor_message_id), [])
        rendered: list[dict[str, Any]] = []
        for event, component in sorted(
            entries,
            key=lambda item: (
                item[0].sequence,
                0 if item[1] == "tool" else 1,
                item[0].event_id,
            ),
        ):
            if component == "prompt":
                prompt_value = _resolve_configuration_event_value(
                    event,
                    view.current_prompt,
                    component_name="提示词",
                )
                rendered.append(
                    _build_openai_developer_message(
                        prompt_value,
                        supplementary_prompts=getattr(event, "supplementary_prompts", ()),
                        available_tool_names=view.available_tool_names,
                    )
                )
            else:
                tool_value = _resolve_configuration_event_value(
                    event,
                    view.current_tools,
                    component_name="工具",
                )
                if tool_value:
                    # 只有客户端函数工具才投影为 input 中的 additional_tools 事件项；
                    # 平台/模型内置托管工具（如 web_search、file_search 等）由客户端在顶层 tools 中发送。
                    function_tools = []
                    for item in tool_value:
                        resolved = _resolve_openai_function_tool(item, tool_catalog)
                        if resolved is not None:
                            function_tools.append(resolved)
                    if function_tools:
                        rendered.append(_build_openai_additional_tools(function_tools))
        return rendered

    start_items = render_group("start", None)
    _attach_initial_configuration_breakpoint(start_items, prompt_cache_breakpoint)
    before = {
        message_id: render_group("before_message_id", message_id)
        for message_id in known_message_ids
        if ("before_message_id", message_id) in grouped
    }
    after = {
        message_id: render_group("after_message_id", message_id)
        for message_id in known_message_ids
        if ("after_message_id", message_id) in grouped
    }
    return start_items, before, after


def _resolve_configuration_event_value(
    event: ConfigurationComponentEvent,
    current: CurrentPromptConfiguration | CurrentToolsConfiguration | None,
    *,
    component_name: str,
) -> Any:
    if event.status == "historical":
        return clone_raw_value(event.snapshot)
    if current is None:
        raise ValueError(f"活动{component_name}配置事件缺少本次实时解析结果。")
    if event.event_id != current.event_id or event.definition_id != current.definition_id:
        raise ValueError(f"活动{component_name}配置事件与本次实时解析结果不匹配。")
    if isinstance(current, CurrentPromptConfiguration):
        return clone_raw_value(current.resolved_content)
    return tuple(clone_raw_value(tool) for tool in current.ordered_definitions)


def _build_openai_developer_message(
    value: Any,
    *,
    supplementary_prompts: tuple[str, ...] = (),
    available_tool_names: tuple[str, ...] = (),
) -> dict[str, Any]:
    tool_names_json = json.dumps(list(available_tool_names), ensure_ascii=False)
    rendered_supplements: list[str] = []
    for sp in supplementary_prompts:
        text = sp.replace("{{available_tools}}", tool_names_json)
        text = text.replace(
            "{{子agent的tools名称数组，根据agent设置的conversation中的最新tools来填充 }}",
            tool_names_json,
        )
        text = text.replace("{{子agent的tools名称数组}}", tool_names_json)
        rendered_supplements.append(text)

    if isinstance(value, str):
        if rendered_supplements:
            full_text = "\n\n".join([*rendered_supplements, value])
        else:
            full_text = value
        content: list[dict[str, Any]] = [{"type": "input_text", "text": full_text}]
    elif isinstance(value, (list, tuple)):
        content = [clone_raw_value(item) for item in value]
        if not all(isinstance(item, dict) for item in content):
            raise TypeError("提示词 snapshot/resolved_content 必须是 str 或内容块字典序列。")
        if rendered_supplements:
            supplement_blocks = [
                {"type": "input_text", "text": text} for text in rendered_supplements
            ]
            content = [*supplement_blocks, *content]
    else:
        raise TypeError("提示词 snapshot/resolved_content 必须是 str 或内容块字典序列。")
    for block in content:
        block.pop("prompt_cache_breakpoint", None)
    return {"role": "developer", "content": content}


def _normalize_openai_response_tool_dict(tool: dict[str, Any]) -> dict[str, Any]:
    """将各种异构/旧协议格式的工具字典归一化为 OpenAI Responses 扁平结构。"""
    # 1. Claude 格式：含 input_schema 且缺少 type
    if "input_schema" in tool and "type" not in tool:
        return {
            "type": "function",
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or {},
            "strict": False,
        }
    # 2. Chat Completions 嵌套格式：{"type": "function", "function": {...}}
    if tool.get("type") == "function" and "function" in tool and isinstance(tool["function"], dict):
        fn = tool["function"]
        return {
            "type": "function",
            "name": fn.get("name", tool.get("name", "")),
            "description": fn.get("description", tool.get("description", "")),
            "parameters": fn.get("parameters") or {},
            "strict": fn.get("strict", False),
        }
    # 3. 缺失 type 的扁平字典
    if "type" not in tool:
        return {
            "type": "function",
            "name": tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters") or {},
            "strict": tool.get("strict", False),
        }
    return clone_raw_value(tool)


def _resolve_openai_function_tool(
    item: Any,
    tool_catalog: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """按统一语义将工具标识（ID 字符串或字典）解析为 OpenAI Responses 客户端函数工具。"""
    if isinstance(item, str):
        tool_name = item.strip()
        if not tool_name:
            return None
        if tool_name in OPENAI_HOSTED_TOOL_TYPES:
            return None
        if tool_name in tool_catalog:
            resolved = tool_catalog[tool_name]
            if is_openai_hosted_tool(resolved):
                return None
            return _normalize_openai_response_tool_dict(resolved)
        return {
            "type": "function",
            "name": tool_name,
            "description": "",
            "parameters": {"type": "object", "properties": {}},
            "strict": False,
        }
    if isinstance(item, dict):
        if is_openai_hosted_tool(item):
            return None
        return _normalize_openai_response_tool_dict(item)
    return None


def _build_openai_additional_tools(value: Any) -> dict[str, Any]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, dict) for item in value):
        raise TypeError("工具 snapshot/ordered_definitions 必须是工具 schema 字典序列。")
    return {
        "type": "additional_tools",
        "role": "developer",
        "tools": [clone_raw_value(item) for item in value],
    }


def _attach_initial_configuration_breakpoint(
    items: list[dict[str, Any]],
    breakpoint_marker: dict[str, Any] | None = None,
) -> None:
    """在初始配置最后一个 developer 提示词内容块上重建显式缓存断点 B。"""
    for item_index in range(len(items) - 1, -1, -1):
        item = items[item_index]
        if item.get("role") != "developer":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if (
                isinstance(block, dict)
                and block.get("type") == "input_text"
                and isinstance(block.get("text"), str)
            ):
                if any(
                    isinstance(later, dict) and later.get("type") == "additional_tools"
                    for later in items[item_index + 1:]
                ):
                    return
                if breakpoint_marker is not None:
                    block["prompt_cache_breakpoint"] = clone_raw_value(breakpoint_marker)
                return
    # 允许没有系统提示词的配置视图，例如仅清空工具集；此时没有合法位置放置 B。


# ── 渲染内部函数 ─────────────────────────────────────────────────

async def _build_openai_user(
    msg: UniMessage,
    resolver: ResourceResolver,
) -> dict[str, Any]:
    """将 user 消息转换为 OpenAI Responses API 格式。

    通过 ResourceResolver 将 resourceId 转为 data URI。
    图片使用 input_image + data URI，PDF 使用 input_file + data URI。
    """
    content: list[dict[str, Any]] = []

    for p in msg.content:
        if p.kind == "text":
            content.append({"type": "input_text", "text": p.text})
        elif p.kind == "image":
            resource = await resolve_resource_access(resolver, p.resource_id)
            marker = multimodal_file_open_marker("image", resource.object_storage_url)
            if marker is not None:
                content.append({"type": "input_text", "text": marker})
            image_url = resource.object_storage_url
            if not image_url:
                data = _require_base64(resource.base64_content, p.resource_id)
                image_url = f"data:{resource.mime_type};base64,{data}"
            content.append({
                "type": "input_image",
                "image_url": image_url,
            })
            if marker is not None:
                content.append({"type": "input_text", "text": multimodal_file_close_marker()})
        elif p.kind == "pdf":
            resource = await resolve_resource_access(resolver, p.resource_id)
            marker = multimodal_file_open_marker("pdf", resource.object_storage_url)
            if marker is not None:
                content.append({"type": "input_text", "text": marker})
            if resource.object_storage_url:
                content.append({
                    "type": "input_file",
                    "file_url": resource.object_storage_url,
                })
            else:
                data = _require_base64(resource.base64_content, p.resource_id)
                content.append({
                    "type": "input_file",
                    "file_data": f"data:{resource.mime_type or 'application/pdf'};base64,{data}",
                })
            if marker is not None:
                content.append({"type": "input_text", "text": multimodal_file_close_marker()})

    return {"type": "message", "role": "user", "content": content}


def _require_base64(base64_content: str | None, resource_id: str) -> str:
    if not base64_content:
        raise ValueError(f"资源缺少公网 URL 和 Base64 内容: {resource_id}")
    return base64_content


async def _build_openai_tool_result_output(
    output: Any,
    is_error: bool,
    resolver: ResourceResolver,
) -> str | list[dict[str, Any]]:
    """构造 OpenAI Responses function_call_output 的 output 字段。

    OpenAI Responses 没有原生 is_error 字段，错误以文本标记编码进 output 内容。
    多模态资源走 Responses 原生 input_image / input_file 内容项。
    """
    normalized_output = prefix_tool_result_error_output(output) if is_error else output
    return await tool_result_output_to_openai_responses_output(normalized_output, resolver)


def _build_openai_assistant(
    msg: UniMessage,
    options: dict[str, Any] | None = None,
) -> list[Any]:
    """将 assistant 消息转换为 OpenAI Responses API 格式。

    同平台（source_provider == "openai_responses"）：展开 output items 到顶层 input
    （含 reasoning / message / function_call，无损）。
    跨平台：合成一条 assistant message item（降级）。

    返回列表是因为同平台回灌时一条 UniMessage 可展开为多个 input items。
    """
    # 同平台无损回灌：展开 output items
    if msg.source_provider == PROVIDER_OPENAI:
        extras = (msg.provider_extras or {}).get(PROVIDER_OPENAI)
        if extras and extras.get("raw") is not None:
            raw_items = _filter_replayable_output_items(extras["raw"])
            if raw_items:
                return raw_items

    include_thinking_text = should_include_cross_protocol_thinking_text(options)
    input_items: list[Any] = []
    pending_text: list[str] = []

    def flush_text() -> None:
        if not pending_text:
            return
        input_items.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "\n".join(pending_text)}],
        })
        pending_text.clear()

    for segment in iter_assistant_semantic_segments(
        msg,
        include_thinking_text=include_thinking_text,
    ):
        if isinstance(segment, AssistantTextSegment):
            pending_text.append(segment.text)
        elif isinstance(segment, AssistantToolUseSegment):
            flush_text()
            input_items.append({
                "type": "function_call",
                "call_id": segment.tool_use_ref,
                "name": segment.name,
                "arguments": serialize_tool_arguments(segment.input),
            })
    flush_text()

    if input_items:
        return input_items
    return [{
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "(empty)"}],
    }]


def _filter_replayable_output_items(raw_output: Any) -> list[Any]:
    if not isinstance(raw_output, list):
        return []
    return [
        clone_raw_value(item)
        for item in raw_output
        if isinstance(item, dict)
        and item.get("type") in _OPENAI_REPLAYABLE_OUTPUT_ITEM_TYPES
    ]
