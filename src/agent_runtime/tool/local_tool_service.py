"""本地工具领域服务。

无状态，协调仓储和 Schema 缓存，提供四个业务用例：
1. 列出所有工具（支持 public 过滤、group 分组）
2. 按名称查询工具
3. 执行工具
4. 获取工具 wiki 列表（按 group 分组）
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
import typing
from typing import Any

from pydantic import BaseModel

from agent_runtime.infrastructure.asyncio_diagnostics import (
    BlockingPoolOverloadedError,
    monitored_to_thread,
)
from agent_runtime.tool.infrastructure._pydantic_model_builder import (
    collect_business_parameters,
    find_runtime_context_parameter_name,
)
from agent_runtime.tool.local_tool_definition import LocalToolDefinition
from agent_runtime.tool.local_tool_repository import LocalToolRepository
from agent_runtime.tool.tool_schema_cache import ToolSchemaCache
from agent_runtime.tool.tool_execution_models import (
    BaseToolResult,
    ToolRuntimeContext,
    build_tool_error_result,
    wrap_tool_output,
)

logger = logging.getLogger(__name__)


class LocalToolService:
    """本地工具领域服务。

    协调工具仓储和 Schema 缓存，对外提供工具查询、执行和文档能力。
    """

    def __init__(
        self,
        repository: LocalToolRepository,
        schema_cache: ToolSchemaCache,
        execution_loop_logger: Any | None = None,
    ) -> None:
        self._repo = repository
        self._schema_cache = schema_cache
        self._execution_loop_logger = execution_loop_logger

    # ── 业务用例 1：列出所有工具 ──────────────────────────────────────

    def list_tools(
        self,
        *,
        public_only: bool = True,
        group_by: bool = False,
    ) -> list[dict[str, Any]] | dict[str, list[dict[str, Any]]]:
        """列出所有工具。

        Args:
            public_only: 是否只返回公开工具，默认 True。
            group_by: 是否按 group 分组返回，默认 False。

        Returns:
            不分组时返回工具信息列表；
            分组时返回 {group: [工具信息列表]} 字典。
        """
        tools = self._repo.list_all()
        if public_only:
            tools = [t for t in tools if t.public]

        tool_items = [self._build_tool_summary(t) for t in tools]

        if not group_by:
            return tool_items

        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in tool_items:
            group = item["group"]
            grouped.setdefault(group, []).append(item)
        return grouped

    # ── 业务用例 2：按名称查询工具 ────────────────────────────────────

    def get_tool(self, tool_name: str) -> dict[str, Any]:
        """按名称查询工具完整信息（不受 public 限制）。

        Args:
            tool_name: 工具名称。

        Returns:
            工具完整信息字典（名称、描述、wiki、分组、各平台 JSON Schema）。

        Raises:
            KeyError: 工具不存在。
        """
        tool_def = self._repo.get_by_name(tool_name)
        return self._build_tool_detail(tool_def)

    # ── 业务用例 3：执行工具 ──────────────────────────────────────────

    async def execute_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> BaseToolResult:
        """执行指定工具。

        默认保持同步和异步工具函数的兼容执行语义：direct 同步函数直接调用，
        异步函数或返回 awaitable 的函数自动 await。只有显式声明 execution_mode="blocking"
        的同步工具会进入 BlockingScheduler 阻塞池执行。若工具函数签名中声明了
        ToolRuntimeContext 类型的参数，框架会注入 runtime_context 实参（None 时使用空
        ToolRuntimeContext()）。

        Args:
            tool_name: 工具名称。
            params: 传给工具函数的业务参数字典（来自 LLM 或 REST 调用方）。
            runtime_context: Runtime 上下文，由调度链路注入。工具未声明
                ToolRuntimeContext 参数时本字段不会传给工具函数。

        Returns:
            统一工具执行结果。旧工具返回普通值时会自动包装为成功结果。

        Raises:
            KeyError: 工具不存在。
        """
        tool_def = self._repo.get_by_name(tool_name)
        started_at = time.perf_counter()
        instance_id = runtime_context.agent_instance_id if runtime_context is not None else None
        request_id = runtime_context.request_id if runtime_context is not None else None
        param_keys = sorted(params)
        if self._execution_loop_logger is not None and instance_id:
            self._execution_loop_logger.record_step(
                instance_id=instance_id,
                stage="local_tool_service.execute",
                status="started",
                summary="LocalToolService 已解析本地工具定义，准备构造工具函数调用参数。",
                details={
                    "request_id": request_id,
                    "task_id": runtime_context.task_id if runtime_context is not None else None,
                    "conversation_id": runtime_context.conversation_id if runtime_context is not None else None,
                    "tool_name": tool_name,
                    "allowed_loop_decisions": list(tool_def.allowed_loop_decisions),
                    "param_keys": param_keys,
                    "param_key_count": len(param_keys),
                    "execution_mode": tool_def.execution_mode,
                    "blocking_pool": tool_def.blocking_pool,
                },
                expected_next="local_tool_service.invoke_callable",
            )
        logger.info(
            "执行本地工具: tool_name=%s, param_keys=%s, param_key_count=%s, execution_mode=%s, blocking_pool=%s",
            tool_name,
            param_keys,
            len(param_keys),
            tool_def.execution_mode,
            tool_def.blocking_pool,
        )
        try:
            kwargs = _build_tool_call_kwargs(tool_def.fn, params, runtime_context)
            if self._execution_loop_logger is not None and instance_id:
                self._execution_loop_logger.record_step(
                    instance_id=instance_id,
                    stage="local_tool_service.invoke_callable",
                    status="started",
                    summary="本地工具函数调用参数已构造，准备执行同步或异步工具函数。",
                    details={
                        "request_id": request_id,
                        "tool_name": tool_name,
                        "injected_runtime_context": runtime_context is not None and any(
                            value is runtime_context for value in kwargs.values()
                        ),
                        "kwarg_keys": sorted(kwargs),
                        "execution_mode": tool_def.execution_mode,
                        "blocking_pool": tool_def.blocking_pool,
                        "blocking_operation": tool_def.blocking_operation,
                    },
                    expected_next="local_tool_service.normalize_result",
                )
            result = await _invoke_tool_callable(tool_def, kwargs)
        except asyncio.CancelledError:
            raise
        except BlockingPoolOverloadedError as exc:
            error_message = (
                "本地工具阻塞调度池已满: "
                f"pool={exc.pool_name} operation={exc.operation} pending={exc.pending} max_pending={exc.max_pending}"
            )
            logger.warning(
                "本地工具阻塞调度池已满: tool_name=%s, execution_mode=%s, blocking_pool=%s, blocking_operation=%s, pending=%s, max_pending=%s, 耗时=%sms",
                tool_name,
                tool_def.execution_mode,
                exc.pool_name,
                exc.operation,
                exc.pending,
                exc.max_pending,
                int((time.perf_counter() - started_at) * 1000),
            )
            if self._execution_loop_logger is not None and instance_id:
                self._execution_loop_logger.record_step(
                    instance_id=instance_id,
                    stage="local_tool_service.execute",
                    status="failed",
                    summary="本地工具阻塞调度池拒绝执行，LocalToolService 将其转换为结构化失败结果。",
                    details={
                        "request_id": request_id,
                        "tool_name": tool_name,
                        "error_type": exc.__class__.__name__,
                        "execution_mode": tool_def.execution_mode,
                        "blocking_pool": exc.pool_name,
                        "blocking_operation": exc.operation,
                        "pending": exc.pending,
                        "max_pending": exc.max_pending,
                        "duration_ms": int((time.perf_counter() - started_at) * 1000),
                    },
                    expected_next="tool_result.error",
                )
            return build_tool_error_result(
                error_code="BlockingPoolOverloadedError",
                error_message=error_message,
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            error_message = str(exc) or exc.__class__.__name__
            logger.exception(
                "本地工具执行失败: %s, 耗时=%sms",
                tool_name,
                int((time.perf_counter() - started_at) * 1000),
            )
            if self._execution_loop_logger is not None and instance_id:
                self._execution_loop_logger.record_step(
                    instance_id=instance_id,
                    stage="local_tool_service.execute",
                    status="failed",
                    summary="本地工具函数抛出异常，LocalToolService 将其转换为结构化失败结果。",
                    details={
                        "request_id": request_id,
                        "tool_name": tool_name,
                        "error_type": exc.__class__.__name__,
                        "error_message": error_message,
                        "duration_ms": int((time.perf_counter() - started_at) * 1000),
                    },
                    expected_next="tool_result.error",
                )
            return build_tool_error_result(
                error_code=exc.__class__.__name__,
                error_message=error_message,
            )
        unified_result = wrap_tool_output(result)
        if unified_result.loop_decision not in tool_def.allowed_loop_decisions:
            logger.warning(
                "本地工具返回不允许的循环决策: %s, loop_decision=%s, allowed=%s, 耗时=%sms",
                tool_name,
                unified_result.loop_decision,
                tool_def.allowed_loop_decisions,
                int((time.perf_counter() - started_at) * 1000),
            )
            if self._execution_loop_logger is not None and instance_id:
                self._execution_loop_logger.record_step(
                    instance_id=instance_id,
                    stage="local_tool_service.loop_decision",
                    status="denied",
                    summary="本地工具返回的 loop decision 不在该工具允许集合内，将被转换为工具错误结果。",
                    details={
                        "request_id": request_id,
                        "tool_name": tool_name,
                        "returned_loop_decision": unified_result.loop_decision,
                        "allowed_loop_decisions": list(tool_def.allowed_loop_decisions),
                        "duration_ms": int((time.perf_counter() - started_at) * 1000),
                    },
                    expected_next="tool_result.error",
                )
            return build_tool_error_result(
                error_code="LoopDecisionNotAllowed",
                error_message=(
                    f"工具 {tool_name} 返回了不允许的循环决策: "
                    f"{unified_result.loop_decision}"
                ),
            )
        logger.info(
            "本地工具执行完成: %s, success=%s, execution_status=%s, loop_decision=%s, 耗时=%sms",
            tool_name,
            unified_result.success,
            unified_result.execution_status,
            unified_result.loop_decision,
            int((time.perf_counter() - started_at) * 1000),
        )
        if self._execution_loop_logger is not None and instance_id:
            self._execution_loop_logger.record_step(
                instance_id=instance_id,
                stage="local_tool_service.execute",
                status="completed",
                summary="本地工具执行结果已归一化，并通过 loop decision 授权校验。",
                details={
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "success": unified_result.success,
                    "execution_status": unified_result.execution_status,
                    "loop_decision": unified_result.loop_decision,
                    "has_frontend_message": unified_result.frontend_message is not None,
                    "duration_ms": int((time.perf_counter() - started_at) * 1000),
                },
                expected_next="local_tool_gateway.return_result",
            )
        return unified_result

    # ── 业务用例 4：获取工具 wiki 列表 ────────────────────────────────

    def get_wiki_list(self) -> dict[str, list[dict[str, Any]]]:
        """获取工具 wiki 列表，按 group 分组。

        默认只返回 public=True 的工具。
        每个工具返回工具名称、wiki、group 和各平台 JSON Schema。

        Returns:
            {group: [工具 wiki 信息列表]} 字典。
        """
        tools = self._repo.list_all()
        tools = [t for t in tools if t.public]

        grouped: dict[str, list[dict[str, Any]]] = {}
        for t in tools:
            item = {
                "tool_name": t.tool_name,
                "wiki": t.wiki,
                "group": t.group,
                "category": t.category,
                "always_enabled": t.always_enabled,
                "allowed_loop_decisions": t.allowed_loop_decisions,
                "schemas": self._schema_cache.get_all_schemas(t),
            }
            grouped.setdefault(t.group, []).append(item)
        return grouped

    # ── 内部方法 ──────────────────────────────────────────────────────

    def _build_tool_summary(self, tool_def: LocalToolDefinition) -> dict[str, Any]:
        """构建工具摘要信息（列表用）。不包含 schemas，schemas 只在详情中返回。"""
        return {
            "tool_name": tool_def.tool_name,
            "description": tool_def.description,
            "group": tool_def.group,
            "wiki": tool_def.wiki,
            "category": tool_def.category,
            "always_enabled": tool_def.always_enabled,
            "allowed_loop_decisions": tool_def.allowed_loop_decisions,
        }

    def _build_tool_detail(self, tool_def: LocalToolDefinition) -> dict[str, Any]:
        """构建工具完整信息（详情用）。"""
        return {
            "tool_name": tool_def.tool_name,
            "description": tool_def.description,
            "group": tool_def.group,
            "public": tool_def.public,
            "wiki": tool_def.wiki,
            "category": tool_def.category,
            "always_enabled": tool_def.always_enabled,
            "allowed_loop_decisions": tool_def.allowed_loop_decisions,
            "schemas": self._schema_cache.get_all_schemas(tool_def),
        }


async def _invoke_tool_callable(tool_def: LocalToolDefinition, kwargs: dict[str, Any]) -> Any:
    """Invoke a local tool callable according to its execution classification."""
    if tool_def.execution_mode == "blocking":
        if inspect.iscoroutinefunction(tool_def.fn):
            result = tool_def.fn(**kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        slow_execution_seconds = (
            tool_def.blocking_slow_execution_seconds
            if tool_def.blocking_slow_execution_seconds is not None
            else 1.0
        )
        result = await monitored_to_thread(
            tool_def.fn,
            operation=tool_def.blocking_operation or f"local_tool.{tool_def.tool_name}",
            caller="LocalToolService.execute_tool",
            pool=tool_def.blocking_pool,
            log=logger,
            slow_execution_seconds=slow_execution_seconds,
            diagnostic_context={
                "tool_name": tool_def.tool_name,
                "execution_mode": tool_def.execution_mode,
                "blocking_pool": tool_def.blocking_pool,
                "blocking_operation": tool_def.blocking_operation,
                "kwarg_keys": sorted(kwargs),
                "kwarg_key_count": len(kwargs),
            },
            **kwargs,
        )
        if inspect.isawaitable(result):
            result = await result
        return result

    result = tool_def.fn(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def _build_tool_call_kwargs(
    fn: Any,
    params: dict[str, Any],
    runtime_context: ToolRuntimeContext | None,
) -> dict[str, Any]:
    """根据工具函数签名构建调用参数。

    处理流程：
    1. 用 collect_business_parameters 拿到剔除 ctx 后的业务参数列表。
    2. 业务参数若是单一 Pydantic 子类，构造该模型实例作为单参；否则按简单
       参数字典直接展开。
    3. 用 find_runtime_context_parameter_name 检测函数是否声明了 ctx 参数，
       若有则注入 runtime_context（None 时构造一个空 ToolRuntimeContext()）。
    """
    business_params = collect_business_parameters(fn)
    kwargs: dict[str, Any] = {}

    if (
        len(business_params) == 1
        and isinstance(business_params[0][2], type)
        and issubclass(business_params[0][2], BaseModel)
    ):
        param_name, _, annotation = business_params[0]
        value = params.get(param_name)
        if isinstance(value, annotation):
            request_model = value
        elif isinstance(value, dict):
            request_model = annotation(**value)
        else:
            request_model = annotation(**params)
        kwargs[param_name] = request_model
    else:
        # 多个简单参数 → 按 params 字典展开（只取业务参数名，避免泄漏额外字段）。
        business_names = {name for name, _, _ in business_params}
        kwargs.update({k: v for k, v in params.items() if k in business_names})

    ctx_param_name = find_runtime_context_parameter_name(fn)
    if ctx_param_name is not None:
        kwargs[ctx_param_name] = runtime_context if runtime_context is not None else ToolRuntimeContext()

    return kwargs
