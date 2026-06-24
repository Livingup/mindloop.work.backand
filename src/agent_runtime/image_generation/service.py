from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent_runtime.file_resource.north_gateway.resource_create_contracts import Base64CreateRequest
from agent_runtime.file_resource.service import FileResourceService
from agent_runtime.image_generation.models import (
    GeneratedImageResource,
    ImageGenerationRequest,
    ImageGenerationResult,
)
from agent_runtime.image_generation.llm_registry_gateway import GPT_IMAGE_AGENT_MODEL_ID
from agent_runtime.image_generation.openai_images_client import OpenAIImagesGenerationClient
from agent_runtime.image_generation.settings_repository import ImageGenerationSettingsRepository
from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_IMAGES_GENERATIONS, API_FORMAT_OPENAI_RESPONSES
from agent_runtime.llm_client.llm_protocol_clients import (
    _aggregate_openai_responses_sse,
    _join_url,
    _json_headers,
    _post_json,
)
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
_GPT_IMAGE_AGENT_MAX_ROUNDS = 5
_GPT_IMAGE_AGENT_SYSTEM_PROMPT = (
    "你是 generate_image 工具内部的临时生图 Agent。"
    "尽最大可能根据用户的需求描述来直接生成图片，不要询问用户。"
    "你只能使用当前请求中提供的内置图片生成工具完成任务。"
    "如果用户需求信息不完整，请自行做合理视觉补全并直接生成图片。"
)

