from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ImageGenerationRequest:
    prompt: str
    mode: str | None = None
    size: str | None = None
    background: str | None = None
    output_format: str | None = None
    output_compression: int | None = None
    n: int = 1
    image_urls: tuple[str, ...] = ()
    image_paths: tuple[str, ...] = ()
    mask_url: str | None = None
    mask_path: str | None = None
    input_fidelity: str | None = None
    relay_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedImageResource:
    resource_id: str
    object_storage_url: str
    local_storage_path: str
    mime_type: str
    revised_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class ImageGenerationResult:
    images: tuple[GeneratedImageResource, ...]
    provider_id: str
    binding_id: str
    model_id: str
    model_alias: str
    api_format: str
    candidate_index: int
    api_key_index: int | None
    attempt_count: int
    request_params: dict[str, Any]
    mode: str = "generate"
