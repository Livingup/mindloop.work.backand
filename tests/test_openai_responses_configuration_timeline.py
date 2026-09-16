import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_runtime.conversation_message.infrastructure.sqlite_conversation_repository import (
    SqliteConversationRepository,
    _json_line_to_message,
    _message_to_json_line,
)
from agent_runtime.conversation_message.message.models import (
    ConfigurationComponentEvent,
    CurrentPromptConfiguration,
    CurrentToolsConfiguration,
    Conversation,
    RequestConfigurationView,
    TextPart,
    UniMessage,
)
from agent_runtime.conversation_message.message.openai_protocol import OpenAIAdapter
from agent_runtime.conversation_message.message import codex_protocol
from agent_runtime.llm_client.llm_protocol_clients import OpenAIResponsesProtocolClient
from agent_runtime.llm_model_registry.llm_model_registry_service import ResolvedLlmProviderModel, LlmRetryPolicy
from agent_runtime.conversation_message.message.models import build_current_request_configuration_view
from agent_runtime.conversation_message.message.openai_protocol import OpenAIRenderResult
from agent_runtime.conversation_message.message.operations import render_messages
from agent_runtime.llm_client.llm_request_models import LlmPromptRequest, LlmToolDefinition
from agent_runtime.llm_client.llm_request_service import LlmRequestService


def _model(api_format="openai_responses"):
    return ResolvedLlmProviderModel(
        provider_id="provider", binding_id="binding", model_id="model",
        model_alias="model", api_format=api_format, sdk_platform="openai",
        api_base_url="https://example.test", custom_headers={}, api_keys=(),
        retry=LlmRetryPolicy(max_retries_per_key=0),
        tool_choice=None, thinking=None, custom_request_params=None,
        max_output_tokens=100, context_window=1000, compression_threshold=None,
        supports_tool_calling=True, supports_structured_output=True,
        supported_input_modalities=("text",),
    )



class _Resolver:
    async def resolve_resource(self, resource_id: str):
        raise AssertionError(f"unexpected resource resolution: {resource_id}")



def _view(conv: Conversation, target_message_id: str) -> RequestConfigurationView:
    prompt_events = (
        ConfigurationComponentEvent(
            event_id="prompt-old",
            definition_id="prompt-v1",
            anchor="start",
            sequence=1,
            status="historical",
            snapshot="old prompt",
        ),
        ConfigurationComponentEvent(
            event_id="prompt-current",
            definition_id="prompt-v2",
            anchor="after_message_id",
            anchor_message_id=target_message_id,
            sequence=3,
            status="active",
        ),
    )
    tool_events = (
        ConfigurationComponentEvent(
            event_id="tools-old",
            definition_id="tools-v1",
            anchor="start",
            sequence=0,
            status="historical",
            snapshot=({"type": "function", "name": "old_tool"},),
        ),
        ConfigurationComponentEvent(
            event_id="tools-current",
            definition_id="tools-v2",
            anchor="after_message_id",
            anchor_message_id=target_message_id,
            sequence=2,
            status="active",
        ),
    )
    return RequestConfigurationView(
        conversation_id=conv.id,
        view_revision="view-2",
        prompt_events=prompt_events,
        tool_events=tool_events,
        current_prompt=CurrentPromptConfiguration(
            event_id="prompt-current",
            definition_id="prompt-v2",
            resolved_content="new prompt",
        ),
        current_tools=CurrentToolsConfiguration(
            event_id="tools-current",
            definition_id="tools-v2",
            ordered_definitions=({"type": "function", "name": "new_tool"},),
        ),
    )



def test_jsonl_message_id_round_trip_and_legacy_compatibility(tmp_path: Path):
    msg = UniMessage(role="user", content=[TextPart("hello")], message_id="msg-fixed")
    encoded = _message_to_json_line(msg)
    assert json.loads(encoded)["message_id"] == "msg-fixed"
    assert _json_line_to_message(encoded).message_id == "msg-fixed"
    legacy = _json_line_to_message(
        json.dumps({"role": "user", "content": [{"kind": "text", "text": "old"}]}),
        legacy_message_id="msg-legacy",
    )
    assert legacy.message_id == "msg-legacy"



def test_responses_timeline_uses_snapshot_then_active_current_and_anchor_order():
    async def run():
        conv = Conversation(
            id="conv-1",
            messages=[
                UniMessage(role="user", content=[TextPart("one")], message_id="m1"),
                UniMessage(role="user", content=[TextPart("two")], message_id="m2"),
            ],
        )
        rendered = await OpenAIAdapter().render(
            conv,
            _Resolver(),
            {
                "openai_responses_configuration_view": _view(conv, "m1"),
                "prompt_cache_breakpoint": {"mode": "explicit", "label": "B"},
                "prompt_cache_options": {"mode": "implicit"},
            },
        )
        assert [item.get("type") for item in rendered.input] == [
            "additional_tools", None, "message", "additional_tools", None, "message",
        ]
        assert rendered.input[0]["tools"][0]["name"] == "old_tool"
        assert rendered.input[1]["content"][0]["text"] == "old prompt"
        assert rendered.input[2]["content"][0]["text"] == "one"
        assert rendered.input[3]["tools"][0]["name"] == "new_tool"
        assert rendered.input[4]["content"][0]["text"] == "new prompt"
        assert rendered.input[5]["content"][0]["text"] == "two"
        assert rendered.input[1]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit", "label": "B"}
        assert rendered.prompt_cache_options == {"mode": "implicit"}
        assert rendered.uses_input_configuration is True
    asyncio.run(run())



def test_responses_client_sends_default_or_defined_instructions_and_no_tools(monkeypatch):
    async def run():
        captured = {}
        async def fake_post(url, headers, body, **kwargs):
            captured.update(body)
            return {"output": []}
        monkeypatch.setattr(
            "agent_runtime.llm_client.llm_protocol_clients._post_json",
            fake_post,
        )
        from agent_runtime.llm_model_registry.llm_model_registry_service import LlmRetryPolicy
        model = ResolvedLlmProviderModel(
            provider_id="provider", binding_id="binding", model_id="model",
            model_alias="model", api_format="openai_responses", sdk_platform="openai",
            api_base_url="https://example.test", custom_headers={}, api_keys=(),
            retry=LlmRetryPolicy(), tool_choice=None, thinking=None, custom_request_params=None,
            max_output_tokens=100, context_window=1000, compression_threshold=None,
            supports_tool_calling=True, supports_structured_output=True,
            supported_input_modalities=("text",),
        )
        from agent_runtime.conversation_message.message.openai_protocol import OpenAIRenderResult
        result = OpenAIRenderResult(
            input=[{"type": "additional_tools", "role": "developer", "tools": []}],
            uses_input_configuration=True,
            prompt_cache_options={"mode": "implicit"},
        )
        await OpenAIResponsesProtocolClient().request(
            resolved_model=model, rendered_messages=result, system_prompt="ignored",
            tools=({"name": "ignored"},), request_params={}, api_key=None,
        )
        assert captured["instructions"] == "你是一个善于学习的AI。"
        assert "tools" not in captured
        assert captured["prompt_cache_options"] == {"mode": "implicit"}

        captured.clear()
        await OpenAIResponsesProtocolClient().request(
            resolved_model=model, rendered_messages=result, system_prompt="ignored",
            tools=({"name": "ignored"},), request_params={"instructions": "自定义特定指令"}, api_key=None,
        )
        assert captured["instructions"] == "自定义特定指令"
        assert "tools" not in captured
    asyncio.run(run())



def test_codex_adapter_removes_responses_timeline_option():
    async def run():
        conv = Conversation(id="codex", messages=[])
        rendered = await codex_protocol.CodexAdapter().render(
            conv, _Resolver(),
            {
                "openai_responses_configuration_view": object(),
            },
        )
        assert rendered.input == []
    asyncio.run(run())



def test_sqlite_repository_keeps_message_id_in_jsonl(tmp_path: Path):
    async def run():
        repo = SqliteConversationRepository(tmp_path)
        conv = Conversation(id="conv", messages=[UniMessage(role="user", content=[TextPart("x")], message_id="fixed")])
        await repo.create(conv, task_id="task", thread_id="thread", agent_id="agent", agent_instance_id="instance")
        loaded = await repo.get("conv")
        assert loaded.messages[0].message_id == "fixed"
        repo.close()
    asyncio.run(run())


