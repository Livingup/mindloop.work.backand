# -*- coding: utf-8 -*-
"""Agent 实例执行过程 Markdown Trace 记录器。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_runtime.conversation_message.message.models import (
    TextPart,
    ThinkingPart,
    ToolResultPart,
    ToolUsePart,
    UniMessage,
)


_TRACE_FILE_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class AgentTraceIdentity:
    """Agent 实例 Trace 文件头所需的运行态身份信息。"""

    # 任务唯一标识。
    task_id: str

    # Agent 运行时实例唯一标识。
    instance_id: str

    # 所属线程标识。
    thread_id: str

    # 静态 Agent 定义 ID。Trace 只记录定义 ID，不记录系统提示词内容。
    agent_definition_id: str

    # 本实例对应的 trace_id，用于和运行日志关联。
    trace_id: str


class AgentTraceRecorder:
    """按 Agent 实例记录执行过程的 Markdown Trace。

    Trace 只记录 Agent 定义 ID 和对话过程详情，刻意不记录系统提示词、工具 schema、工具清单、
    API Key、请求头等敏感或冗余信息。每个 Agent 实例对应一个独立 Markdown 文件，方便人工排障。
    """

    def __init__(self, trace_dir_accessor: Any) -> None:
        # Trace 文件根目录访问器。通过 Callable 实时读取 ConfigRegistry，支持配置热更新后切换目录。
        self._trace_dir_accessor = trace_dir_accessor

    def start_trace(self, identity: AgentTraceIdentity) -> Path:
        """创建 Agent 实例 Trace 文件并写入基础身份信息。"""
        trace_path = self._trace_path(identity)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        now = _utc_now_text()
        content = [
            f"# Agent Trace: {identity.agent_definition_id}",
            f"**Time**: {now}",
            f"**Task ID**: `{identity.task_id}`",
            f"**Instance ID**: `{identity.instance_id}`",
            f"**Thread ID**: `{identity.thread_id}`",
            f"**Trace ID**: `{identity.trace_id}`",
            f"**Agent Definition ID**: `{identity.agent_definition_id}`",
            "",
            "> 本 Trace 只记录 Agent 定义 ID 与对话执行过程详情；不记录系统提示词和工具定义。",
            "",
            "## Execution Events",
            "",
            f"### {now} - instance_started",
            "Agent 实例开始执行。",
            "",
        ]
        trace_path.write_text("\n".join(content), encoding="utf-8")
        return trace_path

    def record_user_message(self, trace_path: Path, message: UniMessage) -> None:
        """追加用户输入消息。"""
        self._append_section(trace_path, "user_message", _format_message(message))

    def record_llm_response(self, trace_path: Path, message: UniMessage) -> None:
        """追加 LLM assistant 响应消息。"""
        self._append_section(trace_path, "assistant_message", _format_message(message))

    def record_tool_result(self, trace_path: Path, message: UniMessage) -> None:
        """追加工具执行结果消息。"""
        self._append_section(trace_path, "tool_result", _format_message(message))

    def record_event(self, trace_path: Path, event_name: str, details: Any = None) -> None:
        """追加普通执行事件。"""
        body = "" if details is None else _format_value(details)
        self._append_section(trace_path, event_name, body)

    def record_failure(self, trace_path: Path, failure_message: str) -> None:
        """追加失败事件。"""
        self._append_section(trace_path, "instance_failed", failure_message)

    def record_completed(self, trace_path: Path) -> None:
        """追加完成事件。"""
        self._append_section(trace_path, "instance_completed", "Agent 实例执行完成。")

    def _append_section(self, trace_path: Path, title: str, body: str) -> None:
        """以 Markdown 小节形式追加执行过程。"""
        now = _utc_now_text()
        with open(trace_path, "a", encoding="utf-8") as file:
            file.write(f"\n### {now} - {title}\n")
            if body:
                file.write(body.rstrip() + "\n")
            file.write("\n")

    def _trace_path(self, identity: AgentTraceIdentity) -> Path:
        """根据 Agent 实例身份生成 Trace 文件路径。"""
        root = Path(str(self._trace_dir_accessor()))
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_agent_id = _safe_file_part(identity.agent_definition_id)
        safe_trace_id = _safe_file_part(identity.trace_id)
        suffix = uuid4().hex[:8]
        return root / f"{safe_agent_id}_{timestamp}_{safe_trace_id}_{suffix}.md"


def _format_message(message: UniMessage) -> str:
    """将统一消息格式化为 Markdown，避免记录 provider raw 大对象。"""
    lines = [f"**role**: `{message.role}`"]
    if message.source_provider:
        lines.append(f"**source_provider**: `{message.source_provider}`")
    if message.source_vendor:
        lines.append(f"**source_vendor**: `{message.source_vendor}`")
    lines.append("")
    for index, part in enumerate(message.content, start=1):
        lines.append(f"#### Part {index}: `{getattr(part, 'kind', 'unknown')}`")
        lines.append(_format_part(part))
        lines.append("")
    return "\n".join(lines)


def _format_part(part: Any) -> str:
    """格式化单个消息内容块。"""
    if isinstance(part, TextPart):
        return part.text
    if isinstance(part, ThinkingPart):
        return part.text or ""
    if isinstance(part, ToolUsePart):
        return _format_value({"name": part.name, "input": part.input})
    if isinstance(part, ToolResultPart):
        return _format_value({"tool_use_ref": part.tool_use_ref, "output": part.output})
    resource_id = getattr(part, "resource_id", None)
    if resource_id is not None:
        return f"resource_id: `{resource_id}`"
    return _format_value(part)


def _format_value(value: Any) -> str:
    """将任意结构化值格式化为 Markdown 代码块。"""
    if isinstance(value, str):
        return value
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        text = str(value)
    return "```json\n" + text + "\n```"


def _safe_file_part(value: str) -> str:
    """把业务标识转换为安全文件名片段。"""
    normalized = _TRACE_FILE_SAFE_PATTERN.sub("_", value).strip("._-")
    return normalized or "agent"


def _utc_now_text() -> str:
    """返回 ISO 格式 UTC 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()
