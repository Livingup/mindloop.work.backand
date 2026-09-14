import asyncio
import copy
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_runtime.agent.agent_execution_engine import (
    DEFAULT_INSTRUCTIONS_FALLBACK,
    STABLE_PREFIX_BASE_TEMPLATES,
    AgentExecutionEngine,
)
from agent_runtime.agent_definition.agent_definition import AgentDefinition, SystemPromptSegment
from agent_runtime.conversation_message.infrastructure.sqlite_conversation_repository import (
    SqliteConversationRepository,
)
from agent_runtime.conversation_message.message.models import (
    Conversation,
    InstructionsSnapshot,
    TextPart,
    UniMessage,
)
from agent_runtime.conversation_message.message.openai_protocol import OpenAIRenderResult
from agent_runtime.llm_client.llm_protocol_clients import OpenAIResponsesProtocolClient
from agent_runtime.llm_client.llm_request_models import LlmPromptRequest, LlmToolDefinition
from agent_runtime.llm_client.llm_request_service import (
    LlmRequestService,
    _resolve_openai_request_configuration,
)
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    LlmRetryPolicy,
    ResolvedLlmProviderModel,
)


class _DummyResolver:
    async def resolve_resource(self, resource_id: str):
        raise AssertionError(f"unexpected resource resolution: {resource_id}")


def _mock_model(api_format="openai_responses"):
    return ResolvedLlmProviderModel(
        provider_id="provider",
        binding_id="binding",
        model_id="model",
        model_alias="model",
        api_format=api_format,
        sdk_platform="openai",
        api_base_url="https://example.test",
        custom_headers={},
        api_keys=(),
        retry=LlmRetryPolicy(max_retries_per_key=0),
        tool_choice=None,
        thinking=None,
        custom_request_params=None,
        max_output_tokens=100,
        context_window=1000,
        compression_threshold=None,
        supports_tool_calling=True,
        supports_structured_output=True,
        supported_input_modalities=("text",),
    )


def _build_engine_for_render_test() -> AgentExecutionEngine:
    engine = object.__new__(AgentExecutionEngine)
    engine._build_mcp_services_prompt = lambda definition: "## MCP Services"
    engine._build_skills_prompt = lambda definition: "## Skills"
    engine._build_available_subagents_prompt = lambda definition: "## Available Subagents"
    engine._resolve_skill_root_directory = lambda: ""
    engine._build_tool_usage_notice = lambda definition, tools: "## Tool Usage Notice"

    prompt_snippets = {
        "General-purpose self-learning agent": MagicMock(text="## General Base Prompt"),
        "MCP服务使用": MagicMock(text="## MCP Usage Prompt"),
        "生命周期工具调用规范": MagicMock(text="## Lifecycle Rules Prompt"),
        "System-runtime-platform-base": MagicMock(text="Instance: {{agent_instance_id}}, CWD: {{current_working_directory}}"),
        "Custom-task-instructions": MagicMock(text="## Custom Task Instructions"),
    }
    snippet_gw = MagicMock()
    snippet_gw.get_snippet = lambda template_id: prompt_snippets.get(template_id, MagicMock(text=f"## {template_id}"))
    engine._prompt_snippet_gateway = snippet_gw
    return engine


def test_first_agent_with_base_templates_creates_snapshot_and_filters_developer_prompt():
    engine = _build_engine_for_render_test()
    conv = Conversation(id="conv-1")
    definition = AgentDefinition(
        agent_id="root-agent",
        name="Root Agent",
        system_prompt=(
            SystemPromptSegment(template_id="General-purpose self-learning agent"),
            SystemPromptSegment(template_id="MCP服务使用"),
            SystemPromptSegment(template_id="生命周期工具调用规范"),
            SystemPromptSegment(template_id="System-runtime-platform-base"),
            SystemPromptSegment(template_id="Custom-task-instructions"),
        ),
    )

    rendered_prompt = engine._render_system_prompt(
        definition=definition,
        agent_instance_id="inst-abc",
        current_working_directory="/test/dir",
        render_options={},
        llm_tools=(),
        conversation=conv,
    )

    # 1. instructions_snapshot 应该被成功初始化并记录 3 大基座模板
    assert conv.instructions_snapshot is not None
    assert conv.instructions_snapshot.template_names == (
        "General-purpose self-learning agent",
        "MCP服务使用",
        "生命周期工具调用规范",
    )
    assert "## General Base Prompt" in conv.instructions_snapshot.content
    assert "## MCP Usage Prompt" in conv.instructions_snapshot.content
    assert "## Lifecycle Rules Prompt" in conv.instructions_snapshot.content

    # 2. 渲染给 developer 消息的系统提示词应当过滤掉 3 大基座模板，仅保留差异部分与 tool_usage_notice
    assert "## General Base Prompt" not in rendered_prompt
    assert "## MCP Usage Prompt" not in rendered_prompt
    assert "## Lifecycle Rules Prompt" not in rendered_prompt
    assert "Instance: inst-abc" in rendered_prompt
    assert f"CWD: {Path('/test/dir').resolve()}" in rendered_prompt
    assert "## Custom Task Instructions" in rendered_prompt
    assert "## Tool Usage Notice" in rendered_prompt