def test_legacy_jsonl_migration_is_persisted_and_repeatable(tmp_path):
    async def run():
        repo = SqliteConversationRepository(tmp_path)
        await repo.create(Conversation(id="legacy"), task_id="task", thread_id="t", agent_id="a", agent_instance_id="i")
        path = tmp_path / "messages/task/legacy.jsonl"
        raw = {"role": "assistant", "content": [{"kind": "text", "text": "old"}],
               "source_provider": "openai_responses",
               "provider_extras": {"openai_responses": {"raw": [{"type": "message", "role": "assistant"}]}}}
        path.write_text(json.dumps(raw) + "\n\n" + json.dumps(raw) + "\n", encoding="utf-8")
        loaded = await repo.get("legacy")
        ids = [m.message_id for m in loaded.messages]
        assert len(set(ids)) == 2
        assert [json.loads(line)["message_id"] for line in path.read_text(encoding="utf-8").splitlines()] == ids
        assert loaded.messages[0].provider_extras == raw["provider_extras"]
        repo.close()
        repo = SqliteConversationRepository(tmp_path)
        try:
            assert [m.message_id for m in (await repo.get("legacy")).messages] == ids
            tables = {r[0] for r in repo._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert tables == {
                "conversations",
                "conversation_state",
                "provider_state",
                "pending_tool_results",
                "conversation_configuration",
            }
        finally:
            repo.close()
    asyncio.run(run())


def test_existing_positional_models_and_duplicate_id_guard():
    msg = UniMessage("assistant", [TextPart("text")], "openai_responses", "vendor", {"x": {}})
    assert msg.source_provider == "openai_responses"
    conv = Conversation(id="c")
    conv.append_message(msg)
    with pytest.raises(ValueError, match="message_id"):
        conv.append_message(msg)
    with pytest.raises(ValueError, match="message_id"):
        UniMessage("user", [], message_id=" ")
    request = LlmPromptRequest(conv, _model(), "p", (), {"x": 1}, {"y": 2})
    assert request.render_options == {"x": 1}
    assert request.request_overrides == {"y": 2}
    result = OpenAIRenderResult([], None, "key", ["reasoning.encrypted_content"])
    assert result.include == ["reasoning.encrypted_content"]


def test_configuration_contract_rejects_missing_snapshot_and_mismatched_current():
    with pytest.raises(ValueError, match="snapshot"):
        ConfigurationComponentEvent("p", "d")
    conv = Conversation(id="c")
    view = _view(conv, "m")
    with pytest.raises(ValueError, match="匹配"):
        replace(view, current_prompt=replace(view.current_prompt, definition_id="wrong"))
    with pytest.raises(ValueError, match="current"):
        replace(view, current_tools=None)
    with pytest.raises(ValueError, match="active"):
        replace(view, prompt_events=(view.prompt_events[1], replace(view.prompt_events[1], event_id="second")))


def test_unknown_and_duplicate_message_anchors_are_rejected():
    async def run():
        conv = Conversation(id="c", messages=[UniMessage("user", [], message_id="m")])
        with pytest.raises(ValueError, match="不存在"):
            await OpenAIAdapter().render(conv, _Resolver(), {"openai_responses_configuration_view": _view(conv, "unknown")})
        conv.messages.append(UniMessage("user", [], message_id="m"))
        with pytest.raises(ValueError, match="唯一"):
            await OpenAIAdapter().render(conv, _Resolver(), {"openai_responses_configuration_view": _view(conv, "m")})
    asyncio.run(run())


def test_independent_prompt_switch_before_expanded_assistant_and_hot_update():
    async def run():
        raw = [{"type": "reasoning", "encrypted_content": "enc"},
               {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}]
        conv = Conversation(id="c", messages=[UniMessage(
            "assistant", [TextPart("answer")], source_provider="openai_responses",
            provider_extras={"openai_responses": {"raw": raw}}, message_id="a")])
        view = _view(conv, "a")
        view = replace(view,
            prompt_events=(view.prompt_events[0], replace(view.prompt_events[1], anchor="before_message_id")),
            tool_events=(ConfigurationComponentEvent("t", "td", status="active"),),
            current_tools=CurrentToolsConfiguration("t", "td", ({"name": "same_tool"},)))
        first = await OpenAIAdapter().render(conv, _Resolver(), {"openai_responses_configuration_view": view})
        assert [i.get("type") for i in first.input] == ["additional_tools", None, None, "reasoning", "message"]
        assert first.input[2]["content"][0]["text"] == "new prompt"
        hot = replace(view, current_prompt=replace(view.current_prompt, resolved_content="hot prompt"))
        second = await OpenAIAdapter().render(conv, _Resolver(), {"openai_responses_configuration_view": hot})
        assert len(second.input) == len(first.input)
        assert second.input[0] == first.input[0]
        assert second.input[1] == first.input[1]
        assert second.input[2]["content"][0]["text"] == "hot prompt"
        assert raw == conv.messages[0].provider_extras["openai_responses"]["raw"]
    asyncio.run(run())


@pytest.mark.parametrize("marker", [{"mode": "explicit", "extra": {"x": 1}}, None])
def test_marker_is_rebuilt_on_last_initial_text_without_mutation(marker):
    async def run():
        conv = Conversation(id="c")
        content = [{"type": "input_text", "text": "first", "prompt_cache_breakpoint": {"old": 1}},
                   {"type": "input_text", "text": "last", "prompt_cache_breakpoint": {"old": 2}}]
        view = RequestConfigurationView("c", "1", prompt_events=(
            ConfigurationComponentEvent("p", "d", snapshot=content),))
        original = copy.deepcopy(content)
        cache_options = {"mode": "implicit", "extra": {"x": 1}}
        options = {"openai_responses_configuration_view": view,
                   "prompt_cache_breakpoint": marker, "prompt_cache_options": cache_options}
        result = await OpenAIAdapter().render(conv, _Resolver(), options)
        blocks = result.input[0]["content"]
        assert "prompt_cache_breakpoint" not in blocks[0]
        if marker is None:
            assert "prompt_cache_breakpoint" not in blocks[1]
        else:
            assert blocks[1]["prompt_cache_breakpoint"] == marker
            blocks[1]["prompt_cache_breakpoint"]["extra"]["x"] = 2
            assert marker["extra"]["x"] == 1
        result.prompt_cache_options["extra"]["x"] = 2
        assert cache_options["extra"]["x"] == 1
        assert content == original
        again = await OpenAIAdapter().render(conv, _Resolver(), options)
        assert again.input[0]["content"][-1].get("prompt_cache_breakpoint") == marker
    asyncio.run(run())


@pytest.mark.parametrize("prompt", [None, [], [{"type": "input_image", "image_url": "x"}]])
def test_tools_only_or_empty_prompt_never_fabricates_marker(prompt):
    async def run():
        conv = Conversation(id="c")
        view = build_current_request_configuration_view(conversation_id="c", system_prompt=None, tools=())
        if prompt is not None:
            view = replace(view, prompt_events=(ConfigurationComponentEvent("p", "d", snapshot=prompt),))
        result = await OpenAIAdapter().render(conv, _Resolver(), {"openai_responses_configuration_view": view})
        assert "prompt_cache_breakpoint" not in json.dumps(result.input)
        assert result.prompt_cache_options is None
    asyncio.run(run())


@pytest.mark.parametrize("field", ["tools", "input", "prompt_cache_breakpoint"])
def test_responses_client_rejects_reserved_configuration_params(field):
    async def run():
        with pytest.raises(ValueError):
            await OpenAIResponsesProtocolClient().request(
                resolved_model=_model(), rendered_messages=OpenAIRenderResult([]),
                system_prompt=None, tools=(), request_params={field: None}, api_key=None)
    asyncio.run(run())


def test_responses_client_rejects_non_string_instructions():
    async def run():
        with pytest.raises(TypeError, match="instructions 必须是字符串或 None"):
            await OpenAIResponsesProtocolClient().request(
                resolved_model=_model(), rendered_messages=OpenAIRenderResult([]),
                system_prompt=None, tools=(), request_params={"instructions": 123}, api_key=None)
    asyncio.run(run())


def test_responses_client_requires_projected_configuration():
    async def run():
        with pytest.raises(ValueError, match="developer"):
            await OpenAIResponsesProtocolClient().request(
                resolved_model=_model(), rendered_messages=OpenAIRenderResult([]),
                system_prompt="missing", tools=(), request_params={}, api_key=None)
    asyncio.run(run())


@pytest.mark.parametrize("cache_options", [{"mode": "explicit", "extra": 1}, None])
def test_service_standard_responses_cache_overrides_and_input_only(monkeypatch, cache_options):
    async def run():
        captured = {}
        async def fake_post(url, headers, body, **kwargs):
            captured.update(copy.deepcopy(body))
            return {"output": []}
        monkeypatch.setattr("agent_runtime.llm_client.llm_protocol_clients._post_json", fake_post)
        service = LlmRequestService((OpenAIResponsesProtocolClient(),), _Resolver())
        params = {"prompt_cache_breakpoint": None, "prompt_cache_options": cache_options,
                  "parallel_tool_calls": False}
        input_items = [{"role": "user", "content": "hello"}]
        await service.request_standard_responses(
            resolved_model=_model(), conversation_id="c", input_items=input_items,
            system_prompt="system", tools=({"type": "function", "name": "tool"},),
            request_params=params, api_key=None, input_configuration_capability=True)
        assert captured["instructions"] == "你是一个善于学习的AI。"
        assert "tools" not in captured
        assert captured["input"][0]["type"] == "additional_tools"
        assert captured["input"][1] == {"role": "developer", "content": [{"type": "input_text", "text": "system"}]}
        assert captured["input"][2:] == input_items
        assert "prompt_cache_breakpoint" not in captured
        assert "prompt_cache_breakpoint" in params
        assert captured["parallel_tool_calls"] is False
        if cache_options is None:
            assert "prompt_cache_options" not in captured
        else:
            assert captured["prompt_cache_options"] == cache_options
        assert captured["store"] is False
        assert "reasoning.encrypted_content" in captured["include"]
    asyncio.run(run())


def test_send_prompt_uses_explicit_view_and_preserves_callers(monkeypatch):
    async def run():
        captured = {}
        async def fake_post(url, headers, body, **kwargs):
            captured.update(copy.deepcopy(body))
            return {"output": [{"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "done"}]}]}
        monkeypatch.setattr("agent_runtime.llm_client.llm_protocol_clients._post_json", fake_post)
        conv = Conversation(id="c", messages=[UniMessage("user", [TextPart("hello")], message_id="m")])
        view = _view(conv, "m")
        service = LlmRequestService((OpenAIResponsesProtocolClient(),), _Resolver())
        overrides = {"prompt_cache_breakpoint": {"mode": "explicit", "tag": "custom"}}
        result = await service.send_prompt(LlmPromptRequest(
            conversation=conv, resolved_model=_model(), system_prompt="new prompt",
            tools=(LlmToolDefinition("new_tool", {"type": "function", "name": "new_tool"}),),
            configuration_view=view, request_overrides=overrides))
        assert "not-used" not in json.dumps(captured["input"])
        assert captured["input"][1]["content"][0]["prompt_cache_breakpoint"] == overrides["prompt_cache_breakpoint"]
        assert "prompt_cache_breakpoint" not in captured
        assert "prompt_cache_breakpoint" in overrides
        assert conv.messages[-1] is result.assistant_message
        assert not hasattr(conv, "configuration_view")
    asyncio.run(run())



def test_explicit_view_rejects_legacy_configuration_overrides():
    async def run():
        conv = Conversation(id="c")
        view = _view(conv, "m")
        service = LlmRequestService((OpenAIResponsesProtocolClient(),), _Resolver())
        with pytest.raises(ValueError, match="不能与 request_overrides"):
            await service.send_prompt(LlmPromptRequest(
                conversation=conv, resolved_model=_model(), system_prompt="new prompt",
                configuration_view=view, request_overrides={"instructions": "override"}))
        with pytest.raises(ValueError, match="system_prompt 内容冲突"):
            await service.send_prompt(LlmPromptRequest(
                conversation=conv, resolved_model=_model(), system_prompt="wrong",
                configuration_view=view))
    asyncio.run(run())



def test_web_native_openai_override_is_projected_into_input(monkeypatch):
    async def run():
        captured = {}
        async def fake_post(url, headers, body, **kwargs):
            captured.update(copy.deepcopy(body))
            return {"output": [{"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "{}"}]}]}
        monkeypatch.setattr("agent_runtime.llm_client.llm_protocol_clients._post_json", fake_post)
        from agent_runtime.web_tools.adapters import OpenAIWebNativeToolAdapter
        conv = Conversation(id="web", messages=[UniMessage("user", [TextPart("search")], message_id="m")])
        service = LlmRequestService((OpenAIResponsesProtocolClient(),), _Resolver())
        result = await service.send_prompt(LlmPromptRequest(
            conversation=conv, resolved_model=_model(), system_prompt="web system",
            request_overrides=OpenAIWebNativeToolAdapter().build_request_overrides(
                operation="web_search",
                request=type("Request", (), {"allowed_domains": (), "blocked_domains": (), "max_results": 3})(),
            )))
        tools = [item for item in captured["input"] if item.get("type") == "additional_tools"]
        assert tools and tools[0]["tools"][0]["type"] == "web_search_preview"
        assert captured["instructions"] == "你是一个善于学习的AI。"
        assert "tools" not in captured
        assert captured["tool_choice"] == {"type": "web_search_preview"}
        assert "web_search_call.action.sources" in captured["include"]
        assert result.assistant_message.role == "assistant"
    asyncio.run(run())



def test_responses_protocol_automatically_projects_synthetic_configuration(monkeypatch):
    async def run():
        captured = {}
        async def fake_post(url, headers, body, **kwargs):
            captured.update(copy.deepcopy(body))
            return {"output": [{"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "ok"}]}]}
        monkeypatch.setattr("agent_runtime.llm_client.llm_protocol_clients._post_json", fake_post)
        conv = Conversation(id="c", messages=[])
        service = LlmRequestService((OpenAIResponsesProtocolClient(),), _Resolver())
        await service.send_prompt(LlmPromptRequest(
            conversation=conv, resolved_model=_model(), system_prompt="system"))
        assert captured["input"][0]["role"] == "developer"
        assert "prompt_cache_breakpoint" not in captured["input"][0]["content"][0]
        assert captured["input"][0]["content"][0]["text"] == "system"
        assert captured["instructions"] == "你是一个善于学习的AI。"
    asyncio.run(run())



def test_initial_joint_cache_breakpoint_is_omitted_when_tools_follow_prompt():
    async def run():
        conv = Conversation(id="c")
        view = RequestConfigurationView(
            conversation_id="c", view_revision="1",
            prompt_events=(ConfigurationComponentEvent("p", "pd", sequence=1, snapshot="prompt"),),
            tool_events=(ConfigurationComponentEvent("t", "td", sequence=2, snapshot=({"name": "tool"},)),),
        )
        result = await OpenAIAdapter().render(conv, _Resolver(), {
            "openai_responses_configuration_view": view,
            "prompt_cache_breakpoint": {"mode": "explicit"},
        })
        assert result.input[0]["role"] == "developer"
        assert result.input[1]["type"] == "additional_tools"
        assert "prompt_cache_breakpoint" not in json.dumps(result.input)
    asyncio.run(run())


@pytest.mark.parametrize("api_format", ["anthropic_messages", "gemini_generate_content",
    "openai_chat_completions", "zhipu_chat_completions", "codex_responses"])
def test_non_responses_adapters_ignore_configuration_view(api_format):
    async def run():
        conv = Conversation(id="c", messages=[UniMessage("user", [TextPart("hello")], message_id="m")])
        original = copy.deepcopy(conv)
        clean = await render_messages(conv, api_format, _Resolver(), {})
        with_view = await render_messages(conv, api_format, _Resolver(), {
            "openai_responses_configuration_view": _view(conv, "m"),
            "prompt_cache_breakpoint": {"mode": "explicit"},
            "prompt_cache_options": {"mode": "implicit"},
        })
        assert with_view == clean
        assert conv == original
    asyncio.run(run())


def test_codex_projection_preserves_message_identity_and_ignores_invalid_options():
    async def run():
        message = UniMessage("assistant", [TextPart("answer")], source_provider="codex_responses",
            provider_extras={"codex_responses": {"raw": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "answer"}]}]}}, message_id="stable")
        conv = Conversation(id="c", messages=[message])
        mapped = codex_protocol._codex_as_openai_conversation(conv)
        assert mapped.messages[0].message_id == "stable"
        result = await codex_protocol.CodexAdapter().render(conv, _Resolver(), {
            "openai_responses_configuration_view": object(),
            "prompt_cache_breakpoint": object(), "prompt_cache_options": object()})
        assert result.uses_input_configuration is False
        assert result.prompt_cache_options is None
        assert "developer" not in json.dumps(result.input)
    asyncio.run(run())


def test_conversation_timeline_direct_rendering_and_cache_prefix_continuity():
    async def run():
        conv = Conversation(
            id="c_timeline",
            messages=[
                UniMessage("user", [TextPart("hello 1")], message_id="m1"),
                UniMessage("assistant", [TextPart("reply 1")], message_id="m2"),
            ],
            system_prompt="stage2 prompt",
            tools=({"type": "function", "name": "tool_v2"},),
        )
        # 记录初始配置（Stage 1）为历史快照
        conv.record_configuration_event(
            ConfigurationComponentEvent(
                event_id="tool-stage1",
                definition_id="def-tool-v1",
                anchor="start",
                sequence=0,
                status="historical",
                snapshot=({"type": "function", "name": "tool_v1"},),
                component="tool",
            )
        )
        conv.record_configuration_event(
            ConfigurationComponentEvent(
                event_id="prompt-stage1",
                definition_id="def-prompt-v1",
                anchor="start",
                sequence=1,
                status="historical",
                snapshot="stage1 prompt",
                component="prompt",
            )
        )
        # 记录切换点（Stage 2）为活跃配置，锚定在 m2 之后
        conv.record_configuration_event(
            ConfigurationComponentEvent(
                event_id="tool-stage2",
                definition_id="def-tool-v2",
                anchor="after_message_id",
                anchor_message_id="m2",
                sequence=2,
                status="active",
                component="tool",
            )
        )
        conv.record_configuration_event(
            ConfigurationComponentEvent(
                event_id="prompt-stage2",
                definition_id="def-prompt-v2",
                anchor="after_message_id",
                anchor_message_id="m2",
                sequence=3,
                status="active",
                component="prompt",
            )
        )

        rendered = await OpenAIAdapter().render(conv, _Resolver())
        assert rendered.uses_input_configuration is True
        assert rendered.prompt_cache_options is None

        # 检查初始阶段（Stage 1）内容与断点
        assert rendered.input[0] == {
            "type": "additional_tools",
            "role": "developer",
            "tools": [{"type": "function", "name": "tool_v1"}],
        }
        assert rendered.input[1] == {
            "role": "developer",
            "content": [
                {
                    "type": "input_text",
                    "text": "stage1 prompt",
                }
            ],
        }
        # 检查通用对话消息
        assert rendered.input[2]["role"] == "user"
        assert rendered.input[3]["role"] == "assistant"

        # 检查切换阶段（Stage 2）追加的配置
        assert rendered.input[4] == {
            "type": "additional_tools",
            "role": "developer",
            "tools": [{"type": "function", "name": "tool_v2"}],
        }
        assert rendered.input[5] == {
            "role": "developer",
            "content": [{"type": "input_text", "text": "stage2 prompt"}],
        }

        # 验证会话通用消息列表未被任何 developer 消息污染
        assert len(conv.messages) == 2
        assert all(m.role in {"user", "assistant"} for m in conv.messages)
        assert not hasattr(conv, "configuration_view")

    asyncio.run(run())


def test_send_prompt_automatically_uses_conversation_timeline_without_explicit_view(monkeypatch):
    async def run():
        captured = {}
        async def fake_post(url, headers, body, **kwargs):
            captured.update(copy.deepcopy(body))
            return {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "stage2 completed"}],
                    }
                ]
            }

        monkeypatch.setattr("agent_runtime.llm_client.llm_protocol_clients._post_json", fake_post)

        conv = Conversation(
            id="conv-auto-timeline",
            messages=[
                UniMessage("user", [TextPart("task step 1")], message_id="step1"),
                UniMessage("assistant", [TextPart("done step 1")], message_id="step1_done"),
            ],
        )
        conv.record_configuration_event(
            ConfigurationComponentEvent(
                event_id="t-initial",
                definition_id="t-def-1",
                anchor="start",
                sequence=0,
                status="historical",
                snapshot=({"type": "function", "name": "initial_tool"},),
            )
        )
        conv.record_configuration_event(
            ConfigurationComponentEvent(
                event_id="p-initial",
                definition_id="p-def-1",
                anchor="start",
                sequence=1,
                status="historical",
                snapshot="initial prompt",
            )
        )
        conv.record_configuration_event(
            ConfigurationComponentEvent(
                event_id="t-active",
                definition_id="t-def-2",
                anchor="after_message_id",
                anchor_message_id="step1_done",
                sequence=2,
                status="active",
            )
        )
        conv.record_configuration_event(
            ConfigurationComponentEvent(
                event_id="p-active",
                definition_id="p-def-2",
                anchor="after_message_id",
                anchor_message_id="step1_done",
                sequence=3,
                status="active",
            )
        )

        service = LlmRequestService((OpenAIResponsesProtocolClient(),), _Resolver())

        # 调用 send_prompt 时不传 configuration_view，仅传实时的 system_prompt 与 tools
        result = await service.send_prompt(
            LlmPromptRequest(
                conversation=conv,
                resolved_model=_model(),
                system_prompt="dynamic active prompt",
                tools=(LlmToolDefinition("active_tool", {"type": "function", "name": "active_tool"}),),
            )
        )

        # 检查 OpenAI 请求体字段：
        # 1. 顶层 instructions 回退为固定默认值，tools 严禁存在
        assert captured["instructions"] == "你是一个善于学习的AI。"
        assert "tools" not in captured
        # 2. 默认不显式传递 prompt_cache_options，采用服务端默认值
        assert "prompt_cache_options" not in captured
        # 3. input 中首部包含 Stage 1 的 additional_tools 与带显式断点的 developer 提示词
        assert captured["input"][0]["type"] == "additional_tools"
        assert captured["input"][0]["tools"] == [{"type": "function", "name": "initial_tool"}]
        assert captured["input"][1]["role"] == "developer"
        assert "prompt_cache_breakpoint" not in captured["input"][1]["content"][0]
        assert captured["input"][1]["content"][0]["text"] == "initial prompt"
        # 4. 尾部追加 Stage 2 的 additional_tools 与动态 real-time 活跃提示词
        assert captured["input"][4]["type"] == "additional_tools"
        assert captured["input"][4]["tools"] == [{"type": "function", "name": "active_tool"}]
        assert captured["input"][5]["role"] == "developer"
        assert captured["input"][5]["content"][0]["text"] == "dynamic active prompt"
        # 5. 消息被安全追加且不含 developer
        assert conv.messages[-1] is result.assistant_message
        assert len(conv.messages) == 3
        assert [m.role for m in conv.messages] == ["user", "assistant", "assistant"]

    asyncio.run(run())


def test_send_prompt_non_responses_protocols_ignore_conversation_timeline(monkeypatch):
    from agent_runtime.llm_client.llm_protocol_clients import (
        AnthropicMessagesProtocolClient,
        OpenAIChatCompletionsProtocolClient,
        GeminiGenerateContentProtocolClient,
        ZhipuChatCompletionsProtocolClient,
    )

    async def run():
        captured_requests = {}

        async def fake_post(url, headers, body, **kwargs):
            captured_requests[url] = copy.deepcopy(body)
            if "messages" in url or "v1/messages" in url:
                return {
                    "id": "msg_claude",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "claude ok"}],
                }
            if "chat/completions" in url:
                return {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "chat ok"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            if "generateContent" in url or "models" in url:
                return {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [{"text": "gemini ok"}],
                            }
                        }
                    ]
                }
            return {}

        monkeypatch.setattr("agent_runtime.llm_client.llm_protocol_clients._post_json", fake_post)

        clients = (
            AnthropicMessagesProtocolClient(),
            OpenAIChatCompletionsProtocolClient(),
            GeminiGenerateContentProtocolClient(),
            ZhipuChatCompletionsProtocolClient(),
        )
        service = LlmRequestService(clients, _Resolver())

        test_cases = [
            ("anthropic_messages", "https://example.test/v1/messages"),
            ("openai_chat_completions", "https://example.test/chat/completions"),
            ("zhipu_chat_completions", "https://example.test/chat/completions"),
            ("gemini_generate_content", "https://example.test/v1beta/models/model:"),
        ]

        for api_format, expected_url in test_cases:
            captured_requests.clear()
            conv = Conversation(
                id=f"conv-{api_format}",
                messages=[UniMessage("user", [TextPart("question")], message_id="q1")],
            )
            conv.record_configuration_event(
                ConfigurationComponentEvent(
                    event_id="t-old",
                    definition_id="t-old-def",
                    anchor="start",
                    sequence=0,
                    status="historical",
                    snapshot=({"type": "function", "name": "old_tool"},),
                )
            )
            conv.record_configuration_event(
                ConfigurationComponentEvent(
                    event_id="p-old",
                    definition_id="p-old-def",
                    anchor="start",
                    sequence=1,
                    status="historical",
                    snapshot="old prompt",
                )
            )

            current_tool_def = LlmToolDefinition("current_tool", {"type": "function", "name": "current_tool"})
            result = await service.send_prompt(
                LlmPromptRequest(
                    conversation=conv,
                    resolved_model=_model(api_format=api_format),
                    system_prompt="latest real-time prompt",
                    tools=(current_tool_def,),
                )
            )

            matched_urls = [u for u in captured_requests if expected_url in u or u.startswith(expected_url.split("?")[0])]
            assert matched_urls, f"expected {expected_url} in {list(captured_requests.keys())}"
            body = captured_requests[matched_urls[0]]

            # 验证时间线中的旧配置完全被忽略，直接使用当前最新配置
            assert "old prompt" not in json.dumps(body)
            assert "old_tool" not in json.dumps(body)
            assert "latest real-time prompt" in json.dumps(body)
            assert "current_tool" in json.dumps(body)

            # 消息列表中绝对不出现 developer 或 additional_tools
            assert "additional_tools" not in json.dumps(body)
            assert conv.messages[0].role == "user"
            assert conv.messages[1].role == "assistant"
            assert not any(m.role == "developer" for m in conv.messages)

    asyncio.run(run())


