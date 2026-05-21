from __future__ import annotations

import re
from pathlib import Path

from agent_runtime.context_compression.models import SECTION_WORKERS
from agent_runtime.context_compression.prompt_repository import (
    ContextCompressionWorkerPrompt,
    ContextCompressionWorkerPromptRepository,
)


_PROMPT_FILES = {
    "transcript_snapshot": "context_compression_worker_prompts_transcript_snapshot.md",
    "historical_conversation": "context_compression_worker_prompts_historical_conversation.md",
}
_WORKER_NAMES = tuple(worker_name for worker_name, _section in SECTION_WORKERS) + (
    "FinalAssemblerWorker",
    "FinalVerifierWorker",
)


class ContextCompressionPromptLoader:
    def __init__(
        self,
        *,
        prompt_repository: ContextCompressionWorkerPromptRepository | None = None,
        docs_dir: str | Path | None = None,
    ) -> None:
        self._prompt_repository = prompt_repository
        self._docs_dir = Path(docs_dir) if docs_dir is not None else Path(__file__).resolve().parents[3] / "docs"

    def load_prompt(self, *, input_mode: str, worker_name: str) -> str:
        default_prompt = load_default_worker_prompt(
            input_mode=input_mode,
            worker_name=worker_name,
            docs_dir=self._docs_dir,
        )
        if self._prompt_repository is None:
            prompt = default_prompt
        else:
            stored = self._prompt_repository.upsert_default(
                input_mode=input_mode,
                worker_name=worker_name,
                default_prompt_text=default_prompt,
            )
            prompt = stored.prompt_text
        return f"{prompt.strip()}\n\n{_TOOL_NAME_COMPATIBILITY_INSTRUCTION}"


def ensure_default_worker_prompts(
    prompt_repository: ContextCompressionWorkerPromptRepository,
    *,
    docs_dir: str | Path | None = None,
) -> list[ContextCompressionWorkerPrompt]:
    actual_docs_dir = Path(docs_dir) if docs_dir is not None else Path(__file__).resolve().parents[3] / "docs"
    prompts: list[ContextCompressionWorkerPrompt] = []
    for input_mode in _PROMPT_FILES:
        for worker_name in _WORKER_NAMES:
            default_prompt = load_default_worker_prompt(
                input_mode=input_mode,
                worker_name=worker_name,
                docs_dir=actual_docs_dir,
            )
            prompts.append(
                prompt_repository.upsert_default(
                    input_mode=input_mode,
                    worker_name=worker_name,
                    default_prompt_text=default_prompt,
                )
            )
    return prompts


def load_default_worker_prompt(
    *,
    input_mode: str,
    worker_name: str,
    docs_dir: str | Path,
) -> str:
    filename = _PROMPT_FILES.get(input_mode)
    if filename is None:
        raise ValueError(f"未知上下文压缩输入模式: {input_mode}")
    text = (Path(docs_dir) / filename).read_text(encoding="utf-8")
    return _extract_prompt(text, worker_name)


def _extract_prompt(markdown: str, worker_name: str) -> str:
    pattern = re.compile(rf"^##\s+{re.escape(worker_name)}\s*$\n\s*```text\n(?P<body>.*?)\n```", re.MULTILINE | re.DOTALL)
    match = pattern.search(markdown)
    if match is None:
        raise KeyError(f"未找到上下文压缩 Worker prompt: {worker_name}")
    return match.group("body").strip()


_TOOL_NAME_COMPATIBILITY_INSTRUCTION = """Runtime 工具名兼容说明：
- 设计文档中的 context_summary.append_section_items 在当前 LLM 工具 API 中暴露为 context_summary_append_section_items。
- 设计文档中的 context_summary.mark_section_complete 在当前 LLM 工具 API 中暴露为 context_summary_mark_section_complete。
- 设计文档中的 context_summary.append_final_markdown_section 在当前 LLM 工具 API 中暴露为 context_summary_append_final_markdown_section。
- 设计文档中的 context_summary.mark_final_complete 在当前 LLM 工具 API 中暴露为 context_summary_mark_final_complete。
- 设计文档中的 context_summary.report_section_issue 在当前 LLM 工具 API 中暴露为 context_summary_report_section_issue。
请调用当前实际暴露的工具名，不要在 assistant 正文中输出压缩结果。"""
