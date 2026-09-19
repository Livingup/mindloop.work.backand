import unittest

from agent_runtime.codex_oauth.models import CODEX_PROVIDER_ID
from agent_runtime.image_generation.llm_registry_gateway import (
    ImageGenerationLlmRegistryGateway,
    _is_supported_image_generation_binding,
    _is_supported_image_generation_resolved_model,
)
from agent_runtime.llm_api_protocol import (
    API_FORMAT_CODEX_RESPONSES,
    API_FORMAT_OPENAI_CHAT_COMPLETIONS,
    API_FORMAT_OPENAI_IMAGES_GENERATIONS,
    API_FORMAT_OPENAI_RESPONSES,
)
from agent_runtime.llm_model_registry.llm_model_metadata import (
    LLM_MODEL_SOURCE_BUILTIN,
    LLM_MODEL_SOURCE_CUSTOM,
    LlmModelMetadata,
)
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    ResolvedLlmProviderModel,
)
from agent_runtime.llm_model_registry.llm_provider_definition import (
    LlmProviderDefinition,
    LlmProviderModelBinding,
    LlmRetryPolicy,
)
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmProviderSaveRequest,
)


class _FakeGateway:
    def __init__(self, *, models=None, providers=None) -> None:
        self._models = {m.model_id: m for m in (models or [])}
        self._providers = {p.provider_id: p for p in (providers or [])}

    def list_model_metadata(self):
        return list(self._models.values())

    def get_model_metadata(self, model_id: str):
        if model_id in self._models:
            return self._models[model_id]
        raise KeyError(model_id)

    def save_model_metadata(self, request):
        model = request.to_metadata()
        self._models[model.model_id] = model
        return model

    def delete_model_metadata(self, model_id: str):
        self._models.pop(model_id, None)

    def list_providers(self):
        return list(self._providers.values())

    def get_provider(self, provider_id: str):
        if provider_id in self._providers:
            return self._providers[provider_id]
        raise KeyError(provider_id)

    def save_provider(self, request: LlmProviderSaveRequest):
        provider = request.to_provider()
        self._providers[provider.provider_id] = provider
        return provider

    def delete_provider(self, provider_id: str):
        self._providers.pop(provider_id, None)

    def resolve_provider_model(self, provider_id: str, binding_id: str):
        provider = self.get_provider(provider_id)
        for b in provider.models:
            if b.id == binding_id:
                return ResolvedLlmProviderModel(
                    provider_id=provider.provider_id,
                    binding_id=b.id,
                    model_id=b.model_id,
                    model_alias=b.model_alias or b.model_id,
                    api_format=b.api_format,
                    sdk_platform="openai",
                    api_base_url="https://api.test/v1",
                    api_keys=(),
                    custom_headers={},
                    retry=LlmRetryPolicy(),
                    tool_choice=None,
                    thinking=None,
                    custom_request_params=None,
                    context_window=128000,
                    max_output_tokens=8192,
                    compression_threshold=None,
                    supports_tool_calling=True,
                    supports_structured_output=False,
                    supported_input_modalities=("text", "image"),
                )
        raise KeyError(binding_id)