def test_send_prompt_hot_reloads_active_configuration_preserving_prefix_cache(monkeypatch):
    async def run():
        captured_history: list[dict[str, Any]] = []

        async def fake_post(url, headers, body, **kwargs):
            captured_history.append(copy.deepcopy(body))
            round_idx = len(captured_history)
            return {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": f"assistant reply {round_idx}"}],
                    }
                ]
            }

        monkeypatch.setattr("agent_runtime.llm_client.llm_protocol_clients._post_json", fake_post)

        conv = Conversation(
            id="conv-hot-reload",
            messages=[
                UniMessage("user", [TextPart("task 1")], message_id="u1"),
                UniMessage("assistant", [TextPart("done 1")], message_id="a1"),
            ],
        )
        # Stage 1 历史事件
        conv.record_configuration_event(
            ConfigurationComponentEvent(
                event_id="t1",
                definition_id="t-def-1",
                anchor="start",
                sequence=0,
                status="historical",
                snapshot=({"type": "function", "name": "tool_v1"},),
            )
        )
        conv.record_configuration_event(
            ConfigurationComponentEvent(
                event_id="p1",
                definition_id="p-def-1",
                anchor="start",
                sequence=1,
                status="historical",
                snapshot="prompt v1",
            )
        )

        # Stage 2 活跃事件（锚定在 a1 之后）
        conv.record_configuration_event(
            ConfigurationComponentEvent(
                event_id="t2",
                definition_id="t-def-2",
                anchor="after_message_id",
                anchor_message_id="a1",
                sequence=2,
                status="active",
            )
        )
        conv.record_configuration_event(
            ConfigurationComponentEvent(
                event_id="p2",
                definition_id="p-def-2",
                anchor="after_message_id",
                anchor_message_id="a1",
                sequence=3,
                status="active",
            )
        )

        service = LlmRequestService((OpenAIResponsesProtocolClient(),), _Resolver())

        # 请求 1：活跃阶段传入版本 A 的提示词与工具
        tool_2_a = LlmToolDefinition("tool_2_a", {"type": "function", "name": "tool_2_a"})
        await service.send_prompt(
            LlmPromptRequest(
                conversation=conv,
                resolved_model=_model(),
                system_prompt="stage 2 prompt ver A",
                tools=(tool_2_a,),
            )
        )

        # 请求 2：热更新当前活跃提示词与工具（不改变时间线 definition_id，仅实时更改内容）
        tool_2_b = LlmToolDefinition("tool_2_b", {"type": "function", "name": "tool_2_b"})
        # 移除请求 1 追加的 assistant 响应以便单纯比对输入结构（或者保留追加新 user 均可）
        body_1 = captured_history[0]
        await service.send_prompt(
            LlmPromptRequest(
                conversation=conv,
                resolved_model=_model(),
                system_prompt="stage 2 prompt ver B (hot reloaded)",
                tools=(tool_2_b,),
            )
        )
        body_2 = captured_history[1]

        # 核心断言：Stage 1 的前缀部分（前 4 项：additional_tools(tool_v1)、developer(prompt v1)、u1、a1）完全一致！
        assert body_1["input"][:4] == body_2["input"][:4]
        # 默认不注入显式断点，由服务端自动隐式缓存
        assert "prompt_cache_breakpoint" not in body_1["input"][1]["content"][0]
        assert "prompt_cache_breakpoint" not in body_2["input"][1]["content"][0]

        # 检查 Stage 2 成功热更新为新内容
        assert body_1["input"][4]["tools"] == [{"type": "function", "name": "tool_2_a"}]
        assert body_1["input"][5]["content"][0]["text"] == "stage 2 prompt ver A"
        assert body_2["input"][4]["tools"] == [{"type": "function", "name": "tool_2_b"}]
        assert body_2["input"][5]["content"][0]["text"] == "stage 2 prompt ver B (hot reloaded)"

    asyncio.run(run())


