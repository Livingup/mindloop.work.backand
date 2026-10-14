"""统一历史对话消息子模块。

该子模块是统一 LLM 调用服务中的消息领域基础层，负责：
- 对话消息的存储、归一化、渲染
- 多 API 协议格式（Anthropic Messages / Gemini generateContent / OpenAI Responses）统一历史消息管理
- 单平台持续对话（思维链连贯、上下文缓存命中）
- 多平台切换对话（自动剥离平台私有字段）
- 全平台协议支持（静态预定义，模块加载时就绪）

不关心：系统提示词、模型参数、执行策略、非标服务商 HTTP 层差异翻译。
这些由上层统一 LLM 调用服务编排。
"""
from agent_runtime.conversation_message.message.operations import (
    add_user_message,
    create_conversation,
    get_supported_providers,
    ingest_response,
    render_messages,
)
from agent_runtime.conversation_message.message.models import (
    MODALITY_IMAGE,
    MODALITY_PDF,
    MODALITY_TEXT,
    PROVIDER_ANTHROPIC_MESSAGES,
    PROVIDER_CLAUDE,
    PROVIDER_GEMINI,
    PROVIDER_GEMINI_GENERATE_CONTENT,
    PROVIDER_OPENAI,
    PROVIDER_OPENAI_CHAT_COMPLETIONS,
    PROVIDER_OPENAI_RESPONSES,
    PROVIDER_ZHIPU_CHAT_COMPLETIONS,
    ROLE_ASSISTANT,
    ROLE_TOOL,
    ROLE_USER,
    Conversation,
    ConversationState,
    ImagePart,
    ImagePartMeta,
    PartMeta,
    PdfPart,
    PdfPartMeta,
    ProviderAdapter,
    TextPart,
    TextPartMeta,
    ThinkingPart,
    ToolResultPart,
    ToolUsePart,
    UniMessage,
    UniPart,
)

__all__ = [
    # 领域操作
    "create_conversation",
    "add_user_message",
    "render_messages",
    "ingest_response",
    "get_supported_providers",
    # 核心模型
    "Conversation",
    "ConversationState",
    "UniMessage",
    "UniPart",
    "TextPart",
    "ImagePart",
    "PdfPart",
    "ThinkingPart",
    "ToolUsePart",
    "ToolResultPart",
    # 接口
    "ProviderAdapter",
    # 常量
    "PROVIDER_ANTHROPIC_MESSAGES",
    "PROVIDER_GEMINI_GENERATE_CONTENT",
    "PROVIDER_OPENAI_CHAT_COMPLETIONS",
    "PROVIDER_OPENAI_RESPONSES",
    "PROVIDER_ZHIPU_CHAT_COMPLETIONS",
    "PROVIDER_CLAUDE",
    "PROVIDER_GEMINI",
    "PROVIDER_OPENAI",
    "ROLE_USER",
    "ROLE_ASSISTANT",
    "ROLE_TOOL",
    "MODALITY_TEXT",
    "MODALITY_IMAGE",
    "MODALITY_PDF",
]
