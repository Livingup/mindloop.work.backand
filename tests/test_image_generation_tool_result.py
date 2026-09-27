import asyncio
import unittest
from unittest.mock import patch

from agent_runtime.conversation_message.message.tool_result_output import (
    tool_result_output_to_claude_content,
    tool_result_output_to_gemini_response_parts,
    tool_result_output_to_openai_responses_output,
)
from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import ResolvedResource
from agent_runtime.image_generation.image_generation_tool import GenerateImageRequest, generate_image
from agent_runtime.image_generation.models import GeneratedImageResource, ImageGenerationResult
from agent_runtime.tool.tool_execution_models import ToolRuntimeContext


class _StubResourceResolver:
    def __init__(self, resources: dict[str, ResolvedResource]) -> None:
        self._resources = resources

    async def resolve_resource(self, resource_id: str) -> ResolvedResource:
        return self._resources[resource_id]


class _ImageGenerationService:
    def __init__(self, result: ImageGenerationResult) -> None:
        self.result = result
        self.requests = []

    async def generate_image(self, request):
        self.requests.append(request)
        return self.result


def _result(*, revised_prompt: str | None = None) -> ImageGenerationResult:
    return ImageGenerationResult(
        images=(
            GeneratedImageResource(
                resource_id="resource-image-1",
                object_storage_url="https://example.test/image.png",
                local_storage_path="D:/tmp/image.png",
                mime_type="image/png",
                revised_prompt=revised_prompt,
            ),
        ),
        provider_id="openai",
        binding_id="binding-1",
        model_id="gpt-image-2",
        model_alias="gpt-image-2",
        api_format="openai_images_generations",
        candidate_index=0,
        api_key_index=0,
        attempt_count=1,
        request_params={},
    )


class ImageGenerationToolResultTests(unittest.TestCase):
    def test_generate_image_tool_result_preserves_prompt_text_when_provider_returns_only_image(self) -> None:
        async def run_case():
            service = _ImageGenerationService(_result())
            with patch("agent_runtime.image_generation.image_generation_tool.get_image_generation_service", lambda: service):
                result = await generate_image(
                    GenerateImageRequest(prompt="Create an enterprise SaaS dashboard"),
                    ToolRuntimeContext(
                        request_id="request-1",
                        task_id="task-1",
                        agent_instance_id="agent-1",
                        conversation_id="conversation-1",
                    ),
                )
            return result, service

        result, service = asyncio.run(run_case())

        self.assertTrue(result.success)
        self.assertEqual(
            result.output,
            [
                {"kind": "text", "text": "Image generation prompt: Create an enterprise SaaS dashboard"},
                {"kind": "image", "resource_id": "resource-image-1"},
            ],
        )
        self.assertEqual(service.requests[0].relay_session_id, "conversation-1")

    def test_generate_image_tool_result_preserves_prompt_text_and_revised_prompt(self) -> None:
        async def run_case():
            service = _ImageGenerationService(_result(revised_prompt="A refined prompt"))
            with patch("agent_runtime.image_generation.image_generation_tool.get_image_generation_service", lambda: service):
                return await generate_image(
                    GenerateImageRequest(prompt="Create an enterprise SaaS dashboard"),
                    ToolRuntimeContext(request_id="request-1", task_id="task-1", agent_instance_id="agent-1"),
                )

        result = asyncio.run(run_case())

        self.assertEqual(
            result.output,
            [
                {"kind": "text", "text": "Image generation prompt: Create an enterprise SaaS dashboard"},
                {"kind": "text", "text": "Revised prompt: A refined prompt"},
                {"kind": "image", "resource_id": "resource-image-1"},
            ],
        )

    def test_generate_image_tool_passes_mode_to_request_and_preserves_in_metadata(self) -> None:
        async def run_case():
            service = _ImageGenerationService(_result())
            with patch("agent_runtime.image_generation.image_generation_tool.get_image_generation_service", lambda: service):
                res = await generate_image(
                    GenerateImageRequest(
                        prompt="Edit this image",
                        mode="edit",
                        image_urls=["https://example.test/img.png"],
                    ),
                    ToolRuntimeContext(request_id="req-1", task_id="task-1", agent_instance_id="agent-1"),
                )
                return res, service

        result, service = asyncio.run(run_case())
        self.assertTrue(result.success)
        self.assertEqual(service.requests[0].mode, "edit")
        self.assertEqual(result.metadata["mode"], "generate")
        self.assertEqual(result.metadata["preview_output"]["mode"], "generate")

    def test_tool_result_multimodal_outputs_are_wrapped_with_multimodal_file_markers(self) -> None:
        resolver = _StubResourceResolver(
            {
                "resource-image-1": ResolvedResource(
                    resource_id="resource-image-1",
                    mime_type="image/png",
                    resource_category="image",
                    object_storage_url="https://example.test/image.png",
                    base64_content=None,
                    local_storage_path="D:/tmp/image.png",
                ),
            }
        )
        tool_output = [
            {"kind": "text", "text": "Image generation prompt: Create an enterprise SaaS dashboard"},
            {"kind": "image", "resource_id": "resource-image-1"},
        ]

        openai_items = asyncio.run(tool_result_output_to_openai_responses_output(tool_output, resolver))
        self.assertEqual(
            openai_items,
            [
                {"type": "input_text", "text": "Image generation prompt: Create an enterprise SaaS dashboard"},
                {"type": "input_text", "text": '<multimodal_file type="image" url="https://example.test/image.png">'},
                {"type": "input_image", "image_url": "https://example.test/image.png"},
                {"type": "input_text", "text": "</multimodal_file>"},
            ],
        )

        claude_blocks = asyncio.run(tool_result_output_to_claude_content(tool_output, resolver))
        self.assertEqual(
            claude_blocks,
            [
                {"type": "text", "text": "Image generation prompt: Create an enterprise SaaS dashboard"},
                {"type": "text", "text": '<multimodal_file type="image" url="https://example.test/image.png">'},
                {"type": "image", "source": {"type": "url", "url": "https://example.test/image.png"}},
                {"type": "text", "text": "</multimodal_file>"},
            ],
        )

        gemini_text, gemini_parts = asyncio.run(tool_result_output_to_gemini_response_parts(tool_output, resolver))
        self.assertEqual(
            gemini_text,
            "Image generation prompt: Create an enterprise SaaS dashboard\n"
            '<multimodal_file type="image" url="https://example.test/image.png"></multimodal_file>',
        )
        self.assertEqual(
            gemini_parts,
            [{"fileData": {"mimeType": "image/png", "fileUri": "https://example.test/image.png"}}],
        )

