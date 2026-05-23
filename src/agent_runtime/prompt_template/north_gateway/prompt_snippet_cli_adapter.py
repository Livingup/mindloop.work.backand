"""提示词片段 CLI 接入适配。

提供 spread-ai prompt 子命令组，支持提示词片段的增删改查。
输出格式为 JSON，方便 Agent / 外部 AI 解析。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

import click

from agent_runtime.prompt_template.north_gateway.prompt_snippet_contracts import (
    PromptSnippetCreateRequest,
    PromptSnippetUpdateRequest,
)


def _success(message: str) -> None:
    """输出成功响应。"""
    click.echo(json.dumps(
        {"status": "success", "message": message},
        ensure_ascii=False,
    ))


def _error_json(message: str, exit_code: int = 1) -> None:
    """输出失败响应 JSON 到 stderr 并退出。"""
    click.echo(
        json.dumps({"status": "error", "message": message}, ensure_ascii=False),
        err=True,
    )
    sys.exit(exit_code)


def _resolve_text(
    text: str | None,
    text_file: str | None,
    *,
    required: bool = False,
) -> str | None:
    """从 --text 或 --text-file 解析文本内容。

    --text-file 优先于 --text；值为 "-" 时从 stdin 读取。
    """
    if text is not None and text_file is not None:
        _error_json("--text 和 --text-file 不能同时指定。")
    if text_file is not None:
        if text_file == "-":
            return sys.stdin.read()
        p = Path(text_file).resolve()
        if not p.exists():
            _error_json(f"文件不存在: {p}")
        return p.read_text(encoding="utf-8")
    if text is not None:
        return text
    if required:
        _error_json("必须指定 --text 或 --text-file。")
    return None


def create_prompt_cli_group(gateway_factory: Callable[[], object]) -> click.Group:
    """创建提示词片段 CLI 命令组。

    Args:
        gateway_factory: 网关工厂函数，在命令执行时延迟创建网关实例。

    Returns:
        click.Group 命令组，挂载到 spread-ai prompt 下。
    """

    @click.group("prompt")
    @click.pass_context
    def prompt_group(ctx: click.Context) -> None:
        """提示词片段增删改查管理。"""
        ctx.ensure_object(dict)
        ctx.obj["gateway"] = gateway_factory()

    @prompt_group.command("list")
    @click.pass_context
    def list_snippets(ctx: click.Context) -> None:
        """列出所有提示词片段。"""
        gateway = ctx.obj["gateway"]
        snippets = gateway.list_snippets()
        result = [
            {"name": s.name, "category": s.category}
            for s in snippets
        ]
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))

    @prompt_group.command("get")
    @click.argument("name")
    @click.pass_context
    def get_snippet(ctx: click.Context, name: str) -> None:
        """根据唯一描述名获取提示词片段。"""
        gateway = ctx.obj["gateway"]
        try:
            snippet = gateway.get_snippet(name)
        except KeyError as e:
            _error_json(str(e))
        click.echo(json.dumps(
            {"name": snippet.name, "text": snippet.text, "category": snippet.category},
            ensure_ascii=False, indent=2,
        ))

    @prompt_group.command("create")
    @click.option("--name", required=True, help="片段唯一描述名。")
    @click.option("--text", default=None, help="提示词文本内容（短文本）。")
    @click.option("--text-file", default=None, help="从文件读取提示词文本，传 - 表示从 stdin 读取。")
    @click.option("--category", default="default", show_default=True, help="分类目录（如 coding、architecture）。")
    @click.pass_context
    def create_snippet(
        ctx: click.Context,
        name: str,
        text: str | None,
        text_file: str | None,
        category: str,
    ) -> None:
        """创建提示词片段。文本通过 --text 或 --text-file 提供。"""
        resolved_text = _resolve_text(text, text_file, required=True)
        gateway = ctx.obj["gateway"]
        try:
            snippet = gateway.create_snippet(
                PromptSnippetCreateRequest(name=name, text=resolved_text, category=category),
            )
        except ValueError as e:
            _error_json(str(e))
        _success(f"创建成功: {name}")

    @prompt_group.command("update")
    @click.argument("current_name")
    @click.option("--rename", "new_name", default=None, help="新的描述名。")
    @click.option("--text", "new_text", default=None, help="新的提示词文本（短文本）。")
    @click.option("--text-file", "new_text_file", default=None, help="从文件读取新提示词文本，传 - 表示从 stdin 读取。")
    @click.option("--category", "new_category", default=None, help="新的分类目录。")
    @click.pass_context
    def update_snippet(
        ctx: click.Context,
        current_name: str,
        new_name: str | None,
        new_text: str | None,
        new_text_file: str | None,
        new_category: str | None,
    ) -> None:
        """更新提示词片段。CURRENT_NAME 为当前片段描述名。"""
        resolved_text = _resolve_text(new_text, new_text_file)
        if new_name is None and resolved_text is None and new_category is None:
            _error_json("至少指定 --rename、--text / --text-file 或 --category 之一。")
        gateway = ctx.obj["gateway"]
        try:
            snippet = gateway.update_snippet(
                PromptSnippetUpdateRequest(
                    current_name=current_name,
                    new_name=new_name,
                    new_text=resolved_text,
                    new_category=new_category,
                ),
            )
        except (KeyError, ValueError) as e:
            _error_json(str(e))
        _success(f"更新成功: {current_name}")

    @prompt_group.command("delete")
    @click.argument("name")
    @click.pass_context
    def delete_snippet(ctx: click.Context, name: str) -> None:
        """删除提示词片段。"""
        gateway = ctx.obj["gateway"]
        gateway.delete_snippet(name)
        _success(f"删除成功: {name}")

    return prompt_group
