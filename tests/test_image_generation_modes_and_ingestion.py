from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from typing import Any

from agent_runtime.image_generation.models import ImageGenerationRequest
from agent_runtime.image_generation.service import (
    MODE_EDIT,
    MODE_GENERATE,
    ImageGenerationService,
)
from agent_runtime.llm_model_registry.llm_execution_policy import UNSUPPORTED_INPUT_MODALITY_STRATEGY_SKIP_CANDIDATE
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    ResolvedLlmExecutionPolicy,
    ResolvedLlmExecutionPolicyCandidate,
    ResolvedLlmProviderModel,
)
from agent_runtime.llm_model_registry.llm_provider_definition import LlmApiKey, LlmRetryPolicy


@dataclass
class _Settings:
    llm_policy_group_id: str = "test-image-policy"


class _SettingsRepository:
    def get_settings(self):
        return _Settings()


class _RegistryGateway:
    def __init__(self, resolved: ResolvedLlmProviderModel) -> None:
        self.resolved = resolved

    def resolve_execution_policy(self, policy_group_id: str):
        return ResolvedLlmExecutionPolicy(
            policy_group_id=policy_group_id,
            name=None,
            description=None,
            unsupported_input_modality_strategy=UNSUPPORTED_INPUT_MODALITY_STRATEGY_SKIP_CANDIDATE,
            candidates=(
                ResolvedLlmExecutionPolicyCandidate(
                    candidate_id="candidate-1",
                    provider_id=self.resolved.provider_id,
                    model_binding_id=self.resolved.binding_id,
                    resolved_model=self.resolved,
                ),
            ),
        )


class _FakeFileResourceService:
    def __init__(self) -> None:
        self.base64_requests: list[Any] = []
        self.url_requests: list[Any] = []

    def create_resources_from_base64_contents(self, requests):
        self.base64_requests.extend(requests)
        return [
            type(
                "CreatedResource",
                (),
                {
                    "resource_id": f"res-b64-{i}",
                    "object_storage_url": f"https://oss.test/b64-{i}.png",
                    "local_storage_path": f"D:/tmp/b64-{i}.png",
                },
            )()
            for i, _ in enumerate(requests)
        ]

    def create_resources_from_remote_urls(self, requests):
        self.url_requests.extend(requests)
        return [
            type(
                "CreatedResource",
                (),
                {
                    "resource_id": f"res-url-{i}",
                    "object_storage_url": f"https://oss.test/url-{i}.png",
                    "local_storage_path": f"D:/tmp/url-{i}.png",
                },
            )()
            for i, _ in enumerate(requests)
        ]


class _FakeImagesClient:
    def __init__(self, response_data: dict[str, Any]) -> None:
        self.response_data = response_data
        self.generate_calls: list[dict[str, Any]] = []
        self.edit_calls: list[dict[str, Any]] = []

    async def generate(self, *, resolved_model, api_key, body):
        self.generate_calls.append({"model": resolved_model, "body": body})
        return self.response_data

    async def edit(self, *, resolved_model, api_key, body):
        self.edit_calls.append({"model": resolved_model, "body": body})
        return self.response_data


def _resolved_images_model() -> ResolvedLlmProviderModel:
    return ResolvedLlmProviderModel(
        provider_id="xixiapi",
        binding_id="gpt-image-2",
        model_id="gpt-image-2",
        model_alias="gpt-image-2",
        api_format="openai_images_generations",
        sdk_platform="openai",
        api_base_url="https://xixiapi.io/v1",
        custom_headers={},
        custom_request_params=None,
        thinking=None,
        api_keys=(LlmApiKey(name="k1", key="sk-test"),),
        retry=LlmRetryPolicy(max_retries_per_key=0),
        tool_choice=None,
        context_window=128000,
        max_output_tokens=8192,
        compression_threshold=None,
        supports_tool_calling=False,
        supports_structured_output=False,
        supported_input_modalities=("text", "image"),
    )


