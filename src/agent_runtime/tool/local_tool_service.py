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
    ) -> None:
        self._repo = repository
        self._schema_cache = schema_cache

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

        支持同步和异步工具函数：同步函数直接调用，异步函数自动 await。
        若工具函数签名中声明了 ToolRuntimeContext 类型的参数，框架会注入
        runtime_context 实参（None 时使用空 ToolRuntimeContext()）。

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
        logger.info("执行本地工具: %s, 参数: %s", tool_name, params)
        try:
            kwargs = _build_tool_call_kwargs(tool_def.fn, params, runtime_context)
            result = tool_def.fn(**kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            logger.exception(
                "本地工具执行失败: %s, 耗时=%sms",
                tool_name,
                int((time.perf_counter() - started_at) * 1000),
            )
            return build_tool_error_result(
                error_code=exc.__class__.__name__,
                error_message=str(exc),
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
