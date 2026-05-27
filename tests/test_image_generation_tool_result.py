import asyncio
import unittest
from unittest.mock import patch

from agent_runtime.conversation_message.message.models import ImagePart, TextPart
from agent_runtime.image_generation.image_generation_tool import GenerateImageRequest, generate_image
from agent_runtime.image_generation.models import GeneratedImageResource, ImageGenerationResult
from agent_runtime.tool.tool_execution_models import ToolRuntimeContext


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
                TextPart(text="Image generation prompt: Create an enterprise SaaS dashboard"),
                ImagePart(resource_id="resource-image-1"),
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
                TextPart(text="Image generation prompt: Create an enterprise SaaS dashboard"),
                TextPart(text="Revised prompt: A refined prompt"),
                ImagePart(resource_id="resource-image-1"),
            ],
        )
