import asyncio
from types import SimpleNamespace

from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine, _AgentExecutionInterruptState
from agent_runtime.agent.agent_instance import AgentInstance
from agent_runtime.agent.agent_task import AgentTask
from agent_runtime.agent_definition.agent_definition import AgentDefinition
from agent_runtime.communication.runtime_event_publisher import RuntimeCommunicationContext
from agent_runtime.conversation_message.message.models import (
    Conversation,
    ROLE_ASSISTANT,
    ROLE_USER,
    TextPart,
    TextPartMeta,
    ToolUsePart,
    UniMessage,
)
from agent_runtime.input_modality import INPUT_MODALITY_TEXT
from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_RESPONSES
from agent_runtime.llm_client.llm_request_models import LlmPromptResponse
from agent_runtime.subagent.subagent_runtime import SubagentRuntime


class _NoopToolDispatcher:
    async def build_llm_tools(self, **_kwargs):
        return ()


class _NoopLocalToolGateway:
    def list_tools(self, *, public_only: bool = False, group_by: bool = False):
        return []


class _AppendingLlmRequestService:
    def __init__(self) -> None:
        self.request_message_count: int | None = None
        self.request_metadata: dict[str, object] | None = None

    async def send_prompt(self, request):
        self.request_message_count = len(request.conversation.messages)
        self.request_metadata = dict(request.communication_context.metadata)
        assistant_message = UniMessage(
            role=ROLE_ASSISTANT,
            content=[
                ToolUsePart(
                    name="create_subagent",
                    input={"agent_definition_id": "assistant", "initial_message": "处理子任务"},
                    tool_use_id="tool-call-create-subagent",
                )
            ],
            source_provider=request.resolved_model.api_format,
        )
        request.conversation.append_message(assistant_message)
        return LlmPromptResponse(
            raw_response={},
            assistant_message=assistant_message,
            provider_id=request.resolved_model.provider_id,
            binding_id=request.resolved_model.binding_id,
            model_id=request.resolved_model.model_id,
            model_alias=request.resolved_model.model_alias,
        )


def test_llm_request_base_message_count_is_snapshot_before_assistant_tool_call() -> None:
    async def run_case() -> None:
        llm_request_service = _AppendingLlmRequestService()
        engine = AgentExecutionEngine(
            runtime_gateway=object(),
            agent_definition_gateway=object(),
            llm_registry_gateway=object(),
            conversation_repository=object(),
            prompt_snippet_gateway=object(),
            local_tool_gateway=_NoopLocalToolGateway(),
            mcp_client_gateway=None,
            skill_gateway=None,
            llm_request_service=llm_request_service,
            tool_dispatcher=_NoopToolDispatcher(),
            trace_recorder=object(),
        )
        conversation = Conversation(id="parent-conversation")
        conversation.append_message(UniMessage(role=ROLE_USER, content=[TextPart("用户第一轮")]), [TextPartMeta()])
        conversation.append_message(
            UniMessage(
                role=ROLE_ASSISTANT,
                content=[TextPart("父 Agent 第一轮回复")],
                source_provider=API_FORMAT_OPENAI_RESPONSES,
            )
        )
        conversation.append_message(UniMessage(role=ROLE_USER, content=[TextPart("用户第二轮")]), [TextPartMeta()])
        base_message_count = len(conversation.messages)

        result = await engine._send_prompt_with_policy_failover(
            task=AgentTask(
                task_id="task-parent",
                root_agent_instance_id="agent-parent",
                agent_instance_ids=("agent-parent",),
            ),
            trace_path="trace.md",
            conversation=conversation,
            agent_definition=AgentDefinition(agent_id="parent-agent"),
            instance=AgentInstance(
                instance_id="agent-parent",
                task_id="task-parent",
                thread_id="thread-parent",
                current_working_directory="D:/project/agent",
                agent_definition_id="parent-agent",
                llm_provider_id="provider-a",
                llm_model_binding_id="binding-a",
                conversation_id=conversation.id,
                trace_id="trace-parent",
            ),
            resolved_candidates=(
                SimpleNamespace(
                    provider_id="provider-a",
                    binding_id="binding-a",
                    model_id="model-a",
                    model_alias="model-a",
                    api_format=API_FORMAT_OPENAI_RESPONSES,
                    supported_input_modalities=(INPUT_MODALITY_TEXT,),
                ),
            ),
            unsupported_modality_strategy="degrade",
            render_options={},
            request_overrides={},
            communication_context=RuntimeCommunicationContext(
                stream_id="stream-parent",
                actor_type="agent_instance",
                actor_id="agent-parent",
                metadata={
                    "task_id": "task-parent",
                    "agent_instance_id": "agent-parent",
                    "conversation_id": conversation.id,
                    "conversation_history_copy_message_count": 0,
                },
            ),
            interrupt_state=_AgentExecutionInterruptState(),
            execution_frame_id=None,
        )

        assert llm_request_service.request_message_count == base_message_count
        assert llm_request_service.request_metadata is not None
        assert llm_request_service.request_metadata["conversation_history_copy_message_count"] == base_message_count
        assert result.conversation_history_copy_message_count == base_message_count
        assert len(result.conversation.messages) == base_message_count + 1

    asyncio.run(run_case())


