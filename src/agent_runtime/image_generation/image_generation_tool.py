from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agent_runtime.conversation_message.message.models import ImagePart
from agent_runtime.image_generation.models import ImageGenerationRequest
from agent_runtime.image_generation.shared_service import get_image_generation_service
from agent_runtime.tool import tool
from agent_runtime.tool.tool_execution_models import BaseToolResult, ToolRuntimeContext


GENERATE_IMAGE_TOOL_DESCRIPTION = """Generate one or more images from a text prompt using the configured gpt-image-2 image generation policy, then store generated images as file resources.

Agent-controlled parameters:
- prompt: required image description, generated from the user's visual intent.
- n: optional image count, integer 1-10, defaults to 1.
- size: optional output size, defaults to 1024x1024. For gpt-image-2, use auto, one of the standard sizes 1024x1024 / 1536x1024 / 1024x1536, or a custom WIDTHxHEIGHT string. For custom sizes, width and height must both be divisible by 16, aspect ratio must be between 1:3 and 3:1, maximum edge is 3840, maximum pixels are 3840x2160, and resolutions above 2560x1440 are experimental.
- background: optional background mode, one of auto / transparent / opaque. Use transparent only when the user asks for transparent background; transparent output requires png or webp.
- output_format: optional image format, one of png / jpeg / webp, defaults to png.
- output_compression: optional compression level, integer 0-100, only for jpeg/webp output; do not set it for png.

Runtime-fixed parameters:
- quality is always high and is not controlled by the Agent.
- model/provider/api key/retry/candidate policy come from image generation settings and the selected image generation LLM registry policy.
- stream and partial_images are not used by this tool; arbitrary request_params, moderation, and user are not accepted."""


class GenerateImageRequest(BaseModel):
    prompt: str = Field(description="Image description generated from the user's visual intent.")
    size: str | None = Field(default=None, description="Requested output size.")
    background: Literal["auto", "transparent", "opaque"] | None = Field(default=None, description="Requested background mode.")
    output_format: Literal["png", "jpeg", "webp"] | None = Field(default=None, description="Requested output image format.")
    output_compression: int | None = Field(default=None, ge=0, le=100, description="Requested jpeg/webp compression level.")
    n: int = Field(default=1, ge=1, le=10, description="Requested image count.")


@tool(
    name="generate_image",
    description=GENERATE_IMAGE_TOOL_DESCRIPTION,
    group="image_generation",
    public=True,
    always_enabled=True,
)
async def generate_image(request: GenerateImageRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    service = get_image_generation_service()
    result = await service.generate_image(
        ImageGenerationRequest(
            prompt=request.prompt,
            size=request.size,
            background=request.background,
            output_format=request.output_format,
            output_compression=request.output_compression,
            n=request.n,
        )
    )
    resources = [
        {
            "resource_id": image.resource_id,
            "object_storage_url": image.object_storage_url,
            "local_storage_path": image.local_storage_path,
            "mime_type": image.mime_type,
            "revised_prompt": image.revised_prompt,
        }
        for image in result.images
    ]
    output = {
        "ui_kind": "image_generation",
        "prompt": request.prompt,
        "resources": resources,
        "provider_id": result.provider_id,
        "binding_id": result.binding_id,
        "model_id": result.model_id,
        "model_alias": result.model_alias,
        "candidate_index": result.candidate_index,
        "api_key_index": result.api_key_index,
        "attempt_count": result.attempt_count,
    }
    return BaseToolResult(
        success=True,
        execution_status="success",
        output=[ImagePart(resource_id=image.resource_id) for image in result.images],
        metadata={
            "ui_kind": "image_generation",
            "preview_output": output,
            "request_id": ctx.request_id,
            "task_id": ctx.task_id,
            "agent_instance_id": ctx.agent_instance_id,
        },
    )
