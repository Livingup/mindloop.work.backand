# -*- coding: utf-8 -*-
import json
import pytest

from agent_runtime.task_note.task_note_prompt_provider import (
    NOTE_UPDATE_SYSTEM_PROMPT_TEMPLATE_ID,
    TASK_NOTE_TOOL_NAMES,
    TaskNotePromptProvider,
)
from agent_runtime.tool.local_tool_factory import LocalToolFactory
from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_CHAT_COMPLETIONS


def test_get_note_tool_names():
    provider = TaskNotePromptProvider()
    names = provider.get_note_tool_names()
    assert len(names) == 37
    assert "create_mission" in names
    assert "note_update_end" in names
    assert "update_task_progress" in names


def test_render_note_system_prompt_from_gateway():
    from types import SimpleNamespace

    class MockGateway:
        def get_snippet(self, name: str):
            assert name == NOTE_UPDATE_SYSTEM_PROMPT_TEMPLATE_ID
            return SimpleNamespace(
                name=name,
                text="你是{{agent_instance_id}}，你处于Note更新状态。可用工具：{{available_tools}}。",
            )

    provider = TaskNotePromptProvider(prompt_snippet_gateway=MockGateway())
    rendered = provider.render_note_system_prompt(
        agent_instance_id="agent-inst-test-001",
    )
    assert "agent-inst-test-001" in rendered
    assert "create_mission" in rendered
    assert "note_update_end" in rendered
    assert "{{agent_instance_id}}" not in rendered
    assert "{{available_tools}}" not in rendered


def test_load_raw_template_missing_raises_keyerror():
    class EmptyGateway:
        def get_snippet(self, name: str):
            raise KeyError(f"未找到: {name}")

    provider = TaskNotePromptProvider(prompt_snippet_gateway=EmptyGateway())
    with pytest.raises(KeyError):
        provider.load_raw_template()


def test_get_note_tools_with_gateway():
    gateway = LocalToolFactory.create()
    provider = TaskNotePromptProvider(local_tool_gateway=gateway)
    tools = provider.get_note_tools(API_FORMAT_OPENAI_CHAT_COMPLETIONS)
    assert len(tools) == 37
    names = {t.name for t in tools}
    assert "create_mission" in names
    assert "note_update_end" in names
    for t in tools:
        assert t.source == "local"
        assert t.schema is not None
        assert "name" in t.schema or "function" in t.schema or "type" in t.schema


def test_boundary_and_resumed_prompts():
    provider = TaskNotePromptProvider()
    snapshot = {"task_id": "task-1", "missions": []}
    boundary = provider.build_compression_boundary_prompt(snapshot)
    assert "上面的历史消息是之前的业务agent的对话记录" in boundary
    assert "task-1" in boundary

    resumed = provider.build_resumed_conversation_prompt(snapshot)
    assert "系统已完成上下文压缩与Note更新" in resumed
    assert "task-1" in resumed