def test_multi_protocol_relay_and_switch_back_to_openai_responses():
    from agent_runtime.conversation_message.message.models import ToolResultPart
    from agent_runtime.conversation_message.message.operations import ingest_response

    async def run():
        conv = Conversation(id="conv-relay-test")
        resolver = _Resolver()

        # 1. 初始阶段配置时间线（由 Agent 设置）
        conv.record_configuration_event(
            ConfigurationComponentEvent(
                event_id="t-init",
                definition_id="t-def-init",
                anchor="start",
                sequence=0,
                status="active",
                component="tool",
            )
        )
        conv.record_configuration_event(
            ConfigurationComponentEvent(
                event_id="p-init",
                definition_id="p-def-init",
                anchor="start",
                sequence=1,
                status="active",
                component="prompt",
            )
        )
        conv.system_prompt = "initial system prompt"
        conv.tools = ({"type": "function", "name": "common_tool"},)

        # 2. 第 1-10 轮：使用 OpenAI Responses 进行对话与工具调用
        for i in range(1, 11):
            conv.append_message(UniMessage("user", [TextPart(f"User message {i}")], message_id=f"u_{i}"))
            raw_openai = {
                "output": [
                    {"type": "reasoning", "encrypted_content": f"enc_{i}"},
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"OpenAI reply {i}"}]},
                    {"type": "function_call", "call_id": f"call_{i}", "name": "common_tool", "arguments": json.dumps({"arg": i})},
                ]
            }
            ingest_response(conv, "openai_responses", raw_openai)
            conv.append_message(UniMessage("tool", [ToolResultPart(f"call_{i}", False, f"tool result {i}")], message_id=f"t_{i}"))

        # 3. 第 11-15 轮：切换到 Claude 进行 5 轮对话
        for i in range(11, 16):
            claude_render = await render_messages(conv, "anthropic_messages", resolver)
            assert len(claude_render.messages) > 0
            conv.append_message(UniMessage("user", [TextPart(f"User message {i}")], message_id=f"u_{i}"))
            raw_claude = {
                "content": [
                    {"type": "thinking", "thinking": f"claude thought {i}", "signature": f"sig_{i}"},
                    {"type": "text", "text": f"Claude reply {i}"},
                    {"type": "tool_use", "id": f"toolu_{i}", "name": "common_tool", "input": {"x": i}},
                ]
            }
            ingest_response(conv, "anthropic_messages", raw_claude)
            conv.append_message(UniMessage("tool", [ToolResultPart(f"toolu_{i}", False, f"claude tool result {i}")], message_id=f"t_{i}"))

        # 4. 第 16-20 轮：切换到 Gemini 进行 5 轮对话
        for i in range(16, 21):
            gemini_render = await render_messages(conv, "gemini_generate_content", resolver)
            assert len(gemini_render.contents) > 0
            conv.append_message(UniMessage("user", [TextPart(f"User message {i}")], message_id=f"u_{i}"))
            raw_gemini = {
                "candidates": [{
                    "content": {
                        "role": "model",
                        "parts": [
                            {"text": f"Gemini reply {i}"},
                            {"functionCall": {"id": f"gemini_call_{i}", "name": "common_tool", "args": {"y": i}}},
                        ],
                    }
                }]
            }
            ingest_response(conv, "gemini_generate_content", raw_gemini)
            conv.append_message(UniMessage("tool", [ToolResultPart(f"gemini_call_{i}", False, f"gemini tool result {i}")], message_id=f"t_{i}"))

        # 5. 第 21-25 轮：切换到 智谱 GLM 进行 5 轮对话
        for i in range(21, 26):
            zhipu_render = await render_messages(conv, "zhipu_chat_completions", resolver)
            assert len(zhipu_render.messages) > 0
            conv.append_message(UniMessage("user", [TextPart(f"User message {i}")], message_id=f"u_{i}"))
            raw_zhipu = {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": f"Zhipu reply {i}",
                        "reasoning_content": f"zhipu think {i}",
                        "tool_calls": [{
                            "id": f"zhipu_call_{i}",
                            "type": "function",
                            "function": {"name": "common_tool", "arguments": json.dumps({"z": i})},
                        }],
                    }
                }]
            }
            ingest_response(conv, "zhipu_chat_completions", raw_zhipu)
            conv.append_message(UniMessage("tool", [ToolResultPart(f"zhipu_call_{i}", False, f"zhipu tool result {i}")], message_id=f"t_{i}"))

        # 6. 第 26 轮：切回 GPT-5.6 (OpenAI Responses) 继续对话
        conv.append_message(UniMessage("user", [TextPart("Final user request round 26")], message_id="u_26"))
        openai_render = await OpenAIAdapter().render(conv, resolver)

        # 验证 OpenAI 渲染结果完备闭环：
        # - 顶部包含 Stage 1 的 additional_tools 与 developer 提示词
        assert openai_render.input[0]["type"] == "additional_tools"
        assert openai_render.input[1]["role"] == "developer"
        assert "prompt_cache_breakpoint" not in openai_render.input[1]["content"][0]

        # - 第 1-10 轮由 OpenAI 产生的 assistant 消息原样重放 native raw（保留 reasoning 与 encrypted_content）
        openai_reasoning_count = sum(1 for item in openai_render.input if item.get("type") == "reasoning")
        assert openai_reasoning_count == 10

        # - 第 11-25 轮跨协议产生的内容（Claude/Gemini/Zhipu）平滑降级为标准的 message 与 function_call
        assert any(item.get("type") == "message" and any("Claude reply 11" in c.get("text", "") for c in item.get("content", [])) for item in openai_render.input)
        assert any(item.get("type") == "message" and any("Gemini reply 16" in c.get("text", "") for c in item.get("content", [])) for item in openai_render.input)
        assert any(item.get("type") == "message" and any("Zhipu reply 21" in c.get("text", "") for c in item.get("content", [])) for item in openai_render.input)

        # - 跨协议的所有 25 次工具调用结果均展开为 function_call_output
        function_call_outputs = [item for item in openai_render.input if item.get("type") == "function_call_output"]
        assert len(function_call_outputs) == 25

        # - 会话消息列表保持纯净通用（只包含 user / assistant / tool，不含 developer 污染）
        assert not any(m.role == "developer" for m in conv.messages)
        assert not hasattr(conv, "configuration_view")

    asyncio.run(run())



