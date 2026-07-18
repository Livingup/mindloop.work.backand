from __future__ import annotations

from pathlib import Path


_PROMPT_DIR = Path(__file__).resolve().parent / "builtin_prompts" / "task_resume_v2"

_WORKER_FILES: dict[str, str] = {
    "TaskRequirementsRemainingGoalsWorker": "01-task-requirements-and-remaining-goals-agent.md",
    "TaskStateExecutionPlanWorker": "worker-01-task-state-and-execution-plan-agent.md",
    "SolutionDesignDecisionsWorker": "worker-02-solution-design-and-key-decisions-agent.md",
    "KeyKnowledgeFindingsWorker": "worker-03-key-knowledge-and-findings-agent.md",
    "AgentCollabStatefulResourcesWorker": "worker-04-multi-agent-collaboration-and-active-resources-agent.md",
    "ExistingWorkArtifactsWorker": "worker-05-existing-work-results-and-artifacts-agent.md",
}


def load_task_resume_v2_prompt(worker_name: str) -> str:
    filename = _WORKER_FILES.get(worker_name)
    if filename is None:
        raise KeyError(f"task_resume_v2 未知 worker prompt: {worker_name}")
    path = _PROMPT_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"内置 prompt 文件缺失: {path}")
    text = path.read_text(encoding="utf-8")
    return _sanitize_worker_prompt(text).strip()


def list_task_resume_v2_worker_names() -> tuple[str, ...]:
    return tuple(_WORKER_FILES.keys())


def _sanitize_worker_prompt(text: str) -> str:
    """清洗会诱导 Worker 关心最终拼接的措辞，拼接由系统负责。"""
    replacements = (
        ("作为最终压缩上下文的第 1 部分，与其余 5 个 Worker 的输出直接拼接后交还目标 Agent 继续执行。", "输出会被系统纳入最终压缩上下文，供目标 Agent 继续执行。"),
        ("最终系统只会按照固定顺序直接拼接这 6 份输出，不会再让其他 Agent 对其自由总结或改写。因此，你必须独立保证第 1 部分完整、准确且可直接使用。", "系统会汇总各角色输出。因此你必须独立保证自己负责的内容完整、准确且可直接使用。"),
        ("最终系统只会直接拼接各角色输出，不会再次总结。因此，你必须保证计划与状态准确、紧凑、可直接续用。", "系统会汇总各角色输出。因此你必须保证计划与状态准确、紧凑、可直接续用。"),
        ("最终系统只会拼接各 Agent 的输出，不会再进行二次总结。因此，你必须保证自己负责的方案与决策内容完整、准确、自包含、可直接续用。", "系统会汇总各角色输出。因此你必须保证自己负责的方案与决策内容完整、准确、自包含、可直接续用。"),
        ("最终系统只会直接拼接各 Agent 的输出，不会再进行二次总结。因此，你必须保证关键任务知识完整、准确、自包含，并尽量让目标 Agent 无需重复读取即可继续工作。", "系统会汇总各角色输出。因此你必须保证关键任务知识完整、准确、自包含，并尽量让目标 Agent 无需重复读取即可继续工作。"),
        ("最终系统只会直接拼接各角色输出，不会再次总结。因此，你的输出必须使目标 Agent 在压缩后能够直接接续已有协作关系和有状态资源。", "系统会汇总各角色输出。因此你的输出必须使目标 Agent 在压缩后能够直接接续已有协作关系和有状态资源。"),
        ("完整压缩上下文由以下 6 个部分组成：", "完整压缩上下文由多个职责部分组成，你只负责其中你的角色部分："),
        ("完整压缩结果由以下 6 部分直接拼接：", "完整压缩结果由多个职责部分组成，你只负责其中你的角色部分："),
        ("完整压缩上下文由以下 6 部分直接拼接：", "完整压缩上下文由多个职责部分组成，你只负责其中你的角色部分："),
        ("你只负责最终压缩上下文中的第 1 部分：", "你只负责："),
        ("只负责最终压缩上下文第 2 部分：", "只负责："),
        ("只负责最终压缩上下文第 3 部分：", "只负责："),
        ("只负责最终压缩上下文第 4 部分：", "只负责："),
        ("只负责最终压缩上下文第 5 部分：", "只负责："),
        ("只负责最终压缩上下文第 6 部分：", "只负责："),
        ("你负责最终压缩上下文第 2 部分：", "你负责："),
        ("你负责。", "你负责本角色对应内容。"),
    )
    cleaned = text
    for old, new in replacements:
        cleaned = cleaned.replace(old, new)
    # 输出工具协议补充：这些 md 原文未强制 tool 协议时，追加统一约束。
    if "context_summary" not in cleaned:
        cleaned = (
            f"{cleaned.rstrip()}\n\n"
            "## Runtime 产出协议\n"
            "请只通过 context_summary 工具增量写入你负责的内容，写完后调用 mark_section_complete。\n"
            "不要在 assistant 正文中输出最终压缩结果，也不要关心系统如何汇总其他角色的输出。\n"
        )
    return cleaned

