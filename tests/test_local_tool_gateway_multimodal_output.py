import asyncio

from agent_runtime.agent.tool_dispatch.tool_dispatcher import AgentToolDispatcher
from agent_runtime.conversation_message.message.models import Conversation, ImagePart, ROLE_TOOL, TextPart, ToolUsePart, UniMessage
from agent_runtime.conversation_message.message.openai_protocol import OpenAIAdapter
from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import ResolvedResource, ResourceMeta
from agent_runtime.tool.local_tool_definition import LocalToolDefinition
from agent_runtime.tool.local_tool_service import LocalToolService
from agent_runtime.tool.north_gateway.local_tool_contracts import ToolExecuteRequest
from agent_runtime.tool.north_gateway.local_tool_gateway_impl import LocalToolGatewayImpl
from agent_runtime.tool.tool_execution_models import BaseToolResult


class _Repository:
    def __init__(self, tool_def: LocalToolDefinition) -> None:
        self._tool_def = tool_def

    def list_all(self):
        return [self._tool_def]

    def get_by_name(self, tool_name: str) -> LocalToolDefinition:
        if tool_name != self._tool_def.tool_name:
            raise KeyError(tool_name)
        return self._tool_def


class _SchemaCache:
    def get_all_schemas(self, tool_def):
        return {}


class _AgentDefinition:
    agent_id = "agent-1"
    tool_ids = ("generate_image",)


class _EventPublisher:
    async def publish(self, *args, **kwargs):
        return None


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


def _generate_image_tool() -> BaseToolResult:
    return BaseToolResult(
        success=True,
        execution_status="success",
        output=[
            TextPart(text="Image generation prompt: draw a cat"),
            ImagePart(resource_id="resource-image-1"),
        ],
    )


def test_local_tool_gateway_preserves_multimodal_tool_output_parts():
    async def run_case():
        gateway = LocalToolGatewayImpl(LocalToolService(
            _Repository(LocalToolDefinition(
                tool_name="generate_image",
                description="Generate image.",
                fn=_generate_image_tool,
            )),
            _SchemaCache(),
        ))

        result = await gateway.execute_tool(ToolExecuteRequest(tool_name="generate_image"))

        assert result.output == [
            TextPart(text="Image generation prompt: draw a cat"),
            ImagePart(resource_id="resource-image-1"),
        ]

    asyncio.run(run_case())


def test_local_multimodal_tool_output_renders_to_openai_responses_function_call_output():
    async def run_case():
        gateway = LocalToolGatewayImpl(LocalToolService(
            _Repository(LocalToolDefinition(
                tool_name="generate_image",
                description="Generate image.",
                fn=_generate_image_tool,
            )),
            _SchemaCache(),
        ))
        dispatcher = AgentToolDispatcher(
            local_tool_gateway=gateway,
            event_publisher=_EventPublisher(),
        )
        tool_use = ToolUsePart(name="generate_image", input={}, tool_use_id="call_1")

        dispatch_result = await dispatcher.dispatch_tool_call(
            agent_definition=_AgentDefinition(),
            tool_use=tool_use,
        )
        conv = Conversation(
            id="conv-local-multimodal-tool-output",
            messages=[
                UniMessage(role=ROLE_TOOL, content=[dispatch_result.result_part]),
            ],
        )

        rendered = await OpenAIAdapter().render(conv, _Resolver())

        assert rendered.input == [
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [
                    {"type": "input_text", "text": "Image generation prompt: draw a cat"},
                    {"type": "input_image", "image_url": "https://example.test/image.png"},
                ],
            },
        ]

    asyncio.run(run_case())