def test_send_prompt_clean_conversation_without_timeline_automatically_projects_input_configuration(monkeypatch):
    """验证未发生切换的纯净会话（无时间线）在请求 OpenAI Responses 时，
    必须无条件将当前 system_prompt 与 tools 转换为 input 内的 developer/additional_tools 事件，
    并在初始 developer 消息末尾打上显式断点，且具备 implicit 自动缓存模式。
    """
    async def run():
        captured = {}
        async def fake_post(url, headers, body, **kwargs):
            captured.update(copy.deepcopy(body))
            return {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "assistant reply"}],
                    }
                ]
            }

        monkeypatch.setattr("agent_runtime.llm_client.llm_protocol_clients._post_json", fake_post)

        # 纯净会话：没有发生过切换，timeline 为空
        conv = Conversation(
            id="clean-conv-001",
            messages=[UniMessage("user", [TextPart("hello, test!")], message_id="m_1")],
            timeline=[],
        )
        assert len(conv.timeline) == 0

        service = LlmRequestService((OpenAIResponsesProtocolClient(),), _Resolver())

        # 调用 send_prompt，未显式传 configuration_view，仅传实时的 system_prompt 与 tools
        result = await service.send_prompt(
            LlmPromptRequest(
                conversation=conv,
                resolved_model=_model(),
                system_prompt="expert agent prompt",
                tools=(LlmToolDefinition("expert_tool", {"type": "function", "name": "expert_tool"}),),
            )
        )

        # 验证请求体：
        # 1. 顶层 instructions 回退为固定默认值，tools 严禁存在
        assert captured["instructions"] == "你是一个善于学习的AI。"
        assert "tools" not in captured
        # 2. 默认不显式传递 prompt_cache_options，采用服务端默认值
        assert "prompt_cache_options" not in captured
        # 3. input 内首部为 additional_tools（序0）与 developer（序1，默认无显式断点）
        assert len(captured["input"]) == 3
        assert captured["input"][0]["type"] == "additional_tools"
        assert captured["input"][0]["tools"] == [{"type": "function", "name": "expert_tool"}]
        assert captured["input"][1]["role"] == "developer"
        assert captured["input"][1]["content"][0]["text"] == "expert agent prompt"
        assert "prompt_cache_breakpoint" not in captured["input"][1]["content"][0]
        # 4. user 消息排在后方（序2）
        assert captured["input"][2]["role"] == "user"
        # 5. parallel_tool_calls 策略正确保留
        assert captured.get("parallel_tool_calls") is True
        # 6. 成功写回 assistant 消息且不污染通用会话流
        assert conv.messages[-1] is result.assistant_message
        assert not any(m.role == "developer" for m in conv.messages)

        # 7. 同时验证直接调用 OpenAIAdapter().render(conv) 当 conv 上挂有 system_prompt/tools 且无 timeline 时的自适配能力
        conv2 = Conversation(
            id="clean-conv-002",
            messages=[UniMessage("user", [TextPart("hello 2")], message_id="m_2")],
            system_prompt="direct conv prompt",
            tools=({"type": "function", "name": "direct_tool"},),
            timeline=[],
        )
        rendered = await OpenAIAdapter().render(conv2, _Resolver())
        assert rendered.uses_input_configuration is True
        assert rendered.input[0]["type"] == "additional_tools"
        assert rendered.input[0]["tools"] == [{"type": "function", "name": "direct_tool"}]
        assert rendered.input[1]["role"] == "developer"
        assert rendered.input[1]["content"][0]["text"] == "direct conv prompt"
        assert "prompt_cache_breakpoint" not in rendered.input[1]["content"][0]

    asyncio.run(run())


