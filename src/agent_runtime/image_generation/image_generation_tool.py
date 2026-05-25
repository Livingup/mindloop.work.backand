from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agent_runtime.image_generation.models import ImageGenerationRequest
from agent_runtime.image_generation.shared_service import get_image_generation_service
from agent_runtime.tool import tool
from agent_runtime.tool.tool_execution_models import BaseToolResult, FrontendMessage, ToolRuntimeContext


class GenerateImageRequest(BaseModel):
    prompt: str = Field(description="Required text prompt describing the image to generate. The Agent must set this per call according to the user's image request.")
    size: Literal["1024x1024", "1536x1024", "1024x1536"] | None = Field(default=None, description="Optional gpt-image-2 output size controlled by the Agent per call. Allowed values: 1024x1024, 1536x1024, 1024x1536. If omitted, the runtime uses 1024x1024.")
    quality: Literal["auto", "high", "medium", "low"] | None = Field(default=None, description="Optional gpt-image-2 quality controlled by the Agent per call. Allowed values: auto, high, medium, low. If omitted, the runtime uses auto.")
    background: Literal["auto", "transparent", "opaque"] | None = Field(default=None, description="Optional gpt-image-2 background mode controlled by the Agent per call. Allowed values: auto, transparent, opaque. Use transparent only when the user wants transparent-background output.")
    output_format: Literal["png", "jpeg", "webp"] | None = Field(default=None, description="Optional output image format controlled by the Agent per call. Allowed values: png, jpeg, webp. If omitted, the runtime uses png.")
    output_compression: int | None = Field(default=None, ge=0, le=100, description="Optional compression level controlled by the Agent per call for jpeg/webp output only. Allowed integer range: 0-100. Do not set it for png output.")
    n: int = Field(default=1, ge=1, le=10, description="Optional number of images controlled by the Agent per call. Allowed integer range: 1-10. If the user does not ask for multiple images, use 1.")


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
