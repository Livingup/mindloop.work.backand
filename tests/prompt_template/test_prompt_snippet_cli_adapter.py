"""提示词片段 CLI 适配器测试。"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from agent_runtime.cli import main


@pytest.fixture()
def runner(tmp_path) -> CliRunner:
    """创建 CLI 测试运行器。"""
    return CliRunner()


@pytest.fixture()
def snippets_dir(tmp_path) -> str:
    """临时片段存储目录。"""
    return str(tmp_path / "snippets")


def _invoke(runner: CliRunner, snippets_dir: str, args: list[str]):
    """执行 CLI 命令并返回结果。"""
    return runner.invoke(main, ["prompt", "--dir", snippets_dir] + args)


def _parse_status(result) -> dict:
    """解析状态响应。"""
    return json.loads(result.output)


# ── 创建 ─────────────────────────────────────────────────────────


def test_cli_create(runner: CliRunner, snippets_dir: str) -> None:
    """CLI 创建片段。"""
    result = _invoke(runner, snippets_dir, ["create", "--name", "测试片段", "--text", "内容"])
    assert result.exit_code == 0
    body = _parse_status(result)
    assert body["status"] == "success"


def test_cli_create_duplicate(runner: CliRunner, snippets_dir: str) -> None:
    """CLI 创建重复片段应返回错误。"""
    _invoke(runner, snippets_dir, ["create", "--name", "重复", "--text", "A"])
    result = _invoke(runner, snippets_dir, ["create", "--name", "重复", "--text", "B"])
    assert result.exit_code != 0


# ── 列表 ─────────────────────────────────────────────────────────


def test_cli_list_empty(runner: CliRunner, snippets_dir: str) -> None:
    """空目录时列表为空数组。"""
    result = _invoke(runner, snippets_dir, ["list"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_cli_list(runner: CliRunner, snippets_dir: str) -> None:
    """列出已创建的片段。"""
    _invoke(runner, snippets_dir, ["create", "--name", "A", "--text", "内容A"])
    _invoke(runner, snippets_dir, ["create", "--name", "B", "--text", "内容B"])
    result = _invoke(runner, snippets_dir, ["list"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    names = {s["name"] for s in data}
    assert names == {"A", "B"}


# ── 查询 ─────────────────────────────────────────────────────────


def test_cli_get(runner: CliRunner, snippets_dir: str) -> None:
    """CLI 查询片段。"""
    _invoke(runner, snippets_dir, ["create", "--name", "查询目标", "--text", "内容"])
    result = _invoke(runner, snippets_dir, ["get", "查询目标"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["name"] == "查询目标"


def test_cli_get_nonexistent(runner: CliRunner, snippets_dir: str) -> None:
    """CLI 查询不存在的片段应返回错误。"""
    result = _invoke(runner, snippets_dir, ["get", "不存在"])
    assert result.exit_code != 0


# ── 更新 ─────────────────────────────────────────────────────────


def test_cli_update_text(runner: CliRunner, snippets_dir: str) -> None:
    """CLI 更新片段文本。"""
    _invoke(runner, snippets_dir, ["create", "--name", "待更新", "--text", "旧内容"])
    result = _invoke(runner, snippets_dir, ["update", "待更新", "--text", "新内容"])
    assert result.exit_code == 0
    body = _parse_status(result)
    assert body["status"] == "success"


def test_cli_update_name(runner: CliRunner, snippets_dir: str) -> None:
    """CLI 更新片段描述名。"""
    _invoke(runner, snippets_dir, ["create", "--name", "旧名", "--text", "内容"])
    result = _invoke(runner, snippets_dir, ["update", "旧名", "--rename", "新名"])
    assert result.exit_code == 0
    body = _parse_status(result)
    assert body["status"] == "success"


# ── 文件读取 ───────────────────────────────────────────


def test_cli_create_with_text_file(runner: CliRunner, snippets_dir: str, tmp_path) -> None:
    """通过 --text-file 从文件读取提示词文本。"""
    text_file = tmp_path / "prompt.txt"
    text_file.write_text("这是一段很长的提示词内容", encoding="utf-8")
    result = _invoke(
        runner, snippets_dir,
        ["create", "--name", "文件片段", "--text-file", str(text_file)],
    )
    assert result.exit_code == 0
    body = _parse_status(result)
    assert body["status"] == "success"


def test_cli_update_with_text_file(runner: CliRunner, snippets_dir: str, tmp_path) -> None:
    """通过 --text-file 更新提示词文本。"""
    _invoke(runner, snippets_dir, ["create", "--name", "待更新", "--text", "旧内容"])
    text_file = tmp_path / "new_prompt.txt"
    text_file.write_text("新的长文本内容", encoding="utf-8")
    result = _invoke(
        runner, snippets_dir,
        ["update", "待更新", "--text-file", str(text_file)],
    )
    assert result.exit_code == 0
    body = _parse_status(result)
    assert body["status"] == "success"


def test_cli_create_text_and_text_file_conflict(runner: CliRunner, snippets_dir: str, tmp_path) -> None:
    """--text 和 --text-file 同时指定应报错。"""
    text_file = tmp_path / "prompt.txt"
    text_file.write_text("内容", encoding="utf-8")
    result = _invoke(
        runner, snippets_dir,
        ["create", "--name", "冲突", "--text", "短文本", "--text-file", str(text_file)],
    )
    assert result.exit_code != 0


# ── 分类目录 ───────────────────────────────────────────


def test_cli_create_with_category(runner: CliRunner, snippets_dir: str) -> None:
    """CLI 创建片段时指定分类目录。"""
    result = _invoke(
        runner, snippets_dir,
        ["create", "--name", "编码规则", "--text", "内容", "--category", "coding"],
    )
    assert result.exit_code == 0
    body = _parse_status(result)
    assert body["status"] == "success"

    # 确认文件实际存到了子目录。
    from pathlib import Path
    subdir = Path(snippets_dir) / "coding"
    assert subdir.exists()
    yaml_files = list(subdir.glob("*.yaml"))
    assert len(yaml_files) == 1


def test_cli_create_without_category(runner: CliRunner, snippets_dir: str) -> None:
    """不指定 category 时片段存储在 default 子目录。"""
    result = _invoke(
        runner, snippets_dir,
        ["create", "--name", "无分类", "--text", "内容"],
    )
    assert result.exit_code == 0
    body = _parse_status(result)
    assert body["status"] == "success"

    from pathlib import Path
    default_dir = Path(snippets_dir) / "default"
    assert default_dir.exists()
    yaml_files = list(default_dir.glob("*.yaml"))
    assert len(yaml_files) == 1


# ── 删除 ─────────────────────────────────────────────────────────


def test_cli_delete(runner: CliRunner, snippets_dir: str) -> None:
    """CLI 删除片段。"""
    _invoke(runner, snippets_dir, ["create", "--name", "待删除", "--text", "内容"])
    result = _invoke(runner, snippets_dir, ["delete", "待删除"])
    assert result.exit_code == 0
    body = _parse_status(result)
    assert body["status"] == "success"

    # 确认已删除。
    list_result = _invoke(runner, snippets_dir, ["list"])
    assert json.loads(list_result.output) == []


# ── 参数校验 ───────────────────────────────────────────


def test_cli_create_empty_name(runner: CliRunner, snippets_dir: str) -> None:
    """空名称应报错。"""
    result = _invoke(runner, snippets_dir, ["create", "--name", "  ", "--text", "内容"])
    assert result.exit_code != 0


def test_cli_create_name_with_slash(runner: CliRunner, snippets_dir: str) -> None:
    """名称包含路径分隔符应报错。"""
    result = _invoke(runner, snippets_dir, ["create", "--name", "a/b", "--text", "内容"])
    assert result.exit_code != 0


def test_cli_create_invalid_category(runner: CliRunner, snippets_dir: str) -> None:
    """非法分类名应报错。"""
    result = _invoke(
        runner, snippets_dir,
        ["create", "--name", "片段", "--text", "内容", "--category", "中文分类"],
    )
    assert result.exit_code != 0


def test_cli_create_category_path_traversal(runner: CliRunner, snippets_dir: str) -> None:
    """分类名包含 .. 应报错。"""
    result = _invoke(
        runner, snippets_dir,
        ["create", "--name", "片段", "--text", "内容", "--category", ".."],
    )
    assert result.exit_code != 0
