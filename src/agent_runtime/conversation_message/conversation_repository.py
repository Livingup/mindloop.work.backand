"""会话持久化仓储接口。

定义会话聚合根的持久化协议接口，具体实现位于 infrastructure/ 下。

会话组织层级：task_id → thread_id → agent_id → agent_instance_id → conversation_id
"""
from __future__ import annotations

from typing import Any, Protocol

from agent_runtime.conversation_message.message.models import (
    Conversation,
    ConversationState,
    UniMessage,
)


class ConversationRepository(Protocol):
    """会话聚合根持久化仓储协议接口。

    会话组织层级：task_id → thread_id → agent_id → agent_instance_id → conversation_id
    - agent_id: Agent 定义标识（模板/类型）
    - agent_instance_id: Agent 运行时实例标识（每次运行唯一）

    支持两种写入模式：
    - 全量覆写：save() 替换全部消息、状态和供应商状态
    - 增量操作：append_message()、save_state()、save_provider_state()
    """

    async def create(
        self,
        conv: Conversation,
        *,
        task_id: str,
        thread_id: str,
        agent_id: str,
        agent_instance_id: str,
    ) -> None:
        """持久化一个新创建的会话，同时记录其所属层级关系。"""
        ...

    async def get(self, conversation_id: str) -> Conversation:
        """根据会话 ID 加载完整会话。未找到时抛出 KeyError。"""
        ...

    async def save(self, conv: Conversation) -> None:
        """全量覆写：替换会话的全部消息、派生状态和供应商状态。"""
        ...

    async def delete(self, conversation_id: str) -> None:
        """删除会话及其所有数据。会话不存在时静默跳过。"""
        ...

    # ── 增量操作 ─────────────────────────────────────────────────

    async def append_message(self, conversation_id: str, msg: UniMessage) -> None:
        """追加一条消息（增量写入，不重写整个文件）。"""
        ...

    async def save_state(self, conversation_id: str, state: ConversationState) -> None:
        """覆写会话的派生统计状态（模态、图片数、PDF 页数等）。"""
        ...

    async def save_provider_state(
        self, conversation_id: str, provider: str, state: Any,
    ) -> None:
        """新增或更新单个 LLM 供应商的会话状态（如缓存路由、缓存断点或思考续接状态）。"""
        ...

    # ── 层级查询 ─────────────────────────────────────────────────

    async def list_by_task(self, task_id: str) -> list[str]:
        """查询指定任务下的所有会话 ID 列表。"""
        ...

    async def list_by_thread(self, thread_id: str) -> list[str]:
        """查询指定线程下的所有会话 ID 列表。"""
        ...

    async def list_by_agent(self, agent_id: str) -> list[str]:
        """查询指定 Agent 定义下的所有会话 ID 列表。"""
        ...

    async def list_by_agent_instance(self, agent_instance_id: str) -> list[str]:
        """查询指定 Agent 运行时实例下的所有会话 ID 列表。"""
        ...
