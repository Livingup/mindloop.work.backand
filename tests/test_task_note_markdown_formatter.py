# -*- coding: utf-8 -*-
import json
import pytest
from pathlib import Path

from agent_runtime.task_note.task_note_markdown_formatter import (
    reverse_lookup_source_code,
    format_task_note_to_markdown,
)
from agent_runtime.task_note.task_note_prompt_provider import TaskNotePromptProvider


def test_reverse_lookup_source_code_with_real_file(tmp_path: Path):
    """测试源码行号反查：正常读取文件并带 1-based 行号格式化代码块。"""
    sample_py = tmp_path / "sample.py"
    sample_py.write_text(
        "def add(a, b):\n"
        "    \"\"\"Add two numbers.\"\"\"\n"
        "    return a + b\n"
        "\n"
        "def sub(a, b):\n"
        "    return a - b\n",
        encoding="utf-8",
    )

    # 1. 单个区间
    snippet = reverse_lookup_source_code(
        path=str(sample_py),
        line_ranges=[{"start_line": 1, "end_line": 3}],
        current_working_directory=str(tmp_path),
    )
    assert snippet is not None
    assert "```python" in snippet
    assert "   1 | def add(a, b):" in snippet
    assert "   2 |     \"\"\"Add two numbers.\"\"\"" in snippet
    assert "   3 |     return a + b" in snippet
    assert "sub" not in snippet

    # 2. 相对路径
    snippet_rel = reverse_lookup_source_code(
        path="sample.py",
        line_ranges='[{"start_line": 5, "end_line": 6}]',
        current_working_directory=str(tmp_path),
    )
    assert snippet_rel is not None
    assert "   5 | def sub(a, b):" in snippet_rel
    assert "   6 |     return a - b" in snippet_rel


def test_reverse_lookup_source_code_missing_file():
    """测试文件不存在时优雅回退，不抛出异常。"""
    snippet = reverse_lookup_source_code(
        path="non_existent_file.py",
        line_ranges=[{"start_line": 10, "end_line": 20}],
    )
    assert snippet is not None
    assert "non_existent_file.py" in snippet
    assert "暂未在磁盘找到" in snippet


