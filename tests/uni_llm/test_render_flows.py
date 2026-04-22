"""统一 LLM 消息领域模型渲染流程验证测试。

验证三家内置 adapter 的渲染能力，包括：
- 同平台 raw 回灌（无损）
- 跨平台公共层降级
- Claude 缓存断点
- OpenAI checkpoint gap 模式
- Gemini cachedContent
- 消息规范化（首条 user、相邻同 role 合并）
"""
import pytest

from agent_runtime.uni_llm.message import (
    create_conversation,
    add_user_message,
    ingest_response,
    render_messages,
    TextPart,
    ImagePart,
    ThinkingPart,
)
from agent_runtime.uni_llm.message.models import TextPartMeta, ImagePartMeta
from agent_runtime.uni_llm.message.claude_protocol import ClaudeRenderResult
from agent_runtime.uni_llm.message.gemini_protocol import GeminiRenderResult
from agent_runtime.uni_llm.message.openai_protocol import OpenAIRenderResult
from agent_runtime.uni_llm.south_gateway.resource_resolver.contracts import (
    ResourceMeta,
)


# ── Mock ResourceResolver ────────────────────────────────────────

class MockResourceResolver:
    """内存 mock 资源解析器，用于测试渲染流程。"""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, ResourceMeta]] = {}

    def add_resource(
        self, resource_id: str, base64_content: str, meta: ResourceMeta,
    ) -> None:
        self._store[resource_id] = (base64_content, meta)

    async def get_base64(self, resource_id: str) -> str:
        return self._store[resource_id][0]

    async def get_resource_meta(self, resource_id: str) -> ResourceMeta:
        return self._store[resource_id][1]


def _make_conv_resolver():
    """创建测试用的 conversation + resolver。"""
    conv = create_conversation()
    resolver = MockResourceResolver()
    return conv, resolver


# ── Claude 渲染 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_render_claude_same_provider_raw_replay():
    """Claude → Claude：同平台 raw 回灌（含 signature，无损）。"""
    conv, resolver = _make_conv_resolver()

    add_user_message(conv, [TextPart(text="Q1")], [TextPartMeta()])
    ingest_response(conv, "claude", {
        "content": [
            {"type": "thinking", "thinking": "...", "signature": "sig_1"},
            {"type": "text", "text": "A1"},
        ],
    })

    add_user_message(conv, [TextPart(text="Q2")], [TextPartMeta()])
    result: ClaudeRenderResult = await render_messages(conv, "claude", resolver)

    # assistant 消息应该是 raw 回灌
    assistant_msg = result.messages[1]
    assert assistant_msg["role"] == "assistant"
    # raw 中包含 signature（无损回灌）
    assert assistant_msg["content"][0]["signature"] == "sig_1"


@pytest.mark.asyncio
async def test_render_claude_cross_provider_downgrade():
    """OpenAI → Claude：跨平台降级只取 text。"""
    conv, resolver = _make_conv_resolver()

    add_user_message(conv, [TextPart(text="Q1")], [TextPartMeta()])
    ingest_response(conv, "openai", {
        "id": "resp_1",
        "output": [
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "enc"},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "OpenAI answer"}]},
        ],
    })

    add_user_message(conv, [TextPart(text="Q2")], [TextPartMeta()])
    result: ClaudeRenderResult = await render_messages(conv, "claude", resolver)

    # 跨平台降级：assistant 只有 text
    assistant_msg = result.messages[1]
    assert assistant_msg["role"] == "assistant"
    assert len(assistant_msg["content"]) == 1
    assert assistant_msg["content"][0]["type"] == "text"
    assert assistant_msg["content"][0]["text"] == "OpenAI answer"


@pytest.mark.asyncio
async def test_render_claude_with_image():
    """Claude 渲染包含图片的 user 消息。"""
    conv, resolver = _make_conv_resolver()
    resolver.add_resource(
        "img_001", "base64data",
        ResourceMeta(mime_type="image/jpeg", resource_category="image"),
    )

    add_user_message(
        conv,
        [TextPart(text="Describe this"), ImagePart(resource_id="img_001")],
        [TextPartMeta(), ImagePartMeta()],
    )

    result: ClaudeRenderResult = await render_messages(conv, "claude", resolver)
    user_content = result.messages[0]["content"]
    assert user_content[0] == {"type": "text", "text": "Describe this"}
    assert user_content[1]["type"] == "image"
    assert user_content[1]["source"]["data"] == "base64data"


