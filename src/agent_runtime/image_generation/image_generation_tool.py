from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent_runtime.image_generation.models import ImageGenerationRequest
from agent_runtime.image_generation.shared_service import get_image_generation_service
from agent_runtime.tool import tool
from agent_runtime.tool.tool_execution_models import BaseToolResult, FrontendMessage, ToolRuntimeContext


class GenerateImageRequest(BaseModel):
    prompt: str = Field(description="Text prompt describing the image to generate.")
    size: str | None = Field(default=None, description="Image size, for example 1024x1024, 1536x1024, 1024x1536, or custom WIDTHxHEIGHT supported by the selected model.")
    quality: str | None = Field(default=None, description="Image quality, for example auto, high, medium, or low.")
    background: str | None = Field(default=None, description="Background mode: auto, transparent, or opaque.")
    output_format: str | None = Field(default=None, description="Output image format: png, jpeg, or webp.")
    output_compression: int | None = Field(default=None, description="Compression level 0-100 for webp/jpeg outputs.")
    moderation: str | None = Field(default=None, description="Moderation level: auto or low.")
    n: int = Field(default=1, description="Number of images to generate, from 1 to 10.")
    user: str | None = Field(default=None, description="Optional end-user identifier for provider abuse monitoring.")
    request_params: dict[str, Any] = Field(default_factory=dict, description="Advanced provider-specific request parameters. model, stream, and partial_images are ignored.")


@tool(
    name="generate_image",
    description="Generate one or more images from a text prompt using the configured image generation LLM policy, then store the generated images as file resources.",
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
            quality=request.quality,
            background=request.background,
            output_format=request.output_format,
            output_compression=request.output_compression,
            moderation=request.moderation,
            n=request.n,
            user=request.user,
            request_params=dict(request.request_params),
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
        output=output,
        frontend_message=FrontendMessage(
            text=f"已生成 {len(resources)} 张图片。",
            text_format="markdown",
            metadata={"ui_kind": "image_generation", "resources": resources},
        ),
        metadata={
            "ui_kind": "image_generation",
            "request_id": ctx.request_id,
            "task_id": ctx.task_id,
            "agent_instance_id": ctx.agent_instance_id,
        },
    )