def test_format_task_note_to_markdown_comprehensive(tmp_path: Path):
    """综合测试 Note 转换为 Markdown 现场手记：命令行反查、父子与远程对话流水、待办、进展等。"""
    code_file = tmp_path / "server.py"
    code_file.write_text(
        "import sys\n"
        "def main():\n"
        "    print('hello world')\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )

    snapshot = {
        "task_id": "task_audit_999",
        "agent_instance_id": "inst_lead_01",
        "missions": [
            {
                "mission_id": "m_001",
                "title": "重构 Note 格式化与反查模块",
                "status": "in_progress",
                "refined_requirement": "支持源码行号反查与命令行执行 ID 反查",
                "raw_prompts": ["请优化切换回业务状态时的 Note 上下文格式", "不要倾倒 json"],
            }
        ],
        "progress": {
            "next_step": "执行 pytest 验证全量 Note 流程闭环",
            "completed_summary": "已完成数据库表调整与反查函数编写",
            "current_work": "正在运行单元测试",
            "current_blockers": "无",
            "recent_validation": "SQL 单元测试通过",
            "handoff_notes": "注意检查 command_id 是否正确标注为命令行执行 ID",
        },
        "todos": [
            {"todo_id": "t_01", "priority": 1, "todo_content": "删除 mission_id 字段", "status": "completed"},
            {"todo_id": "t_02", "priority": 1, "todo_content": "实现 Markdown 格式化", "status": "in_progress"},
            {"todo_id": "t_03", "priority": 2, "todo_content": "编写验证测试", "status": "pending"},
            {"todo_id": "t_04", "priority": 3, "todo_content": "废弃旧脚本", "status": "cancelled"},
        ],
        "solution": {
            "solution_content": "### 整体方案\n采用 Markdown 格式组织现场工作手记，辅以磁盘读取与命令反查字典。",
        },
        "decisions": [
            {
                "decision_id": "dec_01",
                "decision_content": "task_command_records 作为纯技术辅助表，不带 mission_id",
                "rationale": "节约 token 并建立唯一命令执行映射",
                "decision_status": "active",
            }
        ],
        "workspace_context": [
            {
                "context_id": "ctx_01",
                "source_type": "file",
                "description": "服务端主入口",
                "path": str(code_file),
                "line_ranges": [{"start_line": 2, "end_line": 3}],
                "command_id": "cmd_exec_001",
            }
        ],
        "discoveries": [
            {
                "discovery_id": "disc_01",
                "discovery_content": "发现终端工具执行失败时也需返回 command_id",
                "impact": "保证后续无论成功失败都能精确反查命令",
                "command_id": "cmd_exec_002",
            }
        ],
        "issues": [
            {
                "issue_id": "iss_01",
                "issue_content": "原先输出了原始数据库 JSON",
                "status": "resolved",
                "resolution": "使用 TaskNoteMarkdownFormatter 重新格式化",
            }
        ],
        "validations": [
            {
                "validation_id": "val_01",
                "validation_scope": "数据库结构与反查测试",
                "conclusion": "passed",
                "validation_content": "5 项测试用例全部通过",
                "command_id": "cmd_exec_003",
            }
        ],
        "artifacts": [
            {"artifact_id": "art_01", "type": "file", "path": "src/server.py", "description": "业务服务器入口"}
        ],
        "external_states": [
            {
                "external_state_id": "ext_01",
                "target_type": "database",
                "target_identifier": "agent_runtime",
                "change_content": "删除 task_command_records.mission_id 列",
                "current_state": "列已删除，索引已更新",
            }
        ],
        "agent_relationships": [
            {
                "relationship_id": "rel_01",
                "target_agent_type": "child",
                "target_agent_instance_id": "subagent_tester_01",
                "status": "unreleased",
            }
        ],
        "agent_messages": [
            {
                "message_id": "msg_01",
                "target_agent_instance_id": "subagent_tester_01",
                "message_type": "send",
                "message": "请协助运行端到端测试",
                "created_at": "2026-09-11 10:00:00",
            },
            {
                "message_id": "msg_02",
                "target_agent_instance_id": "subagent_tester_01",
                "message_type": "receive",
                "message": "已完成测试，覆盖率 98%",
                "created_at": "2026-09-11 10:00:10",
            },
        ],
        "remote_agent_messages": [
            {
                "remote_message_id": "rmsg_01",
                "remote_agent_instance_id": "remote_agent_chunjing_99",
                "message_type": "send",
                "message": "请求协同调度资源",
                "created_at": "2026-09-11 10:05:00",
            },
            {
                "remote_message_id": "rmsg_02",
                "remote_agent_instance_id": "remote_agent_chunjing_99",
                "message_type": "receive",
                "message": "已分配节点计算资源",
                "created_at": "2026-09-11 10:05:15",
            },
        ],
    }

    command_lookup = {
        "cmd_exec_001": "python server.py --port 8000",
        "cmd_exec_002": "grep -rn 'task_command_records' .",
        "cmd_exec_003": "pytest tests/test_task_note_store.py",
    }

    md = format_task_note_to_markdown(
        snapshot,
        command_lookup=command_lookup,
        current_working_directory=str(tmp_path),
    )

    # 1. 验证行动指针与进展 (Next Step 置顶且醒目)
    assert "## 1. 现场进度与下一步行动指针 (Progress & Next Action)" in md
    assert "下一步明确行动 (Next Step)" in md
    assert "执行 pytest 验证全量 Note 流程闭环" in md
    assert "已完成数据库表调整与反查函数编写" in md

    # 2. 验证 Todos 复选框
    assert "## 2. 任务待办清单 (Todos)" in md
    assert "- [x] [P1] 删除 mission_id 字段" in md
    assert "- [/] [P1] 实现 Markdown 格式化 ⏳ *(进行中)*" in md
    assert "- [ ] [P2] 编写验证测试" in md
    assert "- [ ] ~~[P3] 废弃旧脚本 (已取消)~~" in md

    # 3. 验证命令行执行反查（必须是命令行执行 ID，且包含实际命令行）
    assert "**命令行执行 ID**: `cmd_exec_001` ➔ **执行命令**: `python server.py --port 8000`" in md
    assert "**命令行执行 ID**: `cmd_exec_002` ➔ **执行命令**: `grep -rn 'task_command_records' .`" in md
    assert "**命令行执行 ID**: `cmd_exec_003` ➔ **执行命令**: `pytest tests/test_task_note_store.py`" in md

    # 4. 验证源码行号反查
    assert "def main():" in md
    assert "print('hello world')" in md
    assert "L2-L3" in md or "2-3" in md

    # 5. 验证子 Agent 协作与专线通信流
    assert "### 协作子 Agent: `subagent_tester_01`" in md
    assert "📤 **[发往子 Agent]**: 请协助运行端到端测试" in md
    assert "📥 **[收到子 Agent 回复]**: 已完成测试，覆盖率 98%" in md

    # 6. 验证远程 Agent 通信记录
    assert "### 远程对等 Agent 实例 ID: `remote_agent_chunjing_99`" in md
    assert "📤 **[发往远程 Agent]**: 请求协同调度资源" in md
    assert "📥 **[接收自远程 Agent]**: 已分配节点计算资源" in md


def test_prompt_provider_excludes_commands_from_boundary_prompt():
    """测试触发压缩时，Note Agent 看到的基线快照不包含 commands 辅助表。"""
    provider = TaskNotePromptProvider()
    snapshot = {
        "task_id": "t1",
        "agent_instance_id": "a1",
        "missions": [{"mission_id": "m1", "title": "任务1"}],
        "commands": [
            {"command_id": "cmd_01", "command_line": "echo hello"},
            {"command_id": "cmd_02", "command_line": "ls -l"},
        ],
    }

    boundary = provider.build_compression_boundary_prompt(snapshot)
    assert "任务1" in boundary
    # commands 辅助表应该被剔除，不出现在给 Note Agent 的快照中
    assert "cmd_01" not in boundary
    assert "echo hello" not in boundary


def test_prompt_provider_build_resumed_conversation_prompt_markdown():
    """测试 Note 更新完成后切回业务 Agent，返回规范的 Markdown 工作手记。"""
    provider = TaskNotePromptProvider()
    snapshot = {
        "task_id": "t_prod_01",
        "agent_instance_id": "a_prod_01",
        "progress": {"next_step": "启动服务并验证端口"},
        "commands": [{"command_id": "c1", "command_line": "uvicorn main:app --port 8000"}],
        "validations": [{"validation_id": "v1", "validation_scope": "服务端口", "command_id": "c1", "conclusion": "passed", "validation_content": "8000 端口响应 200"}],
    }

    resumed = provider.build_resumed_conversation_prompt(snapshot)
    assert "系统已完成上下文压缩与Note更新" in resumed
    assert "以下是最新任务现场工作手记快照" in resumed
    assert "# 任务现场工作手记 (Task Work Note)" in resumed
    assert "启动服务并验证端口" in resumed
    assert "**命令行执行 ID**: `c1` ➔ **执行命令**: `uvicorn main:app --port 8000`" in resumed
    # 确认没有未反查的生硬 JSON table 倾倒
    assert "{\n  \"task_id\"" not in resumed
