from __future__ import annotations

from agent_runtime.context_compression.default_prompts import get_default_worker_prompt
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

    def load_prompt(
        self,
        *,
        worker_name: str,
        input_mode: str | None = None,
        profile_id: str | None = None,
    ) -> str:
        from agent_runtime.context_compression.profiles import (
            DEFAULT_ACTIVE_PROFILE_ID,
            prompt_storage_keys_for_profile,
        )

        resolved_profile = profile_id or DEFAULT_ACTIVE_PROFILE_ID
        default_prompt = load_default_worker_prompt(
            input_mode=input_mode,
            profile_id=resolved_profile,
            worker_name=worker_name,
        )
        if self._prompt_repository is None:
            prompt = default_prompt
        else:
            override = None
            for storage_key in prompt_storage_keys_for_profile(resolved_profile):
                override = self._prompt_repository.get_prompt(
                    input_mode=storage_key,
                    worker_name=worker_name,
                )
                if override is not None:
                    break
            prompt = override.prompt_text if override is not None else default_prompt
        return f"{prompt.strip()}\n\n{_TOOL_NAME_COMPATIBILITY_INSTRUCTION}"


def ensure_default_worker_prompts(
    prompt_repository: ContextCompressionWorkerPromptRepository,
) -> list[ContextCompressionWorkerPrompt]:
    """保留兼容入口，但不再把代码内置默认 prompt 写入持久化存储。"""
    return prompt_repository.list_prompts()


def load_default_worker_prompt(
    *,
    worker_name: str,
    input_mode: str | None = None,
    profile_id: str | None = None,
) -> str:
    return get_default_worker_prompt(
        input_mode=input_mode,
        profile_id=profile_id,
        worker_name=worker_name,
    )


_TOOL_NAME_COMPATIBILITY_INSTRUCTION = """Runtime 工具名兼容说明：
- 设计文档中的 context_summary.append_section_items 在当前 LLM 工具 API 中暴露为 context_summary_append_section_items。
- 设计文档中的 context_summary.mark_section_complete 在当前 LLM 工具 API 中暴露为 context_summary_mark_section_complete。
- 设计文档中的 context_summary.append_final_markdown_section 在当前 LLM 工具 API 中暴露为 context_summary_append_final_markdown_section。
- 设计文档中的 context_summary.read_draft 在当前 LLM 工具 API 中暴露为 context_summary_read_draft。
- 设计文档中的 context_summary.replace_draft_lines 在当前 LLM 工具 API 中暴露为 context_summary_replace_draft_lines。
- 设计文档中的 context_summary.mark_final_complete 在当前 LLM 工具 API 中暴露为 context_summary_mark_final_complete。
- 设计文档中的 context_summary.report_section_issue 在当前 LLM 工具 API 中暴露为 context_summary_report_section_issue。
请调用当前实际暴露的工具名，不要在 assistant 正文中输出压缩结果。"""
