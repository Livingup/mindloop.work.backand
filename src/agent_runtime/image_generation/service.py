from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from agent_runtime.file_resource.north_gateway.resource_create_contracts import Base64CreateRequest
from agent_runtime.file_resource.service import FileResourceService
from agent_runtime.image_generation.models import (
    GeneratedImageResource,
    ImageGenerationRequest,
    ImageGenerationResult,
)
from agent_runtime.image_generation.openai_images_client import OpenAIImagesGenerationClient
from agent_runtime.image_generation.settings_repository import ImageGenerationSettingsRepository
from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_IMAGES_GENERATIONS
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import LlmModelRegistryGateway
from agent_runtime.llm_model_registry.llm_model_registry_service import ResolvedLlmProviderModel


_ENV_VAR_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*))?\}$")
_SIZE_PATTERN = re.compile(r"^([1-9]\d*)x([1-9]\d*)$")
_MIME_BY_OUTPUT_FORMAT = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
_DEFAULT_OUTPUT_FORMAT = "png"
_DEFAULT_SIZE = "1024x1024"
_DEFAULT_QUALITY = "high"
_STANDARD_SIZES = {"1024x1024", "1536x1024", "1024x1536"}
_MAX_SIZE_EDGE = 3840
_MAX_SIZE_PIXELS = 3840 * 2160


class ImageGenerationConfigurationError(ValueError):
    pass