@pytest.mark.asyncio
async def test_render_claude_cache_breakpoints():
    """Claude 缓存断点累积策略。"""
    conv, resolver = _make_conv_resolver()

    # 3 轮对话
    for i in range(3):
        add_user_message(conv, [TextPart(text=f"Q{i}")], [TextPartMeta()])
        ingest_response(conv, "claude", {
            "content": [{"type": "text", "text": f"A{i}"}],
        })
    add_user_message(conv, [TextPart(text="Q3")], [TextPartMeta()])

    result: ClaudeRenderResult = await render_messages(conv, "claude", resolver)
    # 应该有断点
    assert len(result.applied_cache_breakpoint_indexes) > 0

    # 写回断点后再渲染，历史断点应被累积
    conv.provider_state["claude"] = {
        "cache_breakpoint_indexes": result.applied_cache_breakpoint_indexes,
    }
    add_user_message(conv, [TextPart(text="Q4")], [TextPartMeta()])
    ingest_response(conv, "claude", {
        "content": [{"type": "text", "text": "A4"}],
    })
    add_user_message(conv, [TextPart(text="Q5")], [TextPartMeta()])

    result2: ClaudeRenderResult = await render_messages(conv, "claude", resolver)
    # 历史断点 + 新断点都应存在
    assert len(result2.applied_cache_breakpoint_indexes) >= len(
        result.applied_cache_breakpoint_indexes
    )


@pytest.mark.asyncio
async def test_render_claude_first_message_must_be_user():
    """Claude 渲染时首条消息必须是 user（规范化保证）。"""
    conv, resolver = _make_conv_resolver()

    # 直接 ingest 一条 assistant（跳过 user），模拟异常场景
    ingest_response(conv, "claude", {
        "content": [{"type": "text", "text": "orphan assistant"}],
    })
    add_user_message(conv, [TextPart(text="Q")], [TextPartMeta()])

    result: ClaudeRenderResult = await render_messages(conv, "claude", resolver)
    assert result.messages[0]["role"] == "user"


# ── Gemini 渲染 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_render_gemini_same_provider_raw_replay():
    """Gemini → Gemini：同平台 raw 回灌（含 thoughtSignature，无损）。"""
    conv, resolver = _make_conv_resolver()

    add_user_message(conv, [TextPart(text="Q1")], [TextPartMeta()])
    ingest_response(conv, "gemini", {
        "candidates": [{
            "content": {
                "role": "model",
                "parts": [
                    {"text": "thought", "thought": True, "thoughtSignature": "ts_1"},
                    {"text": "A1"},
                ],
            },
        }],
    })
    add_user_message(conv, [TextPart(text="Q2")], [TextPartMeta()])

    result: GeminiRenderResult = await render_messages(conv, "gemini", resolver)
    model_msg = result.contents[1]
    assert model_msg["role"] == "model"
    # raw 回灌包含 thoughtSignature
    assert model_msg["parts"][0]["thoughtSignature"] == "ts_1"


@pytest.mark.asyncio
async def test_render_gemini_cross_provider_downgrade():
    """Claude → Gemini：跨平台降级。"""
    conv, resolver = _make_conv_resolver()

    add_user_message(conv, [TextPart(text="Q1")], [TextPartMeta()])
    ingest_response(conv, "claude", {
        "content": [
            {"type": "thinking", "thinking": "...", "signature": "sig"},
            {"type": "text", "text": "Claude answer"},
        ],
    })
    add_user_message(conv, [TextPart(text="Q2")], [TextPartMeta()])

    result: GeminiRenderResult = await render_messages(conv, "gemini", resolver)
    model_msg = result.contents[1]
    assert model_msg["role"] == "model"
    # 跨平台降级只有 text
    assert model_msg["parts"] == [{"text": "Claude answer"}]


@pytest.mark.asyncio
async def test_render_gemini_cached_content():
    """Gemini cachedContent 透传。"""
    conv, resolver = _make_conv_resolver()
    conv.provider_state["gemini"] = {"cached_content": "cachedContents/abc"}

    add_user_message(conv, [TextPart(text="Q")], [TextPartMeta()])
    result: GeminiRenderResult = await render_messages(conv, "gemini", resolver)
    assert result.cached_content == "cachedContents/abc"


# ── OpenAI 渲染 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_render_openai_same_provider_raw_replay():
    """OpenAI → OpenAI：同平台 raw 回灌（展开 output items 到 input）。"""
    conv, resolver = _make_conv_resolver()

    add_user_message(conv, [TextPart(text="Q1")], [TextPartMeta()])
    raw_output = [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "enc"},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "A1"}]},
    ]
    ingest_response(conv, "openai", {"id": "resp_1", "output": raw_output})

    add_user_message(conv, [TextPart(text="Q2")], [TextPartMeta()])
    result: OpenAIRenderResult = await render_messages(
        conv, "openai", resolver, {"stored": False},
    )

    # 同平台回灌：output items 展开到 input
    assert result.input[1] == raw_output[0]  # reasoning
    assert result.input[2] == raw_output[1]  # message
    assert result.include == ["reasoning.encrypted_content"]


