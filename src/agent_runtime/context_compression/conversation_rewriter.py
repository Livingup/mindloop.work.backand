from __future__ import annotations

from agent_runtime.context_compression.models import CompressionJob, CompressionRange
from agent_runtime.conversation_message.message.models import Conversation, TextPart, TextPartMeta, UniMessage, ROLE_USER
from agent_runtime.conversation_message.message.operations import create_conversation


def rewrite_conversation_with_summary(
    *,
    source_conversation: Conversation,
    job: CompressionJob,
    range_plan: CompressionRange,
    final_markdown: str,
) -> tuple[Conversation, str]:
    target_conversation = create_conversation()
    summary_message_id = f"summary-{job.job_id}"
    summary_message = UniMessage(
        role=ROLE_USER,
        content=[TextPart(text=_build_summary_markdown(job, range_plan, final_markdown))],
        provider_extras={
            "context_compression": {
                "summary_message_id": summary_message_id,
                "archive_job_id": job.job_id,
                "job_id": job.job_id,
                "root_conversation_id": job.root_conversation_id,
                "source_conversation_id": job.source_conversation_id,
                "target_conversation_id": target_conversation.id,
                "compression_index": job.compression_index,
                "compressed_range": range_plan.compressed_range_payload(),
                "retained_tail_range": range_plan.retained_tail_range_payload(),
            }
        },
    )
    target_conversation.append_message(summary_message, [TextPartMeta()])
    for message in source_conversation.messages[range_plan.retained_tail_start_index:]:
        target_conversation.append_message(message)
    target_conversation.provider_state = {}
    target_conversation.state.last_context_usage = None
    return target_conversation, summary_message_id


def _build_summary_markdown(job: CompressionJob, range_plan: CompressionRange, final_markdown: str) -> str:
    del job, range_plan  # internal compression metadata stays in provider_extras, not visible Markdown
    usage_contract = "\n".join([
        "# Runtime Compressed Task Context",
        "",
        "## Usage Contract",
        "- This Markdown was generated automatically by AgentRuntime context compression.",
        "- It is historical task context, not a new user task.",
        "- If this summary conflicts with later retained raw tail messages, the retained raw tail messages take precedence.",
        "",
    ])
    return f"{usage_contract}{final_markdown.strip()}\n"