class ImageGenerationService:
    def __init__(
        self,
        *,
        settings_repository: ImageGenerationSettingsRepository,
        llm_registry_gateway: LlmModelRegistryGateway,
        file_resource_service: FileResourceService,
        images_client: OpenAIImagesGenerationClient | None = None,
    ) -> None:
        self._settings_repository = settings_repository
        self._llm_registry_gateway = llm_registry_gateway
        self._file_resource_service = file_resource_service
        self._images_client = images_client or OpenAIImagesGenerationClient()

    def get_settings(self):
        return self._settings_repository.get_settings()

    def save_settings(
        self,
        *,
        llm_policy_group_id: str | None,
    ):
        normalized_policy_id = llm_policy_group_id.strip() if isinstance(llm_policy_group_id, str) else None
        if normalized_policy_id == "":
            normalized_policy_id = None
        if normalized_policy_id is not None:
            self._require_usable_policy(normalized_policy_id)
        return self._settings_repository.save_settings(
            llm_policy_group_id=normalized_policy_id,
        )

    def validate_policy(self, policy_group_id: str) -> None:
        self._require_usable_policy(policy_group_id)

    async def generate_image(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        if not request.prompt.strip():
            raise ValueError("prompt must not be empty.")
        settings = self._settings_repository.get_settings()
        if not settings.llm_policy_group_id:
            raise ImageGenerationConfigurationError("Image generation LLM policy is not configured.")
        policy = self._llm_registry_gateway.resolve_execution_policy(settings.llm_policy_group_id)
        request_body = self._build_request_body(request)
        last_error: Exception | None = None
        attempt_count = 0
        for candidate_index, candidate in enumerate(policy.candidates):
            resolved = candidate.resolved_model
            self._assert_supported_model(resolved)
            api_key_items = list(resolved.api_keys)
            api_keys: list[str | None] = [item.key for item in api_key_items] or [None]
            max_retries = max(0, int(resolved.retry.max_retries_per_key))
            for api_key_index, raw_api_key in enumerate(api_keys):
                try:
                    api_key = _resolve_api_key(raw_api_key) if raw_api_key is not None else None
                except Exception as exc:
                    last_error = exc
                    continue
                for retry_index in range(max_retries + 1):
                    attempt_count += 1
                    try:
                        response = await self._images_client.generate(
                            resolved_model=resolved,
                            api_key=api_key,
                            body=request_body,
                        )
                        images = self._ingest_images(response, output_format=str(request_body.get("output_format") or _DEFAULT_OUTPUT_FORMAT))
                        if not images:
                            raise RuntimeError("OpenAI Images API returned no image data.")
                        return ImageGenerationResult(
                            images=tuple(images),
                            provider_id=resolved.provider_id,
                            binding_id=resolved.binding_id,
                            model_id=resolved.model_id,
                            model_alias=resolved.model_alias,
                            api_format=resolved.api_format,
                            candidate_index=candidate_index,
                            api_key_index=api_key_index,
                            attempt_count=attempt_count,
                            request_params=dict(request_body),
                        )
                    except Exception as exc:
                        last_error = exc
                        if retry_index < max_retries:
                            await _sleep_before_retry(resolved.retry.initial_delay_seconds, resolved.retry.max_delay_seconds, retry_index)
                            continue
                        break
        if last_error is not None:
            raise RuntimeError(f"Image generation failed after {attempt_count} attempts: {last_error}") from last_error
        raise RuntimeError("Image generation failed: no usable policy candidate.")

    def _build_request_body(
        self,
        request: ImageGenerationRequest,
    ) -> dict[str, Any]:
        output_format = request.output_format or _DEFAULT_OUTPUT_FORMAT
        if request.background == "transparent" and output_format not in {"png", "webp"}:
            raise ValueError("background=transparent requires output_format to be png or webp.")
        if request.output_compression is not None and output_format not in {"jpeg", "webp"}:
            raise ValueError("output_compression is only supported for jpeg or webp output.")
        body: dict[str, Any] = {
            "prompt": request.prompt,
            "n": _bounded_count(request.n),
            "size": _normalize_size(request.size),
            "quality": _DEFAULT_QUALITY,
            "output_format": output_format,
        }
        for key, value in {
            "background": request.background,
            "output_compression": request.output_compression,
        }.items():
            if value is not None:
                body[key] = value
        return body

    def _ingest_images(self, response: dict[str, Any], *, output_format: str) -> list[GeneratedImageResource]:
        data = response.get("data")
        if not isinstance(data, list):
            raise RuntimeError("OpenAI Images API response data must be a list.")
        mime_type = _mime_type_for_output_format(output_format)
        create_requests: list[Base64CreateRequest] = []
        revised_prompts: list[str | None] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            b64_json = item.get("b64_json")
            if not isinstance(b64_json, str) or not b64_json.strip():
                continue
            create_requests.append(Base64CreateRequest(resource_base64_content=b64_json, resource_mime_type=mime_type))
            revised_prompt = item.get("revised_prompt")
            revised_prompts.append(str(revised_prompt) if revised_prompt is not None else None)
        if not create_requests:
            return []
        created = self._file_resource_service.create_resources_from_base64_contents(create_requests)
        return [
            GeneratedImageResource(
                resource_id=item.resource_id,
                object_storage_url=item.object_storage_url,
                local_storage_path=item.local_storage_path,
                mime_type=mime_type,
                revised_prompt=revised_prompts[index] if index < len(revised_prompts) else None,
            )
            for index, item in enumerate(created)
        ]

    def _require_usable_policy(self, policy_group_id: str) -> None:
        policy = self._llm_registry_gateway.resolve_execution_policy(policy_group_id)
        if not policy.candidates:
            raise ImageGenerationConfigurationError("Image generation LLM policy has no candidates.")
        for candidate in policy.candidates:
            self._assert_supported_model(candidate.resolved_model)

    def _assert_supported_model(self, resolved: ResolvedLlmProviderModel) -> None:
        if resolved.api_format != API_FORMAT_OPENAI_IMAGES_GENERATIONS:
            raise ImageGenerationConfigurationError(
                f"Image generation only supports api_format={API_FORMAT_OPENAI_IMAGES_GENERATIONS}: {resolved.provider_id}/{resolved.binding_id}"
            )


def _bounded_count(value: int) -> int:
    count = int(value)
    if count < 1 or count > 10:
        raise ValueError("n must be between 1 and 10.")
    return count


def _normalize_size(value: str | None) -> str:
    if value is None:
        return _DEFAULT_SIZE
    normalized = value.strip().lower()
    if normalized == "auto" or normalized in _STANDARD_SIZES:
        return normalized
    match = _SIZE_PATTERN.match(normalized)
    if match is None:
        raise ValueError("size must be auto or a WIDTHxHEIGHT string.")
    width = int(match.group(1))
    height = int(match.group(2))
    if width % 16 != 0 or height % 16 != 0:
        raise ValueError("size width and height must both be divisible by 16.")
    if width > _MAX_SIZE_EDGE or height > _MAX_SIZE_EDGE:
        raise ValueError("size width and height must not exceed 3840.")
    if width * height > _MAX_SIZE_PIXELS:
        raise ValueError("size must not exceed 3840x2160 pixels.")
    if width * 3 < height or height * 3 < width:
        raise ValueError("size aspect ratio must be between 1:3 and 3:1.")
    return normalized


def _mime_type_for_output_format(output_format: str) -> str:
    normalized = output_format.strip().lower()
    if normalized not in _MIME_BY_OUTPUT_FORMAT:
        raise ValueError("output_format must be one of: png, jpeg, webp.")
    return _MIME_BY_OUTPUT_FORMAT[normalized]


def _resolve_api_key(key: str) -> str:
    match = _ENV_VAR_PATTERN.match(key.strip())
    if match is None:
        return key
    env_key = match.group(1)
    default = match.group(2)
    env_value = os.environ.get(env_key)
    if env_value is not None:
        return env_value
    if default is not None:
        return default
    raise ValueError(f"LLM API Key environment variable is not set: {env_key}")


async def _sleep_before_retry(initial_delay: float, max_delay: float, retry_index: int) -> None:
    delay = max(0.0, float(initial_delay)) * (2 ** max(0, retry_index - 1))
    if max_delay > 0:
        delay = min(delay, float(max_delay))
    if delay > 0:
        await asyncio.sleep(delay)