@pytest.mark.asyncio
async def test_render_openai_checkpoint_gap_mode():
    """OpenAI store=true checkpoint gap 模式。"""
    conv, resolver = _make_conv_resolver()

    # 第一轮 store=true
    add_user_message(conv, [TextPart(text="Q1")], [TextPartMeta()])
    ingest_response(conv, "openai", {
        "id": "resp_1",
        "output": [{"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "A1"}]}],
        "prompt_cache_key": "pck_1",
    }, {"stored": True})

    # 第二轮 store=true → 应该走 gap 模式
    add_user_message(conv, [TextPart(text="Q2")], [TextPartMeta()])
    result: OpenAIRenderResult = await render_messages(
        conv, "openai", resolver, {"stored": True},
    )

    assert result.previous_response_id == "resp_1"
    assert result.prompt_cache_key == "pck_1"
    # gap 模式只发 checkpoint 之后的消息（Q2）
    assert len(result.input) == 1
    assert result.input[0]["role"] == "user"


@pytest.mark.asyncio
async def test_render_openai_fullsend_when_not_stored():
    """OpenAI store=false 全量展开，忽略 checkpoint。"""
    conv, resolver = _make_conv_resolver()

    add_user_message(conv, [TextPart(text="Q1")], [TextPartMeta()])
    ingest_response(conv, "openai", {
        "id": "resp_1",
        "output": [{"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "A1"}]}],
    }, {"stored": True})

    add_user_message(conv, [TextPart(text="Q2")], [TextPartMeta()])

    # store=false 忽略 checkpoint
    result: OpenAIRenderResult = await render_messages(
        conv, "openai", resolver, {"stored": False},
    )

    assert result.previous_response_id is None
    # 全量展开：Q1 + A1 + Q2
    assert len(result.input) >= 3


@pytest.mark.asyncio
async def test_render_openai_cross_provider_downgrade():
    """Claude → OpenAI：跨平台降级。"""
    conv, resolver = _make_conv_resolver()

    add_user_message(conv, [TextPart(text="Q1")], [TextPartMeta()])
    ingest_response(conv, "claude", {
        "content": [
            {"type": "thinking", "thinking": "...", "signature": "sig"},
            {"type": "text", "text": "Claude answer"},
        ],
    })
    add_user_message(conv, [TextPart(text="Q2")], [TextPartMeta()])

    result: OpenAIRenderResult = await render_messages(
        conv, "openai", resolver, {"stored": False},
    )

    # 跨平台降级的 assistant 应该是 message item
    downgraded = result.input[1]
    assert downgraded["role"] == "assistant"
    assert downgraded["content"][0]["text"] == "Claude answer"


# ── 跨平台多轮切换 ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cross_platform_switch_no_error():
    """Claude 3 轮 → Gemini 2 轮 → OpenAI 1 轮，全程不报错。"""
    conv, resolver = _make_conv_resolver()

    # Claude 3 轮
    for i in range(3):
        add_user_message(conv, [TextPart(text=f"C{i}")], [TextPartMeta()])
        ingest_response(conv, "claude", {
            "content": [{"type": "text", "text": f"CA{i}"}],
        })

    # 切到 Gemini 2 轮
    for i in range(2):
        add_user_message(conv, [TextPart(text=f"G{i}")], [TextPartMeta()])
        ingest_response(conv, "gemini", {
            "candidates": [{"content": {"role": "model",
                "parts": [{"text": f"GA{i}"}]}}],
        })

    # 切到 OpenAI 1 轮
    add_user_message(conv, [TextPart(text="O0")], [TextPartMeta()])

    # 渲染 OpenAI —— 所有历史消息混合 Claude/Gemini 的 assistant
    result: OpenAIRenderResult = await render_messages(
        conv, "openai", resolver, {"stored": False},
    )
    assert len(result.input) > 0

    # 渲染 Claude —— 所有历史消息混合 Gemini/OpenAI 的 assistant
    result2: ClaudeRenderResult = await render_messages(
        conv, "claude", resolver,
    )
    assert len(result2.messages) > 0

    # 渲染 Gemini
    result3: GeminiRenderResult = await render_messages(
        conv, "gemini", resolver,
    )
    assert len(result3.contents) > 0
