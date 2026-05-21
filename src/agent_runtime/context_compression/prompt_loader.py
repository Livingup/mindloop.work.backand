from __future__ import annotations

from agent_runtime.context_compression.default_prompts import (
    get_default_worker_prompt,
    list_default_worker_prompt_keys,
)
from agent_runtime.context_compression.prompt_repository import (
    ContextCompressionWorkerPrompt,
    ContextCompressionWorkerPromptRepository,
)


class ContextCompressionPromptLoader:
    def __init__(
        self,
        *,
        prompt_repository: ContextCompressionWorkerPromptRepository | None = None,
    ) -> None:
        self._prompt_repository = prompt_repository

    def load_prompt(self, *, input_mode: str, worker_name: str) -> str:
        default_prompt = load_default_worker_prompt(
            input_mode=input_mode,
            worker_name=worker_name,
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
) -> list[ContextCompressionWorkerPrompt]:
    prompts: list[ContextCompressionWorkerPrompt] = []
    for input_mode, worker_name in list_default_worker_prompt_keys():
        default_prompt = load_default_worker_prompt(
            input_mode=input_mode,
            worker_name=worker_name,
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
) -> str:
    return get_default_worker_prompt(input_mode=input_mode, worker_name=worker_name)


_TOOL_NAME_COMPATIBILITY_INSTRUCTION = """Runtime 工具名兼容说明：
- 设计文档中的 context_summary.append_section_items 在当前 LLM 工具 API 中暴露为 context_summary_append_section_items。
- 设计文档中的 context_summary.mark_section_complete 在当前 LLM 工具 API 中暴露为 context_summary_mark_section_complete。
- 设计文档中的 context_summary.append_final_markdown_section 在当前 LLM 工具 API 中暴露为 context_summary_append_final_markdown_section。
- 设计文档中的 context_summary.mark_final_complete 在当前 LLM 工具 API 中暴露为 context_summary_mark_final_complete。
- 设计文档中的 context_summary.report_section_issue 在当前 LLM 工具 API 中暴露为 context_summary_report_section_issue。
请调用当前实际暴露的工具名，不要在 assistant 正文中输出压缩结果。"""
