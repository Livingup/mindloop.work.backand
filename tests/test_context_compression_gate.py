import unittest

from agent_runtime.context_compression.config import ContextCompressionConfig
from agent_runtime.context_compression.gate import decide_context_compression
from agent_runtime.conversation_message.message.models import (
    ROLE_ASSISTANT,
    ROLE_TOOL,
    TextPart,
    TextPartMeta,
    ToolResultPart,
    ToolUsePart,
    UniMessage,
)
from agent_runtime.conversation_message.message.operations import create_conversation
from agent_runtime.input_modality import INPUT_MODALITY_TEXT
from agent_runtime.llm_model_registry.llm_model_registry_service import ResolvedLlmProviderModel
from agent_runtime.llm_model_registry.llm_provider_definition import LlmApiKey, LlmRetryPolicy


def _candidate(*, threshold: int = 100) -> ResolvedLlmProviderModel:
    return ResolvedLlmProviderModel(
        provider_id="provider-a",
        binding_id="binding-a",
        model_id="model-a",
        model_alias="alias-a",
        api_format="openai_responses",
        sdk_platform="openai",
        api_base_url="https://example.invalid",
        api_keys=(LlmApiKey(key="test-key", name="test"),),
        custom_headers={},
        retry=LlmRetryPolicy(),
        tool_choice=None,
        thinking=None,
        custom_request_params=None,
        context_window=1000,
        max_output_tokens=4096,
        compression_threshold=threshold,
        supports_tool_calling=True,
        supports_structured_output=True,
        supported_input_modalities=(INPUT_MODALITY_TEXT,),
    )


def _conversation_after_tool_call():
    conversation = create_conversation()
    conversation.add_user_message([TextPart("question")], [TextPartMeta()])
    conversation.append_message(
        UniMessage(
            role=ROLE_ASSISTANT,
            content=[ToolUsePart(name="file_read", input={"path": "a.py"}, tool_use_id="call-1")],
        )
    )
    return conversation


class ContextCompressionGateProjectedUsageTests(unittest.TestCase):
    def test_no_incremental_messages_keeps_previous_usage(self) -> None:
        conversation = _conversation_after_tool_call()
        conversation.state.last_context_usage = {
            "context_window_used_tokens": 90,
            "recorded_message_count": len(conversation.messages),
        }

        decision = decide_context_compression(
            conversation,
            _candidate(threshold=100),
            ContextCompressionConfig(),
        )

        self.assertFalse(decision.should_compress)
        self.assertEqual(decision.reason, "below_threshold")
        self.assertEqual(decision.base_used_tokens, 90)
        self.assertEqual(decision.estimated_incremental_tokens, 0)
        self.assertEqual(decision.projected_used_tokens, 90)
        self.assertEqual(decision.used_tokens, 90)
        self.assertEqual(decision.estimated_incremental_message_count, 0)

    def test_tool_result_after_recorded_usage_can_trigger_compression(self) -> None:
        conversation = _conversation_after_tool_call()
        conversation.state.last_context_usage = {
            "context_window_used_tokens": 90,
            "recorded_message_count": len(conversation.messages),
        }
        conversation.append_message(
            UniMessage(
                role=ROLE_TOOL,
                content=[
                    ToolResultPart(
                        tool_use_ref="call-1",
                        is_error=False,
                        output=[{"kind": "text", "text": "x" * 160}],
                    )
                ],
            )
        )

        decision = decide_context_compression(
            conversation,
            _candidate(threshold=100),
            ContextCompressionConfig(),
        )

        self.assertTrue(decision.should_compress)
        self.assertEqual(decision.reason, "projected_context_usage_exceeded_threshold")
        self.assertEqual(decision.base_used_tokens, 90)
        self.assertGreater(decision.estimated_incremental_tokens or 0, 0)
        self.assertGreaterEqual(decision.projected_used_tokens or 0, 100)
        self.assertEqual(decision.used_tokens, decision.projected_used_tokens)
        self.assertEqual(decision.estimated_incremental_message_count, 1)

    def test_legacy_usage_estimates_tail_after_latest_assistant_message(self) -> None:
        conversation = _conversation_after_tool_call()
        conversation.state.last_context_usage = {
            "context_window_used_tokens": 90,
        }
        conversation.append_message(
            UniMessage(
                role=ROLE_TOOL,
                content=[
                    ToolResultPart(
                        tool_use_ref="call-1",
                        is_error=False,
                        output=[{"kind": "text", "text": "x" * 160}],
                    )
                ],
            )
        )

        decision = decide_context_compression(
            conversation,
            _candidate(threshold=100),
            ContextCompressionConfig(),
        )

        self.assertTrue(decision.should_compress)
        self.assertEqual(decision.reason, "projected_context_usage_exceeded_threshold")
        self.assertEqual(decision.recorded_message_count, 2)
        self.assertEqual(decision.estimated_incremental_message_count, 1)

    def test_incremental_user_message_is_included_without_forcing_compression(self) -> None:
        conversation = _conversation_after_tool_call()
        conversation.state.last_context_usage = {
            "context_window_used_tokens": 10,
            "recorded_message_count": len(conversation.messages),
        }
        conversation.add_user_message([TextPart("short follow up")], [TextPartMeta()])

        decision = decide_context_compression(
            conversation,
            _candidate(threshold=100),
            ContextCompressionConfig(),
        )

        self.assertFalse(decision.should_compress)
        self.assertEqual(decision.reason, "below_threshold")
        self.assertEqual(decision.base_used_tokens, 10)
        self.assertGreater(decision.estimated_incremental_tokens or 0, 0)
        self.assertEqual(decision.estimated_incremental_message_count, 1)
        self.assertLess(decision.projected_used_tokens or 0, 100)


if __name__ == "__main__":
    unittest.main()
