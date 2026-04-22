"""统一 LLM 消息领域模型核心流程验证测试。

验证归一化、状态追踪、未知 provider 报错等基础能力。
渲染测试（需要 async + ResourceResolver mock）在 test_render_flows.py 中。
"""
from agent_runtime.uni_llm.message import (
    create_conversation,
    add_user_message,
    ingest_response,
    TextPart,
    ThinkingPart,
)
from agent_runtime.uni_llm.message.models import TextPartMeta


def test_create_conversation():
    conv = create_conversation()
    assert conv.id
    assert conv.messages == []
    assert conv.state.providers == []
    assert conv.state.image_count == 0


def test_add_user_message():
    conv = create_conversation()
    add_user_message(conv, [TextPart(text="Hello")], [TextPartMeta()])
    assert len(conv.messages) == 1
    assert conv.messages[0].role == "user"
    assert conv.messages[0].source_provider is None
    assert conv.messages[0].provider_extras is None
    assert "text" in conv.state.modalities


def test_ingest_claude_response():
    conv = create_conversation()
    add_user_message(conv, [TextPart(text="Hi")], [TextPartMeta()])

    claude_resp = {
        "content": [
            {"type": "thinking", "thinking": "Let me think...", "signature": "sig_abc"},
            {"type": "text", "text": "Hi there!"},
        ]
    }
    msg = ingest_response(conv, "claude", claude_resp)

    assert len(conv.messages) == 2
    assert msg.role == "assistant"
    assert msg.source_provider == "claude"
    assert [p.kind for p in msg.content] == ["thinking", "text"]
    # raw 保存了完整的原始 content 数组
    assert msg.provider_extras["claude"]["raw"] is claude_resp["content"]
    assert "claude" in conv.state.providers


def test_ingest_openai_response_with_store():
    conv = create_conversation()
    add_user_message(conv, [TextPart(text="Q1")], [TextPartMeta()])

    oai_resp = {
        "id": "resp_123",
        "output": [
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "enc...",
             "summary": [{"text": "thinking"}]},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Hello from OpenAI"}]},
        ],
        "prompt_cache_key": "pck_abc",
    }
    msg = ingest_response(conv, "openai", oai_resp, {"stored": True})

    assert msg.source_provider == "openai"
    assert [p.kind for p in msg.content] == ["thinking", "text"]
    # store=true 时 checkpoint 被记录
    oai_state = conv.provider_state.get("openai")
    assert oai_state is not None
    assert oai_state["last_response_id"] == "resp_123"
    assert oai_state["last_message_index"] == 1
    assert oai_state["prompt_cache_key"] == "pck_abc"


def test_ingest_openai_response_without_store():
    conv = create_conversation()
    add_user_message(conv, [TextPart(text="Q1")], [TextPartMeta()])

    oai_resp = {"id": "resp_456", "output": [
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "No store"}]},
    ]}
    ingest_response(conv, "openai", oai_resp, {"stored": False})

    # store=false 时不记录 checkpoint
    assert conv.provider_state.get("openai") is None


def test_ingest_gemini_response():
    conv = create_conversation()
    add_user_message(conv, [TextPart(text="Q")], [TextPartMeta()])

    gemini_resp = {
        "candidates": [{
            "content": {
                "role": "model",
                "parts": [
                    {"text": "Deep thought", "thought": True, "thoughtSignature": "ts_abc"},
                    {"text": "Gemini says hi"},
                ],
            },
        }],
    }
    msg = ingest_response(conv, "gemini", gemini_resp)

    assert msg.source_provider == "gemini"
    assert [p.kind for p in msg.content] == ["thinking", "text"]
    assert msg.provider_extras["gemini"]["raw"] is gemini_resp["candidates"][0]["content"]["parts"]


def test_multi_provider_state_tracking():
    conv = create_conversation()

    add_user_message(conv, [TextPart(text="Q1")], [TextPartMeta()])
    ingest_response(conv, "claude", {"content": [{"type": "text", "text": "A1"}]})

    add_user_message(conv, [TextPart(text="Q2")], [TextPartMeta()])
    ingest_response(conv, "openai", {"id": "r1", "output": [
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "A2"}]},
    ]})

    add_user_message(conv, [TextPart(text="Q3")], [TextPartMeta()])
    ingest_response(conv, "gemini", {"candidates": [{"content": {"role": "model",
        "parts": [{"text": "A3"}]}}]})

    assert set(conv.state.providers) == {"claude", "openai", "gemini"}
    assert len(conv.messages) == 6  # 3 user + 3 assistant


def test_vendor_metadata():
    conv = create_conversation()
    add_user_message(conv, [TextPart(text="Q")], [TextPartMeta()])

    msg = ingest_response(
        conv, "claude",
        {"content": [{"type": "text", "text": "A"}]},
        {"vendor": "openrouter"},
    )
    assert msg.source_vendor == "openrouter"
    assert "openrouter" in conv.state.vendors


def test_unknown_provider_raises():
    conv = create_conversation()

    try:
        ingest_response(conv, "unknown_provider", {})
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "unknown_provider" in str(e)


def test_preflight_claude_image_limit():
    conv = create_conversation()

    # 模拟超限：直接设置 state
    conv.state.image_count = 101

    try:
        from agent_runtime.uni_llm.message.claude_protocol import ClaudeAdapter
        adapter = ClaudeAdapter()
        adapter.preflight(conv)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "图片数" in str(e)
