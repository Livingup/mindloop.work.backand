# -*- coding: utf-8 -*-
"""LLM 注册数据通用业务校验规则。

该文件集中维护模型 ID、服务商 ID、模型接入项 ID、JSON 自定义请求字段等
跨实体复用的业务校验规则，避免不同聚合根各自实现不一致的文件名安全约束。
"""
from __future__ import annotations

import json
import re
from typing import Iterable


# 用户自定义注册数据的 ID 同时会作为 YAML 文件名或 URL 路径片段，必须避免路径穿越、隐藏文件和
# Windows 保留设备名问题。允许点号是为了兼容 gemini-3.1-pro、gpt-5.4 等模型型号命名。
_REGISTRY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

_WINDOWS_RESERVED_FILE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


# LLM 输入模态采用开放字符串集合；这里仅校验文件/API 友好的格式，不把未来新模态写死在代码里。
_MODALITY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


# API 协议格式由 LLM 统一调用客户端消费，本模块只校验其可作为稳定配置枚举值使用的格式。
_API_FORMAT_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")


def validate_registry_id(value: str, field_name: str) -> None:
    """校验用户自定义注册 ID 的文件名安全性。

    模型元数据 ID、服务商 ID、服务商模型接入项 ID 都会出现在 YAML 文件名、CLI 参数或 REST 路径中。
    因此必须禁止空值、路径分隔符、隐藏文件名和 Windows 保留设备名，确保跨平台可落盘、可寻址。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空。")
    if value != value.strip():
        raise ValueError(f"{field_name} 不能包含首尾空白字符。")
    if "/" in value or "\\" in value:
        raise ValueError(f"{field_name} 不能包含路径分隔符。")
    if value.startswith("."):
        raise ValueError(f"{field_name} 不能以点号开头，避免生成隐藏文件。")
    if not _REGISTRY_ID_PATTERN.match(value):
        raise ValueError(
            f"{field_name} '{value}' 格式无效，要求以字母或数字开头，"
            "仅包含字母、数字、下划线、短横线和点号，长度 1~128。"
        )
    if value.upper() in _WINDOWS_RESERVED_FILE_NAMES:
        raise ValueError(f"{field_name} 不能使用 Windows 保留文件名: {value}")


def validate_optional_text(value: str | None, field_name: str) -> None:
    """校验可选文本字段；传入 None 表示未配置，传入字符串则不能只有空白。"""
    if value is not None and not value.strip():
        raise ValueError(f"{field_name} 不能是空白字符串。")


def validate_positive_int(value: int, field_name: str) -> None:
    """校验必须为正整数的模型容量字段。"""
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} 必须是正整数。")


def validate_non_negative_int(value: int, field_name: str) -> None:
    """校验必须为非负整数的重试/等待类配置字段。"""
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} 必须是非负整数。")


def validate_modalities(values: Iterable[str]) -> tuple[str, ...]:
    """校验模型支持的输入模态数组并返回去重后的不可变元组。

    业务规则：
    - 输入模态不能为空，模型至少应声明 text/image/audio/video/pdf 等一种输入能力；
    - 当前不枚举限制所有合法模态，避免未来新模态需要修改代码；
    - 同一模型的模态声明不允许重复，避免管理后台展示和调用侧判断产生歧义。
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        modality = str(item).strip()
        if not modality:
            raise ValueError("supported_input_modalities 不能包含空值。")
        if not _MODALITY_PATTERN.match(modality):
            raise ValueError(
                f"输入模态 '{modality}' 格式无效，要求以字母开头，"
                "仅包含字母、数字、下划线和短横线。"
            )
        key = modality.lower()
        if key in seen:
            raise ValueError(f"supported_input_modalities 存在重复模态: {modality}")
        seen.add(key)
        normalized.append(modality)
    if not normalized:
        raise ValueError("supported_input_modalities 至少需要声明一种输入模态。")
    return tuple(normalized)


def validate_unique_registry_ids(values: Iterable[str], field_name: str) -> None:
    """校验同一业务集合内的 ID 不重复。"""
    seen: set[str] = set()
    for value in values:
        validate_registry_id(value, field_name)
        if value in seen:
            raise ValueError(f"{field_name} 存在重复值: {value}")
        seen.add(value)


def validate_api_format(value: str) -> None:
    """校验服务商模型接入项的 API 协议格式字段。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("api_format 不能为空。")
    if value != value.strip():
        raise ValueError("api_format 不能包含首尾空白字符。")
    if not _API_FORMAT_PATTERN.match(value):
        raise ValueError(
            f"api_format '{value}' 格式无效，要求以字母开头，"
            "仅包含字母、数字、下划线、短横线和点号。"
        )


def validate_json_string(value: str | None, field_name: str) -> None:
    """校验自定义请求字段是否为合法 JSON 字符串。

    thinking、tool_choice、custom_request_params 都是用户自定义 JSON 字符串。本模块不理解其中业务含义，
    只负责在保存配置时提前发现 JSON 语法错误，避免后续 LLM 统一调用客户端运行时才失败。
    """
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是 JSON 字符串。")
    if not value.strip():
        raise ValueError(f"{field_name} 不能是空白字符串。")
    try:
        json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} 不是合法 JSON 字符串: {exc.msg}") from exc
