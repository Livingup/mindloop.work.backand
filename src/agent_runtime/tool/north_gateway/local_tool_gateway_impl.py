"""本地工具北向网关实现。

将 LocalToolService 领域服务适配为北向网关契约（LocalToolGateway），
将内部字典转换为对外 DTO 数据模型。
"""
from __future__ import annotations

from typing import Any

from agent_runtime.tool.local_tool_service import LocalToolService
from agent_runtime.tool.north_gateway.local_tool_contracts import (
    ToolDetail,
    ToolExecuteRequest,
    ToolExecuteResult,
    ToolSummary,
    ToolWikiItem,
)
from agent_runtime.tool.tool_execution_models import ToolRuntimeContext


class LocalToolGatewayImpl:
    """本地工具北向网关实现。

    实现 LocalToolGateway 协议，外部消费方通过此实例访问本模块能力。
    """

    def __init__(
        self,
        service: LocalToolService,
        execution_loop_logger: Any | None = None,
    ) -> None:
        self._service = service
        self._execution_loop_logger = execution_loop_logger

    def list_tools(
        self,
        *,
        public_only: bool = True,
        group_by: bool = False,
    ) -> list[ToolSummary] | dict[str, list[ToolSummary]]:
        """列出所有工具。"""
        result = self._service.list_tools(public_only=public_only, group_by=group_by)

        if group_by:
            # result 是 dict[str, list[dict]]
            grouped: dict[str, list[ToolSummary]] = {}
            for group_name, items in result.items():
                grouped[group_name] = [self._to_summary(i) for i in items]
            return grouped
        else:
            # result 是 list[dict]
            return [self._to_summary(i) for i in result]

    def get_tool(self, tool_name: str) -> ToolDetail:
        """按名称查询工具完整信息。"""
        info = self._service.get_tool(tool_name)
        return ToolDetail(
            tool_name=info["tool_name"],
            description=info["description"],
            group=info["group"],
            public=info["public"],
            wiki=info["wiki"],
            category=info["category"],
            always_enabled=info["always_enabled"],
            allowed_loop_decisions=info["allowed_loop_decisions"],
            schemas=info.get("schemas", {}),
        )

    async def execute_tool(self, request: ToolExecuteRequest) -> ToolExecuteResult:
        """执行工具。

        从请求中提取 Runtime 元数据构造 ToolRuntimeContext 注入给领域服务，
        领域服务再按工具签名决定是否注入到工具实现函数。
        """
        runtime_context = ToolRuntimeContext(
            request_id=request.request_id,
            task_id=request.task_id,
            agent_instance_id=request.agent_instance_id,
            conversation_id=request.conversation_id,
            conversation_history_copy_message_count=request.conversation_history_copy_message_count,
            trace_id=request.trace_id,
            communication_stream_id=request.communication_stream_id,
            tool_name=request.tool_name,
            parent_tool_call_id=request.parent_tool_call_id or request.request_id,
            parent_llm_request_id=request.parent_llm_request_id,
            parent_llm_call_id=request.parent_llm_call_id,
            execution_scope=request.execution_scope,
            communication_context=request.communication_context,
            event_publisher=request.event_publisher,
        )
        if self._execution_loop_logger is not None and request.agent_instance_id:
            self._execution_loop_logger.record_step(
                instance_id=request.agent_instance_id,
                stage="local_tool_gateway.execute",
                status="started",
                summary="本地工具北向网关已构造 ToolRuntimeContext，准备调用 LocalToolService。",
                details={
                    "request_id": request.request_id,
                    "task_id": request.task_id,
                    "conversation_id": request.conversation_id,
                    "conversation_history_copy_message_count": request.conversation_history_copy_message_count,
                    "trace_id": request.trace_id,
                    "communication_stream_id": request.communication_stream_id,
                    "tool_name": request.tool_name,
                },
                expected_next="local_tool_service.execute",
            )
        try:
            result = await self._service.execute_tool(
                request.tool_name,
                request.params,
                runtime_context=runtime_context,
            )
        except Exception as exc:
            if self._execution_loop_logger is not None and request.agent_instance_id:
                self._execution_loop_logger.record_step(
                    instance_id=request.agent_instance_id,
                    stage="local_tool_gateway.execute",
                    status="failed",
                    summary="本地工具北向网关调用 LocalToolService 失败。",
                    details={
                        "request_id": request.request_id,
                        "task_id": request.task_id,
                        "conversation_id": request.conversation_id,
                        "tool_name": request.tool_name,
                        "error_type": exc.__class__.__name__,
                        "error_message": str(exc),
                    },
                    expected_next="tool_dispatch.exception",
                )
            raise
        if self._execution_loop_logger is not None and request.agent_instance_id:
            self._execution_loop_logger.record_step(
                instance_id=request.agent_instance_id,
                stage="local_tool_gateway.execute",
                status="completed",
                summary="本地工具北向网关已收到 LocalToolService 统一工具结果。",
                details={
                    "request_id": request.request_id,
                    "task_id": request.task_id,
                    "conversation_id": request.conversation_id,
                    "tool_name": request.tool_name,
                    "success": result.success,
                    "execution_status": result.execution_status,
                    "loop_decision": result.loop_decision,
                },
                expected_next="tool_dispatch.normalize_result_part",
            )
        payload = result.model_dump(mode="python", exclude={"output"})
        payload.pop("tool_name", None)
        return ToolExecuteResult(
            tool_name=request.tool_name,
            output=result.output,
            **payload,
        )

    def get_wiki_list(self) -> dict[str, list[ToolWikiItem]]:
        """获取工具 wiki 列表，按 group 分组。"""
        result = self._service.get_wiki_list()
        grouped: dict[str, list[ToolWikiItem]] = {}
        for group_name, items in result.items():
            grouped[group_name] = [
                ToolWikiItem(
                    tool_name=i["tool_name"],
                    wiki=i["wiki"],
                    group=i["group"],
                    category=i["category"],
                    always_enabled=i["always_enabled"],
                    allowed_loop_decisions=i["allowed_loop_decisions"],
                    schemas=i.get("schemas", {}),
                )
                for i in items
            ]
        return grouped

    @staticmethod
    def _to_summary(info: dict[str, Any]) -> ToolSummary:
        """内部字典转 ToolSummary DTO。"""
        return ToolSummary(
            tool_name=info["tool_name"],
            description=info["description"],
            group=info["group"],
            wiki=info.get("wiki"),
            category=info["category"],
            always_enabled=info["always_enabled"],
            allowed_loop_decisions=info["allowed_loop_decisions"],
        )
