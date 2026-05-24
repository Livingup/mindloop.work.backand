# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.llm_api_protocol import (
    API_FORMAT_ANTHROPIC_MESSAGES,
    API_FORMAT_CODEX_RESPONSES,
    API_FORMAT_GEMINI_GENERATE_CONTENT,
    API_FORMAT_OPENAI_CHAT_COMPLETIONS,
    API_FORMAT_OPENAI_RESPONSES,
    API_FORMAT_ZHIPU_CHAT_COMPLETIONS,
)


@dataclass(frozen=True, slots=True)
class LlmSemanticStreamEvent:
    event_type: str
    payload: dict[str, Any]


@dataclass(slots=True)
class _OpenAIResponsesToolState:
    tool_call_id: str
    tool_name: str | None = None
    arguments_parts: list[str] = field(default_factory=list)
    completed: bool = False


@dataclass(slots=True)
class _ChatToolState:
    tool_call_id: str
    index: int
    tool_name: str | None = None
    arguments_parts: list[str] = field(default_factory=list)
    started: bool = False
    arguments_completed: bool = False
    completed: bool = False


@dataclass(slots=True)
class _AnthropicBlockState:
    block_type: str
    part_id: str
    tool_name: str | None = None
    arguments_parts: list[str] = field(default_factory=list)
    completed: bool = False