logger = logging.getLogger(__name__)


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

    async def generate_image(
        self,
        request: ImageGenerationRequest,
        *,
        tool_context: Any | None = None,
    ) -> ImageGenerationResult:
        if not request.prompt.strip():
            raise ValueError("prompt must not be empty.")
        settings = self._settings_repository.get_settings()
        if not settings.llm_policy_group_id:
            raise ImageGenerationConfigurationError("Image generation LLM policy is not configured.")
        policy = self._llm_registry_gateway.resolve_execution_policy(settings.llm_policy_group_id)
        request_body = self._build_request_body(request)
        use_edit_endpoint = "images" in request_body
        internal_communication_context = _tool_internal_communication_context(tool_context)
        event_publisher = getattr(tool_context, "event_publisher", None)
        llm_call_id = f"llm-call-image-generation-{uuid.uuid4().hex}"
        last_error: Exception | None = None
        attempt_count = 0
        for candidate_index, candidate in enumerate(policy.candidates):
            resolved = candidate.resolved_model
            self._assert_supported_model(resolved)
            api_key_items = list(resolved.api_keys)
            api_keys: list[str | None] = [item.key for item in api_key_items] or [None]
            tool_count = 1 if _is_gpt_image_agent_model(resolved) else 0
            max_retries = max(0, int(resolved.retry.max_retries_per_key))
            for api_key_index, raw_api_key in enumerate(api_keys):
                api_key_name = api_key_items[api_key_index].name if api_key_index < len(api_key_items) else None
                try:
                    api_key = _resolve_api_key(raw_api_key) if raw_api_key is not None else None
                except Exception as exc:
                    last_error = exc
                    continue
                for retry_index in range(max_retries + 1):
                    attempt_count += 1
                    attempt_started_at = time.perf_counter()
                    llm_request_id = _build_image_generation_llm_request_id(
                        llm_call_id=llm_call_id,
                        candidate_index=candidate_index,
                        api_key_index=api_key_index,
                        retry_index=retry_index,
                    )
                    request_payload = _build_image_generation_llm_request_payload(
                        llm_call_id=llm_call_id,
                        llm_request_id=llm_request_id,
                        resolved=resolved,
                        candidate_index=candidate_index,
                        api_key_index=api_key_index,
                        api_key_label=_image_generation_api_key_label(api_key_name),
                        retry_index=retry_index,
                        attempt_count=attempt_count,
                        tool_count=tool_count,
                        api_key_count=len(api_keys),
                        tool_context=tool_context,
                    )
                    await _publish_internal_llm_event(
                        event_publisher,
                        internal_communication_context,
                        "llm.request.started",
                        request_payload,
                        request,
                    )
                    try:
                        if _is_gpt_image_agent_model(resolved):
                            response = await self._run_gpt_image_agent(
                                resolved_model=resolved,
                                api_key=api_key,
                                request=request,
                                request_body=request_body,
                            )
                        elif use_edit_endpoint:
                            response = await self._images_client.edit(
                                resolved_model=resolved,
                                api_key=api_key,
                                body=request_body,
                            )
                        else:
                            response = await self._images_client.generate(
                                resolved_model=resolved,
                                api_key=api_key,
                                body=request_body,
                            )
                        images = self._ingest_images(response, output_format=str(request_body.get("output_format") or _DEFAULT_OUTPUT_FORMAT))
                        if not images:
                            raise RuntimeError("OpenAI Images API returned no image data.")
                        elapsed_ms = int((time.perf_counter() - attempt_started_at) * 1000)
                        await _publish_internal_llm_event(
                            event_publisher,
                            internal_communication_context,
                            "llm.response.started",
                            {**request_payload, "content_type": "image_generation"},
                            request,
                        )
                        await _publish_internal_llm_event(
                            event_publisher,
                            internal_communication_context,
                            "llm.response.completed",
                            {
                                **request_payload,
                                "assistant_part_count": len(images),
                                "thinking_part_count": 0,
                                "tool_use_part_count": tool_count,
                                "duration_ms": elapsed_ms,
                                "call_duration_ms": elapsed_ms,
                            },
                            request,
                        )
                        logger.info(
                            "Image generation attempt succeeded: policy=%s candidate_index=%s provider=%s binding=%s api_key_index=%s retry_index=%s attempt_count=%s elapsed_ms=%s",
                            settings.llm_policy_group_id,
                            candidate_index,
                            resolved.provider_id,
                            resolved.binding_id,
                            api_key_index,
                            retry_index,
                            attempt_count,
                            elapsed_ms,
                        )
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
                            request_params=_sanitize_request_params(request_body),
                        )
                    except Exception as exc:
                        last_error = exc
                        elapsed_ms = int((time.perf_counter() - attempt_started_at) * 1000)
                        will_retry = retry_index < max_retries
                        await _publish_internal_llm_event(
                            event_publisher,
                            internal_communication_context,
                            "llm.request.failed",
                            {
                                **request_payload,
                                "error_type": exc.__class__.__name__,
                                "error_message": str(exc),
                                "failed_stage": "image_generation",
                                "partial_response": False,
                                "retryable": will_retry,
                                "will_retry": will_retry,
                                "final": not will_retry,
                                "duration_ms": elapsed_ms,
                            },
                            request,
                        )
                        logger.warning(
                            "Image generation attempt failed: policy=%s candidate_index=%s provider=%s binding=%s api_key_index=%s retry_index=%s max_retries=%s attempt_count=%s elapsed_ms=%s error=%s: %s",
                            settings.llm_policy_group_id,
                            candidate_index,
                            resolved.provider_id,
                            resolved.binding_id,
                            api_key_index,
                            retry_index,
                            max_retries,
                            attempt_count,
                            elapsed_ms,
                            exc.__class__.__name__,
                            exc,
                        )
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
        images = _image_references_from_request(request.image_urls, request.image_paths)
        if images:
            body["images"] = images
        mask = _mask_reference_from_request(request.mask_url, request.mask_path)
        if mask is not None:
            if not images:
                raise ValueError("mask_url or mask_path requires at least one input image.")
            body["mask"] = mask
        if request.input_fidelity is not None and not images:
            raise ValueError("input_fidelity requires at least one input image.")
        for key, value in {
            "background": request.background,
            "output_compression": request.output_compression,
            "input_fidelity": request.input_fidelity,
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
        if resolved.api_format == API_FORMAT_OPENAI_IMAGES_GENERATIONS:
            return
        if _is_gpt_image_agent_model(resolved):
            return
        if resolved.api_format == API_FORMAT_OPENAI_RESPONSES:
            raise ImageGenerationConfigurationError(
                f"Image generation only supports model_id={GPT_IMAGE_AGENT_MODEL_ID} for api_format={API_FORMAT_OPENAI_RESPONSES}: {resolved.provider_id}/{resolved.binding_id}"
            )
        else:
            raise ImageGenerationConfigurationError(
                f"Image generation only supports api_format={API_FORMAT_OPENAI_IMAGES_GENERATIONS} or {API_FORMAT_OPENAI_RESPONSES}/{GPT_IMAGE_AGENT_MODEL_ID}: {resolved.provider_id}/{resolved.binding_id}"
            )

    async def _run_gpt_image_agent(
        self,
        *,
        resolved_model: ResolvedLlmProviderModel,
        api_key: str | None,
        request: ImageGenerationRequest,
        request_body: dict[str, Any],
    ) -> dict[str, Any]:
        resolved_model = _with_gpt_image_agent_relay_session_header(
            resolved_model,
            request.relay_session_id,
        )
        input_items = _build_gpt_image_agent_initial_input(request, request_body)
        request_params = _merge_gpt_image_agent_request_params(resolved_model)
        output_format = str(request_body.get("output_format") or _DEFAULT_OUTPUT_FORMAT)
        for round_index in range(_GPT_IMAGE_AGENT_MAX_ROUNDS):
            body: dict[str, Any] = {
                **request_params,
                "model": resolved_model.model_alias,
                "input": input_items,
                "instructions": _GPT_IMAGE_AGENT_SYSTEM_PROMPT,
                "tools": [{"type": "image_generation"}],
                "tool_choice": {"type": "image_generation"},
                "store": False,
                "stream": True,
            }
            if resolved_model.max_output_tokens:
                body.setdefault("max_output_tokens", resolved_model.max_output_tokens)
            raw_response = await _post_openai_responses_json(
                resolved_model=resolved_model,
                api_key=api_key,
                body=body,
            )
            data = _extract_gpt_image_agent_image_data(raw_response, output_format=output_format)
            if data:
                return {"data": data}
            input_items.extend(_replayable_gpt_image_agent_output_items(raw_response))
            input_items.append(_build_gpt_image_agent_continue_input(round_index))
        raise RuntimeError(f"gpt-5.5 image agent did not return image data after {_GPT_IMAGE_AGENT_MAX_ROUNDS} rounds.")


def _tool_internal_linkage_payload(tool_context: Any | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"execution_scope": "tool_internal"}
    for attr_name, payload_key in (
        ("parent_tool_call_id", "parent_tool_call_id"),
        ("tool_name", "tool_name"),
        ("parent_llm_request_id", "parent_llm_request_id"),
        ("parent_llm_call_id", "parent_llm_call_id"),
    ):
        value = getattr(tool_context, attr_name, None)
        if value is not None:
            payload[payload_key] = value
    return payload


def _tool_internal_communication_context(tool_context: Any | None) -> Any | None:
    communication_context = getattr(tool_context, "communication_context", None)
    if communication_context is None:
        return None
    with_metadata = getattr(communication_context, "with_metadata", None)
    if not callable(with_metadata):
        return communication_context
    return with_metadata(**_tool_internal_linkage_payload(tool_context))


def _build_image_generation_llm_request_id(
    *,
    llm_call_id: str,
    candidate_index: int,
    api_key_index: int,
    retry_index: int,
) -> str:
    return f"{llm_call_id}:candidate-{candidate_index}:key-{api_key_index}:retry-{retry_index}"


def _build_image_generation_llm_request_payload(
    *,
    llm_call_id: str,
    llm_request_id: str,
    resolved: ResolvedLlmProviderModel,
    candidate_index: int,
    api_key_index: int,
    api_key_label: str | None,
    retry_index: int,
    attempt_count: int,
    tool_count: int,
    api_key_count: int,
    tool_context: Any | None,
) -> dict[str, Any]:
    return {
        "llm_call_id": llm_call_id,
        "llm_request_id": llm_request_id,
        "candidate_index": candidate_index,
        "provider_id": resolved.provider_id,
        "binding_id": resolved.binding_id,
        "model_id": resolved.model_id,
        "model_alias": resolved.model_alias,
        "api_format": resolved.api_format,
        "api_key_index": api_key_index,
        "api_key_label": api_key_label,
        "retry_index": retry_index,
        "attempt_count": attempt_count,
        "tool_count": tool_count,
        "api_key_count": api_key_count,
        **_tool_internal_linkage_payload(tool_context),
    }


def _image_generation_api_key_label(api_key_name: str | None) -> str | None:
    normalized = api_key_name.strip() if isinstance(api_key_name, str) else ""
    return normalized or None


async def _publish_internal_llm_event(
    event_publisher: Any | None,
    communication_context: Any | None,
    event_type: str,
    payload: dict[str, Any],
    request: ImageGenerationRequest,
) -> None:
    if event_publisher is None or communication_context is None:
        return
    try:
        await event_publisher.publish(
            communication_context,
            event_type,
            payload,
            source="image_generation",
            metadata={"conversation_id": request.relay_session_id},
        )
    except Exception:
        logger.exception("Failed to publish image generation internal LLM event: event_type=%s", event_type)


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


def _image_references_from_request(image_urls: tuple[str, ...], image_paths: tuple[str, ...]) -> list[dict[str, str]]:
    references = [{"image_url": _normalize_image_url(url)} for url in image_urls if url.strip()]
    references.extend({"image_url": _image_path_to_data_url(path)} for path in image_paths if path.strip())
    if len(references) > 16:
        raise ValueError("image edit supports at most 16 input images.")
    return references


def _mask_reference_from_request(mask_url: str | None, mask_path: str | None) -> dict[str, str] | None:
    normalized_url = mask_url.strip() if isinstance(mask_url, str) else ""
    normalized_path = mask_path.strip() if isinstance(mask_path, str) else ""
    if normalized_url and normalized_path:
        raise ValueError("mask_url and mask_path cannot both be provided.")
    if normalized_url:
        return {"image_url": _normalize_image_url(normalized_url)}
    if normalized_path:
        return {"image_url": _image_path_to_data_url(normalized_path)}
    return None


def _normalize_image_url(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("http://") or normalized.startswith("https://") or normalized.startswith("data:image/"):
        return normalized
    raise ValueError("image URLs must be http(s) URLs or data:image/... URLs.")


def _image_path_to_data_url(path: str) -> str:
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise ValueError(f"image path does not exist or is not a file: {path}")
    mime_type, _encoding = mimetypes.guess_type(str(file_path))
    if not mime_type or not mime_type.startswith("image/"):
        raise ValueError(f"image path must point to an image file: {path}")
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _sanitize_request_params(value: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(value)
    if isinstance(sanitized.get("images"), list):
        sanitized["images"] = [_sanitize_image_reference(item) for item in sanitized["images"]]
    if isinstance(sanitized.get("mask"), dict):
        sanitized["mask"] = _sanitize_image_reference(sanitized["mask"])
    return sanitized


def _sanitize_image_reference(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    image_url = value.get("image_url")
    if not isinstance(image_url, str) or not image_url.startswith("data:image/"):
        return dict(value)
    return {"image_url": f"{image_url.split(',', 1)[0]},<base64 omitted>"}


def _is_gpt_image_agent_model(resolved: ResolvedLlmProviderModel) -> bool:
    return resolved.model_id == GPT_IMAGE_AGENT_MODEL_ID and resolved.api_format == API_FORMAT_OPENAI_RESPONSES


def _with_gpt_image_agent_relay_session_header(
    resolved_model: ResolvedLlmProviderModel,
    relay_session_id: str | None,
) -> ResolvedLlmProviderModel:
    headers = dict(resolved_model.custom_headers)
    if any(key.lower() == "x-relay-session-id" for key in headers):
        return resolved_model
    session_id = _normalize_relay_session_id(relay_session_id) or _fallback_gpt_image_agent_relay_session_id(resolved_model)
    headers["x-relay-session-id"] = session_id
    return replace(resolved_model, custom_headers=headers)


def _normalize_relay_session_id(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _fallback_gpt_image_agent_relay_session_id(resolved_model: ResolvedLlmProviderModel) -> str:
    return f"image-generation:{resolved_model.provider_id}:{resolved_model.binding_id}"


async def _post_openai_responses_json(
    *,
    resolved_model: ResolvedLlmProviderModel,
    api_key: str | None,
    body: dict[str, Any],
) -> dict[str, Any]:
    url = _join_url(resolved_model.api_base_url, "responses")
    headers = _json_headers(resolved_model.custom_headers)
    if api_key:
        headers.setdefault("Authorization", f"Bearer {api_key}")
    response = await _post_json(
        url,
        headers,
        body,
        sse_aggregator=_aggregate_openai_responses_sse,
    )
    if not isinstance(response, dict):
        raise RuntimeError("OpenAI Responses image agent response must be a JSON object.")
    return response


def _merge_gpt_image_agent_request_params(resolved_model: ResolvedLlmProviderModel) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    _merge_json_field(merged, resolved_model.custom_request_params, "custom_request_params")
    _merge_json_field(merged, resolved_model.thinking, "thinking")
    return merged


def _merge_json_field(target: dict[str, Any], value: str | None, field_name: str) -> None:
    if value is None or not value.strip():
        return
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object.")
    target.update(parsed)


def _build_gpt_image_agent_initial_input(request: ImageGenerationRequest, request_body: dict[str, Any]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": _build_gpt_image_agent_prompt(request, request_body),
        }
    ]
    for image in request_body.get("images") or []:
        if isinstance(image, dict) and isinstance(image.get("image_url"), str):
            content.append({"type": "input_image", "image_url": image["image_url"]})
    mask = request_body.get("mask")
    if isinstance(mask, dict) and isinstance(mask.get("image_url"), str):
        content.append({"type": "input_image", "image_url": mask["image_url"]})
    return [{"type": "message", "role": "user", "content": content}]


def _build_gpt_image_agent_prompt(request: ImageGenerationRequest, request_body: dict[str, Any]) -> str:
    lines = [
        "请根据以下 generate_image 工具参数直接生成图片，不要询问用户。",
        f"用户生图需求: {request.prompt}",
        f"输出数量: {request_body.get('n')}",
        f"输出尺寸: {request_body.get('size')}",
        f"输出格式: {request_body.get('output_format')}",
        f"背景模式: {request_body.get('background', 'auto')}",
        f"质量: {request_body.get('quality')}",
    ]
    if request_body.get("output_compression") is not None:
        lines.append(f"输出压缩: {request_body.get('output_compression')}")
    if request_body.get("input_fidelity") is not None:
        lines.append(f"输入参考/编辑保真度: {request_body.get('input_fidelity')}")
    if request_body.get("images"):
        lines.append("已附加输入/参考/编辑图片，请结合这些图片完成生成或编辑。")
    if request_body.get("mask"):
        lines.append("已附加局部编辑 mask 图片，请按 mask 限定区域完成编辑。")
    return "\n".join(lines)


def _build_gpt_image_agent_continue_input(round_index: int) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": (
                    f"第 {round_index + 1} 轮没有返回图片结果。"
                    "请不要询问用户，不要只返回说明文字，立刻调用内置图片生成工具并返回图片。"
                ),
            }
        ],
    }


