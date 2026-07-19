from __future__ import annotations

import hashlib
from pathlib import Path

from agent_runtime.file_tools.file_edit_tools import (
    FileCreateRequest,
    FileDeleteRequest,
    FileModifyEdit,
    FileModifyRequest,
    FileMoveRequest,
    file_create,
    file_delete,
    file_modify,
    file_move,
    _parse_optional_expected_sha256,
    _validate_expected_sha256,
)
from agent_runtime.tool.tool_execution_models import ToolRuntimeContext


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(path: Path, text: str) -> str:
    """Write exact UTF-8 bytes without Windows newline translation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _ctx() -> ToolRuntimeContext:
    return ToolRuntimeContext()


def test_parse_optional_expected_sha256_blank_and_prefix() -> None:
    assert _parse_optional_expected_sha256(None) is None
    assert _parse_optional_expected_sha256("") is None
    assert _parse_optional_expected_sha256("   ") is None
    digest = "a" * 64
    assert _parse_optional_expected_sha256(digest) == digest
    assert _parse_optional_expected_sha256(f"SHA256:{digest.upper()}") == digest


def test_parse_optional_expected_sha256_invalid_format() -> None:
    try:
        _parse_optional_expected_sha256("not-a-hash")
        assert False, "expected InvalidExpectedHash"
    except Exception as exc:  # FileEditBusinessError
        assert getattr(exc, "error_code", None) == "InvalidExpectedHash"


def test_validate_expected_sha256_blank_is_not_hash_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    digest = _write(target, "hello\n")
    assert _validate_expected_sha256(target, "") == digest
    assert _validate_expected_sha256(target, None) == digest


def test_file_create_blank_expected_hash_requires_overwrite_hash(tmp_path: Path) -> None:
    target = tmp_path / "exists.txt"
    _write(target, "old\n")
    result = file_create(
        FileCreateRequest(
            project_root=str(tmp_path),
            file_path="exists.txt",
            content_lines=["new"],
            overwrite=True,
            expected_content_sha256="",
            dry_run=True,
        ),
        _ctx(),
    )
    assert result.success is False
    assert result.error_code == "ExpectedHashRequiredForOverwrite"


def test_file_create_invalid_and_stale_and_correct_hash(tmp_path: Path) -> None:
    target = tmp_path / "exists.txt"
    digest = _write(target, "old\n")

    invalid = file_create(
        FileCreateRequest(
            project_root=str(tmp_path),
            file_path="exists.txt",
            content_lines=["new"],
            overwrite=True,
            expected_content_sha256="zz",
            dry_run=True,
        ),
        _ctx(),
    )
    assert invalid.success is False
    assert invalid.error_code == "InvalidExpectedHash"

    stale = file_create(
        FileCreateRequest(
            project_root=str(tmp_path),
            file_path="exists.txt",
            content_lines=["new"],
            overwrite=True,
            expected_content_sha256="b" * 64,
            dry_run=True,
        ),
        _ctx(),
    )
    assert stale.success is False
    assert stale.error_code == "HashMismatch"

    ok = file_create(
        FileCreateRequest(
            project_root=str(tmp_path),
            file_path="exists.txt",
            content_lines=["new"],
            overwrite=True,
            expected_content_sha256=f"sha256:{digest}",
            dry_run=False,
        ),
        _ctx(),
    )
    assert ok.success is True
    assert ok.output["overwritten"] is True
    assert target.read_text(encoding="utf-8") == "new\n"


def test_file_modify_blank_invalid_stale_correct(tmp_path: Path) -> None:
    target = tmp_path / "mod.txt"
    digest = _write(target, "hello\n")
    edit = FileModifyEdit(feature_desc="replace hello", search_lines=["hello"], replace_lines=["world"])

    blank = file_modify(
        FileModifyRequest(
            project_root=str(tmp_path),
            file_path="mod.txt",
            edits=[edit],
            expected_content_sha256="",
            dry_run=True,
        ),
        _ctx(),
    )
    assert blank.success is True
    assert blank.output["would_modify"] is True

    invalid = file_modify(
        FileModifyRequest(
            project_root=str(tmp_path),
            file_path="mod.txt",
            edits=[edit],
            expected_content_sha256="123",
            dry_run=True,
        ),
        _ctx(),
    )
    assert invalid.success is False
    assert invalid.error_code == "InvalidExpectedHash"

    stale = file_modify(
        FileModifyRequest(
            project_root=str(tmp_path),
            file_path="mod.txt",
            edits=[edit],
            expected_content_sha256="c" * 64,
            dry_run=True,
        ),
        _ctx(),
    )
    assert stale.success is False
    assert stale.error_code == "HashMismatch"

    ok = file_modify(
        FileModifyRequest(
            project_root=str(tmp_path),
            file_path="mod.txt",
            edits=[edit],
            expected_content_sha256=digest,
            dry_run=False,
        ),
        _ctx(),
    )
    assert ok.success is True
    assert target.read_text(encoding="utf-8") == "world\n"


def test_file_delete_blank_invalid_stale_correct(tmp_path: Path) -> None:
    target = tmp_path / "del.txt"
    digest = _write(target, "remove-me\n")

    blank = file_delete(
        FileDeleteRequest(
            project_root=str(tmp_path),
            file_path="del.txt",
            reason="probe blank",
            expected_content_sha256="",
            dry_run=True,
        ),
        _ctx(),
    )
    assert blank.success is True
    assert blank.output["would_delete"] is True

    invalid = file_delete(
        FileDeleteRequest(
            project_root=str(tmp_path),
            file_path="del.txt",
            reason="probe invalid",
            expected_content_sha256="not-hex",
            dry_run=True,
        ),
        _ctx(),
    )
    assert invalid.success is False
    assert invalid.error_code == "InvalidExpectedHash"

    stale = file_delete(
        FileDeleteRequest(
            project_root=str(tmp_path),
            file_path="del.txt",
            reason="probe stale",
            expected_content_sha256="d" * 64,
            dry_run=True,
        ),
        _ctx(),
    )
    assert stale.success is False
    assert stale.error_code == "HashMismatch"

    ok = file_delete(
        FileDeleteRequest(
            project_root=str(tmp_path),
            file_path="del.txt",
            reason="delete with correct hash",
            expected_content_sha256=digest,
            dry_run=False,
        ),
        _ctx(),
    )
    assert ok.success is True
    assert not target.exists()


def test_file_move_source_and_target_hash_branches(tmp_path: Path) -> None:
    source = tmp_path / "src.txt"
    existing_target = tmp_path / "dst.txt"
    source_digest = _write(source, "source\n")
    target_digest = _write(existing_target, "target\n")

    blank_source = file_move(
        FileMoveRequest(
            project_root=str(tmp_path),
            source_path="src.txt",
            target_path="new.txt",
            reason="blank source",
            expected_source_sha256="",
            dry_run=True,
        ),
        _ctx(),
    )
    assert blank_source.success is True
    assert blank_source.output["would_move"] is True

    invalid_source = file_move(
        FileMoveRequest(
            project_root=str(tmp_path),
            source_path="src.txt",
            target_path="new.txt",
            reason="invalid source",
            expected_source_sha256="bad",
            dry_run=True,
        ),
        _ctx(),
    )
    assert invalid_source.success is False
    assert invalid_source.error_code == "InvalidExpectedHash"

    stale_source = file_move(
        FileMoveRequest(
            project_root=str(tmp_path),
            source_path="src.txt",
            target_path="new.txt",
            reason="stale source",
            expected_source_sha256="e" * 64,
            dry_run=True,
        ),
        _ctx(),
    )
    assert stale_source.success is False
    assert stale_source.error_code == "HashMismatch"

    blank_target_existing = file_move(
        FileMoveRequest(
            project_root=str(tmp_path),
            source_path="src.txt",
            target_path="dst.txt",
            reason="blank target existing",
            overwrite=True,
            expected_target_sha256="",
            dry_run=True,
        ),
        _ctx(),
    )
    assert blank_target_existing.success is False
    assert blank_target_existing.error_code == "ExpectedHashRequiredForOverwrite"

    invalid_target = file_move(
        FileMoveRequest(
            project_root=str(tmp_path),
            source_path="src.txt",
            target_path="dst.txt",
            reason="invalid target",
            overwrite=True,
            expected_target_sha256="xyz",
            dry_run=True,
        ),
        _ctx(),
    )
    assert invalid_target.success is False
    assert invalid_target.error_code == "InvalidExpectedHash"

    stale_target = file_move(
        FileMoveRequest(
            project_root=str(tmp_path),
            source_path="src.txt",
            target_path="dst.txt",
            reason="stale target",
            overwrite=True,
            expected_target_sha256="f" * 64,
            dry_run=True,
        ),
        _ctx(),
    )
    assert stale_target.success is False
    assert stale_target.error_code == "HashMismatch"

    blank_target_missing = file_move(
        FileMoveRequest(
            project_root=str(tmp_path),
            source_path="src.txt",
            target_path="missing.txt",
            reason="blank target missing",
            expected_target_sha256="",
            dry_run=True,
        ),
        _ctx(),
    )
    assert blank_target_missing.success is True
    assert blank_target_missing.output["would_move"] is True

    provided_target_missing = file_move(
        FileMoveRequest(
            project_root=str(tmp_path),
            source_path="src.txt",
            target_path="missing.txt",
            reason="provided target missing",
            expected_target_sha256=target_digest,
            dry_run=True,
        ),
        _ctx(),
    )
    assert provided_target_missing.success is False
    assert provided_target_missing.error_code == "TargetHashProvidedButMissing"

    ok_overwrite = file_move(
        FileMoveRequest(
            project_root=str(tmp_path),
            source_path="src.txt",
            target_path="dst.txt",
            reason="correct hashes",
            expected_source_sha256=source_digest,
            overwrite=True,
            expected_target_sha256=target_digest,
            dry_run=False,
        ),
        _ctx(),
    )
    assert ok_overwrite.success is True
    assert ok_overwrite.output["overwritten"] is True
    assert not source.exists()
    assert existing_target.read_text(encoding="utf-8") == "source\n"