def test_first_agent_without_base_templates_creates_default_fallback_snapshot():
    engine = _build_engine_for_render_test()
    conv = Conversation(id="conv-2")
    definition = AgentDefinition(
        agent_id="lightweight-agent",
        name="Lightweight Agent",
        system_prompt=(
            SystemPromptSegment(template_id="Custom-task-instructions"),
        ),
    )

    rendered_prompt = engine._render_system_prompt(
        definition=definition,
        agent_instance_id="inst-xyz",
        current_working_directory="/test/dir",
        render_options={},
        llm_tools=(),
        conversation=conv,
    )

    # 1. instructions_snapshot 应该采用默认兜底文本
    assert conv.instructions_snapshot is not None
    assert conv.instructions_snapshot.template_names == ()
    assert conv.instructions_snapshot.content == DEFAULT_INSTRUCTIONS_FALLBACK

    # 2. 开发者系统提示词应保留自定义任务提示词
    assert "## Custom Task Instructions" in rendered_prompt
    assert "## Tool Usage Notice" in rendered_prompt


def test_snapshot_immutability_on_subsequent_agent_switch():
    engine = _build_engine_for_render_test()
    conv = Conversation(id="conv-3")

    # 初始 Agent 创建了快照
    initial_snapshot = InstructionsSnapshot(
        template_names=("General-purpose self-learning agent", "MCP服务使用", "生命周期工具调用规范"),
        content="## Stable Prefix Instructions Content",
    )
    conv.instructions_snapshot = initial_snapshot

    # 发生定义切换或 Note 刷新，传入新 Agent 定义
    second_definition = AgentDefinition(
        agent_id="note-update-agent",
        name="Note Update Agent",
        system_prompt=(
            SystemPromptSegment(template_id="General-purpose self-learning agent"),
            SystemPromptSegment(template_id="MCP服务使用"),
            SystemPromptSegment(template_id="Custom-task-instructions"),
        ),
    )

    rendered_prompt = engine._render_system_prompt(
        definition=second_definition,
        agent_instance_id="inst-note",
        current_working_directory="/test/dir",
        render_options={},
        llm_tools=(),
        conversation=conv,
    )

    # 快照对象完全不可变，引用与内容均未被修改
    assert conv.instructions_snapshot is initial_snapshot
    assert conv.instructions_snapshot.content == "## Stable Prefix Instructions Content"

    # 新定义中的命中基座模板仍被过滤
    assert "## General Base Prompt" not in rendered_prompt
    assert "## MCP Usage Prompt" not in rendered_prompt
    assert "## Custom Task Instructions" in rendered_prompt


def test_resolve_openai_request_configuration_propagates_snapshot():
    conv = Conversation(id="conv-openai")
    conv.instructions_snapshot = InstructionsSnapshot(
        template_names=("General-purpose self-learning agent",),
        content="稳定前缀指令集",
    )

    params, options, effective_system, effective_tools, config_view = (
        _resolve_openai_request_configuration(
            conversation_id="conv-openai",
            system_prompt="差异开发者指令",
            tools=(),
            request_params={},
            render_options={},
            conversation=conv,
        )
    )

    # params["instructions"] 自动注入了 instructions_snapshot.content
    assert params["instructions"] == "稳定前缀指令集"
    # effective_system 保留开发者消息 system_prompt
    assert effective_system == "差异开发者指令"


