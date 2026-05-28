from __future__ import annotations

import json
from typing import Any

from agent_runtime.context_compression.job_store import InMemoryCompressionJobStore
from agent_runtime.conversation_message.message.models import ToolResultPart, ToolUsePart
from agent_runtime.conversation_message.message.tool_result_output import normalize_tool_result_output
from agent_runtime.llm_api_protocol import (
    API_FORMAT_ANTHROPIC_MESSAGES,
    API_FORMAT_GEMINI_GENERATE_CONTENT,
    API_FORMAT_OPENAI_CHAT_COMPLETIONS,
    API_FORMAT_OPENAI_RESPONSES,
    API_FORMAT_ZHIPU_CHAT_COMPLETIONS,
)
from agent_runtime.llm_client.llm_request_models import LlmToolDefinition

APPEND_SECTION_ITEMS = "context_summary_append_section_items"
MARK_SECTION_COMPLETE = "context_summary_mark_section_complete"
APPEND_FINAL_MARKDOWN_SECTION = "context_summary_append_final_markdown_section"
MARK_FINAL_COMPLETE = "context_summary_mark_final_complete"
REPORT_SECTION_ISSUE = "context_summary_report_section_issue"


class ContextSummaryToolExecutor:
    def __init__(self, job_store: InMemoryCompressionJobStore) -> None:
        self._job_store = job_store

    def build_tools(
        self,
        api_format: str,
        *,
        allowed_tool_names: set[str] | None = None,
    ) -> tuple[LlmToolDefinition, ...]:
        return tuple(
            LlmToolDefinition(name=name, schema=_schema_for(api_format, name, description, parameters), source="context_summary")
            for name, description, parameters in _TOOL_DEFINITIONS
            if allowed_tool_names is None or name in allowed_tool_names
        )

    def execute(
        self,
        *,
        job_id: str,
        worker_name: str,
        expected_section: str | None = None,
        tool_use: ToolUsePart,
        allowed_tool_names: set[str] | None = None,
    ) -> ToolResultPart:
        try:
            if allowed_tool_names is not None and tool_use.name not in allowed_tool_names:
                raise PermissionError(f"当前 Worker 不允许调用上下文压缩工具: {tool_use.name}")
            result = self._execute(job_id=job_id, worker_name=worker_name, expected_section=expected_section, tool_use=tool_use)
            return ToolResultPart(tool_use_ref=tool_use.tool_use_id or tool_use.name, is_error=False, output=normalize_tool_result_output(result))
        except Exception as exc:
            return ToolResultPart(
                tool_use_ref=tool_use.tool_use_id or tool_use.name,
                is_error=True,
                output=normalize_tool_result_output({"error_type": exc.__class__.__name__, "error_message": str(exc)}),
            )

    def _execute(self, *, job_id: str, worker_name: str, expected_section: str | None, tool_use: ToolUsePart) -> dict[str, Any]:
        arguments = _ensure_dict(tool_use.input)
        if tool_use.name == APPEND_SECTION_ITEMS:
            section = str(arguments.get("section") or "").strip()
            items = arguments.get("items")
            if not section:
                raise ValueError("section 必须是非空字符串。")
            if not isinstance(items, list):
                raise ValueError("items 必须是数组。")
            total = self._job_store.append_section_items(job_id, worker_name=worker_name, section=section, expected_section=expected_section, items=items)
            return {"success": True, "total_section_items": total}
        if tool_use.name == MARK_SECTION_COMPLETE:
            section = str(arguments.get("section") or "").strip()
            if not section:
                raise ValueError("section 必须是非空字符串。")
            completed = self._job_store.mark_section_complete(job_id, section, expected_section=expected_section)
            return {"success": True, "completed_section_count": completed}
        if tool_use.name == APPEND_FINAL_MARKDOWN_SECTION:
            markdown = str(arguments.get("markdown") or "").strip()
            if not markdown:
                raise ValueError("markdown 必须是非空字符串。")
            chunks = self._job_store.append_final_markdown(job_id, markdown)
            return {"success": True, "final_markdown_chunks": chunks}
        if tool_use.name == MARK_FINAL_COMPLETE:
            self._job_store.update_status(job_id, "assembled")
            return {"success": True, "status": "assembled"}
        if tool_use.name == REPORT_SECTION_ISSUE:
            issue = dict(arguments)
            issue_count = self._job_store.report_issue(job_id, issue)
            return {"success": True, "issue_count": issue_count}
        raise ValueError(f"不支持的上下文压缩工具: {tool_use.name}")


