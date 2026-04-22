"""从函数签名动态创建 Pydantic 模型。

@tool 装饰的函数有两种参数风格：
1. 简单参数：def search(query: str, max_results: int = 10) → 动态创建 Pydantic 模型
2. Pydantic 参数：def analyze(params: AnalyzeParams) → 直接返回该 Pydantic 模型类

各平台 Schema 生成器需要 Pydantic 模型才能利用 SDK 能力生成 JSON Schema。
本模块负责从任意函数签名提取或创建对应的 Pydantic 模型。
"""
from __future__ import annotations

import inspect
import typing
from typing import Any, Callable

from pydantic import BaseModel, create_model


def build_pydantic_model(fn: Callable[..., Any]) -> type[BaseModel]:
    """从函数签名提取或动态创建 Pydantic 模型。

    规则：
    - 如果函数只有一个参数且该参数类型是 Pydantic BaseModel 子类，
      直接返回该模型类（开发者已显式定义了参数结构）。
    - 否则，从函数签名的类型注解和默认值动态创建一个 Pydantic 模型。

    Args:
        fn: 被解析的工具函数。

    Returns:
        Pydantic BaseModel 子类。
    """
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn)

    # 收集有效参数（跳过 self/cls 和 return）。
    params = [
        (name, p)
        for name, p in sig.parameters.items()
        if name not in ("self", "cls")
    ]

    # 检查是否只有一个 Pydantic 模型参数。
    if len(params) == 1:
        name, p = params[0]
        annotation = hints.get(name, Any)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation

    # 动态创建 Pydantic 模型。
    fields: dict[str, Any] = {}
    for name, p in params:
        annotation = hints.get(name, Any)
        if p.default is inspect.Parameter.empty:
            # 必填参数：(type, ...) 表示 required。
            fields[name] = (annotation, ...)
        else:
            # 有默认值的可选参数。
            fields[name] = (annotation, p.default)

    return create_model("ToolParameters", **fields)
