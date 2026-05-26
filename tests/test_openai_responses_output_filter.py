import asyncio

from agent_runtime.conversation_message.message.models import (
    PROVIDER_OPENAI,
    ROLE_ASSISTANT,
    ROLE_TOOL,
    Conversation,
    ImagePart,
    TextPart,
    ToolResultPart,
    UniMessage,
)
from agent_runtime.conversation_message.message.openai_protocol import OpenAIAdapter
from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import (
    ResolvedResource,
    ResourceMeta,
)


class _Resolver:
    async def get_base64(self, resource_id: str) -> str:
        return "base64-image"

    async def get_public_url(self, resource_id: str) -> str | None:
        return "https://example.test/image.png"

    async def get_resource_meta(self, resource_id: str) -> ResourceMeta:
        return ResourceMeta(mime_type="image/png", resource_category="image")

    async def resolve_resource(self, resource_id: str) -> ResolvedResource:
        return ResolvedResource(
            resource_id=resource_id,
            mime_type="image/png",
            resource_category="image",
            object_storage_url="https://example.test/image.png",
            base64_content=None,
            local_storage_path=None,
        )


def _output_item_types(items):
    return [item.get("type") for item in items]


def test_openai_normalize_does_not_persist_native_output_items_in_replay_raw():
    msg = OpenAIAdapter().normalize_response({
        "output": [
            {
                "id": "ig_1",
                "type": "image_generation_call",
                "status": "completed",
                "result": "very-large-base64-image-result",
            },
            {
                "id": "ws_1",
                "type": "web_search_call",
                "status": "completed",
                "action": {"sources": [{"url": "https://example.test"}]},
            },
            {
                "id": "rs_1",
                "type": "reasoning",
                "encrypted_content": "encrypted",
                "summary": [{"text": "thinking"}],
            },
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            },
            {
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_1",
                "name": "generate_image",
                "arguments": "{\"prompt\": \"draw\"}",
            },
        ],
    })

    raw = msg.provider_extras[PROVIDER_OPENAI]["raw"]

    assert _output_item_types(raw) == ["reasoning", "message", "function_call"]
    assert "image_generation_call" not in str(raw)
    assert "web_search_call" not in str(raw)
    assert "very-large-base64-image-result" not in str(raw)


def test_openai_render_filters_existing_contaminated_native_output_items():
    async def run_case():
        conv = Conversation(
            id="conv-openai-native-output-filter",
            messages=[
                UniMessage(
                    role=ROLE_ASSISTANT,
                    content=[TextPart(text="done")],
                    source_provider=PROVIDER_OPENAI,
                    provider_extras={
                        PROVIDER_OPENAI: {
                            "raw": [
                                {
                                    "id": "ig_1",
                                    "type": "image_generation_call",
                                    "status": "generating",
                                    "result": "very-large-base64-image-result",
                                },
                                {
                                    "id": "ws_1",
                                    "type": "web_search_call",
                                    "status": "completed",
                                },
                                {
                                    "id": "msg_1",
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [{"type": "output_text", "text": "done"}],
                                },
                            ],
                        },
                    },
                ),
            ],
        )

        rendered = await OpenAIAdapter().render(conv, _Resolver())

        assert _output_item_types(rendered.input) == ["message"]
        assert "image_generation_call" not in str(rendered.input)
        assert "web_search_call" not in str(rendered.input)
        assert "very-large-base64-image-result" not in str(rendered.input)

    asyncio.run(run_case())


def test_openai_tool_result_with_image_resource_still_renders_as_multimodal_output():
    async def run_case():
        conv = Conversation(
            id="conv-openai-tool-result-image",
            messages=[
                UniMessage(
                    role=ROLE_TOOL,
                    content=[
                        ToolResultPart(
                            tool_use_ref="call_1",
                            is_error=False,
                            output=[
                                TextPart(text="revised prompt"),
                                ImagePart(resource_id="img_resource_1"),
                            ],
                        ),
                    ],
                ),
            ],
        )

        rendered = await OpenAIAdapter().render(conv, _Resolver())

        assert rendered.input == [
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [
                    {"type": "input_text", "text": "revised prompt"},
                    {"type": "input_image", "image_url": "https://example.test/image.png"},
                ],
            },
        ]

    asyncio.run(run_case())
