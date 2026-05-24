from __future__ import annotations

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
        return await OpenAIAdapter().render(_codex_as_openai_conversation(conv), resolver, options)

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
