from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agent_runtime.file_tools.file_tools import TextSearchRequest, text_search


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _request(search_root: str | Path, query: str = "needle", **kwargs) -> TextSearchRequest:
    payload = {
        "search_root": str(search_root),
        "query": query,
        "regex": False,
        "case_sensitive": False,
        "extensions": [],
        "path_globs": [],
        "limit": 50,
        "offset": 0,
        "exclude_names": [],
        "include_hidden": False,
        "respect_gitignore": False,
    }
    payload.update(kwargs)
    return TextSearchRequest(**payload)


def test_directory_mode_finds_matches_across_files(tmp_path: Path) -> None:
    _write(tmp_path / "a.txt", "alpha needle one\n")
    _write(tmp_path / "b.py", "beta needle two\n")
    _write(tmp_path / "c.md", "no match here\n")

    result = text_search(_request(tmp_path, "needle"))

    assert result.success is True
    assert result.output["search_target_type"] == "directory"
    assert result.output["search_root"] == str(tmp_path.resolve())
    lines = {(match["path"], match["line"]) for match in result.output["matches"]}
    assert any(path.endswith("a.txt") and "needle one" in line for path, line in lines)
    assert any(path.endswith("b.py") and "needle two" in line for path, line in lines)
    assert all(Path(path).is_absolute() for path, _ in lines)


def test_directory_mode_respects_extensions_and_path_globs(tmp_path: Path) -> None:
    _write(tmp_path / "keep.py", "needle keep\n")
    _write(tmp_path / "drop.txt", "needle drop\n")
    _write(tmp_path / "nested" / "keep.py", "needle nested\n")

    result = text_search(
        _request(
            tmp_path,
            "needle",
            extensions=["py"],
            path_globs=["nested/*"],
        )
    )

    assert result.success is True
    assert result.output["search_target_type"] == "directory"
    assert len(result.output["matches"]) == 1
    assert result.output["matches"][0]["path"].endswith(str(Path("nested") / "keep.py")) or result.output["matches"][0]["path"].endswith("nested/keep.py") or result.output["matches"][0]["path"].endswith("nested\\keep.py")
    assert "needle nested" in result.output["matches"][0]["line"]


def test_file_mode_multi_line_hits_and_no_hit(tmp_path: Path) -> None:
    target = _write(tmp_path / "notes.txt", "needle line1\nplain\nneedle line3\n")

    hits = text_search(_request(target, "needle"))
    assert hits.success is True
    assert hits.output["search_target_type"] == "file"
    assert hits.output["search_root"] == str(target.resolve())
    assert [match["line_number"] for match in hits.output["matches"]] == [1, 3]
    assert all(match["path"] == str(target.resolve()) for match in hits.output["matches"])

    misses = text_search(_request(target, "absent-token"))
    assert misses.success is True
    assert misses.output["search_target_type"] == "file"
    assert misses.output["matches"] == []
    assert misses.output["has_more"] is False
    assert misses.output["next_offset"] is None


def test_file_mode_is_not_not_a_directory(tmp_path: Path) -> None:
    target = _write(tmp_path / "single.txt", "needle\n")
    result = text_search(_request(target, "needle"))
    assert result.success is True
    assert result.error_code is None or result.error_code != "NotADirectory"
    assert result.output["search_target_type"] == "file"


def test_file_mode_ignores_directory_candidate_filters(tmp_path: Path) -> None:
    # 扩展名/path_globs/exclude_names/include_hidden 在文件模式必须 no-op，不能排除显式文件。
    target = _write(tmp_path / ".hidden-data.dat", "needle in hidden dat\n")

    result = text_search(
        _request(
            target,
            "needle",
            extensions=["py"],
            path_globs=["**/*.py"],
            exclude_names=[".hidden-data.dat", "hidden-data.dat"],
            include_hidden=False,
            respect_gitignore=True,
        )
    )

    assert result.success is True
    assert result.output["search_target_type"] == "file"
    assert len(result.output["matches"]) == 1
    assert result.output["matches"][0]["path"] == str(target.resolve())
    assert "needle in hidden dat" in result.output["matches"][0]["line"]


