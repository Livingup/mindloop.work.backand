from __future__ import annotations

import json
from typing import Any, Sequence

from agent_runtime.conversation_message.infrastructure.sqlite_conversation_repository import (
    _json_line_to_message,
    _message_to_json_line,
)
from agent_runtime.conversation_message.message.models import (
    ImagePart,
    PdfPart,
    TextPart,
    ThinkingPart,
    ToolResultPart,
    ToolUsePart,
    UniMessage,
)


QUALITY_ANALYSIS_PROMPT_SUFFIX = (
    "请帮我分析下这次的上下文压缩质量如何。上下文压缩的原因是上下文空间不足了，"
    "舍去无用的过时的噪音和无效信息重新整理上下文，压缩不应该导致后续任务丢失剩余任务需要的上下文信息。"
    "帮我分析下此次压缩的质量如何，如果存在问题请指出问题，不要矫枉过正，指出实实在在存在的问题！"
)


def messages_to_jsonl_text(messages: Sequence[UniMessage]) -> str:
    lines = [_message_to_json_line(message) for message in messages]
    return ("\n".join(lines) + ("\n" if lines else ""))


def load_messages_from_jsonl_text(text: str) -> list[UniMessage]:
    messages: list[UniMessage] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        messages.append(_json_line_to_message(line))
    return messages


def native_history_to_markdown(messages: Sequence[UniMessage]) -> str:
    """把 compressed-range 的 native UniMessage 历史转成确定性快照 Markdown。"""
    if not messages:
        return "（无历史消息）"
    blocks: list[str] = []
    for index, message in enumerate(messages):
        role = str(message.role or "unknown")
        header = f"### [{index}] {role}"
        body_lines = _message_body_lines(message)
        if body_lines:
            blocks.append(header + "\n" + "\n".join(body_lines))
        else:
            blocks.append(header + "\n（空消息）")
    return "\n\n".join(blocks).strip()


def _message_body_lines(message: UniMessage) -> list[str]:
    lines: list[str] = []
    for part in message.content or []:
        if isinstance(part, TextPart):
            text = str(part.text or "").rstrip()
            if text:
                lines.append(text)
            continue
        if isinstance(part, ImagePart):
            lines.append(f"[image resource_id={part.resource_id}]")
            continue
        if isinstance(part, PdfPart):
            lines.append(f"[pdf resource_id={part.resource_id}]")
            continue
        if isinstance(part, ThinkingPart):
            # 思考块对质量分析通常噪音较大；保留存在性与可选摘要。
            summary = str(part.text or "").strip()
            if summary:
                lines.append(f"[thinking] {summary}")
            else:
                lines.append("[thinking]")
            continue
        if isinstance(part, ToolUsePart):
            lines.append(f"[tool_use name={part.name} id={part.tool_use_id or ''}]")
            rendered = _compact_json(part.input)
            if rendered:
                lines.append(rendered)
            continue
        if isinstance(part, ToolResultPart):
            error_flag = " error=true" if part.is_error else ""
            lines.append(f"[tool_result ref={part.tool_use_ref}{error_flag}]")
            rendered = _compact_tool_output(part.output)
            if rendered:
                lines.append(rendered)
            continue
        lines.append(f"[unknown_part type={type(part).__name__}]")
    return lines


def _compact_tool_output(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return _truncate(output.strip(), 4000)
    if isinstance(output, list):
        # 多模态 tool result：只摘要 part 类型与 resource。
        fragments: list[str] = []
        for item in output:
            if isinstance(item, TextPart):
                text = str(item.text or "").strip()
                if text:
                    fragments.append(_truncate(text, 1500))
            elif isinstance(item, ImagePart):
                fragments.append(f"[image resource_id={item.resource_id}]")
            elif isinstance(item, PdfPart):
                fragments.append(f"[pdf resource_id={item.resource_id}]")
            else:
                fragments.append(_truncate(_compact_json(item), 800))
        return "\n".join(fragment for fragment in fragments if fragment)
    return _truncate(_compact_json(output), 4000)


def _compact_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + "\n…(truncated)"