def _extract_gpt_image_agent_image_data(raw_response: dict[str, Any], *, output_format: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in _iter_dicts_deep(raw_response.get("output")):
        item_type = item.get("type")
        if item_type not in {"image_generation_call", "image_generation"}:
            continue
        result = item.get("result") or item.get("image") or item.get("b64_json")
        if isinstance(result, str) and result.strip():
            b64_json, mime_type = _normalize_generated_image_result(result, output_format)
            items.append({"b64_json": b64_json, "revised_prompt": _text_or_none(item.get("revised_prompt")) or _text_or_none(item.get("prompt"))})
            continue
        if isinstance(result, dict):
            b64_json = result.get("b64_json") or result.get("base64") or result.get("data")
            if isinstance(b64_json, str) and b64_json.strip():
                normalized, _mime_type = _normalize_generated_image_result(b64_json, output_format)
                items.append({"b64_json": normalized, "revised_prompt": _text_or_none(result.get("revised_prompt")) or _text_or_none(result.get("prompt"))})
    return items


def _normalize_generated_image_result(value: str, output_format: str) -> tuple[str, str]:
    stripped = value.strip()
    if stripped.startswith("data:image/"):
        header, _separator, data = stripped.partition(",")
        mime_type = header.removeprefix("data:").split(";", 1)[0] or _mime_type_for_output_format(output_format)
        return data, mime_type
    return stripped, _mime_type_for_output_format(output_format)


def _replayable_gpt_image_agent_output_items(raw_response: dict[str, Any]) -> list[dict[str, Any]]:
    output = raw_response.get("output")
    if not isinstance(output, list):
        return []
    replayable: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"reasoning", "message", "function_call"}:
            replayable.append(copy.deepcopy(item))
    return replayable


def _iter_dicts_deep(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts_deep(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts_deep(child)


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


async def _sleep_before_retry(initial_delay: float, max_delay: float, retry_index: int) -> None:
    delay = max(0.0, float(initial_delay)) * (2 ** max(0, retry_index - 1))
    if max_delay > 0:
        delay = min(delay, float(max_delay))
    if delay > 0:
        await asyncio.sleep(delay)
