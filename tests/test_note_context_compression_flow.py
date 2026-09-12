# -*- coding: utf-8 -*-
"""Note 上下文压缩流程集成测试。

覆盖：
1. 触发门禁触发 -> 原地切换为 note_update 状态 -> 边界消息与 Timeline 冻结/激活 -> 持久化 execution_substate -> Dual-switch 使用 Note Prompt 和 Note Tools。
2. note_update 状态下严格工具权限隔离：仅允许 32 个 Note 工具，非 Note 工具拦截并返回工具错误。
3. note_update_end 工具调用完成 -> 从 PG 读取最新快照 -> 创建全新 Conversation -> 切换 instance conversation_id -> 退出 note_update 状态 -> loop_decision="continue"。
4. 暂停/中断恢复场景：已是 note_update 状态的 instance 继续使用 Note Prompt 和 Note Tools。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from agent_runtime.agent.agent_execution_engine import (
    AgentExecutionEngine,
    AgentExecutionRequest,
    _AgentExecutionInterruptState,
)
from agent_runtime.agent.agent_instance import (
    EXECUTION_SUBSTATE_NOTE_UPDATE,
    AgentInstance,
)
from agent_runtime.agent.agent_task import AgentTask
from agent_runtime.agent_definition.agent_definition import AgentDefinition
from agent_runtime.communication.runtime_event_publisher import (
    NoopRuntimeEventPublisher,
    RuntimeCommunicationContext,
)
from agent_runtime.context_compression.gate import CompressionDecision
from agent_runtime.conversation_message.message.models import (
    Conversation,
    ROLE_ASSISTANT,
    ROLE_USER,
    TextPart,
    TextPartMeta,
    ToolResultPart,
    ToolUsePart,
    UniMessage,
)
from agent_runtime.input_modality import INPUT_MODALITY_TEXT
from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_RESPONSES
from agent_runtime.llm_client.llm_request_models import LlmPromptResponse, LlmToolDefinition
from agent_runtime.task_note.task_note_prompt_provider import (
    TASK_NOTE_TOOL_NAMES,
    TaskNotePromptProvider,
)
from agent_runtime.task_note.task_note_snapshot_service import TaskNoteSnapshotService
from agent_runtime.agent.tool_dispatch.tool_dispatcher import ToolDispatchResult
from agent_runtime.tool.tool_execution_models import BaseToolResult, LoopControl


class _MockLlmRequestService:
    def __init__(self, response_messages: list[UniMessage] | None = None) -> None:
        self.sent_requests = []
        self._responses = list(response_messages) if response_messages else []

    async def send_prompt(self, request):
        self.sent_requests.append(request)
        if self._responses:
            msg = self._responses.pop(0)
        else:
            msg = UniMessage(
                role=ROLE_ASSISTANT,
                content=[TextPart("Note 更新中...")],
                source_provider=request.resolved_model.api_format,
            )
        request.conversation.append_message(msg)
        return LlmPromptResponse(
            raw_response={},
            assistant_message=msg,
            provider_id=request.resolved_model.provider_id,
            binding_id=request.resolved_model.binding_id,
            model_id=request.resolved_model.model_id,
            model_alias=request.resolved_model.model_alias,
        )


class _MockConversationRepository:
    def __init__(self, conversations: dict[str, Conversation] | None = None) -> None:
        self.conversations = conversations or {}
        self.saved_conversations = []
        self.created_conversations = []

    async def get(self, conversation_id: str) -> Conversation:
        if conversation_id in self.conversations:
            return self.conversations[conversation_id]
        raise KeyError(conversation_id)

    async def save(self, conversation: Conversation) -> None:
        self.saved_conversations.append(conversation)
        self.conversations[conversation.id] = conversation

    async def create(self, conversation: Conversation, **kwargs) -> None:
        self.created_conversations.append((conversation, kwargs))
        self.conversations[conversation.id] = conversation


class _MockRuntimeGateway:
    def __init__(self, instance: AgentInstance | None = None) -> None:
        self.instance = instance
        self.substate_updates: list[tuple[str, str | None]] = []
        self.conversation_switches: list[tuple[str, str]] = []

    def start_task(self, task_id: str) -> None:
        pass

    def start_agent_instance(self, instance_id: str) -> None:
        pass

    def pause_task(self, task_id: str) -> None:
        pass

    def pause_agent_instance(self, instance_id: str) -> AgentInstance | None:
        return self.instance

    def interrupt_agent_instance(self, instance_id: str, failure_message: str) -> None:
        pass

    def get_task(self, task_id: str) -> AgentTask:
        return AgentTask(task_id=task_id, root_agent_instance_id=self.instance.instance_id if self.instance else "inst-1", agent_instance_ids=("inst-1",))

    def get_agent_instance(self, instance_id: str) -> AgentInstance:
        return self.instance

    def drain_agent_instance_user_messages(self, instance_id: str, *, predicate: Any = None):
        if self.instance and self.instance.instance_id == instance_id:
            updated, drained = self.instance.drain_pending_user_messages(predicate=predicate)
            self.instance = updated
            return updated, drained
        return self.instance, ()

    def update_agent_instance_execution_substate(
        self,
        instance_id: str,
        execution_substate: str | None,
    ) -> None:
        self.substate_updates.append((instance_id, execution_substate))
        if self.instance and self.instance.instance_id == instance_id:
            if execution_substate == EXECUTION_SUBSTATE_NOTE_UPDATE:
                self.instance = self.instance.enter_note_update()
            else:
                self.instance = self.instance.exit_note_update()

    def switch_agent_instance_conversation(
        self,
        instance_id: str,
        new_conversation_id: str,
    ) -> None:
        self.conversation_switches.append((instance_id, new_conversation_id))
        if self.instance and self.instance.instance_id == instance_id:
            self.instance = replace(self.instance, conversation_id=new_conversation_id)


class _MockPromptSnippetGateway:
    def get_snippet(self, name: str):
        return SimpleNamespace(
            name=name,
            text="你是{{agent_instance_id}}，你现在从业务agent状态变为 **系统Note 更新 状态**。可用tools：{{available_tools}}。",
            content="你是{{agent_instance_id}}，你现在从业务agent状态变为 **系统Note 更新 状态**。可用tools：{{available_tools}}。",
        )

    def create_snippet(self, request):
        pass


class _MockLocalToolGateway:
    def list_tools(self, *, public_only: bool = False, group_by: bool = False):
        tools = []
        for name in sorted(TASK_NOTE_TOOL_NAMES):
            tools.append(
                SimpleNamespace(
                    tool_name=name,
                    group="task_note",
                    category="business" if name != "note_update_end" else "system",
                    always_enabled=name == "note_update_end",
                    description=f"{name} tool description",
                )
            )
        tools.append(
            SimpleNamespace(
                tool_name="message_user",
                group="lifecycle",
                category="lifecycle",
                always_enabled=True,
                description="回复用户消息",
            )
        )
        tools.append(
            SimpleNamespace(
                tool_name="run_bash_command",
                group="system",
                category="business",
                always_enabled=False,
                description="执行终端命令",
            )
        )
        return tools

    def get_tool(self, tool_name: str):
        return SimpleNamespace(
            tool_name=tool_name,
            schemas={
                API_FORMAT_OPENAI_RESPONSES: {
                    "type": "function",
                    "name": tool_name,
                    "description": f"{tool_name} description",
                    "parameters": {"type": "object", "properties": {}},
                    "strict": False,
                }
            },
        )


class NoteContextCompressionFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prompt_snippet_gateway = _MockPromptSnippetGateway()
        self.local_tool_gateway = _MockLocalToolGateway()
        self.task_note_prompt_provider = TaskNotePromptProvider(
            prompt_snippet_gateway=self.prompt_snippet_gateway,
            local_tool_gateway=self.local_tool_gateway,
        )
        self.mock_snapshot_service = MagicMock(spec=TaskNoteSnapshotService)
        self.mock_snapshot_service.build_task_note_snapshot_json = AsyncMock(
            return_value=json.dumps({"task_guidelines": [{"content": "测试指引"}]}, ensure_ascii=False)
        )
        self.mock_snapshot_service.query_latest_note_snapshot_json = AsyncMock(
            return_value=json.dumps({"task_guidelines": [{"content": "测试指引"}]}, ensure_ascii=False)
        )

        self.instance = AgentInstance(
            instance_id="inst-1",
            task_id="task-100",
            thread_id="thread-100",
            current_working_directory="G:/project/agent",
            agent_definition_id="developer",
            llm_provider_id="provider-a",
            llm_model_binding_id="binding-a",
            conversation_id="conv-1",
            trace_id="trace-100",
        )
        self.runtime_gateway = _MockRuntimeGateway(self.instance)
        self.conversation_repo = _MockConversationRepository()
        self.llm_request_service = _MockLlmRequestService()

        async def _mock_dispatch(agent_definition, tool_use, communication_context):
            if tool_use.name == "note_update_end":
                return ToolDispatchResult(
                    tool_name="note_update_end",
                    source="local",
                    result_part=ToolResultPart(
                        tool_use_ref=tool_use.tool_use_id,
                        is_error=False,
                        output="Note 更新完成",
                    ),
                    tool_result=BaseToolResult(
                        output="Note 更新完成",
                        loop_control=LoopControl(decision="continue"),
                    ),
                )
            if tool_use.name == "message_user":
                return ToolDispatchResult(
                    tool_name="message_user",
                    source="local",
                    result_part=ToolResultPart(
                        tool_use_ref=tool_use.tool_use_id,
                        is_error=False,
                        output="已回复用户",
                    ),
                    tool_result=BaseToolResult(
                        output="已回复用户",
                        loop_control=LoopControl(decision="pause"),
                    ),
                )
            return ToolDispatchResult(
                tool_name=tool_use.name,
                source="local",
                result_part=ToolResultPart(
                    tool_use_ref=tool_use.tool_use_id,
                    is_error=False,
                    output=f"{tool_use.name} executed",
                ),
                tool_result=BaseToolResult(output="ok"),
            )

        self.engine = AgentExecutionEngine(
            runtime_gateway=self.runtime_gateway,
            agent_definition_gateway=SimpleNamespace(
                get_agent_definition=lambda aid: AgentDefinition(
                    agent_id=aid,
                    group="default",
                    tool_ids=("message_user", "run_bash_command"),
                )
            ),
            llm_registry_gateway=SimpleNamespace(
                resolve_provider_model=lambda p, b: SimpleNamespace(
                    provider_id=p,
                    binding_id=b,
                    model_id="model-a",
                    model_alias="model-a",
                    api_format=API_FORMAT_OPENAI_RESPONSES,
                    supported_input_modalities=(INPUT_MODALITY_TEXT,),
                )
            ),
            conversation_repository=self.conversation_repo,
            prompt_snippet_gateway=self.prompt_snippet_gateway,
            local_tool_gateway=self.local_tool_gateway,
            mcp_client_gateway=None,
            skill_gateway=None,
            llm_request_service=self.llm_request_service,
            tool_dispatcher=SimpleNamespace(
                build_llm_tools=AsyncMock(return_value=(LlmToolDefinition("message_user", {}, "local"),)),
                dispatch_tool_call=AsyncMock(side_effect=_mock_dispatch),
            ),
            trace_recorder=SimpleNamespace(
                start_trace=MagicMock(return_value="trace.md"),
                record_llm_turn=AsyncMock(),
                record_llm_response=MagicMock(),
                record_failure=MagicMock(),
                record_tool_result=MagicMock(),
                record_event=MagicMock(),
            ),
            event_publisher=NoopRuntimeEventPublisher(),
            task_note_prompt_provider=self.task_note_prompt_provider,
            task_note_snapshot_service=self.mock_snapshot_service,
        )

    def test_compression_gate_trigger_switches_to_note_update_state(self) -> None:
        """测试门禁触发时原地状态切换、Timeline 冻结、持久化和 Dual-switch Note Prompt/Tools。"""
        async def run_test() -> None:
            # 开启门禁触发
            self.engine._check_compression_gate = MagicMock(
                return_value=CompressionDecision(
                    should_compress=True,
                    reason="token_limit_exceeded",
                    used_tokens=9000,
                    threshold=8000,
                )
            )

            conversation = Conversation(id="conv-1")
            conversation.append_message(UniMessage(role=ROLE_USER, content=[TextPart("用户初始任务")]), [TextPartMeta()])
            await self.conversation_repo.save(conversation)

            task = AgentTask(task_id="task-100", root_agent_instance_id="inst-1", agent_instance_ids=("inst-1",))
            instance = AgentInstance(
                instance_id="inst-1",
                task_id="task-100",
                thread_id="thread-100",
                current_working_directory="G:/project/agent",
                agent_definition_id="developer",
                llm_provider_id="provider-a",
                llm_model_binding_id="binding-a",
                conversation_id=conversation.id,
                trace_id="trace-100",
            )
            self.runtime_gateway.instance = instance

            result = await self.engine._send_prompt_with_policy_failover(
                task=task,
                trace_path="trace.md",
                conversation=conversation,
                agent_definition=AgentDefinition(agent_id="developer"),
                instance=instance,
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
                    stream_id="stream-1",
                    actor_type="agent_instance",
                    actor_id="inst-1",
                    metadata={"task_id": "task-100", "agent_instance_id": "inst-1"},
                ),
                interrupt_state=_AgentExecutionInterruptState(),
                execution_frame_id=None,
            )

            # 验证 Step 1: 调用了快照服务
            self.mock_snapshot_service.build_task_note_snapshot_json.assert_called_with(
                task_id="task-100",
                agent_instance_id="inst-1",
            )

            # 验证 Step 2: 注入了边界 User 消息
            self.assertGreaterEqual(len(conversation.messages), 2)
            boundary_msg = conversation.messages[-2]
            self.assertEqual(boundary_msg.role, ROLE_USER)
            self.assertIn("note agent", boundary_msg.content[0].text)
            self.assertIn("note完整快照", boundary_msg.content[0].text)
            self.assertIn("测试指引", boundary_msg.content[0].text)

            # 验证 Step 3: Timeline 记录了冻结与激活
            self.assertGreater(len(conversation.timeline), 0)
            timeline_statuses = [e.status for e in conversation.timeline]
            self.assertIn("historical", timeline_statuses)
            self.assertIn("active", timeline_statuses)

            # 验证 Step 4: 持久化了 execution_substate = "note_update"
            self.assertEqual(
                self.runtime_gateway.substate_updates,
                [("inst-1", EXECUTION_SUBSTATE_NOTE_UPDATE)],
            )
            self.assertEqual(result.instance.execution_substate, EXECUTION_SUBSTATE_NOTE_UPDATE)

            # 验证 Step 5: LLM 发送使用了 Note System Prompt 和 Note Tools
            self.assertEqual(len(self.llm_request_service.sent_requests), 1)
            sent_req = self.llm_request_service.sent_requests[0]
            self.assertIn("系统Note 更新 状态", sent_req.system_prompt)
            self.assertIn("inst-1", sent_req.system_prompt)
            tool_names = [t.name for t in sent_req.tools]
            self.assertIn("note_update_end", tool_names)
            self.assertIn("create_task_todo", tool_names)
            self.assertNotIn("run_bash_command", tool_names)

        asyncio.run(run_test())

    def test_tool_permission_isolation_in_note_update_state(self) -> None:
        """测试在 note_update 状态下非 Note 工具调用被严格拦截并返回工具错误。"""
        async def run_test() -> None:
            instance = AgentInstance(
                instance_id="inst-1",
                task_id="task-100",
                thread_id="thread-100",
                current_working_directory="G:/project/agent",
                agent_definition_id="developer",
                llm_provider_id="provider-a",
                llm_model_binding_id="binding-a",
                conversation_id="conv-1",
                trace_id="trace-100",
                execution_substate=EXECUTION_SUBSTATE_NOTE_UPDATE,
            )
            self.runtime_gateway.instance = instance

            non_note_tool_call = ToolUsePart(
                name="run_bash_command",
                input={"command": "dir"},
                tool_use_id="call-1",
            )
            comm_context = RuntimeCommunicationContext(
                stream_id="stream-1",
                actor_type="agent_instance",
                actor_id="inst-1",
            )
            dispatch_result = await self.engine._execute_single_tool_use(
                definition=AgentDefinition(agent_id="developer"),
                tool_use=non_note_tool_call,
                communication_context=comm_context,
                instance=instance,
            )
            self.assertTrue(dispatch_result.result_part.is_error)
            output_content = dispatch_result.result_part.output
            output_text = (
                output_content[0]["text"]
                if isinstance(output_content, list) and output_content and isinstance(output_content[0], dict)
                else str(output_content)
            )
            self.assertIn("仅允许调用 Task Note 工具", output_text)

        asyncio.run(run_test())

    def test_resumed_instance_in_note_update_state_continues_note_mode(self) -> None:
        """测试暂停恢复后，instance 已处于 note_update 状态时，直接使用 Note Prompt 和 Tools，不再重复触发门禁。"""
        async def run_test() -> None:
            conversation = Conversation(id="conv-1")
            conversation.append_message(UniMessage(role=ROLE_USER, content=[TextPart("用户任务")]), [TextPartMeta()])
            await self.conversation_repo.save(conversation)

            instance = AgentInstance(
                instance_id="inst-1",
                task_id="task-100",
                thread_id="thread-100",
                current_working_directory="G:/project/agent",
                agent_definition_id="developer",
                llm_provider_id="provider-a",
                llm_model_binding_id="binding-a",
                conversation_id=conversation.id,
                trace_id="trace-100",
                execution_substate=EXECUTION_SUBSTATE_NOTE_UPDATE,
            )
            self.runtime_gateway.instance = instance

            result = await self.engine._send_prompt_with_policy_failover(
                task=AgentTask(task_id="task-100", root_agent_instance_id="inst-1", agent_instance_ids=("inst-1",)),
                trace_path="trace.md",
                conversation=conversation,
                agent_definition=AgentDefinition(agent_id="developer"),
                instance=instance,
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
                    stream_id="stream-1",
                    actor_type="agent_instance",
                    actor_id="inst-1",
                    metadata={"task_id": "task-100", "agent_instance_id": "inst-1"},
                ),
                interrupt_state=_AgentExecutionInterruptState(),
                execution_frame_id=None,
            )

            self.assertEqual(len(self.runtime_gateway.substate_updates), 0)
            self.assertEqual(result.instance.execution_substate, EXECUTION_SUBSTATE_NOTE_UPDATE)

            sent_req = self.llm_request_service.sent_requests[0]
            self.assertIn("系统Note 更新 状态", sent_req.system_prompt)
            tool_names = [t.name for t in sent_req.tools]
            self.assertIn("note_update_end", tool_names)
            self.assertIn("create_task_todo", tool_names)

        asyncio.run(run_test())

    def test_note_update_end_swaps_conversation_and_clears_substate(self) -> None:
        """测试在执行循环中调用 note_update_end 时，原子创建新会话、切换 conversation_id 并恢复业务状态。"""
        async def run_test() -> None:
            # 准备两轮 LLM 回复：
            # 第一轮：处于 note_update 状态，LLM 调用 note_update_end 工具
            # 第二轮：切换到全新会话后，LLM 调用 message_user 工具向用户汇报并暂停该轮
            msg_round1 = UniMessage(
                role=ROLE_ASSISTANT,
                content=[
                    ToolUsePart(
                        name="note_update_end",
                        input={},
                        tool_use_id="tu-1",
                    )
                ],
                source_provider=API_FORMAT_OPENAI_RESPONSES,
            )
            msg_round2 = UniMessage(
                role=ROLE_ASSISTANT,
                content=[
                    ToolUsePart(
                        name="message_user",
                        input={"message": "已在全新会话中继续推进业务任务。"},
                        tool_use_id="tu-2",
                    )
                ],
                source_provider=API_FORMAT_OPENAI_RESPONSES,
            )
            self.llm_request_service._responses = [msg_round1, msg_round2]

            old_conversation = Conversation(id="conv-old")
            old_conversation.append_message(UniMessage(role=ROLE_USER, content=[TextPart("原始任务")]), [TextPartMeta()])
            await self.conversation_repo.save(old_conversation)

            task = AgentTask(task_id="task-100", root_agent_instance_id="inst-1", agent_instance_ids=("inst-1",))
            instance = AgentInstance(
                instance_id="inst-1",
                task_id="task-100",
                thread_id="thread-100",
                current_working_directory="G:/project/agent",
                agent_definition_id="developer",
                llm_provider_id="provider-a",
                llm_model_binding_id="binding-a",
                conversation_id=old_conversation.id,
                trace_id="trace-100",
                execution_substate=EXECUTION_SUBSTATE_NOTE_UPDATE,
            )
            self.runtime_gateway.instance = instance

            request = AgentExecutionRequest(
                thread_id="thread-100",
                current_working_directory="G:/project/agent",
                agent_definition_id="developer",
                llm_provider_id="provider-a",
                llm_model_binding_id="binding-a",
                user_parts=(TextPart(text="继续执行"),),
                user_parts_meta=(TextPartMeta(),),
                task_id="task-100",
                conversation_id=old_conversation.id,
                communication_stream_id="stream-1",
            )

            with TemporaryDirectory() as tmp_dir:
                exec_result = await self.engine._run_agent_instance(
                    task=task,
                    instance=instance,
                    request=request,
                    interrupt_state=_AgentExecutionInterruptState(),
                )

                # 1. 验证调用了快照服务
                self.mock_snapshot_service.build_task_note_snapshot_json.assert_called_with(
                    task_id="task-100",
                    agent_instance_id="inst-1",
                )

                # 2. 验证切换了 Agent 实例的 Conversation
                self.assertEqual(len(self.runtime_gateway.conversation_switches), 1)
                inst_id, new_conv_id = self.runtime_gateway.conversation_switches[0]
                self.assertEqual(inst_id, "inst-1")
                self.assertNotEqual(new_conv_id, old_conversation.id)

                # 3. 验证更新了 execution_substate 为 None
                self.assertIn(("inst-1", None), self.runtime_gateway.substate_updates)

                # 4. 验证新会话创建并包含 Note 快照初始消息
                self.assertIn(new_conv_id, self.conversation_repo.conversations)
                new_conv = self.conversation_repo.conversations[new_conv_id]
                first_msg = new_conv.messages[0]
                self.assertEqual(first_msg.role, ROLE_USER)
                self.assertIn("系统已完成上下文压缩与Note更新", first_msg.content[0].text)
                self.assertIn("测试指引", first_msg.content[0].text)

                # 5. 验证执行循环在全新会话中完成了第二轮业务回复
                self.assertEqual(exec_result.conversation_id, new_conv_id)

        asyncio.run(run_test())


if __name__ == '__main__':
    unittest.main()