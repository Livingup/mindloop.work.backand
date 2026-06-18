from __future__ import annotations

import pytest
from fastapi import HTTPException

from agent_runtime.file_tools.workspace_rest_adapter import (
    _list_workspace_memory,
    _read_workspace_memory,
    _save_workspace_memory,
    _resolve_agent_memory_file,
    _sha256_hex,
)


def test_workspace_memory_lists_reads_and_saves_controlled_memory_files(tmp_path):
    memory_file = tmp_path / ".spread-ai" / "memory" / "Development" / "memory.md"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text("old memory", encoding="utf-8")

    listing = _list_workspace_memory(tmp_path)

    assert listing.memory_root_exists is True
    assert [entry.agent_id for entry in listing.entries] == ["Development"]
    assert listing.entries[0].relative_path == ".spread-ai/memory/Development/memory.md"
    assert listing.entries[0].summary == "old memory"

    content = _read_workspace_memory(tmp_path, "Development")
    assert content.content == "old memory"
    assert content.content_sha256 == _sha256_hex(b"old memory")
    assert content.summary == "old memory"

    saved = _save_workspace_memory(tmp_path, "Development", "new memory", content.content_sha256)

    assert saved.status == "saved"
    assert saved.content_sha256 == _sha256_hex(b"new memory")
    assert saved.summary == "new memory"
    assert memory_file.read_text(encoding="utf-8") == "new memory"


def test_workspace_memory_returns_empty_when_memory_root_missing(tmp_path):
    listing = _list_workspace_memory(tmp_path)

    assert listing.memory_root_exists is False
    assert listing.entries == []


def test_workspace_memory_rejects_stale_sha(tmp_path):
    memory_file = tmp_path / ".spread-ai" / "memory" / "Development" / "memory.md"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text("fresh", encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        _save_workspace_memory(tmp_path, "Development", "overwrite", _sha256_hex(b"stale"))

    assert exc_info.value.status_code == 409
    assert memory_file.read_text(encoding="utf-8") == "fresh"


@pytest.mark.parametrize(
    "agent_id",
    [
        "../Development",
        "Development/memory.md",
        "Development\\memory.md",
        "C:/Development",
        "..",
        "Development..escape",
    ],
)
def test_workspace_memory_rejects_agent_id_path_injection(tmp_path, agent_id):
    memory_file = tmp_path / ".spread-ai" / "memory" / "Development" / "memory.md"
    memory_file.parent.mkdir(parents=True)
    memory_file.write_text("memory", encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        _resolve_agent_memory_file(tmp_path, agent_id, require_exists=True)

    assert exc_info.value.status_code == 400


def test_workspace_memory_ignores_non_memory_files_and_nested_files(tmp_path):
    root = tmp_path / ".spread-ai" / "memory"
    (root / "Development").mkdir(parents=True)
    (root / "Development" / "notes.md").write_text("ignore", encoding="utf-8")
    (root / "Nested" / "inner").mkdir(parents=True)
    (root / "Nested" / "inner" / "memory.md").write_text("ignore", encoding="utf-8")

    listing = _list_workspace_memory(tmp_path)

    assert listing.memory_root_exists is True
    assert listing.entries == []