_TOOL_DEFINITIONS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        APPEND_SECTION_ITEMS,
        "对应 context_summary.append_section_items。只能把上下文压缩条目写入当前压缩 job，不会读写业务文件、终端、MCP 或主 Conversation。section 必须是非空字符串；items 必须是数组；每个 item.content 必须是非空字符串；priority 只能用 P0/P1/P2；status 用 active/superseded/resolved/unknown；resource_references 必须保存 Agent 可再读取定位信息，不要只写内部 resource_id。",
        {
            "type": "object",
            "properties": {
                "section": {"type": "string", "description": "目标章节名，必须是非空字符串，例如 requirements_constraints、progress_plan、code_architecture、observations_evidence、decisions_rationale、todos_risks。"},
                "items": {
                    "type": "array",
                    "description": "要追加的压缩条目数组，至少包含一个对象。每个对象的 content 必须是非空字符串；priority 只能用 P0、P1 或 P2；status 用 active、superseded、resolved 或 unknown；涉及文件、URL、URI、附件、图片、PDF、trace、终端或代码符号时必须在 resource_references 中写 Agent 可再读取定位信息。",
                    "items": {"type": "object"},
                },
            },
            "required": ["section", "items"],
        },
    ),
    (
        MARK_SECTION_COMPLETE,
        "对应 context_summary.mark_section_complete。声明当前 Worker 负责的 section 已完成。section 必须是非空字符串，且应与本 Worker 要输出的章节一致。",
        {"type": "object", "properties": {"section": {"type": "string", "description": "已完成章节名，必须是非空字符串。"}}, "required": ["section"]},
    ),
    (
        APPEND_FINAL_MARKDOWN_SECTION,
        "对应 context_summary.append_final_markdown_section。只能把最终 Runtime Compressed Task Context Markdown 的一个章节或片段写入当前压缩 job。markdown 必须是非空 Markdown 文本，必须包含 Agent 可再读取资源引用，不能写入主 Conversation。",
        {"type": "object", "properties": {"markdown": {"type": "string", "description": "最终压缩 Markdown 的一个章节或片段，必须是非空字符串。"}}, "required": ["markdown"]},
    ),
    (
        MARK_FINAL_COMPLETE,
        "对应 context_summary.mark_final_complete。声明最终 Markdown 已经全部写入。无需参数。",
        {"type": "object", "properties": {}},
    ),
    (
        REPORT_SECTION_ISSUE,
        "对应 context_summary.report_section_issue。报告压缩或校验问题，只写入当前压缩 job。severity 必须说明 P0/P1/P2 或 blocker；message 必须是非空字符串；如果有来源，应带 source_message_ranges 或 resource_references。",
        {"type": "object", "properties": {"severity": {"type": "string", "description": "问题级别，必须说明 P0/P1/P2 或 blocker。"}, "message": {"type": "string", "description": "问题说明，必须是非空字符串。"}}, "required": ["message"]},
    ),
)


def _schema_for(api_format: str, name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    if api_format == API_FORMAT_OPENAI_RESPONSES:
        return {"type": "function", "name": name, "description": description, "parameters": parameters, "strict": False}
    if api_format in {API_FORMAT_OPENAI_CHAT_COMPLETIONS, API_FORMAT_ZHIPU_CHAT_COMPLETIONS}:
        return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}
    if api_format == API_FORMAT_ANTHROPIC_MESSAGES:
        return {"name": name, "description": description, "input_schema": parameters}
    if api_format == API_FORMAT_GEMINI_GENERATE_CONTENT:
        return {"name": name, "description": description, "parameters": parameters}
    raise ValueError(f"上下文压缩工具暂不支持 api_format={api_format}")


def _ensure_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("工具参数必须是 JSON 对象。")