class LlmSemanticStreamParser:
    def __init__(self, api_format: str) -> None:
        self._api_format = api_format
        self._thinking_started = False
        self._thinking_completed = False
        self._content_started = False
        self._content_completed = False
        self._openai_response_tools_by_item: dict[str, _OpenAIResponsesToolState] = {}
        self._openai_response_tools_by_output_index: dict[int, _OpenAIResponsesToolState] = {}
        self._chat_tools_by_index: dict[int, _ChatToolState] = {}
        self._anthropic_blocks: dict[int, _AnthropicBlockState] = {}
        self._gemini_tools_by_index: dict[int, _ChatToolState] = {}

    def parse_provider_event(
        self,
        *,
        event_name: str,
        data: dict[str, Any] | None,
        done: bool = False,
    ) -> list[LlmSemanticStreamEvent]:
        if done:
            return []
        if not isinstance(data, dict):
            return []
        if self._api_format in {API_FORMAT_OPENAI_RESPONSES, API_FORMAT_CODEX_RESPONSES}:
            return self._parse_openai_responses_event(event_name, data)
        if self._api_format in {API_FORMAT_OPENAI_CHAT_COMPLETIONS, API_FORMAT_ZHIPU_CHAT_COMPLETIONS}:
            return self._parse_chat_completions_event(event_name, data)
        if self._api_format == API_FORMAT_ANTHROPIC_MESSAGES:
            return self._parse_anthropic_messages_event(event_name, data)
        if self._api_format == API_FORMAT_GEMINI_GENERATE_CONTENT:
            return self._parse_gemini_generate_content_event(event_name, data)
        return []

    def _parse_openai_responses_event(self, event_name: str, data: dict[str, Any]) -> list[LlmSemanticStreamEvent]:
        data_type = str(data.get("type") or event_name)
        events: list[LlmSemanticStreamEvent] = []
        if data_type == "response.output_item.added" and isinstance(data.get("item"), dict):
            item = data["item"]
            item_type = item.get("type")
            if item_type == "reasoning":
                events.extend(self._start_thinking(provider_event_type=data_type, part_id=_text_or_none(item.get("id"))))
            elif item_type == "function_call":
                events.extend(self._complete_thinking(provider_event_type=data_type, part_id=None))
                events.extend(self._complete_content(provider_event_type=data_type, part_id=None))
                state = self._openai_response_tool_state(data, item)
                events.append(LlmSemanticStreamEvent(
                    "llm.tool_call.started",
                    {
                        "tool_call_id": state.tool_call_id,
                        "tool_name": state.tool_name or "",
                        "provider_tool_call_id": state.tool_call_id,
                        "provider_event_type": data_type,
                        "output_index": _int_or_none(data.get("output_index")),
                        "item_id": _text_or_none(item.get("id")),
                    },
                ))
        elif data_type in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
            delta = _text_or_none(data.get("delta"))
            if delta:
                events.extend(self._start_thinking(provider_event_type=data_type, part_id=_text_or_none(data.get("item_id"))))
                events.append(LlmSemanticStreamEvent(
                    "llm.thinking.delta",
                    {
                        "delta": delta,
                        "provider_event_type": data_type,
                        "part_id": _text_or_none(data.get("item_id")),
                    },
                ))
        elif data_type in {"response.reasoning_summary_text.done", "response.reasoning_text.done"}:
            events.extend(self._complete_thinking(provider_event_type=data_type, part_id=_text_or_none(data.get("item_id"))))
        elif data_type == "response.output_text.delta":
            delta = _text_or_none(data.get("delta"))
            if delta:
                events.extend(self._complete_thinking(provider_event_type=data_type, part_id=None))
                events.extend(self._start_content(provider_event_type=data_type, part_id=_text_or_none(data.get("item_id"))))
                events.append(LlmSemanticStreamEvent(
                    "llm.content.delta",
                    {
                        "delta": delta,
                        "provider_event_type": data_type,
                        "part_id": _text_or_none(data.get("item_id")),
                        "content_index": _int_or_none(data.get("content_index")),
                    },
                ))
        elif data_type in {"response.output_text.done", "response.content_part.done"}:
            events.extend(self._complete_content(provider_event_type=data_type, part_id=_text_or_none(data.get("item_id"))))
        elif data_type == "response.function_call_arguments.delta":
            state = self._find_openai_response_tool_state(data)
            delta = _text_or_none(data.get("delta"))
            if state and delta:
                events.extend(self._complete_thinking(provider_event_type=data_type, part_id=None))
                state.arguments_parts.append(delta)
                events.append(LlmSemanticStreamEvent(
                    "llm.tool_call.arguments.delta",
                    {
                        "tool_call_id": state.tool_call_id,
                        "tool_name": state.tool_name or "",
                        "delta": delta,
                        "arguments_partial": "".join(state.arguments_parts),
                        "provider_event_type": data_type,
                        "output_index": _int_or_none(data.get("output_index")),
                        "item_id": _text_or_none(data.get("item_id")),
                    },
                ))
        elif data_type == "response.function_call_arguments.done":
            state = self._find_openai_response_tool_state(data)
            if state:
                arguments = _text_or_none(data.get("arguments")) or "".join(state.arguments_parts)
                if arguments and not state.arguments_parts:
                    state.arguments_parts.append(arguments)
                events.append(LlmSemanticStreamEvent(
                    "llm.tool_call.arguments.completed",
                    {
                        "tool_call_id": state.tool_call_id,
                        "tool_name": state.tool_name or "",
                        "arguments": arguments,
                        "arguments_preview": _parse_arguments_preview(arguments),
                        "provider_event_type": data_type,
                        "output_index": _int_or_none(data.get("output_index")),
                        "item_id": _text_or_none(data.get("item_id")),
                    },
                ))
        elif data_type == "response.output_item.done" and isinstance(data.get("item"), dict):
            item = data["item"]
            item_type = item.get("type")
            if item_type == "reasoning":
                events.extend(self._complete_thinking(provider_event_type=data_type, part_id=_text_or_none(item.get("id"))))
            elif item_type == "function_call":
                state = self._openai_response_tool_state(data, item)
                if not state.arguments_parts and isinstance(item.get("arguments"), str):
                    state.arguments_parts.append(item["arguments"])
                state.completed = True
                arguments = "".join(state.arguments_parts)
                events.append(LlmSemanticStreamEvent(
                    "llm.tool_call.completed",
                    {
                        "tool_call_id": state.tool_call_id,
                        "tool_name": state.tool_name or "",
                        "arguments": arguments,
                        "arguments_preview": _parse_arguments_preview(arguments),
                        "provider_event_type": data_type,
                        "output_index": _int_or_none(data.get("output_index")),
                        "item_id": _text_or_none(item.get("id")),
                    },
                ))
        elif data_type == "response.completed":
            events.extend(self._complete_content(provider_event_type=data_type, part_id=None))
            events.extend(self._complete_thinking(provider_event_type=data_type, part_id=None))
            events.append(LlmSemanticStreamEvent(
                "llm.response.completed",
                {"provider_event_type": data_type, "usage": _extract_openai_responses_usage(data)},
            ))
        elif data_type in {"response.failed", "response.incomplete", "error"}:
            events.append(LlmSemanticStreamEvent(
                "llm.response.failed",
                {"provider_event_type": data_type, "error": data.get("error") or data},
            ))
        return events

    def _openai_response_tool_state(self, data: dict[str, Any], item: dict[str, Any]) -> _OpenAIResponsesToolState:
        output_index = _int_or_none(data.get("output_index"))
        item_id = _text_or_none(item.get("id"))
        tool_call_id = _text_or_none(item.get("call_id")) or item_id or f"response-tool-{output_index if output_index is not None else len(self._openai_response_tools_by_item)}"
        state = self._openai_response_tools_by_item.get(item_id or tool_call_id)
        if state is None and output_index is not None:
            state = self._openai_response_tools_by_output_index.get(output_index)
        if state is None:
            state = _OpenAIResponsesToolState(tool_call_id=tool_call_id, tool_name=_text_or_none(item.get("name")))
        if item.get("name"):
            state.tool_name = str(item["name"])
        self._openai_response_tools_by_item[item_id or tool_call_id] = state
        if output_index is not None:
            self._openai_response_tools_by_output_index[output_index] = state
        return state

    def _find_openai_response_tool_state(self, data: dict[str, Any]) -> _OpenAIResponsesToolState | None:
        item_id = _text_or_none(data.get("item_id"))
        output_index = _int_or_none(data.get("output_index"))
        if item_id and item_id in self._openai_response_tools_by_item:
            return self._openai_response_tools_by_item[item_id]
        if output_index is not None and output_index in self._openai_response_tools_by_output_index:
            return self._openai_response_tools_by_output_index[output_index]
        return None

    def _parse_chat_completions_event(self, event_name: str, data: dict[str, Any]) -> list[LlmSemanticStreamEvent]:
        events: list[LlmSemanticStreamEvent] = []
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                delta = {}
            reasoning_delta = _first_text(delta, "reasoning_content", "thinking", "reasoning")
            if reasoning_delta:
                events.extend(self._start_thinking(provider_event_type=event_name, part_id=None))
                events.append(LlmSemanticStreamEvent(
                    "llm.thinking.delta",
                    {"delta": reasoning_delta, "provider_event_type": event_name},
                ))
            content_delta = _text_or_none(delta.get("content"))
            if content_delta:
                events.extend(self._complete_thinking(provider_event_type=event_name, part_id=None))
                events.extend(self._start_content(provider_event_type=event_name, part_id=None))
                events.append(LlmSemanticStreamEvent(
                    "llm.content.delta",
                    {"delta": content_delta, "provider_event_type": event_name},
                ))
            for tool_call_delta in delta.get("tool_calls") or []:
                if not isinstance(tool_call_delta, dict):
                    continue
                events.extend(self._complete_thinking(provider_event_type=event_name, part_id=None))
                events.extend(self._complete_content(provider_event_type=event_name, part_id=None))
                events.extend(self._parse_chat_tool_call_delta(event_name, tool_call_delta))
            finish_reason = choice.get("finish_reason")
            if finish_reason == "tool_calls":
                events.extend(self._complete_all_chat_tools(event_name))
            if finish_reason is not None:
                events.extend(self._complete_content(provider_event_type=event_name, part_id=None))
                events.extend(self._complete_thinking(provider_event_type=event_name, part_id=None))
                events.append(LlmSemanticStreamEvent(
                    "llm.response.completed",
                    {"provider_event_type": event_name, "finish_reason": finish_reason, "usage": data.get("usage")},
                ))
        return events

    def _parse_chat_tool_call_delta(self, provider_event_type: str, tool_call_delta: dict[str, Any]) -> list[LlmSemanticStreamEvent]:
        events: list[LlmSemanticStreamEvent] = []
        index = int(tool_call_delta.get("index", len(self._chat_tools_by_index)))
        state = self._chat_tools_by_index.get(index)
        function_delta = tool_call_delta.get("function") or {}
        if not isinstance(function_delta, dict):
            function_delta = {}
        incoming_id = _text_or_none(tool_call_delta.get("id"))
        incoming_name = _text_or_none(function_delta.get("name"))
        if state is None:
            state = _ChatToolState(tool_call_id=incoming_id or f"chat-tool-{index}", index=index, tool_name=incoming_name)
            self._chat_tools_by_index[index] = state
        if incoming_id and state.tool_call_id.startswith("chat-tool-") and not state.started:
            state.tool_call_id = incoming_id
        if incoming_name:
            state.tool_name = incoming_name
        if not state.started and (incoming_id or incoming_name or tool_call_delta.get("type")):
            state.started = True
            events.append(LlmSemanticStreamEvent(
                "llm.tool_call.started",
                {
                    "tool_call_id": state.tool_call_id,
                    "tool_name": state.tool_name or "",
                    "provider_tool_call_id": incoming_id or state.tool_call_id,
                    "provider_event_type": provider_event_type,
                    "tool_call_index": index,
                },
            ))
        arguments_delta = _text_or_none(function_delta.get("arguments"))
        if arguments_delta:
            if not state.started:
                state.started = True
                events.append(LlmSemanticStreamEvent(
                    "llm.tool_call.started",
                    {
                        "tool_call_id": state.tool_call_id,
                        "tool_name": state.tool_name or "",
                        "provider_tool_call_id": state.tool_call_id,
                        "provider_event_type": provider_event_type,
                        "tool_call_index": index,
                    },
                ))
            state.arguments_parts.append(arguments_delta)
            events.append(LlmSemanticStreamEvent(
                "llm.tool_call.arguments.delta",
                {
                    "tool_call_id": state.tool_call_id,
                    "tool_name": state.tool_name or "",
                    "delta": arguments_delta,
                    "arguments_partial": "".join(state.arguments_parts),
                    "provider_event_type": provider_event_type,
                    "tool_call_index": index,
                },
            ))
        return events

    def _complete_all_chat_tools(self, provider_event_type: str) -> list[LlmSemanticStreamEvent]:
        events: list[LlmSemanticStreamEvent] = []
        for index in sorted(self._chat_tools_by_index):
            state = self._chat_tools_by_index[index]
            arguments = "".join(state.arguments_parts)
            if not state.arguments_completed:
                state.arguments_completed = True
                events.append(LlmSemanticStreamEvent(
                    "llm.tool_call.arguments.completed",
                    {
                        "tool_call_id": state.tool_call_id,
                        "tool_name": state.tool_name or "",
                        "arguments": arguments,
                        "arguments_preview": _parse_arguments_preview(arguments),
                        "provider_event_type": provider_event_type,
                        "tool_call_index": index,
                    },
                ))
            if not state.completed:
                state.completed = True
                events.append(LlmSemanticStreamEvent(
                    "llm.tool_call.completed",
                    {
                        "tool_call_id": state.tool_call_id,
                        "tool_name": state.tool_name or "",
                        "arguments": arguments,
                        "arguments_preview": _parse_arguments_preview(arguments),
                        "provider_event_type": provider_event_type,
                        "tool_call_index": index,
                    },
                ))
        return events

    def _parse_anthropic_messages_event(self, event_name: str, data: dict[str, Any]) -> list[LlmSemanticStreamEvent]:
        data_type = str(data.get("type") or event_name)
        events: list[LlmSemanticStreamEvent] = []
        if data_type == "content_block_start" and isinstance(data.get("content_block"), dict):
            index = int(data.get("index", 0))
            block = data["content_block"]
            block_type = str(block.get("type") or "text")
            part_id = _text_or_none(block.get("id")) or f"anthropic-block-{index}"
            state = _AnthropicBlockState(block_type=block_type, part_id=part_id, tool_name=_text_or_none(block.get("name")))
            self._anthropic_blocks[index] = state
            if block_type in {"thinking", "redacted_thinking"}:
                events.extend(self._start_thinking(provider_event_type=data_type, part_id=part_id))
            elif block_type == "tool_use":
                events.append(LlmSemanticStreamEvent(
                    "llm.tool_call.started",
                    {
                        "tool_call_id": part_id,
                        "tool_name": state.tool_name or "",
                        "provider_tool_call_id": part_id,
                        "provider_event_type": data_type,
                        "tool_call_index": index,
                    },
                ))
        elif data_type == "content_block_delta" and isinstance(data.get("delta"), dict):
            index = int(data.get("index", 0))
            state = self._anthropic_blocks.get(index)
            delta = data["delta"]
            delta_type = str(delta.get("type") or "")
            if delta_type == "thinking_delta":
                text = _text_or_none(delta.get("thinking"))
                if text:
                    events.extend(self._start_thinking(provider_event_type=data_type, part_id=state.part_id if state else None))
                    events.append(LlmSemanticStreamEvent(
                        "llm.thinking.delta",
                        {"delta": text, "provider_event_type": data_type, "part_id": state.part_id if state else None},
                    ))
            elif delta_type == "text_delta":
                text = _text_or_none(delta.get("text"))
                if text:
                    if state and state.block_type in {"thinking", "redacted_thinking"}:
                        events.extend(self._start_thinking(provider_event_type=data_type, part_id=state.part_id))
                        events.append(LlmSemanticStreamEvent(
                            "llm.thinking.delta",
                            {"delta": text, "provider_event_type": data_type, "part_id": state.part_id},
                        ))
                    else:
                        events.extend(self._start_content(provider_event_type=data_type, part_id=state.part_id if state else None))
                        events.append(LlmSemanticStreamEvent(
                            "llm.content.delta",
                            {"delta": text, "provider_event_type": data_type, "part_id": state.part_id if state else None},
                        ))
            elif delta_type == "input_json_delta" and state and state.block_type == "tool_use":
                partial_json = _text_or_none(delta.get("partial_json"))
                if partial_json:
                    state.arguments_parts.append(partial_json)
                    events.append(LlmSemanticStreamEvent(
                        "llm.tool_call.arguments.delta",
                        {
                            "tool_call_id": state.part_id,
                            "tool_name": state.tool_name or "",
                            "delta": partial_json,
                            "arguments_partial": "".join(state.arguments_parts),
                            "provider_event_type": data_type,
                            "tool_call_index": index,
                        },
                    ))
        elif data_type == "content_block_stop":
            index = int(data.get("index", 0))
            state = self._anthropic_blocks.get(index)
            if state and state.block_type in {"thinking", "redacted_thinking"}:
                events.extend(self._complete_thinking(provider_event_type=data_type, part_id=state.part_id))
            elif state and state.block_type == "text":
                events.extend(self._complete_content(provider_event_type=data_type, part_id=state.part_id))
            elif state and state.block_type == "tool_use":
                arguments = "".join(state.arguments_parts)
                events.append(LlmSemanticStreamEvent(
                    "llm.tool_call.arguments.completed",
                    {
                        "tool_call_id": state.part_id,
                        "tool_name": state.tool_name or "",
                        "arguments": arguments,
                        "arguments_preview": _parse_arguments_preview(arguments),
                        "provider_event_type": data_type,
                        "tool_call_index": index,
                    },
                ))
                events.append(LlmSemanticStreamEvent(
                    "llm.tool_call.completed",
                    {
                        "tool_call_id": state.part_id,
                        "tool_name": state.tool_name or "",
                        "arguments": arguments,
                        "arguments_preview": _parse_arguments_preview(arguments),
                        "provider_event_type": data_type,
                        "tool_call_index": index,
                    },
                ))
                state.completed = True
        elif data_type == "message_stop":
            events.extend(self._complete_content(provider_event_type=data_type, part_id=None))
            events.extend(self._complete_thinking(provider_event_type=data_type, part_id=None))
            events.append(LlmSemanticStreamEvent(
                "llm.response.completed",
                {"provider_event_type": data_type},
            ))
        return events

    def _parse_gemini_generate_content_event(self, event_name: str, data: dict[str, Any]) -> list[LlmSemanticStreamEvent]:
        events: list[LlmSemanticStreamEvent] = []
        for candidate in data.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content") or {}
            if not isinstance(content, dict):
                continue
            for part_index, part in enumerate(content.get("parts") or []):
                if not isinstance(part, dict):
                    continue
                text = _text_or_none(part.get("text"))
                part_id = f"gemini-part-{part_index}"
                if text and part.get("thought") is True:
                    events.extend(self._start_thinking(provider_event_type=event_name, part_id=part_id))
                    events.append(LlmSemanticStreamEvent(
                        "llm.thinking.delta",
                        {"delta": text, "provider_event_type": event_name, "part_id": part_id},
                    ))
                elif text:
                    events.extend(self._start_content(provider_event_type=event_name, part_id=part_id))
                    events.append(LlmSemanticStreamEvent(
                        "llm.content.delta",
                        {"delta": text, "provider_event_type": event_name, "part_id": part_id},
                    ))
                function_call = part.get("functionCall")
                if isinstance(function_call, dict):
                    events.extend(self._parse_gemini_function_call(event_name, part_index, function_call))
            if candidate.get("finishReason") is not None:
                events.extend(self._complete_content(provider_event_type=event_name, part_id=None))
                events.extend(self._complete_thinking(provider_event_type=event_name, part_id=None))
                events.append(LlmSemanticStreamEvent(
                    "llm.response.completed",
                    {
                        "provider_event_type": event_name,
                        "finish_reason": candidate.get("finishReason"),
                        "usage": data.get("usageMetadata"),
                    },
                ))
        return events

    def _parse_gemini_function_call(
        self,
        provider_event_type: str,
        part_index: int,
        function_call: dict[str, Any],
    ) -> list[LlmSemanticStreamEvent]:
        events: list[LlmSemanticStreamEvent] = []
        name = _text_or_none(function_call.get("name")) or ""
        state = self._gemini_tools_by_index.get(part_index)
        if state is None:
            state = _ChatToolState(
                tool_call_id=_build_gemini_tool_call_id(name, part_index),
                index=part_index,
                tool_name=name,
            )
            self._gemini_tools_by_index[part_index] = state
        if name:
            state.tool_name = name
        if not state.started:
            state.started = True
            events.append(LlmSemanticStreamEvent(
                "llm.tool_call.started",
                {
                    "tool_call_id": state.tool_call_id,
                    "tool_name": state.tool_name or "",
                    "provider_tool_call_id": state.tool_call_id,
                    "provider_event_type": provider_event_type,
                    "tool_call_index": part_index,
                },
            ))
        arguments = function_call.get("args")
        arguments_text = json.dumps(arguments, ensure_ascii=False) if arguments is not None else ""
        if not state.arguments_completed:
            state.arguments_parts = [arguments_text] if arguments_text else []
            state.arguments_completed = True
            events.append(LlmSemanticStreamEvent(
                "llm.tool_call.arguments.completed",
                {
                    "tool_call_id": state.tool_call_id,
                    "tool_name": state.tool_name or "",
                    "arguments": arguments_text,
                    "arguments_preview": _parse_arguments_preview(arguments_text),
                    "provider_event_type": provider_event_type,
                    "tool_call_index": part_index,
                },
            ))
        if not state.completed:
            state.completed = True
            events.append(LlmSemanticStreamEvent(
                "llm.tool_call.completed",
                {
                    "tool_call_id": state.tool_call_id,
                    "tool_name": state.tool_name or "",
                    "arguments": arguments_text,
                    "arguments_preview": _parse_arguments_preview(arguments_text),
                    "provider_event_type": provider_event_type,
                    "tool_call_index": part_index,
                },
            ))
        return events

    def _start_thinking(self, *, provider_event_type: str, part_id: str | None) -> list[LlmSemanticStreamEvent]:
        if self._thinking_started:
            return []
        self._thinking_started = True
        self._thinking_completed = False
        return [LlmSemanticStreamEvent(
            "llm.thinking.started",
            {"provider_event_type": provider_event_type, "part_id": part_id},
        )]

    def _complete_thinking(self, *, provider_event_type: str, part_id: str | None) -> list[LlmSemanticStreamEvent]:
        if not self._thinking_started or self._thinking_completed:
            return []
        self._thinking_completed = True
        return [LlmSemanticStreamEvent(
            "llm.thinking.completed",
            {"provider_event_type": provider_event_type, "part_id": part_id},
        )]

    def _start_content(self, *, provider_event_type: str, part_id: str | None) -> list[LlmSemanticStreamEvent]:
        if self._content_started:
            return []
        self._content_started = True
        self._content_completed = False
        return [LlmSemanticStreamEvent(
            "llm.content.started",
            {"provider_event_type": provider_event_type, "part_id": part_id},
        )]

    def _complete_content(self, *, provider_event_type: str, part_id: str | None) -> list[LlmSemanticStreamEvent]:
        if not self._content_started or self._content_completed:
            return []
        self._content_completed = True
        return [LlmSemanticStreamEvent(
            "llm.content.completed",
            {"provider_event_type": provider_event_type, "part_id": part_id},
        )]


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _first_text(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _text_or_none(source.get(key))
        if value:
            return value
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _parse_arguments_preview(arguments: str) -> dict[str, Any] | None:
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {"arguments": arguments}
    return parsed if isinstance(parsed, dict) else {"arguments": parsed}


def _build_gemini_tool_call_id(name: str, part_index: int) -> str:
    safe_name = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in name)
    return f"gemini_call_{part_index}_{safe_name or 'tool'}"


def _extract_openai_responses_usage(data: dict[str, Any]) -> Any:
    response = data.get("response")
    if isinstance(response, dict):
        return response.get("usage")
    return data.get("usage")
