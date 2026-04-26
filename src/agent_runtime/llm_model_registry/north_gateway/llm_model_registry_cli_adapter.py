# -*- coding: utf-8 -*-
"""LLM 服务商与模型注册 CLI 接入适配。

提供 spread-ai llm-registry 子命令组，支持模型元数据、服务商定义和服务商模型接入项解析查询。
复杂输入通过 YAML 文件提交，输出统一为 JSON，便于用户、脚本和外部 Agent 自动化调用。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import click
import yaml

from agent_runtime.llm_model_registry.llm_model_metadata import LlmModelMetadata
from agent_runtime.llm_model_registry.llm_provider_definition import (
    LlmApiKey,
    LlmProviderDefinition,
    LlmProviderModelBinding,
    LlmRetryPolicy,
)
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    ResolvedLlmProviderModel,
)
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmModelMetadataSaveRequest,
    LlmModelRegistryGateway,
    LlmProviderSaveRequest,
)


def _output_json(data: Any) -> None:
    """输出 JSON 到 stdout。"""
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


def _error_json(message: str, exit_code: int = 1) -> None:
    """输出失败响应 JSON 到 stderr 并退出。"""
    click.echo(
        json.dumps({"status": "error", "message": message}, ensure_ascii=False),
        err=True,
    )
    sys.exit(exit_code)


def _read_yaml_file(yaml_file: str) -> dict[str, Any]:
    """读取 YAML 文件并要求内容为对象结构。"""
    yaml_path = Path(yaml_file)
    if not yaml_path.exists():
        _error_json(f"文件不存在: {yaml_file}")
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        _error_json("YAML 文件内容必须是对象结构。")
    return data


def _model_request_from_yaml(data: dict[str, Any]) -> LlmModelMetadataSaveRequest:
    """从模型元数据 YAML 构建保存请求。"""
    model_id = str(data.get("model_id", ""))
    if not model_id:
        raise ValueError("保存模型元数据时 YAML 必须包含 model_id。")
    metadata = LlmModelMetadata.from_dict(model_id, data)
    return LlmModelMetadataSaveRequest.from_metadata(metadata)


def _provider_request_from_yaml(data: dict[str, Any]) -> LlmProviderSaveRequest:
    """从服务商定义 YAML 构建保存请求。"""
    provider_id = str(data.get("provider_id", ""))
    if not provider_id:
        raise ValueError("保存 LLM 服务商时 YAML 必须包含 provider_id。")
    provider = LlmProviderDefinition.from_dict(provider_id, data)
    return LlmProviderSaveRequest.from_provider(provider)


def _model_to_dict(metadata: LlmModelMetadata) -> dict[str, Any]:
    """将模型元数据序列化为 CLI JSON 字典。"""
    return metadata.to_dict()


def _provider_to_dict(provider: LlmProviderDefinition) -> dict[str, Any]:
    """将服务商定义序列化为 CLI JSON 字典。"""
    return provider.to_dict()


def _binding_to_dict(binding: LlmProviderModelBinding) -> dict[str, Any]:
    """将服务商模型接入项序列化为 CLI JSON 字典。"""
    return binding.to_dict()


def _resolved_to_dict(resolved: ResolvedLlmProviderModel) -> dict[str, Any]:
    """将解析后的服务商模型调用配置序列化为 CLI JSON 字典。"""
    return resolved.to_dict()


def create_llm_model_registry_cli_group(
    gateway_factory: Callable[[], LlmModelRegistryGateway],
) -> click.Group:
    """创建 LLM 服务商与模型注册 CLI 命令组。

    Args:
        gateway_factory: 网关工厂函数，在命令执行时延迟创建网关实例。

    Returns:
        click.Group 命令组，挂载到 spread-ai llm-registry 下。
    """

    @click.group("llm-registry", help="LLM 服务商与模型注册管理")
    @click.pass_context
    def registry_group(ctx: click.Context) -> None:
        ctx.ensure_object(dict)
        ctx.obj["gateway"] = gateway_factory()

    @registry_group.group("models", help="模型型号元数据管理")
    def models_group() -> None:
        """模型型号元数据子命令组。"""

    @models_group.command("list", help="列出所有模型元数据")
    @click.pass_context
    def list_models(ctx: click.Context) -> None:
        """列出所有模型型号元数据。"""
        gateway = ctx.obj["gateway"]
        _output_json([_model_to_dict(item) for item in gateway.list_model_metadata()])

    @models_group.command("get", help="获取指定模型元数据详情")
    @click.argument("model_id")
    @click.pass_context
    def get_model(ctx: click.Context, model_id: str) -> None:
        """查看指定模型元数据。"""
        gateway = ctx.obj["gateway"]
        try:
            metadata = gateway.get_model_metadata(model_id)
            _output_json(_model_to_dict(metadata))
        except KeyError as e:
            _error_json(str(e))

    @models_group.command("save", help="从 YAML 文件新增或覆盖保存模型元数据")
    @click.option("--file", "yaml_file", required=True, help="模型元数据 YAML 文件路径。")
    @click.pass_context
    def save_model(ctx: click.Context, yaml_file: str) -> None:
        """从 YAML 文件保存模型元数据。"""
        try:
            data = _read_yaml_file(yaml_file)
            gateway = ctx.obj["gateway"]
            metadata = gateway.save_model_metadata(_model_request_from_yaml(data))
            _output_json({
                "status": "success",
                "message": f"已保存 LLM 模型元数据: {metadata.model_id}",
                "model_id": metadata.model_id,
            })
        except (ValueError, KeyError) as e:
            _error_json(str(e))
        except Exception as e:
            _error_json(f"保存 LLM 模型元数据失败: {e}")

    @models_group.command("delete", help="删除指定模型元数据")
    @click.argument("model_id")
    @click.pass_context
    def delete_model(ctx: click.Context, model_id: str) -> None:
        """删除指定模型元数据。"""
        try:
            gateway = ctx.obj["gateway"]
            gateway.delete_model_metadata(model_id)
            _output_json({"status": "success", "message": f"已删除 LLM 模型元数据: {model_id}"})
        except ValueError as e:
            _error_json(str(e))

    @registry_group.group("providers", help="LLM 服务商定义管理")
    def providers_group() -> None:
        """LLM 服务商定义子命令组。"""

    @providers_group.command("list", help="列出所有 LLM 服务商")
    @click.pass_context
    def list_providers(ctx: click.Context) -> None:
        """列出所有 LLM 服务商定义。"""
        gateway = ctx.obj["gateway"]
        _output_json([_provider_to_dict(item) for item in gateway.list_providers()])

    @providers_group.command("get", help="获取指定 LLM 服务商详情")
    @click.argument("provider_id")
    @click.pass_context
    def get_provider(ctx: click.Context, provider_id: str) -> None:
        """查看指定 LLM 服务商定义。"""
        gateway = ctx.obj["gateway"]
        try:
            provider = gateway.get_provider(provider_id)
            _output_json(_provider_to_dict(provider))
        except KeyError as e:
            _error_json(str(e))

    @providers_group.command("save", help="从 YAML 文件新增或覆盖保存 LLM 服务商")
    @click.option("--file", "yaml_file", required=True, help="LLM 服务商 YAML 文件路径。")
    @click.pass_context
    def save_provider(ctx: click.Context, yaml_file: str) -> None:
        """从 YAML 文件保存 LLM 服务商定义。"""
        try:
            data = _read_yaml_file(yaml_file)
            gateway = ctx.obj["gateway"]
            provider = gateway.save_provider(_provider_request_from_yaml(data))
            _output_json({
                "status": "success",
                "message": f"已保存 LLM 服务商定义: {provider.provider_id}",
                "provider_id": provider.provider_id,
            })
        except (ValueError, KeyError) as e:
            _error_json(str(e))
        except Exception as e:
            _error_json(f"保存 LLM 服务商定义失败: {e}")

    @providers_group.command("delete", help="删除指定 LLM 服务商")
    @click.argument("provider_id")
    @click.pass_context
    def delete_provider(ctx: click.Context, provider_id: str) -> None:
        """删除指定 LLM 服务商定义。"""
        gateway = ctx.obj["gateway"]
        gateway.delete_provider(provider_id)
        _output_json({"status": "success", "message": f"已删除 LLM 服务商定义: {provider_id}"})

    @registry_group.command("resolve", help="解析服务商模型接入项的最终调用配置")
    @click.argument("provider_id")
    @click.argument("binding_id")
    @click.pass_context
    def resolve_provider_model(
        ctx: click.Context,
        provider_id: str,
        binding_id: str,
    ) -> None:
        """解析指定服务商模型接入项。"""
        gateway = ctx.obj["gateway"]
        try:
            resolved = gateway.resolve_provider_model(provider_id, binding_id)
            _output_json(_resolved_to_dict(resolved))
        except KeyError as e:
            _error_json(str(e))

    return registry_group