def test_explicit_prompt_cache_breakpoint_opt_in(monkeypatch):
    """验证当通过 options 或 request_overrides 显式声明 prompt_cache_breakpoint 时，
    系统依然支持在 developer 提示词内容块上正确挂载显式断点。
    """
    async def run():
        captured = {}
        async def fake_post(url, headers, body, **kwargs):
            captured.update(copy.deepcopy(body))
            return {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "assistant reply"}],
                    }
                ]
            }

        monkeypatch.setattr("agent_runtime.llm_client.llm_protocol_clients._post_json", fake_post)

        conv = Conversation(
            id="optin-conv-001",
            messages=[UniMessage("user", [TextPart("hello optin")], message_id="m_1")],
            timeline=[],
        )
        service = LlmRequestService((OpenAIResponsesProtocolClient(),), _Resolver())

        # 显式传入 prompt_cache_breakpoint
        await service.send_prompt(
            LlmPromptRequest(
                conversation=conv,
                resolved_model=_model(),
                system_prompt="optin prompt",
                tools=(),
                request_overrides={"prompt_cache_breakpoint": {"mode": "explicit"}},
            )
        )

        assert captured["input"][0]["role"] == "developer"
        assert captured["input"][0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert captured["input"][0]["content"][0]["text"] == "optin prompt"

    asyncio.run(run())


def test_subagent_timeline_seeding_and_placeholder_replacement():
    """验证子会话从父会话种子化时时间线的完整构建逻辑：
    1. 父会话的历史配置被冻结为 historical snapshot（start 锚点）；
    2. 子 Agent 活跃提示词事件被锚定在 boundary 消息之后；
    3. 阶段 ① 替换：{{父agent实例id}} 在创建事件时被替换为父实例 ID；
    4. 阶段 ② 替换：{{available_tools}} 在 Responses 适配器渲染时被替换为可用工具名 JSON 数组并置顶；
    5. 当子工具为父工具子集时，不追加任何 additional_tools 事件。
    """
    from types import SimpleNamespace
    from agent_runtime.subagent.subagent_runtime import SubagentRuntime

    async def run():
        parent_conv = Conversation(
            id="conv-parent-001",
            system_prompt="Parent system prompt",
            tools=(
                {"type": "function", "name": "read_file", "description": "read"},
                {"type": "function", "name": "list_dir", "description": "list"},
            ),
        )
        parent_conv.append_message(UniMessage(role="user", content=[TextPart("父第一轮消息")]))
        parent_conv.append_message(UniMessage(role="assistant", content=[TextPart("父回复")]))

        class _Repo:
            def __init__(self):
                self.saved: Conversation | None = None
            async def get(self, cid):
                return parent_conv
            async def create(self, conv, **kwargs):
                self.saved = conv

        repo = _Repo()
        runtime = SubagentRuntime(
            runtime_gateway=object(),
            agent_definition_gateway=object(),
            llm_registry_gateway=object(),
            execution_engine=object(),
            event_publisher=object(),
            conversation_repository=repo,
        )

        parent_def = SimpleNamespace(agent_id="parent_agent", tool_ids=("read_file", "list_dir"), mcp_tools=())
        child_def = SimpleNamespace(agent_id="child_agent", tool_ids=("read_file",), mcp_tools=())

        child_conv_id = await runtime._seed_child_conversation_from_parent_prefix(
            parent=SimpleNamespace(
                instance_id="inst-parent-999",
                task_id="task-001",
                conversation_id=parent_conv.id,
                agent_definition_id="parent_agent",
            ),
            parent_agent_name="ParentAgent",
            child_instance_id="inst-child-111",
            child_thread_id="thread-child",
            child_agent_definition_id="child_agent",
            parent_conversation_id=parent_conv.id,
            conversation_history_copy_message_count=2,
            parent_definition=parent_def,
            child_definition=child_def,
        )

        child_conv = repo.saved
        assert child_conv is not None
        # 验证 timeline 事件
        assert len(child_conv.timeline) == 3
        # 1. 历史 tool snapshot
        assert child_conv.timeline[0].component == "tool"
        assert child_conv.timeline[0].status == "historical"
        assert child_conv.timeline[0].anchor == "start"
        assert child_conv.timeline[0].snapshot == parent_conv.tools
        # 2. 历史 prompt snapshot
        assert child_conv.timeline[1].component == "prompt"
        assert child_conv.timeline[1].status == "historical"
        assert child_conv.timeline[1].anchor == "start"
        assert child_conv.timeline[1].snapshot == "Parent system prompt"
        # 3. 活跃 child prompt
        boundary_id = child_conv.messages[-1].message_id
        child_prompt_event = child_conv.timeline[2]
        assert child_prompt_event.component == "prompt"
        assert child_prompt_event.status == "active"
        assert child_prompt_event.anchor == "after_message_id"
        assert child_prompt_event.anchor_message_id == boundary_id
        # 验证阶段 ① 替换：包含 inst-parent-999，但还保留 {{available_tools}}
        assert "inst-parent-999" in child_prompt_event.supplementary_prompts[0]
        assert "{{available_tools}}" in child_prompt_event.supplementary_prompts[0]

        # 模拟 child agent 运行时设置自己的 prompt 和 tools 并执行 render
        child_conv.system_prompt = "Child specific instructions"
        child_conv.tools = ({"type": "function", "name": "read_file", "description": "read"},)

        rendered = await OpenAIAdapter().render(child_conv, _Resolver())
        items = rendered.input
        assert items[0]["type"] == "additional_tools"
        assert len(items[0]["tools"]) == 2
        assert items[1]["role"] == "developer"
        assert items[1]["content"][0]["text"] == "Parent system prompt"

        # 找到 boundary 消息后面的项
        # items[2] = user (父消息1), items[3] = assistant (父回复), items[4] = user (boundary)
        assert items[4]["role"] == "user"
        assert "ParentAgent" in items[4]["content"][0]["text"]

        # items[5] 应该是 developer 消息，且不是 additional_tools (因为被父工具包含)
        assert items[5]["role"] == "developer"
        dev_text = items[5]["content"][0]["text"]
        # 验证阶段 ② 替换：{{available_tools}} 已经被替换为 ["read_file"]
        assert '["read_file"]' in dev_text
        assert "你是由inst-parent-999创建的子agent" in dev_text
        assert "Child specific instructions" in dev_text
        assert len(items) == 6

    asyncio.run(run())


def test_subagent_timeline_delta_tools_when_not_subset():
    """验证当子 Agent 拥有父 Agent 不具备的新工具时：
    1. 子会话创建时在 boundary 后挂载 active tool 事件；
    2. 渲染时仅在 boundary 后增量输出差集 additional_tools；
    3. 补充提示词中的可用工具范围仍完整包含全部可用工具。
    """
    from types import SimpleNamespace
    from agent_runtime.subagent.subagent_runtime import SubagentRuntime

    async def run():
        parent_conv = Conversation(
            id="conv-parent-002",
            system_prompt="Parent prompt",
            tools=({"type": "function", "name": "read_file"},),
        )
        parent_conv.append_message(UniMessage(role="user", content=[TextPart("hello")]))

        class _Repo:
            def __init__(self):
                self.saved: Conversation | None = None
            async def get(self, cid):
                return parent_conv
            async def create(self, conv, **kwargs):
                self.saved = conv

        repo = _Repo()
        runtime = SubagentRuntime(
            runtime_gateway=object(),
            agent_definition_gateway=object(),
            llm_registry_gateway=object(),
            execution_engine=object(),
            event_publisher=object(),
            conversation_repository=repo,
        )

        parent_def = SimpleNamespace(agent_id="parent_agent", tool_ids=("read_file",), mcp_tools=())
        # child has read_file AND new_extra_tool
        child_def = SimpleNamespace(agent_id="child_agent", tool_ids=("read_file", "new_extra_tool"), mcp_tools=())

        child_conv_id = await runtime._seed_child_conversation_from_parent_prefix(
            parent=SimpleNamespace(
                instance_id="inst-parent-888",
                task_id="task-002",
                conversation_id=parent_conv.id,
                agent_definition_id="parent_agent",
            ),
            parent_agent_name="ParentAgent",
            child_instance_id="inst-child-222",
            child_thread_id="thread-child-2",
            child_agent_definition_id="child_agent",
            parent_conversation_id=parent_conv.id,
            conversation_history_copy_message_count=1,
            parent_definition=parent_def,
            child_definition=child_def,
        )

        child_conv = repo.saved
        assert child_conv is not None
        boundary_id = child_conv.messages[-1].message_id
        # 应有 4 个事件：父tool(historical), 父prompt(historical), 子tool(active), 子prompt(active)
        assert len(child_conv.timeline) == 4
        assert child_conv.timeline[2].component == "tool"
        assert child_conv.timeline[2].status == "active"
        assert child_conv.timeline[2].anchor_message_id == boundary_id

        # 运行时设置 child 完整工具
        child_conv.system_prompt = "Child prompt"
        child_conv.tools = (
            {"type": "function", "name": "read_file"},
            {"type": "function", "name": "new_extra_tool"},
        )

        rendered = await OpenAIAdapter().render(child_conv, _Resolver())
        items = rendered.input
        # items[0] = additional_tools (read_file)
        assert items[0]["tools"] == [{"type": "function", "name": "read_file"}]
        # items[1] = developer (Parent prompt)
        assert items[1]["role"] == "developer"
        # items[2] = user (hello)
        assert items[2]["role"] == "user"
        # items[3] = user (boundary)
        assert items[3]["role"] == "user"
        # items[4] = additional_tools AFTER boundary, containing ONLY new_extra_tool!
        assert items[4]["type"] == "additional_tools"
        assert items[4]["tools"] == [{"type": "function", "name": "new_extra_tool"}]
        # items[5] = developer (Child prompt with supplementary prompt)
        assert items[5]["role"] == "developer"
        dev_text = items[5]["content"][0]["text"]
        assert '["read_file", "new_extra_tool"]' in dev_text
        assert "你是由inst-parent-888创建的子agent" in dev_text

    asyncio.run(run())


def test_sqlite_conversation_repository_preserves_timeline_and_config(tmp_path):
    """验证 SqliteConversationRepository 内存缓存完整保留 timeline、system_prompt 和 tools。"""
    async def run():
        repo = SqliteConversationRepository(tmp_path)
        conv = Conversation(
            id="conv-cache-test",
            system_prompt="My system prompt",
            tools=({"type": "function", "name": "t1"},),
            timeline=[
                ConfigurationComponentEvent(
                    event_id="e1",
                    definition_id="def1",
                    anchor="start",
                    status="historical",
                    snapshot="My system prompt",
                    component="prompt",
                )
            ],
        )
        await repo.create(
            conv,
            task_id="task-1",
            thread_id="th-1",
            agent_id="ag-1",
            agent_instance_id="inst-1",
        )

        loaded = await repo.get("conv-cache-test")
        assert loaded.system_prompt == "My system prompt"
        assert len(loaded.tools) == 1
        assert loaded.tools[0]["name"] == "t1"
        assert len(loaded.timeline) == 1
        assert loaded.timeline[0].event_id == "e1"

        # Update and save
        loaded.system_prompt = "Updated prompt"
        loaded.timeline.append(
            ConfigurationComponentEvent(
                event_id="e2",
                definition_id="def2",
                anchor="start",
                status="active",
                component="tool",
            )
        )
        await repo.save(loaded)

        loaded2 = await repo.get("conv-cache-test")
        assert loaded2.system_prompt == "Updated prompt"
        assert len(loaded2.timeline) == 2

        await repo.delete("conv-cache-test")
        with pytest.raises(KeyError):
            await repo.get("conv-cache-test")

    asyncio.run(run())


def test_send_prompt_subagent_with_subset_tools_succeeds_without_conflict(monkeypatch):
    """验证当子 Agent 工具是父工具的子集时（无 active 工具事件），send_prompt 不会误报配置视图与 tools 冲突。"""
    async def run():
        captured = {}
        async def fake_post(url, headers, body, **kwargs):
            captured.update(copy.deepcopy(body))
            return {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "subagent completed"}],
                    }
                ]
            }

        monkeypatch.setattr("agent_runtime.llm_client.llm_protocol_clients._post_json", fake_post)

        conv = Conversation(
            id="conv-subagent-subset",
            messages=[
                UniMessage("user", [TextPart("parent task")], message_id="msg-1"),
                UniMessage("assistant", [TextPart("dispatch subtask")], message_id="msg-2"),
            ],
            timeline=[
                ConfigurationComponentEvent(
                    event_id="evt-parent-tools",
                    definition_id="parent",
                    anchor="start",
                    sequence=0,
                    status="historical",
                    snapshot=(
                        {"type": "function", "name": "tool_a"},
                        {"type": "function", "name": "tool_b"},
                    ),
                    component="tool",
                ),
                ConfigurationComponentEvent(
                    event_id="evt-parent-prompt",
                    definition_id="parent",
                    anchor="start",
                    sequence=1,
                    status="historical",
                    snapshot="parent system prompt",
                    component="prompt",
                ),
                ConfigurationComponentEvent(
                    event_id="evt-child-prompt",
                    definition_id="child",
                    anchor="after_message_id",
                    anchor_message_id="msg-2",
                    sequence=1,
                    status="active",
                    component="prompt",
                    supplementary_prompts=("你是由 parent-id 创建的子 agent，可用工具范围是 {{available_tools}}",),
                ),
            ],
        )

        service = LlmRequestService((OpenAIResponsesProtocolClient(),), _Resolver())

        # 子 Agent 仅声明 tool_a（父工具包含它，故 timeline 中无 active tool event）
        child_tools = (
            LlmToolDefinition("tool_a", {"type": "function", "name": "tool_a"}),
        )

        result = await service.send_prompt(
            LlmPromptRequest(
                conversation=conv,
                resolved_model=_model(),
                system_prompt="child agent prompt",
                tools=child_tools,
            )
        )

        assert conv.messages[-1] is result.assistant_message
        assert "prompt_cache_options" not in captured
        # 输入最前面有父历史工具
        assert captured["input"][0]["type"] == "additional_tools"
        assert captured["input"][0]["tools"] == [
            {"type": "function", "name": "tool_a"},
            {"type": "function", "name": "tool_b"},
        ]
        # 边界处有子 agent developer 提示词，且 {{available_tools}} 被正确填充为子 Agent 的工具范围 ["tool_a"]
        dev_msg = next(item for item in captured["input"] if "content" in item and any("child agent prompt" in c.get("text", "") for c in item["content"]))
        assert '["tool_a"]' in dev_msg["content"][0]["text"]

    asyncio.run(run())


