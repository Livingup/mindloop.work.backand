# -*- coding: utf-8 -*-
from __future__ import annotations

from agent_runtime.input_modality import INPUT_MODALITY_IMAGE, INPUT_MODALITY_TEXT
from agent_runtime.llm_model_registry.llm_model_metadata import (
    LLM_MODEL_SOURCE_BUILTIN,
    LlmModelMetadata,
)

BUILTIN_IMAGE_GENERATION_MODEL_ID = "gpt-image-2"


def builtin_llm_model_metadata() -> tuple[LlmModelMetadata, ...]:
    return ()


def builtin_image_generation_model_metadata() -> tuple[LlmModelMetadata, ...]:
    return (
        LlmModelMetadata(
            model_id=BUILTIN_IMAGE_GENERATION_MODEL_ID,
            context_window=32000,
            max_output_tokens=32000,
            group="image_generation",
            source=LLM_MODEL_SOURCE_BUILTIN,
            supports_tool_calling=False,
            supports_structured_output=False,
            supported_input_modalities=(INPUT_MODALITY_TEXT, INPUT_MODALITY_IMAGE),
        ),
    )
