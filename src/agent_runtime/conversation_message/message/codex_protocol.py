from __future__ import annotations

from dataclasses import replace
from typing import Any

from agent_runtime.conversation_message.message.models import Conversation, UniMessage
from agent_runtime.conversation_message.message.openai_protocol import OpenAIAdapter, OpenAIRenderResult
from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import ResourceResolver
from agent_runtime.llm_api_protocol import API_FORMAT_CODEX_RESPONSES, API_FORMAT_OPENAI_RESPONSES


class CodexAdapter:
    @property
    def provider(self) -> str:
        return API_FORMAT_CODEX_RESPONSES

    def normalize_response(self, raw_response: Any) -> UniMessage:
        message = OpenAIAdapter().normalize_response(raw_response)
        message.source_provider = API_FORMAT_CODEX_RESPONSES
        message.provider_extras = {
            API_FORMAT_CODEX_RESPONSES: {
                "raw": raw_response.get("output") if isinstance(raw_response, dict) else None,
            },
        }
        return message

    async def render(
        self,
        conv: Conversation,
        resolver: ResourceResolver,
        options: dict[str, Any] | None = None,
    ) -> OpenAIRenderResult:
        rendered = await OpenAIAdapter().render(_codex_as_openai_conversation(conv), resolver, options)
        return replace(rendered, input=_normalize_codex_input(rendered.input))

    def preflight(self, conv: Conversation) -> None:
        OpenAIAdapter().preflight(conv)

    def update_state(
        self,
        conv: Conversation,
        raw_response: Any,
        options: dict[str, Any] | None = None,
    ) -> None:
        provider_state = dict(conv.provider_state.get(API_FORMAT_CODEX_RESPONSES) or {})
        provider_state.pop("last_response_id", None)
        provider_state.pop("last_message_index", None)
        provider_state["prompt_cache_key"] = conv.id
        provider_state["stateless"] = True
        conv.provider_state[API_FORMAT_CODEX_RESPONSES] = provider_state


def _codex_as_openai_conversation(conv: Conversation) -> Conversation:
    mapped_messages: list[UniMessage] = []
    for message in conv.messages:
        if message.source_provider != API_FORMAT_CODEX_RESPONSES:
            mapped_messages.append(message)
            continue
        codex_extras = (message.provider_extras or {}).get(API_FORMAT_CODEX_RESPONSES)
        if not codex_extras:
            mapped_messages.append(message)
            continue
        mapped_messages.append(
            UniMessage(
                role=message.role,
                content=message.content,
                source_provider=API_FORMAT_OPENAI_RESPONSES,
                source_vendor=message.source_vendor,
                provider_extras={API_FORMAT_OPENAI_RESPONSES: codex_extras},
            )
        )
    return Conversation(
        id=conv.id,
        messages=mapped_messages,
        state=conv.state,
        provider_state=conv.provider_state,
    )


def _normalize_codex_input(input_items: list[Any]) -> list[Any]:
    """Normalize only Codex-bound message items to the public InputItem schema.

    Complete same-source Codex output items and all tool-loop items remain untouched.
    """
    return [_normalize_codex_input_item(item) for item in input_items]


def _normalize_codex_input_item(item: Any) -> Any:
    if not isinstance(item, dict) or item.get("type") != "message":
        return item
    role = item.get("role")
    content = item.get("content")
    if role == "user" and _is_synthetic_text_message(
        item,
        role="user",
        expected_type="input_text",
    ):
        return {"role": "user", "content": _join_text_content(content)}
    if (
        role == "assistant"
        and not _is_complete_output_message(item)
        and _is_synthetic_assistant_fallback(item)
    ):
        return {"role": "assistant", "content": _join_text_content(content)}
    return item


def _is_complete_output_message(item: dict[str, Any]) -> bool:
    return (
        isinstance(item.get("id"), str)
        and bool(item["id"])
        and item.get("type") == "message"
        and item.get("role") == "assistant"
        and item.get("status") in {"in_progress", "completed", "incomplete"}
        and isinstance(item.get("content"), list)
    )


def _is_synthetic_assistant_fallback(item: dict[str, Any]) -> bool:
    return _is_synthetic_text_message(
        item,
        role="assistant",
        expected_type="output_text",
    )


def _is_synthetic_text_message(
    item: dict[str, Any],
    *,
    role: str,
    expected_type: str,
) -> bool:
    content = item.get("content")
    return (
        set(item) == {"type", "role", "content"}
        and item.get("type") == "message"
        and item.get("role") == role
        and _is_text_content_list(content, expected_type)
        and all(set(part) == {"type", "text"} for part in content)
    )


def _is_text_content_list(value: Any, expected_type: str) -> bool:
    return (
        isinstance(value, list)
        and all(
            isinstance(part, dict)
            and part.get("type") == expected_type
            and isinstance(part.get("text"), str)
            for part in value
        )
    )


def _join_text_content(value: list[dict[str, Any]]) -> str:
    return "\n".join(str(part["text"]) for part in value)
