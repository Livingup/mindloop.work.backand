from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.conversation_message.message.models import UniMessage


@dataclass(frozen=True, slots=True)
class ContextCompressionWorkerInput:
    """上下文压缩 Worker 的输入载荷。

    transcript_snapshot 模式只传递一条文本 user 消息；native_multimodal_history
    模式会把待压缩的原始会话消息作为 Worker 历史消息，再追加一条压缩分析指令。
    当原生历史来源于 OpenAI Responses 会话时，可携带源会话的 prompt_cache_key，
    让 Worker 复用同一历史前缀缓存，但不复用源会话 relay session。
    """

    input_mode: str
    user_text: str
    history_messages: tuple[UniMessage, ...] = ()
    openai_responses_prompt_cache_key: str | None = None

    @property
    def uses_history_messages(self) -> bool:
        return bool(self.history_messages)