def test_resolve_openai_request_configuration_respects_explicit_override():
    conv = Conversation(id="conv-override")
    conv.instructions_snapshot = InstructionsSnapshot(
        template_names=("General-purpose self-learning agent",),
        content="稳定前缀指令集",
    )

    params, options, effective_system, effective_tools, config_view = (
        _resolve_openai_request_configuration(
            conversation_id="conv-override",
            system_prompt="差异开发者指令",
            tools=(),
            request_params={"instructions": "显式覆盖指令"},
            render_options={},
            conversation=conv,
        )
    )

    assert params["instructions"] == "显式覆盖指令"
    assert effective_system == "差异开发者指令"


def test_llm_request_service_end_to_end_openai_responses_snapshot_routing(monkeypatch):
    captured = {}

    async def fake_post(url, headers, body, **kwargs):
        captured.update(copy.deepcopy(body))
        return {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello"}],
                }
            ]
        }

    monkeypatch.setattr("agent_runtime.llm_client.llm_protocol_clients._post_json", fake_post)

    conv = Conversation(
        id="conv-e2e",
        messages=[UniMessage("user", [TextPart("ping")], message_id="m1")],
        instructions_snapshot=InstructionsSnapshot(
            template_names=("base1",),
            content="全局稳定 instructions 快照",
        ),
    )

    service = LlmRequestService((OpenAIResponsesProtocolClient(),), _DummyResolver())

    async def run():
        await service.send_prompt(
            LlmPromptRequest(
                conversation=conv,
                resolved_model=_mock_model(),
                system_prompt="局部差异 developer prompt",
            )
        )

    asyncio.run(run())

    # 1. 顶层 instructions 来自 instructions_snapshot.content
    assert captured["instructions"] == "全局稳定 instructions 快照"

    # 2. 差异 developer prompt 落在 input 里的 developer 消息
    developer_inputs = [
        item for item in captured["input"]
        if item.get("role") == "developer"
    ]
    assert len(developer_inputs) == 1
    assert developer_inputs[0]["content"][0]["text"] == "局部差异 developer prompt"


def test_subagent_inherits_instructions_snapshot():
    parent_conv = Conversation(
        id="parent-conv",
        instructions_snapshot=InstructionsSnapshot(
            template_names=("General-purpose self-learning agent", "MCP服务使用", "生命周期工具调用规范"),
            content="## Parent Instructions",
        ),
    )
    # 模拟 subagent_runtime 中的继承逻辑
    from agent_runtime.conversation_message.message.operations import create_conversation
    child_conv = create_conversation()
    child_conv.instructions_snapshot = getattr(parent_conv, "instructions_snapshot", None)

    assert child_conv.instructions_snapshot is not None
    assert child_conv.instructions_snapshot == parent_conv.instructions_snapshot
    assert child_conv.instructions_snapshot.content == "## Parent Instructions"


def test_sqlite_conversation_repository_instructions_snapshot_roundtrip(tmp_path):
    repo = SqliteConversationRepository(tmp_path)

    conv = Conversation(
        id="conv-repo",
        instructions_snapshot=InstructionsSnapshot(
            template_names=("General-purpose self-learning agent", "MCP服务使用"),
            content="持久化指令快照内容",
        ),
    )

    async def run():
        await repo.create(
            conv,
            task_id="task-1",
            thread_id="thread-1",
            agent_id="agent-1",
            agent_instance_id="inst-1",
        )

        # 重新创建 repo 实例从 SQLite 读取
        repo2 = SqliteConversationRepository(tmp_path)
        loaded = await repo2.get("conv-repo")

        assert loaded is not None
        assert loaded.instructions_snapshot is not None
        assert loaded.instructions_snapshot.template_names == (
            "General-purpose self-learning agent",
            "MCP服务使用",
        )
        assert loaded.instructions_snapshot.content == "持久化指令快照内容"

        # 更新为 None 并保存
        loaded.instructions_snapshot = None
        await repo2.save(loaded)

        repo3 = SqliteConversationRepository(tmp_path)
        loaded2 = await repo3.get("conv-repo")
        assert loaded2 is not None
        assert loaded2.instructions_snapshot is None

    asyncio.run(run())
