"""从函数签名动态创建 Pydantic 模型。

@tool 装饰的函数有两种参数风格：
1. 简单参数：def search(query: str, max_results: int = 10) → 动态创建 Pydantic 模型
2. Pydantic 参数：def analyze(params: AnalyzeParams) → 直接返回该 Pydantic 模型类

工具函数还可以可选声明一个 ToolRuntimeContext 类型的参数（推荐命名 `ctx`）来接收
Runtime 注入的上下文。该参数会被本模块自动从 schema 模型中剔除，不会暴露给 LLM。

各平台 Schema 生成器需要 Pydantic 模型才能利用 SDK 能力生成 JSON Schema。
本模块负责从任意函数签名提取或创建对应的 Pydantic 模型。
"""
from __future__ import annotations

import inspect
import typing
from typing import Any, Callable

from pydantic import BaseModel, create_model

from agent_runtime.tool.tool_execution_models import ToolRuntimeContext


def is_runtime_context_annotation(annotation: Any) -> bool:
    """判断给定的类型注解是否是 ToolRuntimeContext 或其子类。"""
    return isinstance(annotation, type) and issubclass(annotation, ToolRuntimeContext)


def collect_business_parameters(
    fn: Callable[..., Any],
) -> list[tuple[str, inspect.Parameter, Any]]:
    """收集工具函数中暴露给 LLM 的业务参数（已剔除 self/cls 和 ToolRuntimeContext 注入参数）。

    返回值列表元素为 ``(参数名, inspect.Parameter, 类型注解)``。
    """
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn)
    business: list[tuple[str, inspect.Parameter, Any]] = []
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        annotation = hints.get(name, Any)
        if is_runtime_context_annotation(annotation):
            continue
        business.append((name, param, annotation))
    return business


def find_runtime_context_parameter_name(fn: Callable[..., Any]) -> str | None:
    """返回工具函数中 ToolRuntimeContext 类型参数的名称（若存在），否则返回 None。"""
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn)
    for name, _ in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        annotation = hints.get(name, Any)
        if is_runtime_context_annotation(annotation):
            return name
    return None


def build_pydantic_model(fn: Callable[..., Any]) -> type[BaseModel]:
    """从函数签名提取或动态创建 Pydantic 模型。

    规则：
    - 如果函数业务参数（剔除 ToolRuntimeContext 后）只有一个且该参数类型是
      Pydantic BaseModel 子类，直接返回该模型类（开发者已显式定义了参数结构）。
    - 否则，从函数签名的剩余业务参数的类型注解和默认值动态创建一个 Pydantic 模型。

    Args:
        fn: 被解析的工具函数。

    Returns:
        Pydantic BaseModel 子类。
    """
    business_params = collect_business_parameters(fn)

    # 检查是否只有一个 Pydantic 模型业务参数。
    if len(business_params) == 1:
        _, _, annotation = business_params[0]
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation

    # 动态创建 Pydantic 模型。
    fields: dict[str, Any] = {}
    for name, param, annotation in business_params:
        if param.default is inspect.Parameter.empty:
            # 必填参数：(type, ...) 表示 required。
            fields[name] = (annotation, ...)
        else:
            # 有默认值的可选参数。
            fields[name] = (annotation, param.default)

    return create_model("ToolParameters", **fields)
