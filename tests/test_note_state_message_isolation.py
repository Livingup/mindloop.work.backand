# -*- coding: utf-8 -*-
"""Note 状态下消息精准路由与隔离缓存自动化测试。

验证：
1. AgentInstance.drain_pending_user_messages 支持 predicate 过滤消费，未命中消息安全保留；
2. Note 状态下，真实人类用户消息正常消费进 Note 对话，外部远程 Agent 和本地父子 Agent 消息保留在队列中；
3. 退出 Note 状态并切换全新业务会话后，先前保留的外部 Agent 消息被全量消费进新业务会话。
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_runtime.agent.agent_execution_engine import (
    AgentExecutionEngine,
    AgentExecutionRequest,
    _is_note_state_allowed_user_message,
)
from agent_runtime.agent.agent_instance import (
    AGENT_INSTANCE_STATUS_RUNNING,
    EXECUTION_SUBSTATE_NOTE_UPDATE,
    AgentInstance,
    PendingAgentUserMessage,
)
from agent_runtime.agent.agent_task import AgentTask
from agent_runtime.agent.agent_trace_recorder import AgentTraceRecorder
from agent_runtime.agent.tool_dispatch.tool_dispatcher import ToolDispatchResult
from agent_runtime.task_note.task_note_prompt_provider import TASK_NOTE_TOOL_NAMES
from agent_runtime.agent_definition.agent_definition import AgentDefinition
from agent_runtime.communication.runtime_event_publisher import (
    NoopRuntimeEventPublisher,
    RuntimeCommunicationContext,
)
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
from agent_runtime.task_note.task_note_snapshot_service import TaskNoteSnapshotService
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
                content=[TextPart("执行中...")],
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
    def __init__(self, instance: AgentInstance) -> None:
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
        return AgentTask(
            task_id=task_id,
            root_agent_instance_id=self.instance.instance_id,
            agent_instance_ids=(self.instance.instance_id,),
        )

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


class _MockLocalToolGateway:
    def list_tools(self, *, public_only: bool = False, group_by: bool = False):
        tools = []
        for name in sorted(TASK_NOTE_TOOL_NAMES):
            tools.append(
                SimpleNamespace(
                    tool_name=name,
                    group="task_note",
                    category="system" if name == "note_update_end" else "business",
                    always_enabled=name == "note_update_end",
                    description=f"{name} description",
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


def test_agent_instance_drain_predicate_unit():
    """单元测试：AgentInstance 的按条件 drain 行为。"""
    user_msg = PendingAgentUserMessage(
        message_id="msg-user-1",
        user_text="请注意这个需求",
        metadata={"source_type": "user"},
    )
    subagent_msg = PendingAgentUserMessage(
        message_id="msg-subagent-1",
        user_text="【@本地Agent Sub1 inst-2】已完成代码编写",
        metadata={
            "source_type": "local_agent",
            "transport": "subagent",
            "channel": "subagent",
            "subagent": {"from_instance_id": "inst-2"},
        },
    )
    remote_msg = PendingAgentUserMessage(
        message_id="msg-remote-1",
        user_text="【@远程Agent Remote1】外部数据已就绪",
        metadata={
            "source_type": "remote_agent",
            "transport": "lacp",
            "channel": "lacp",
        },
    )

    inst = AgentInstance(
        instance_id="inst-1",
        task_id="task-1",
        thread_id="thread-1",
        conversation_id="conv-1",
        trace_id="trace-1",
        current_working_directory="/test",
        agent_definition_id="def-1",
        llm_provider_id="prov-1",
        llm_model_binding_id="bind-1",
    )
    inst = inst.enqueue_user_message(user_msg)
    inst = inst.enqueue_user_message(subagent_msg)
    inst = inst.enqueue_user_message(remote_msg)

    assert len(inst.pending_user_messages) == 3

    # 1. 模拟 Note 状态过滤 drain：只消费人类用户消息
    inst_after_note_drain, drained = inst.drain_pending_user_messages(
        predicate=_is_note_state_allowed_user_message,
    )
    assert len(drained) == 1
    assert drained[0].message_id == "msg-user-1"
    assert len(inst_after_note_drain.pending_user_messages) == 2
    assert inst_after_note_drain.pending_user_messages[0].message_id == "msg-subagent-1"
    assert inst_after_note_drain.pending_user_messages[1].message_id == "msg-remote-1"

    # 2. 再次在 Note 状态下 drain：队列中只有外部消息，不会被取出
    inst_unchanged, drained_second = inst_after_note_drain.drain_pending_user_messages(
        predicate=_is_note_state_allowed_user_message,
    )
    assert len(drained_second) == 0
    assert len(inst_unchanged.pending_user_messages) == 2

    # 3. 切回业务状态全量 drain：所有保留的消息全部取出
    inst_empty, drained_all = inst_unchanged.drain_pending_user_messages(predicate=None)
    assert len(drained_all) == 2
    assert drained_all[0].message_id == "msg-subagent-1"
    assert drained_all[1].message_id == "msg-remote-1"
    assert len(inst_empty.pending_user_messages) == 0


@pytest.mark.asyncio
async def test_execution_engine_note_state_message_isolation_flow():
    """流程测试：ExecutionEngine 在 Note 状态下过滤外部消息，切回业务后全量消费。"""
    conv_id = "conv-note-test"
    initial_conv = Conversation(id=conv_id)
    initial_conv.append_message(
        UniMessage(role=ROLE_USER, content=[TextPart("初始任务")]),
        [TextPartMeta()],
    )

    user_msg = PendingAgentUserMessage(
        message_id="msg-user-note",
        user_text="人类指令：请将 Note 聚焦在关键改动上",
        metadata={"source_type": "user"},
    )
    subagent_msg = PendingAgentUserMessage(
        message_id="msg-subagent-work",
        user_text="【@本地Agent Worker inst-worker】子任务进度汇报",
        metadata={"source_type": "local_agent", "transport": "subagent"},
    )
    remote_msg = PendingAgentUserMessage(
        message_id="msg-remote-sync",
        user_text="【@远程Agent Peer inst-peer】数据同步完成",
        metadata={"source_type": "remote_agent", "transport": "lacp"},
    )

    instance = AgentInstance(
        instance_id="inst-1",
        task_id="task-1",
        thread_id="thread-1",
        conversation_id=conv_id,
        trace_id="trace-1",
        current_working_directory="/test",
        agent_definition_id="def-1",
        llm_provider_id="prov-1",
        llm_model_binding_id="bind-1",
        status=AGENT_INSTANCE_STATUS_RUNNING,
        execution_substate=EXECUTION_SUBSTATE_NOTE_UPDATE,  # 已经处于 Note 状态
    )
    instance = instance.enqueue_user_message(user_msg)
    instance = instance.enqueue_user_message(subagent_msg)
    instance = instance.enqueue_user_message(remote_msg)

    repo = _MockConversationRepository({conv_id: initial_conv})
    gateway = _MockRuntimeGateway(instance)

    # 模拟 LLM 响应：Note Agent 整理完毕，调用 note_update_end 结束 Note 状态
    tool_use_note = ToolUsePart(name="note_update_end", input={"summary": "note finished"}, tool_use_id="call-note-1")
    llm_response_msg = UniMessage(
        role=ROLE_ASSISTANT,
        content=[TextPart("Note 整理完成，准备退出。"), tool_use_note],
        source_provider=API_FORMAT_OPENAI_RESPONSES,
    )
    # 随后的第 2 轮（切回业务后）调用 message_user 并自然 pause 结束
    tool_use_biz = ToolUsePart(name="message_user", input={"message": "ok"}, tool_use_id="call-biz-1")
    biz_response_msg = UniMessage(
        role=ROLE_ASSISTANT,
        content=[TextPart("收到子 Agent 与远程 Agent 的消息，继续推进业务。"), tool_use_biz],
        source_provider=API_FORMAT_OPENAI_RESPONSES,
    )
    llm_service = _MockLlmRequestService([llm_response_msg, biz_response_msg])

    tool_dispatcher = MagicMock()
    async def _mock_dispatch(agent_definition, tool_use, communication_context):
        if tool_use.name == "note_update_end":
            return ToolDispatchResult(
                tool_name="note_update_end",
                source="local",
                result_part=ToolResultPart(
                    tool_use_ref=tool_use.tool_use_id,
                    output={"status": "completed"},
                    is_error=False,
                ),
                tool_result=BaseToolResult(
                    output={"status": "completed"},
                    loop_control=LoopControl(decision="continue"),
                ),
            )
        if tool_use.name == "message_user":
            return ToolDispatchResult(
                tool_name="message_user",
                source="local",
                result_part=ToolResultPart(
                    tool_use_ref=tool_use.tool_use_id,
                    output={"status": "completed"},
                    is_error=False,
                ),
                tool_result=BaseToolResult(
                    output={"status": "completed"},
                    loop_control=LoopControl(decision="pause"),
                ),
            )
        return ToolDispatchResult(
            tool_name=tool_use.name,
            source="local",
            result_part=ToolResultPart(
                tool_use_ref=tool_use.tool_use_id,
                output={"status": "ok"},
                is_error=False,
            ),
            tool_result=BaseToolResult(output="ok"),
        )
    tool_dispatcher.dispatch_tool_call = AsyncMock(side_effect=_mock_dispatch)
    tool_dispatcher.build_llm_tools = AsyncMock(return_value=[])

    note_prompt_provider = MagicMock()
    note_prompt_provider.get_note_tools.return_value = [
        LlmToolDefinition(
            name="note_update_end",
            schema={"name": "note_update_end"},
        )
    ]
    note_prompt_provider.get_note_tool_names.return_value = ["note_update_end"]
    note_prompt_provider.render_note_system_prompt.return_value = "你是 Note Agent"
    note_prompt_provider.build_resumed_conversation_prompt.return_value = "# 现场工作手记恢复"

    note_snapshot_service = MagicMock()
    note_snapshot_service.build_task_note_snapshot_json = AsyncMock(return_value={"snapshot": "mock"})
    note_snapshot_service.async_cleanup_task_note = AsyncMock()

    trace_recorder = SimpleNamespace(
        start_trace=MagicMock(return_value="trace.md"),
        record_llm_turn=AsyncMock(),
        record_llm_response=MagicMock(),
        record_failure=MagicMock(),
        record_tool_result=MagicMock(),
        record_event=MagicMock(),
        record_user_message=MagicMock(),
    )
    engine = AgentExecutionEngine(
        runtime_gateway=gateway,
        conversation_repository=repo,
        agent_definition_gateway=MagicMock(get_agent_definition=lambda _id: AgentDefinition(
            agent_id="def-1",
            name="Worker",
            system_prompt=(),
            tool_ids=("note_update_end", "message_user"),
        )),
        llm_registry_gateway=MagicMock(resolve_runtime_execution_policy=lambda **_: (
            (SimpleNamespace(
                provider_id="prov-1",
                binding_id="bind-1",
                model_id="gpt-4o",
                model_alias="gpt-4o",
                api_format=API_FORMAT_OPENAI_RESPONSES,
                supported_modalities=(INPUT_MODALITY_TEXT,),
            ),),
            "degrade",
        )),
        prompt_snippet_gateway=_MockPromptSnippetGateway(),
        local_tool_gateway=_MockLocalToolGateway(),
        mcp_client_gateway=None,
        skill_gateway=None,
        llm_request_service=llm_service,
        trace_recorder=trace_recorder,
        tool_dispatcher=tool_dispatcher,
        event_publisher=NoopRuntimeEventPublisher(),
        task_note_snapshot_service=note_snapshot_service,
        task_note_prompt_provider=note_prompt_provider,
    )

    request = AgentExecutionRequest(
        thread_id="thread-1",
        current_working_directory="/test",
        agent_definition_id="def-1",
        llm_provider_id="prov-1",
        llm_model_binding_id="bind-1",
        user_parts=(),
        user_parts_meta=(),
        task_id="task-1",
        conversation_id=conv_id,
    )

    task = gateway.get_task("task-1")
    await asyncio.wait_for(
        engine._run_agent_instance(
            task=task,
            instance=instance,
            request=request,
        ),
        timeout=30.0,
    )

    # 检查 1：旧 Note 会话（initial_conv）中的消息
    # 应该只包含了人类消息，绝不包含子 Agent 和远程 Agent 消息！
    note_conv_messages = initial_conv.messages
    note_user_texts = [
        part.text
        for msg in note_conv_messages
        if msg.role == ROLE_USER
        for part in msg.content
        if isinstance(part, TextPart)
    ]
    # 验证人类消息被追加到了 Note 会话
    assert any("人类指令：请将 Note 聚焦在关键改动上" in t for t in note_user_texts)
    # 验证外部 Agent 消息绝对没有进入 Note 会话
    assert not any("子任务进度汇报" in t for t in note_user_texts)
    assert not any("数据同步完成" in t for t in note_user_texts)

    # 检查 2：新业务会话（new_conversation）
    # 实例已被切换到新会话
    assert len(gateway.conversation_switches) >= 1
    new_conv_id = gateway.conversation_switches[-1][1]
    assert new_conv_id != conv_id
    new_conv = repo.conversations[new_conv_id]

    new_conv_user_texts = [
        part.text
        for msg in new_conv.messages
        if msg.role == ROLE_USER
        for part in msg.content
        if isinstance(part, TextPart)
    ]
    # 验证第一条是工作手记
    assert any("现场工作手记恢复" in t for t in new_conv_user_texts)
    # 验证之前在 Note 期间保留的子 Agent 消息和远程 Agent 消息，被全量消费追加到了新业务会话尾部！
    assert any("子任务进度汇报" in t for t in new_conv_user_texts)
    assert any("数据同步完成" in t for t in new_conv_user_texts)
    # 实例待处理队列已经被清空
    assert len(gateway.instance.pending_user_messages) == 0