def test_send_prompt_subagent_with_delta_tools_succeeds_without_conflict(monkeypatch):
    """验证当子 Agent 工具包含新增差集工具时，send_prompt 不会误报配置视图与 tools 冲突。"""
    async def run():
        captured = {}
        async def fake_post(url, headers, body, **kwargs):
            captured.update(copy.deepcopy(body))
            return {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "delta subagent completed"}],
                    }
                ]
            }

        monkeypatch.setattr("agent_runtime.llm_client.llm_protocol_clients._post_json", fake_post)

        conv = Conversation(
            id="conv-subagent-delta",
            messages=[
                UniMessage("user", [TextPart("parent task")], message_id="msg-1"),
                UniMessage("assistant", [TextPart("dispatch subtask")], message_id="msg-2"),
            ],
            timeline=[
                ConfigurationComponentEvent(
                    event_id="evt-parent-tools",
                    definition_id="parent",
                    anchor="start",
                    sequence=0,
                    status="historical",
                    snapshot=(
                        {"type": "function", "name": "tool_a"},
                    ),
                    component="tool",
                ),
                ConfigurationComponentEvent(
                    event_id="evt-parent-prompt",
                    definition_id="parent",
                    anchor="start",
                    sequence=1,
                    status="historical",
                    snapshot="parent prompt",
                    component="prompt",
                ),
                ConfigurationComponentEvent(
                    event_id="evt-child-tool",
                    definition_id="child",
                    anchor="after_message_id",
                    anchor_message_id="msg-2",
                    sequence=0,
                    status="active",
                    component="tool",
                ),
                ConfigurationComponentEvent(
                    event_id="evt-child-prompt",
                    definition_id="child",
                    anchor="after_message_id",
                    anchor_message_id="msg-2",
                    sequence=1,
                    status="active",
                    component="prompt",
                ),
            ],
        )

        service = LlmRequestService((OpenAIResponsesProtocolClient(),), _Resolver())

        # 子 Agent 具备 tool_a（父已有）和 tool_c（新增差集）
        child_tools = (
            LlmToolDefinition("tool_a", {"type": "function", "name": "tool_a"}),
            LlmToolDefinition("tool_c", {"type": "function", "name": "tool_c"}),
        )

        result = await service.send_prompt(
            LlmPromptRequest(
                conversation=conv,
                resolved_model=_model(),
                system_prompt="child agent prompt",
                tools=child_tools,
            )
        )

        assert conv.messages[-1] is result.assistant_message
        # 验证 input 中：
        # 1. 首部父历史工具只有 tool_a
        assert captured["input"][0]["type"] == "additional_tools"
        assert captured["input"][0]["tools"] == [{"type": "function", "name": "tool_a"}]
        # 2. 边界处差集追加工具只有 tool_c，不重复声明 tool_a
        delta_tools_items = [
            item for item in captured["input"]
            if item.get("type") == "additional_tools" and item.get("tools") == [{"type": "function", "name": "tool_c"}]
        ]
        assert len(delta_tools_items) == 1

    asyncio.run(run())