def test_file_mode_limit_offset_pagination(tmp_path: Path) -> None:
    content = "".join(f"needle {index}\n" for index in range(1, 6))
    target = _write(tmp_path / "paged.txt", content)

    page1 = text_search(_request(target, "needle", limit=2, offset=0))
    assert page1.success is True
    assert [match["line_number"] for match in page1.output["matches"]] == [1, 2]
    assert page1.output["has_more"] is True
    assert page1.output["next_offset"] == 2
    assert page1.output["returned_count"] == 2

    page2 = text_search(_request(target, "needle", limit=2, offset=2))
    assert [match["line_number"] for match in page2.output["matches"]] == [3, 4]
    assert page2.output["has_more"] is True
    assert page2.output["next_offset"] == 4

    page3 = text_search(_request(target, "needle", limit=2, offset=4))
    assert [match["line_number"] for match in page3.output["matches"]] == [5]
    assert page3.output["has_more"] is False
    assert page3.output["next_offset"] is None

    # 分页窗口无重复命中
    all_lines = [
        match["line"]
        for page in (page1, page2, page3)
        for match in page.output["matches"]
    ]
    assert all_lines == ["needle 1", "needle 2", "needle 3", "needle 4", "needle 5"]


def test_invalid_regex_returns_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _write(tmp_path / "regex.txt", "abc\n")
    monkeypatch.setattr("agent_runtime.file_tools.file_tools.shutil.which", lambda _name: None)

    result = text_search(_request(target, "(", regex=True))
    assert result.success is False
    assert result.error_code == "InvalidRegex"


def test_path_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "missing-dir" / "nope.txt"
    result = text_search(_request(missing, "needle"))
    assert result.success is False
    assert result.error_code == "PathNotFound"
    assert "搜索路径不存在" in (result.error_message or "")


def test_python_file_fallback_when_rg_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _write(tmp_path / "fallback.txt", "needle fallback\n")
    monkeypatch.setattr("agent_runtime.file_tools.file_tools.shutil.which", lambda _name: None)

    result = text_search(_request(target, "needle"))
    assert result.success is True
    assert result.output["backend"] == "python"
    assert result.output["search_target_type"] == "file"
    assert len(result.output["matches"]) == 1
    assert result.output["matches"][0]["path"] == str(target.resolve())


def test_python_binary_file_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"hello\x00needle\x00world")
    monkeypatch.setattr("agent_runtime.file_tools.file_tools.shutil.which", lambda _name: None)

    result = text_search(_request(target, "needle"))
    assert result.success is True
    assert result.output["backend"] == "python"
    assert result.output["search_target_type"] == "file"
    assert result.output["matches"] == []
    assert result.output["skipped_count"] == 1
    assert result.output["skipped_files"][0]["path"] == str(target.resolve())
    assert result.output["skipped_files"][0]["reason"] == "binary_file"


def test_file_mode_uses_ripgrep_when_available(tmp_path: Path) -> None:
    import shutil

    if shutil.which("rg") is None:
        pytest.skip("rg not available")

    target = _write(tmp_path / "rg-file.txt", "needle via ripgrep\n")
    result = text_search(_request(target, "needle"))
    assert result.success is True
    assert result.output["backend"] == "ripgrep"
    assert result.output["search_target_type"] == "file"
    assert len(result.output["matches"]) == 1
    assert result.output["matches"][0]["path"] == str(target.resolve())


def test_not_searchable_path_for_non_file_non_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 用 mock 模拟 exists=True 但既非 file 也非 dir（如特殊设备节点）。
    fake = tmp_path / "special"
    fake.write_text("x", encoding="utf-8")

    class _FakePath:
        def __init__(self, _value: str):
            self._value = str(fake.resolve())

        def expanduser(self):
            return self

        def resolve(self):
            return self

        def exists(self):
            return True

        def is_file(self):
            return False

        def is_dir(self):
            return False

        def __str__(self):
            return self._value

    monkeypatch.setattr("agent_runtime.file_tools.file_tools.Path", _FakePath)
    result = text_search(_request(fake, "needle"))
    assert result.success is False
    assert result.error_code == "NotSearchablePath"