class ImageGenerationModesAndIngestionTests(unittest.TestCase):
    def test_mode_generate_dispatches_to_generate_endpoint_with_response_format(self) -> None:
        client = _FakeImagesClient({
            "data": [{"b64_json": "base64-data-1", "revised_prompt": "prompt 1"}]
        })
        file_service = _FakeFileResourceService()
        service = ImageGenerationService(
            settings_repository=_SettingsRepository(),
            llm_registry_gateway=_RegistryGateway(_resolved_images_model()),
            file_resource_service=file_service,
            llm_request_service=None,
            images_client=client,
        )

        result = asyncio.run(
            service.generate_image(
                ImageGenerationRequest(
                    prompt="A beautiful sunset",
                    mode="generate",
                    size="1024x1024",
                )
            )
        )

        self.assertEqual(len(client.generate_calls), 1)
        self.assertEqual(len(client.edit_calls), 0)
        sent_body = client.generate_calls[0]["body"]
        self.assertEqual(sent_body["response_format"], "b64_json")
        self.assertNotIn("images", sent_body)
        self.assertEqual(result.mode, MODE_GENERATE)
        self.assertEqual(len(result.images), 1)
        self.assertEqual(result.images[0].resource_id, "res-b64-0")

    def test_mode_generate_with_images_raises_error(self) -> None:
        service = ImageGenerationService(
            settings_repository=_SettingsRepository(),
            llm_registry_gateway=_RegistryGateway(_resolved_images_model()),
            file_resource_service=_FakeFileResourceService(),
            llm_request_service=None,
            images_client=_FakeImagesClient({}),
        )

        with self.assertRaises(ValueError) as ctx:
            asyncio.run(
                service.generate_image(
                    ImageGenerationRequest(
                        prompt="A sunset",
                        mode="generate",
                        image_urls=("https://example.test/pic.png",),
                    )
                )
            )
        self.assertIn("mode='generate' does not accept input images", str(ctx.exception))

    def test_mode_edit_requires_images(self) -> None:
        service = ImageGenerationService(
            settings_repository=_SettingsRepository(),
            llm_registry_gateway=_RegistryGateway(_resolved_images_model()),
            file_resource_service=_FakeFileResourceService(),
            llm_request_service=None,
            images_client=_FakeImagesClient({}),
        )

        with self.assertRaises(ValueError) as ctx:
            asyncio.run(
                service.generate_image(
                    ImageGenerationRequest(
                        prompt="Turn this into anime",
                        mode="edit",
                    )
                )
            )
        self.assertIn("mode='edit' requires at least one source image", str(ctx.exception))

    def test_mode_edit_dispatches_to_edit_endpoint(self) -> None:
        client = _FakeImagesClient({
            "data": [{"b64_json": "base64-edited-data", "revised_prompt": "anime prompt"}]
        })
        file_service = _FakeFileResourceService()
        service = ImageGenerationService(
            settings_repository=_SettingsRepository(),
            llm_registry_gateway=_RegistryGateway(_resolved_images_model()),
            file_resource_service=file_service,
            llm_request_service=None,
            images_client=client,
        )

        result = asyncio.run(
            service.generate_image(
                ImageGenerationRequest(
                    prompt="Turn this into anime",
                    mode="edit",
                    image_urls=("https://example.test/source.png",),
                    input_fidelity="high",
                )
            )
        )

        self.assertEqual(len(client.edit_calls), 1)
        self.assertEqual(len(client.generate_calls), 0)
        sent_body = client.edit_calls[0]["body"]
        self.assertEqual(sent_body["response_format"], "b64_json")
        self.assertEqual(len(sent_body["images"]), 1)
        self.assertEqual(sent_body["input_fidelity"], "high")
        self.assertEqual(result.mode, MODE_EDIT)

    def test_omitted_mode_auto_resolves_to_edit_when_images_present(self) -> None:
        client = _FakeImagesClient({
            "data": [{"b64_json": "base64-data"}]
        })
        service = ImageGenerationService(
            settings_repository=_SettingsRepository(),
            llm_registry_gateway=_RegistryGateway(_resolved_images_model()),
            file_resource_service=_FakeFileResourceService(),
            llm_request_service=None,
            images_client=client,
        )

        result = asyncio.run(
            service.generate_image(
                ImageGenerationRequest(
                    prompt="Redraw",
                    image_urls=("https://example.test/source.png",),
                )
            )
        )

        self.assertEqual(len(client.edit_calls), 1)
        self.assertEqual(result.mode, MODE_EDIT)

    def test_omitted_mode_auto_resolves_to_generate_when_no_images(self) -> None:
        client = _FakeImagesClient({
            "data": [{"b64_json": "base64-data"}]
        })
        service = ImageGenerationService(
            settings_repository=_SettingsRepository(),
            llm_registry_gateway=_RegistryGateway(_resolved_images_model()),
            file_resource_service=_FakeFileResourceService(),
            llm_request_service=None,
            images_client=client,
        )

        result = asyncio.run(
            service.generate_image(
                ImageGenerationRequest(
                    prompt="Draw a cat",
                )
            )
        )

        self.assertEqual(len(client.generate_calls), 1)
        self.assertEqual(result.mode, MODE_GENERATE)

    def test_ingest_images_supports_remote_url_responses(self) -> None:
        # Simulates providers like xixiapi that return {"url": "https://r2.../xyz.png"}
        client = _FakeImagesClient({
            "data": [
                {"url": "https://r2.52image.xyz/cache/generated/da2c79d29dd840918ab11f1b161dac29.png"}
            ]
        })
        file_service = _FakeFileResourceService()
        service = ImageGenerationService(
            settings_repository=_SettingsRepository(),
            llm_registry_gateway=_RegistryGateway(_resolved_images_model()),
            file_resource_service=file_service,
            llm_request_service=None,
            images_client=client,
        )

        result = asyncio.run(
            service.generate_image(
                ImageGenerationRequest(
                    prompt="A cat",
                    mode="generate",
                )
            )
        )

        self.assertEqual(len(result.images), 1)
        self.assertEqual(result.images[0].resource_id, "res-url-0")
        self.assertEqual(len(file_service.url_requests), 1)
        self.assertEqual(
            file_service.url_requests[0].resource_url,
            "https://r2.52image.xyz/cache/generated/da2c79d29dd840918ab11f1b161dac29.png"
        )
        self.assertEqual(file_service.url_requests[0].resource_mime_type, "image/png")
