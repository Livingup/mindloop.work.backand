"""assistant 消息跨协议投影辅助函数。"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Iterable

from agent_runtime.conversation_message.message.models import (
    ROLE_ASSISTANT,
    ROLE_TOOL,
    ThinkingPart,
    ToolUsePart,
    UniMessage,
)


@dataclass(frozen=True, slots=True)
class AssistantTextSegment:
    """跨协议可复用的 assistant 文本片段。"""

    text: str


@dataclass(frozen=True, slots=True)
class AssistantToolUseSegment:
    """跨协议可复用的 assistant 工具调用片段。"""

    name: str
    input: Any
    tool_use_ref: str


AssistantSemanticSegment = AssistantTextSegment | AssistantToolUseSegment


def clone_raw_value(value: Any) -> Any:
    """复制平台 raw，避免渲染阶段动态字段污染已持久化的历史消息。"""
    return copy.deepcopy(value)


def iter_assistant_semantic_segments(
    msg: UniMessage,
    *,
    include_thinking_text: bool = False,
) -> Iterable[AssistantSemanticSegment]:
    """按原始公共语义顺序输出可跨协议重建的 assistant 片段。

    平台私有 thinking 凭证不能跨协议伪造；当调用方显式允许时，只有公共层里的
    thinking 明文/摘要会作为普通 assistant 文本投影，不会投影成目标协议的 thinking 字段。
    """
    for part in msg.content:
        if part.kind == "text" and getattr(part, "text", None):
            yield AssistantTextSegment(text=part.text)
        elif part.kind == "thinking" and include_thinking_text:
            thinking_text = getattr(part, "text", None)
            if thinking_text:
                yield AssistantTextSegment(text=thinking_text)
        elif part.kind == "tool_use":
            tool_use = _require_tool_use_part(part)
            yield AssistantToolUseSegment(
                name=tool_use.name,
                input=tool_use.input,
                tool_use_ref=tool_use_ref(tool_use),
            )


def should_include_cross_protocol_thinking_text(options: dict[str, Any] | None) -> bool:
    """读取跨协议 thinking 明文降级开关，默认不把推理内容注入目标模型上下文。"""
    return bool((options or {}).get("include_cross_protocol_thinking_text"))


def tool_use_ref(tool_use: ToolUsePart) -> str:
    """返回工具结果可引用的稳定工具调用标识。"""
    return tool_use.tool_use_id or tool_use.name


def build_tool_name_by_ref(messages: list[UniMessage]) -> dict[str, str]:
    """从历史 assistant 工具调用中建立 tool_use_ref 到工具名称的映射。"""
    result: dict[str, str] = {}
    for message in messages:
        if message.role != ROLE_ASSISTANT:
            continue
        for part in message.content:
            if part.kind != "tool_use":
                continue
            tool_use = _require_tool_use_part(part)
            result[tool_use_ref(tool_use)] = tool_use.name
    return result


def tool_name_for_result(tool_use_ref_value: str, tool_name_by_ref: dict[str, str]) -> str:
    """返回 tool_result 在仅按名称关联的协议中应使用的函数名称。"""
    return tool_name_by_ref.get(tool_use_ref_value, tool_use_ref_value)


def normalize_tool_input_object(value: Any) -> dict[str, Any]:
    """把工具入参规范为函数调用协议普遍接受的 JSON object。"""
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"input": value}
        if isinstance(parsed, dict):
            return parsed
        return {"input": parsed}
    return {"input": value}


def serialize_tool_arguments(value: Any) -> str:
    """把统一工具入参稳定序列化为 Chat/Responses function arguments 字符串。"""
    return json.dumps(normalize_tool_input_object(value), ensure_ascii=False, separators=(",", ":"))


def serialize_tool_output(value: Any) -> str:
    """把工具执行结果稳定序列化为协议 tool/function result content。"""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def message_has_tool_results(msg: UniMessage) -> bool:
    """判断消息是否包含工具结果。"""
    if msg.role != ROLE_TOOL:
        return False
    return any(part.kind == "tool_result" for part in msg.content)


def _require_tool_use_part(value: Any) -> ToolUsePart:
    if not isinstance(value, ToolUsePart):
        raise TypeError(f"期望 ToolUsePart，实际为 {type(value)}")
    return value