def test_sqlite_conversation_repository_persists_timeline_across_process_restart(tmp_path):
    """验证 SqliteConversationRepository 跨进程重启（清空内存字典缓存并重新打开数据库）能够完整恢复时间线与配置。"""
    from agent_runtime.conversation_message.infrastructure.sqlite_conversation_repository import (
        _TIMELINE_CACHE,
        _SYSTEM_PROMPT_CACHE,
        _TOOLS_CACHE,
    )

    async def run():
        repo = SqliteConversationRepository(tmp_path)
        msg1 = UniMessage(role="user", content=[TextPart(text="Hello")], message_id="msg_001")
        conv = Conversation(
            id="conv-restart-test",
            messages=[msg1],
            system_prompt="Initial business prompt",
            tools=({"type": "function", "name": "biz_tool"},),
            timeline=[
                ConfigurationComponentEvent(
                    event_id="e_start_prompt",
                    definition_id="def_biz",
                    anchor="start",
                    sequence=1,
                    status="historical",
                    snapshot="Initial business prompt",
                    component="prompt",
                ),
                ConfigurationComponentEvent(
                    event_id="e_start_tool",
                    definition_id="def_biz",
                    anchor="start",
                    sequence=0,
                    status="historical",
                    snapshot=({"type": "function", "name": "biz_tool"},),
                    component="tool",
                ),
                ConfigurationComponentEvent(
                    event_id="e_note_prompt",
                    definition_id="system_note_update",
                    anchor="after_message_id",
                    anchor_message_id="msg_001",
                    sequence=1,
                    status="active",
                    component="prompt",
                ),
            ],
        )
        await repo.create(
            conv,
            task_id="task-restart",
            thread_id="thread-restart",
            agent_id="agent-scout",
            agent_instance_id="inst-restart",
        )

        # 模拟完全关闭进程：清空所有内存全局缓存，并关闭连接
        repo.close()
        _TIMELINE_CACHE.clear()
        _SYSTEM_PROMPT_CACHE.clear()
        _TOOLS_CACHE.clear()

        # 重新创建仓储实例连接同一磁盘目录
        fresh_repo = SqliteConversationRepository(tmp_path)
        try:
            loaded = await fresh_repo.get("conv-restart-test")
            assert loaded.id == "conv-restart-test"
            assert loaded.system_prompt == "Initial business prompt"
            assert len(loaded.tools) == 1
            assert loaded.tools[0]["name"] == "biz_tool"
            assert len(loaded.messages) == 1
            assert loaded.messages[0].message_id == "msg_001"

            # 验证时间线 100% 完整从 SQLite 恢复
            assert len(loaded.timeline) == 3
            assert loaded.timeline[0].event_id == "e_start_prompt"
            assert loaded.timeline[0].anchor == "start"
            assert loaded.timeline[0].snapshot == "Initial business prompt"
            assert loaded.timeline[1].event_id == "e_start_tool"
            assert loaded.timeline[1].snapshot == ({"type": "function", "name": "biz_tool"},)
            assert loaded.timeline[2].event_id == "e_note_prompt"
            assert loaded.timeline[2].anchor == "after_message_id"
            assert loaded.timeline[2].anchor_message_id == "msg_001"
            assert loaded.timeline[2].status == "active"

            # 验证物理删除
            await fresh_repo.delete("conv-restart-test")
            cfg_rows = fresh_repo._conn.execute(
                "SELECT COUNT(*) FROM conversation_configuration WHERE conversation_id = ?",
                ("conv-restart-test",),
            ).fetchone()[0]
            assert cfg_rows == 0
        finally:
            fresh_repo.close()

    asyncio.run(run())


def test_sqlite_conversation_repository_prunes_dangling_timeline_events_on_message_removal(tmp_path):
    """验证当会话消息被裁剪/删除时，依赖已删除 message_id 的时间线事件被自动物理剔除。"""
    async def run():
        repo = SqliteConversationRepository(tmp_path)
        msg1 = UniMessage(role="user", content=[TextPart(text="Msg 1")], message_id="msg_1")
        msg2 = UniMessage(role="user", content=[TextPart(text="Msg 2")], message_id="msg_2")
        conv = Conversation(
            id="conv-prune-test",
            messages=[msg1, msg2],
            timeline=[
                ConfigurationComponentEvent(
                    event_id="e_start",
                    definition_id="def_1",
                    anchor="start",
                    status="historical",
                    snapshot="Start",
                    component="prompt",
                ),
                ConfigurationComponentEvent(
                    event_id="e_after_msg2",
                    definition_id="def_2",
                    anchor="after_message_id",
                    anchor_message_id="msg_2",
                    status="active",
                    component="prompt",
                ),
            ],
        )
        await repo.create(
            conv,
            task_id="task-p",
            thread_id="th-p",
            agent_id="ag-p",
            agent_instance_id="inst-p",
        )

        # 模拟消息裁剪/历史截断：msg_2 被移除
        conv.messages = [msg1]
        await repo.save(conv)

        # 验证内存与磁盘上的孤儿事件均被剔除
        loaded = await repo.get("conv-prune-test")
        assert len(loaded.timeline) == 1
        assert loaded.timeline[0].event_id == "e_start"

        repo.close()

    asyncio.run(run())


def test_responses_client_omits_prompt_cache_options_by_default_and_supports_explicit_pass(monkeypatch):
    async def run():
        captured = {}

        async def fake_post(url, headers, body, **kwargs):
            captured.clear()
            captured.update(copy.deepcopy(body))
            return {"output": []}

        monkeypatch.setattr(
            "agent_runtime.llm_client.llm_protocol_clients._post_json",
            fake_post,
        )
        model = _model()
        from agent_runtime.conversation_message.message.openai_protocol import OpenAIRenderResult

        # 1. 默认渲染结果中 prompt_cache_options 为 None，发送时不携带 prompt_cache_options
        result_default = OpenAIRenderResult(
            input=[{"type": "additional_tools", "role": "developer", "tools": []}],
            uses_input_configuration=True,
            prompt_cache_options=None,
        )
        await OpenAIResponsesProtocolClient().request(
            resolved_model=model,
            rendered_messages=result_default,
            system_prompt=None,
            tools=(),
            request_params={},
            api_key=None,
        )
        assert "prompt_cache_options" not in captured

        # 2. 显式在 request_params 传递 prompt_cache_options 时能够正常透传
        await OpenAIResponsesProtocolClient().request(
            resolved_model=model,
            rendered_messages=result_default,
            system_prompt=None,
            tools=(),
            request_params={"prompt_cache_options": {"mode": "explicit"}},
            api_key=None,
        )
        assert captured["prompt_cache_options"] == {"mode": "explicit"}

        # 3. 显式传递 None 时不携带
        await OpenAIResponsesProtocolClient().request(
            resolved_model=model,
            rendered_messages=result_default,
            system_prompt=None,
            tools=(),
            request_params={"prompt_cache_options": None},
            api_key=None,
        )
        assert "prompt_cache_options" not in captured

    asyncio.run(run())

