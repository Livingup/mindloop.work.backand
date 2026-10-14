# -*- coding: utf-8 -*-
"""系统统一提示词变量模块单元测试。"""
from __future__ import annotations

import json
from pathlib import Path

from agent_runtime.prompt_template.prompt_variables import (
    BUILTIN_PROMPT_VARIABLES,
    PromptVariableContext,
    build_prompt_variable_values,
    get_builtin_prompt_variables,
    list_builtin_prompt_variables,
    render_prompt_variables,
)


def test_builtin_prompt_variables_count_and_fields() -> None:
    """验证预置变量总数与字段完整性。"""
    variables = get_builtin_prompt_variables()
    assert len(variables) == 20

    metadata_list = list_builtin_prompt_variables()
    assert len(metadata_list) == 20

    names = {v.name for v in variables}
    assert "agent_instance_id" in names
    assert "agent_name" in names
    assert "parent_agent_instance_id" in names
    assert "current_working_directory" in names
    assert "dedicated_skill_file" in names
    assert "dedicated_memory_file" in names
    assert "available_tools" in names
    assert "available_mcp_services" in names
    assert "available_subagents" in names
    assert "available_skills" in names
    assert "git_repo_roots" in names
    assert "frontend_channel" in names
    assert "supported_text_formats" in names
    assert "preferred_text_format" in names

    # 验证每一个变量都包含占位符、名称、含义说明与取值逻辑说明字段
    for item in metadata_list:
        assert item["placeholder"].startswith("{{") and item["placeholder"].endswith("}}")
        assert len(item["name"]) > 0
        assert len(item["description"]) > 0
        assert len(item["value_logic"]) > 0
        assert len(item["category"]) > 0


def test_build_prompt_variable_values(tmp_path: Path) -> None:
    """验证根据上下文求值生成字典。"""
    context = PromptVariableContext(
        agent_instance_id="inst-12345",
        agent_name="测试助手",
        current_working_directory=str(tmp_path),
        parent_agent_instance_id="inst-parent-99",
        skill_root_directory=str(tmp_path / "skills"),
        dedicated_skill_file=str(tmp_path / "skills" / "agent-1" / "SKILL.md"),
        dedicated_memory_file=str(tmp_path / ".spread-ai" / "memory" / "agent-1" / "memory.md"),
        available_tools=("file_read", "file_write", "run_command"),
        available_mcp_services="- `git-kb`: 本地Git知识库",
        available_subagents="- agent定义id：`research`；功能：调研",
        available_skills="- `pytest`: 运行测试",
        git_repo_roots=f"- `{tmp_path}`",
        frontend_channel="web",
        supported_text_formats="html, markdown",
        preferred_text_format="html",
    )

    values = build_prompt_variable_values(context)
    assert values["agent_instance_id"] == "inst-12345"
    assert values["agent_name"] == "测试助手"
    assert values["parent_agent_instance_id"] == "inst-parent-99"
    assert values["current_working_directory"] == str(tmp_path.resolve())
    assert values["dedicated_skill_file"] == str(tmp_path / "skills" / "agent-1" / "SKILL.md")
    assert values["dedicated_memory_file"] == str(tmp_path / ".spread-ai" / "memory" / "agent-1" / "memory.md")
    assert json.loads(values["available_tools"]) == ["file_read", "file_write", "run_command"]
    assert values["available_mcp_services"] == "- `git-kb`: 本地Git知识库"
    assert values["available_subagents"] == "- agent定义id：`research`；功能：调研"
    assert values["available_skills"] == "- `pytest`: 运行测试"
    assert values["git_repo_roots"] == f"- `{tmp_path}`"
    assert values["frontend_channel"] == "web"
    assert values["supported_text_formats"] == "html, markdown"
    assert values["preferred_text_format"] == "html"
    assert len(values["os_platform"]) > 0
    assert len(values["python_version"]) > 0


def test_render_prompt_variables() -> None:
    """验证模板字符串替换与未知占位符安全保留。"""
    template = (
        "你好，我是 {{agent_name}} (实例: {{agent_instance_id}})。\n"
        "工作目录: {{current_working_directory}}。\n"
        "可用工具: {{available_tools}}。\n"
        "未知占位符: {{unknown_variable_should_stay}}。"
    )

    variables = {
        "agent_name": "编程助手",
        "agent_instance_id": "inst-test",
        "current_working_directory": "/workspace/project",
        "available_tools": '["file_read", "file_write"]',
    }

    rendered = render_prompt_variables(template, variables)
    assert "你好，我是 编程助手 (实例: inst-test)。" in rendered
    assert "工作目录: /workspace/project。" in rendered
    assert '可用工具: ["file_read", "file_write"]。' in rendered
    assert "未知占位符: {{unknown_variable_should_stay}}。" in rendered
