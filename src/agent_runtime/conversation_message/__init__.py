"""会话消息业务包。

架构分层：
- message/: 统一历史对话消息子模块（领域模型 + 领域服务）
- south_gateway/: 南向依赖网关（资源解析等外部服务适配）
- conversation_repository.py: 会话持久化仓储接口（领域层）
- infrastructure/: 会话持久化仓储实现（基础设施层）
"""
from agent_runtime.conversation_message.conversation_repository import (
    ConversationRepository,
)
from agent_runtime.conversation_message.infrastructure import (
    InMemoryConversationRepository,
    SqliteConversationRepository,
)