class ImageGenerationLlmRegistryGatewayTests(unittest.TestCase):
    def test_protocol_support_checks_allow_any_responses_model(self) -> None:
        binding_gpt6 = LlmProviderModelBinding(
            id="gpt-6-b",
            model_id="gpt-6",
            model_alias="gpt-6",
            api_format=API_FORMAT_OPENAI_RESPONSES,
        )
        self.assertTrue(_is_supported_image_generation_binding("openai", binding_gpt6))

        binding_codex = LlmProviderModelBinding(
            id="codex-b",
            model_id="gpt-6",
            model_alias="gpt-6",
            api_format=API_FORMAT_CODEX_RESPONSES,
        )
        self.assertTrue(_is_supported_image_generation_binding(CODEX_PROVIDER_ID, binding_codex))

        binding_dall_e = LlmProviderModelBinding(
            id="dalle-b",
            model_id="dall-e-3",
            model_alias="dall-e-3",
            api_format=API_FORMAT_OPENAI_IMAGES_GENERATIONS,
        )
        self.assertTrue(_is_supported_image_generation_binding("openai", binding_dall_e))

        binding_chat = LlmProviderModelBinding(
            id="chat-b",
            model_id="gpt-4o",
            model_alias="gpt-4o",
            api_format=API_FORMAT_OPENAI_CHAT_COMPLETIONS,
        )
        self.assertFalse(_is_supported_image_generation_binding("openai", binding_chat))

    def test_save_provider_allows_openai_responses_and_rejects_unsupported(self) -> None:
        image_gw = _FakeGateway()
        llm_gw = _FakeGateway()
        gateway = ImageGenerationLlmRegistryGateway(
            image_registry_gateway=image_gw,
            llm_registry_gateway=llm_gw,
        )

        valid_request = LlmProviderSaveRequest(
            provider_id="custom-openai",
            group="default",
            api_base_url="https://api.openai.com/v1",
            api_keys=(),
            custom_headers={},
            retry=LlmRetryPolicy(),
            models=(
                LlmProviderModelBinding(
                    id="b-gpt6",
                    model_id="gpt-6",
                    model_alias="gpt-6",
                    api_format=API_FORMAT_OPENAI_RESPONSES,
                ),
                LlmProviderModelBinding(
                    id="b-img",
                    model_id="dall-e-3",
                    model_alias="dall-e-3",
                    api_format=API_FORMAT_OPENAI_IMAGES_GENERATIONS,
                ),
            ),
        )
        saved = gateway.save_provider(valid_request)
        self.assertEqual(len(saved.models), 2)

        invalid_request = LlmProviderSaveRequest(
            provider_id="custom-openai",
            group="default",
            api_base_url="https://api.openai.com/v1",
            api_keys=(),
            custom_headers={},
            retry=LlmRetryPolicy(),
            models=(
                LlmProviderModelBinding(
                    id="b-chat",
                    model_id="gpt-4o",
                    model_alias="gpt-4o",
                    api_format=API_FORMAT_OPENAI_CHAT_COMPLETIONS,
                ),
            ),
        )
        with self.assertRaises(ValueError) as ctx:
            gateway.save_provider(invalid_request)
        self.assertIn("openai_images_generations 或 openai_responses", str(ctx.exception))

    def test_list_and_get_model_metadata_aggregates_custom_and_supported_models(self) -> None:
        image_gw = _FakeGateway(
            models=[
                LlmModelMetadata(
                    model_id="gpt-image-2",
                    context_window=32000,
                    max_output_tokens=32000,
                    group="image_generation",
                    source=LLM_MODEL_SOURCE_BUILTIN,
                ),
                LlmModelMetadata(
                    model_id="custom-img-model",
                    context_window=16000,
                    max_output_tokens=4096,
                    group="image_generation",
                    source=LLM_MODEL_SOURCE_CUSTOM,
                ),
            ]
        )
        llm_gw = _FakeGateway(
            models=[
                LlmModelMetadata(
                    model_id="gpt-6",
                    context_window=128000,
                    max_output_tokens=8192,
                    group="default",
                ),
                LlmModelMetadata(
                    model_id="gpt-4o",
                    context_window=128000,
                    max_output_tokens=4096,
                    group="default",
                ),
            ],
            providers=[
                LlmProviderDefinition(
                    provider_id="openai",
                    group="default",
                    api_base_url="https://api.openai.com/v1",
                    api_keys=(),
                    custom_headers={},
                    retry=LlmRetryPolicy(),
                    models=(
                        LlmProviderModelBinding(
                            id="b-gpt6",
                            model_id="gpt-6",
                            model_alias="gpt-6",
                            api_format=API_FORMAT_OPENAI_RESPONSES,
                        ),
                    ),
                )
            ],
        )
        gateway = ImageGenerationLlmRegistryGateway(
            image_registry_gateway=image_gw,
            llm_registry_gateway=llm_gw,
        )

        models = gateway.list_model_metadata()
        model_ids = [m.model_id for m in models]
        self.assertIn("gpt-image-2", model_ids)
        self.assertIn("custom-img-model", model_ids)
        self.assertIn("gpt-6", model_ids)
        # gpt-4o 没有支持图片生成的 binding，因此不会被自动混入图片模型列表
        self.assertNotIn("gpt-4o", model_ids)

        # 能够解析出 gpt-6 的模型元数据
        meta_gpt6 = gateway.get_model_metadata("gpt-6")
        self.assertEqual(meta_gpt6.model_id, "gpt-6")

        # 能够解析出 gpt-6 的服务商接入项
        resolved = gateway.resolve_provider_model("openai", "b-gpt6")
        self.assertEqual(resolved.model_id, "gpt-6")
        self.assertEqual(resolved.api_format, API_FORMAT_OPENAI_RESPONSES)
