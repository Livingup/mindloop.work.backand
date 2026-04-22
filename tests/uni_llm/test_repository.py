"""Tests for ConversationRepository implementations (InMemory + SQLite)."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from agent_runtime.uni_llm.message.models import (
    Conversation,
    ConversationState,
    TextPart,
    ImagePart,
    ThinkingPart,
    UniMessage,
)
from agent_runtime.uni_llm.conversation_repository import (
    InMemoryConversationRepository,
    SqliteConversationRepository,
)


# ── Helpers ──────────────────────────────────────────────────────

def _make_conv(conv_id: str = "conv-1") -> Conversation:
    return Conversation(
        id=conv_id,
        messages=[],
        state=ConversationState(),
        provider_state={},
    )


def _user_msg(text: str) -> UniMessage:
    return UniMessage(role="user", content=[TextPart(text=text)])


def _assistant_msg(text: str, provider: str = "claude") -> UniMessage:
    return UniMessage(
        role="assistant",
        content=[TextPart(text=text)],
        source_provider=provider,
        provider_extras={provider: {"raw": [{"type": "text", "text": text}]}},
    )


def _image_user_msg() -> UniMessage:
    return UniMessage(
        role="user",
        content=[TextPart(text="Look at this"), ImagePart(resource_id="img-001")],
    )


def _thinking_assistant_msg() -> UniMessage:
    return UniMessage(
        role="assistant",
        content=[ThinkingPart(text="Let me think..."), TextPart(text="Answer")],
        source_provider="claude",
    )


# ── Fixture: both implementations ────────────────────────────────

@pytest.fixture(params=["memory", "sqlite"])
def repo(request, tmp_path):
    if request.param == "memory":
        return InMemoryConversationRepository()
    data_dir = tmp_path / "conv_data"
    r = SqliteConversationRepository(data_dir)
    request.addfinalizer(r.close)
    return r


# ── Tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_and_get(repo):
    conv = _make_conv()
    await repo.create(conv, task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a1")
    loaded = await repo.get(conv.id)
    assert loaded.id == conv.id
    assert loaded.messages == []


@pytest.mark.asyncio
async def test_get_not_found(repo):
    with pytest.raises(KeyError):
        await repo.get("nonexistent")


@pytest.mark.asyncio
async def test_append_message_incremental(repo):
    conv = _make_conv()
    await repo.create(conv, task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a1")

    msg = _user_msg("Hello")
    await repo.append_message(conv.id, msg)

    loaded = await repo.get(conv.id)
    assert len(loaded.messages) == 1
    assert loaded.messages[0].role == "user"
    assert loaded.messages[0].content[0].text == "Hello"


@pytest.mark.asyncio
async def test_append_multiple_messages_order(repo):
    conv = _make_conv()
    await repo.create(conv, task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a1")

    await repo.append_message(conv.id, _user_msg("Q1"))
    await repo.append_message(conv.id, _assistant_msg("A1"))
    await repo.append_message(conv.id, _user_msg("Q2"))

    loaded = await repo.get(conv.id)
    assert len(loaded.messages) == 3
    assert loaded.messages[0].content[0].text == "Q1"
    assert loaded.messages[1].content[0].text == "A1"
    assert loaded.messages[1].source_provider == "claude"
    assert loaded.messages[2].content[0].text == "Q2"


@pytest.mark.asyncio
async def test_append_message_with_image(repo):
    conv = _make_conv()
    await repo.create(conv, task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a1")

    await repo.append_message(conv.id, _image_user_msg())

    loaded = await repo.get(conv.id)
    parts = loaded.messages[0].content
    assert len(parts) == 2
    assert parts[0].kind == "text"
    assert parts[1].kind == "image"
    assert parts[1].resource_id == "img-001"


@pytest.mark.asyncio
async def test_append_message_with_thinking(repo):
    conv = _make_conv()
    await repo.create(conv, task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a1")

    await repo.append_message(conv.id, _thinking_assistant_msg())

    loaded = await repo.get(conv.id)
    parts = loaded.messages[0].content
    assert parts[0].kind == "thinking"
    assert parts[0].text == "Let me think..."
    assert parts[1].kind == "text"


@pytest.mark.asyncio
async def test_provider_extras_roundtrip(repo):
    conv = _make_conv()
    await repo.create(conv, task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a1")

    msg = _assistant_msg("Hi", provider="openai")
    await repo.append_message(conv.id, msg)

    loaded = await repo.get(conv.id)
    extras = loaded.messages[0].provider_extras
    assert extras is not None
    assert "openai" in extras
    assert extras["openai"]["raw"] == [{"type": "text", "text": "Hi"}]


@pytest.mark.asyncio
async def test_save_state_incremental(repo):
    conv = _make_conv()
    await repo.create(conv, task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a1")

    new_state = ConversationState(
        providers=["claude"],
        vendors=["aws"],
        modalities=["text", "image"],
        image_count=3,
        pdf_page_count=10,
    )
    await repo.save_state(conv.id, new_state)

    loaded = await repo.get(conv.id)
    assert loaded.state.providers == ["claude"]
    assert loaded.state.vendors == ["aws"]
    assert loaded.state.modalities == ["text", "image"]
    assert loaded.state.image_count == 3
    assert loaded.state.pdf_page_count == 10


@pytest.mark.asyncio
async def test_save_provider_state_incremental(repo):
    conv = _make_conv()
    await repo.create(conv, task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a1")

    await repo.save_provider_state(conv.id, "openai", {"last_response_id": "resp-1"})
    await repo.save_provider_state(conv.id, "claude", {"cache_breakpoint_indexes": [0, 2]})

    loaded = await repo.get(conv.id)
    assert loaded.provider_state["openai"]["last_response_id"] == "resp-1"
    assert loaded.provider_state["claude"]["cache_breakpoint_indexes"] == [0, 2]


@pytest.mark.asyncio
async def test_save_full_overwrite(repo):
    conv = _make_conv()
    await repo.create(conv, task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a1")

    # Add messages incrementally
    await repo.append_message(conv.id, _user_msg("old"))
    await repo.append_message(conv.id, _assistant_msg("old reply"))

    # Now full overwrite with different content
    conv2 = Conversation(
        id=conv.id,
        messages=[_user_msg("new"), _assistant_msg("new reply")],
        state=ConversationState(providers=["claude"], image_count=5),
        provider_state={"claude": {"key": "val"}},
    )
    await repo.save(conv2)

    loaded = await repo.get(conv.id)
    assert len(loaded.messages) == 2
    assert loaded.messages[0].content[0].text == "new"
    assert loaded.messages[1].content[0].text == "new reply"
    assert loaded.state.image_count == 5
    assert loaded.provider_state["claude"]["key"] == "val"


@pytest.mark.asyncio
async def test_delete(repo):
    conv = _make_conv()
    await repo.create(conv, task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a1")

    await repo.delete(conv.id)

    with pytest.raises(KeyError):
        await repo.get(conv.id)


@pytest.mark.asyncio
async def test_delete_nonexistent_is_noop(repo):
    await repo.delete("nonexistent")


# ── Hierarchy query tests ────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_by_task(repo):
    await repo.create(_make_conv("c1"), task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a1")
    await repo.create(_make_conv("c2"), task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a2")
    await repo.create(_make_conv("c3"), task_id="t2", thread_id="th2", agent_id="d2", agent_instance_id="a3")

    result = await repo.list_by_task("t1")
    assert sorted(result) == ["c1", "c2"]


@pytest.mark.asyncio
async def test_list_by_thread(repo):
    await repo.create(_make_conv("c1"), task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a1")
    await repo.create(_make_conv("c2"), task_id="t1", thread_id="th2", agent_id="d1", agent_instance_id="a2")

    assert await repo.list_by_thread("th1") == ["c1"]
    assert await repo.list_by_thread("th2") == ["c2"]


@pytest.mark.asyncio
async def test_list_by_agent(repo):
    await repo.create(_make_conv("c1"), task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a1")
    await repo.create(_make_conv("c2"), task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a2")
    await repo.create(_make_conv("c3"), task_id="t1", thread_id="th1", agent_id="d2", agent_instance_id="a3")

    result = await repo.list_by_agent("d1")
    assert sorted(result) == ["c1", "c2"]


@pytest.mark.asyncio
async def test_list_by_agent_instance(repo):
    await repo.create(_make_conv("c1"), task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a1")
    await repo.create(_make_conv("c2"), task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a1")
    await repo.create(_make_conv("c3"), task_id="t1", thread_id="th1", agent_id="d1", agent_instance_id="a2")

    result = await repo.list_by_agent_instance("a1")
    assert sorted(result) == ["c1", "c2"]