def test_seed_child_conversation_copies_parent_prefix_into_independent_conversation() -> None:
    async def run_case() -> None:
        parent_conversation = Conversation(id="parent-conversation")
        for index in range(4):
            parent_conversation.append_message(
                UniMessage(role=ROLE_USER, content=[TextPart(f"父会话消息 {index}")]),
                [TextPartMeta()],
            )

        class _ConversationRepository:
            def __init__(self) -> None:
                self.created_conversation: Conversation | None = None
                self.create_kwargs: dict[str, object] | None = None

            async def get(self, conversation_id: str) -> Conversation:
                assert conversation_id == parent_conversation.id
                return parent_conversation

            async def create(self, conversation: Conversation, **kwargs):
                self.created_conversation = conversation
                self.create_kwargs = dict(kwargs)

        repository = _ConversationRepository()
        runtime = SubagentRuntime(
            runtime_gateway=object(),
            agent_definition_gateway=object(),
            llm_registry_gateway=object(),
            execution_engine=object(),
            event_publisher=object(),
            conversation_repository=repository,
        )
        child_conversation_id = await runtime._seed_child_conversation_from_parent_prefix(
            parent=SimpleNamespace(
                instance_id="agent-parent",
                task_id="task-parent",
                conversation_id=parent_conversation.id,
            ),
            parent_agent_name="父Agent",
            child_instance_id="agent-child",
            child_thread_id="thread-child",
            child_agent_definition_id="assistant",
            parent_conversation_id=parent_conversation.id,
            conversation_history_copy_message_count=3,
        )

        assert repository.created_conversation is not None
        child_conversation = repository.created_conversation
        assert child_conversation_id == child_conversation.id
        assert child_conversation.id != parent_conversation.id
        assert len(child_conversation.messages) == 4
        assert repository.create_kwargs == {
            "task_id": "task-parent",
            "thread_id": "thread-child",
            "agent_id": "assistant",
            "agent_instance_id": "agent-child",
        }
        boundary_message = child_conversation.messages[-1]
        assert boundary_message.role == ROLE_USER
        assert boundary_message.content[0].text == (
            "【@本地Agent 父Agent agent-parent】"
            "上面的历史消息是我父Agent的历史对话消息，"
            "作为你的任务背景认知上下文，只需要你帮我完成下面的任务"
        )
        assert child_conversation.messages[0] is not parent_conversation.messages[0]
        child_conversation.messages[0].content[0].text = "子会话独立修改"
        assert parent_conversation.messages[0].content[0].text == "父会话消息 0"

    asyncio.run(run_case())


def test_seed_child_conversation_boundary_message_closes_conversation_state() -> None:
    async def run_case() -> None:
        parent_conversation = Conversation(id="parent-conversation")

        class _ConversationRepository:
            def __init__(self) -> None:
                self.created_conversation: Conversation | None = None

            async def get(self, conversation_id: str) -> Conversation:
                assert conversation_id == parent_conversation.id
                return parent_conversation

            async def create(self, conversation: Conversation, **_kwargs):
                self.created_conversation = conversation

        repository = _ConversationRepository()
        runtime = SubagentRuntime(
            runtime_gateway=object(),
            agent_definition_gateway=object(),
            llm_registry_gateway=object(),
            execution_engine=object(),
            event_publisher=object(),
            conversation_repository=repository,
        )
        await runtime._seed_child_conversation_from_parent_prefix(
            parent=SimpleNamespace(
                instance_id="agent-parent",
                task_id="task-parent",
                conversation_id=parent_conversation.id,
            ),
            parent_agent_name="父Agent",
            child_instance_id="agent-child",
            child_thread_id="thread-child",
            child_agent_definition_id="assistant",
            parent_conversation_id=parent_conversation.id,
            conversation_history_copy_message_count=0,
        )

        assert repository.created_conversation is not None
        child_conversation = repository.created_conversation
        assert len(child_conversation.messages) == 1
        assert child_conversation.messages[0].role == ROLE_USER
        assert child_conversation.state.modalities == [INPUT_MODALITY_TEXT]

    asyncio.run(run_case())
