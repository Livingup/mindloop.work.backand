import asyncio
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from agent_runtime.image_generation.models import ImageGenerationRequest
from agent_runtime.image_generation.service import ImageGenerationService
from agent_runtime.llm_model_registry.llm_execution_policy import UNSUPPORTED_INPUT_MODALITY_STRATEGY_SKIP_CANDIDATE
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    ResolvedLlmExecutionPolicy,
    ResolvedLlmExecutionPolicyCandidate,
    ResolvedLlmProviderModel,
)
from agent_runtime.llm_model_registry.llm_provider_definition import LlmApiKey, LlmRetryPolicy


@dataclass
class _Settings:
    llm_policy_group_id: str = "image-policy"


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
                    candidate_id="candidate-gpt-image-agent",
                    provider_id=self.resolved.provider_id,
                    model_binding_id=self.resolved.binding_id,
                    resolved_model=self.resolved,
                ),
            ),
        )


class _FileResourceService:
    def create_resources_from_base64_contents(self, requests):
        assert len(requests) == 1
        assert requests[0].resource_base64_content == "base64-image-result"
        assert requests[0].resource_mime_type == "image/png"
        return [
            type(
                "CreatedResource",
                (),
                {
                    "resource_id": "resource-gpt-image-agent",
                    "object_storage_url": "https://example.test/resource-gpt-image-agent.png",
                    "local_storage_path": "D:/tmp/resource-gpt-image-agent.png",
                },
            )()
        ]


def _resolved_gpt_image_agent() -> ResolvedLlmProviderModel:
    return ResolvedLlmProviderModel(
        provider_id="openai",
        binding_id="gpt-55",
        model_id="gpt-5.5",
        model_alias="gpt-5.5",
        api_format="openai_responses",
        sdk_platform="openai",
        api_base_url="https://api.openai.test/v1",
        api_keys=(LlmApiKey(key="test-key"),),
        custom_headers={},
        retry=LlmRetryPolicy(max_retries_per_key=0),
        tool_choice='{"should_not_override": true}',
        thinking=None,
        custom_request_params='{"temperature": 0.2, "tools": [{"type": "web_search_preview"}], "tool_choice": "auto"}',
        context_window=128000,
        max_output_tokens=8192,
        compression_threshold=None,
        supports_tool_calling=True,
        supports_structured_output=False,
        supported_input_modalities=("text", "image"),
    )


class GptImageAgentGenerationTests(unittest.TestCase):
    def test_gpt_image_agent_loops_until_image_result_and_keeps_generation_contract(self) -> None:
        calls = []

        async def fake_post_openai_responses_json(*, resolved_model, api_key, body):
            calls.append(body)
            self.assertEqual(resolved_model.model_id, "gpt-5.5")
            self.assertEqual(api_key, "test-key")
            if len(calls) == 1:
                return {
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "我会直接生成图片。"}],
                        }
                    ]
                }
            return {
                "output": [
                    {
                        "type": "image_generation_call",
                        "status": "completed",
                        "result": "base64-image-result",
                        "revised_prompt": "A neon cyberpunk cat poster",
                    }
                ]
            }

        async def run_case():
            service = ImageGenerationService(
                settings_repository=_SettingsRepository(),
                llm_registry_gateway=_RegistryGateway(_resolved_gpt_image_agent()),
                file_resource_service=_FileResourceService(),
            )
            return await service.generate_image(
                ImageGenerationRequest(
                    prompt="画一张霓虹赛博朋克猫海报",
                    size="1024x1024",
                    output_format="png",
                )
            )

        with patch("agent_runtime.image_generation.service._post_openai_responses_json", fake_post_openai_responses_json):
            result = asyncio.run(run_case())

        self.assertEqual(result.api_format, "openai_responses")
        self.assertEqual(result.model_id, "gpt-5.5")
        self.assertEqual(result.provider_id, "openai")
        self.assertEqual(result.binding_id, "gpt-55")
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(result.images[0].resource_id, "resource-gpt-image-agent")
        self.assertEqual(result.images[0].revised_prompt, "A neon cyberpunk cat poster")
        self.assertEqual(len(calls), 2)

        first_call = calls[0]
        self.assertIn("尽最大可能根据用户的需求描述来直接生成图片，不要询问用户", first_call["instructions"])
        self.assertEqual(first_call["tools"], [{"type": "image_generation"}])
        self.assertEqual(first_call["tool_choice"], {"type": "image_generation"})
        self.assertIs(first_call["store"], False)
        self.assertIs(first_call["stream"], True)
        self.assertEqual(first_call["temperature"], 0.2)
        self.assertEqual(first_call["input"][0]["role"], "user")
        self.assertIn("画一张霓虹赛博朋克猫海报", first_call["input"][0]["content"][0]["text"])

        second_call = calls[1]
        self.assertEqual(second_call["tools"], [{"type": "image_generation"}])
        self.assertEqual(second_call["tool_choice"], {"type": "image_generation"})
        self.assertEqual(second_call["input"][-1]["role"], "user")
        self.assertIn("不要询问用户", second_call["input"][-1]["content"][0]["text"])
