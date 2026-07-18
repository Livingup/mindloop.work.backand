from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.conversation_message.message.models import UniMessage


@dataclass(frozen=True, slots=True)
class ContextCompressionWorkerInput:
    """上下文压缩 Worker 的输入载荷。

    当前仅支持 native_multimodal_history：把待压缩的原始会话消息作为 Worker
    历史消息，再追加一条压缩分析指令（user_text）。

    Worker 每次执行都会创建独立临时 Conversation。OpenAI Responses 协议的
    prompt_cache_key 应保持该 Worker Conversation 自己的默认会话 ID，不从被压缩
    的目标 Agent 会话继承缓存 key 或 relay session。
    """

    input_mode: str
    user_text: str
    history_messages: tuple[UniMessage, ...] = ()

    @property
    def uses_history_messages(self) -> bool:
        return bool(self.history_messages)
