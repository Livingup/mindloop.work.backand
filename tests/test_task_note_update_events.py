# -*- coding: utf-8 -*-
"""验证子 Agent 协作与 LACP 远程消息触发 task_note.updated 事件广播。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest

from agent_runtime.agent_definition.agent_definition import AgentDefinition
from agent_runtime.agent.tool_dispatch.tool_dispatcher import AgentToolDispatcher
from agent_runtime.communication.runtime_event_publisher import RuntimeCommunicationContext
from agent_runtime.conversation_message.message.models import ToolUsePart
from agent_runtime.lacp.bridge import AgentRuntimeLacpBridge
from agent_runtime.lacp.models import LacpInboundMessage
from agent_runtime.tool.tool_execution_models import BaseToolResult


class MockEventPublisher:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, context, event_name, payload, **kwargs):
        self.events.append({
            "context": context,
            "event_name": event_name,
            "payload": payload,
            "kwargs": kwargs,
        })


@pytest.mark.asyncio
async def test_dispatcher_create_subagent_broadcasts_to_parent_and_child():
    publisher = MockEventPublisher()
    mock_local_gateway = MagicMock()
    mock_local_gateway.get_tool.return_value = SimpleNamespace(category="system", always_enabled=True)
    mock_local_gateway.execute_tool = AsyncMock(
        return_value=BaseToolResult(
            success=True,
            output={
                "task_id": "task-100",
                "child_agent_instance_id": "child-agent-2",
            },
        )
    )

    dispatcher = AgentToolDispatcher(
        local_tool_gateway=mock_local_gateway,
        event_publisher=publisher,
    )

    comm_context = RuntimeCommunicationContext(
        stream_id="stream-1",
        metadata={
            "task_id": "task-100",
            "agent_instance_id": "parent-agent-1",
            "scene_id": "scene-1",
        },
    )

    await dispatcher.dispatch_tool_call(
        agent_definition=AgentDefinition(agent_id="parent_agent", tool_ids=("create_subagent",)),
        tool_use=ToolUsePart(name="create_subagent", input={"agent_definition_id": "child"}),
        communication_context=comm_context,
    )

    note_events = [e for e in publisher.events if e["event_name"] == "task_note.updated"]
    assert len(note_events) == 2
    instances = {e["payload"]["agent_instance_id"] for e in note_events}
    assert instances == {"parent-agent-1", "child-agent-2"}
    assert all(e["payload"]["tool_name"] == "create_subagent" for e in note_events)


@pytest.mark.asyncio
async def test_dispatcher_send_agent_message_broadcasts_to_both_agents():
    publisher = MockEventPublisher()
    mock_local_gateway = MagicMock()
    mock_local_gateway.get_tool.return_value = SimpleNamespace(category="system", always_enabled=True)
    mock_local_gateway.execute_tool = AsyncMock(
        return_value=BaseToolResult(
            success=True,
            output={
                "task_id": "task-100",
                "target_agent_instance_id": "child-agent-2",
            },
        )
    )

    dispatcher = AgentToolDispatcher(
        local_tool_gateway=mock_local_gateway,
        event_publisher=publisher,
    )

    comm_context = RuntimeCommunicationContext(
        stream_id="stream-1",
        metadata={
            "task_id": "task-100",
            "agent_instance_id": "parent-agent-1",
            "scene_id": "scene-1",
        },
    )

    await dispatcher.dispatch_tool_call(
        agent_definition=AgentDefinition(agent_id="parent_agent", tool_ids=("send_agent_message",)),
        tool_use=ToolUsePart(name="send_agent_message", input={"to_agent_instance_id": "child-agent-2", "content": "hi"}),
        communication_context=comm_context,
    )

    note_events = [e for e in publisher.events if e["event_name"] == "task_note.updated"]
    assert len(note_events) == 2
    instances = {e["payload"]["agent_instance_id"] for e in note_events}
    assert instances == {"parent-agent-1", "child-agent-2"}
    assert all(e["payload"]["tool_name"] == "send_agent_message" for e in note_events)


@pytest.mark.asyncio
async def test_dispatcher_end_subagent_broadcasts_to_both_agents():
    publisher = MockEventPublisher()
    mock_local_gateway = MagicMock()
    mock_local_gateway.get_tool.return_value = SimpleNamespace(category="system", always_enabled=True)
    mock_local_gateway.execute_tool = AsyncMock(
        return_value=BaseToolResult(
            success=True,
            output={
                "task_id": "task-100",
                "released_agent_instance_ids": ["child-agent-2", "child-agent-3"],
            },
        )
    )

    dispatcher = AgentToolDispatcher(
        local_tool_gateway=mock_local_gateway,
        event_publisher=publisher,
    )

    comm_context = RuntimeCommunicationContext(
        stream_id="stream-1",
        metadata={
            "task_id": "task-100",
            "agent_instance_id": "parent-agent-1",
            "scene_id": "scene-1",
        },
    )

    await dispatcher.dispatch_tool_call(
        agent_definition=AgentDefinition(agent_id="parent_agent", tool_ids=("end_subagent",)),
        tool_use=ToolUsePart(name="end_subagent", input={"child_agent_instance_ids": ["child-agent-2"]}),
        communication_context=comm_context,
    )

    note_events = [e for e in publisher.events if e["event_name"] == "task_note.updated"]
    assert len(note_events) == 3
    instances = {e["payload"]["agent_instance_id"] for e in note_events}
    assert instances == {"parent-agent-1", "child-agent-2", "child-agent-3"}
    assert all(e["payload"]["tool_name"] == "end_subagent" for e in note_events)


@pytest.mark.asyncio
async def test_lacp_bridge_inbound_message_broadcasts_task_note_updated():
    publisher = MockEventPublisher()
    mock_runtime_gateway = MagicMock()
    mock_runtime_gateway.get_agent_instance.return_value = SimpleNamespace(
        instance_id="agent-recv-1",
        task_id="task-999",
    )
    mock_runtime_gateway.get_task.return_value = SimpleNamespace(
        task_id="task-999",
        scene_id="scene-test",
    )

    mock_execution_engine = MagicMock()
    mock_execution_engine.submit_agent_instance_message = AsyncMock(return_value=SimpleNamespace(status="queued"))
    mock_execution_engine._event_publisher = publisher

    mock_registration_registry = MagicMock()
    mock_registration_registry.is_registered.return_value = True

    bridge = AgentRuntimeLacpBridge(
        lacp_client=MagicMock(),
        registration_registry=mock_registration_registry,
        runtime_gateway=mock_runtime_gateway,
        agent_definition_gateway=MagicMock(),
        execution_engine=mock_execution_engine,
    )

    inbound = LacpInboundMessage.from_frame({
        "type": "message",
        "message_id": "msg-12345",
        "source_tag": "@PeerAgent remote-sender-99",
        "from_instance_id": "remote-sender-99",
        "from_role_name": "PeerAgent",
        "from_workspace": "D:/workspace",
        "to_instance_id": "agent-recv-1",
        "content": "Remote sync completed",
        "attachments": [],
    })

    ok = await bridge.handle_inbound_message(inbound)
    assert ok is True

    note_events = [e for e in publisher.events if e["event_name"] == "task_note.updated"]
    assert len(note_events) == 1
    ev = note_events[0]
    assert ev["payload"]["task_id"] == "task-999"
    assert ev["payload"]["agent_instance_id"] == "agent-recv-1"
    assert ev["payload"]["scene_id"] == "scene-test"
    assert ev["payload"]["tool_name"] == "lacp_inbound_message"
