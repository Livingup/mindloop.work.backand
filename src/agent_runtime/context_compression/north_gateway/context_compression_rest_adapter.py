# -*- coding: utf-8 -*-
"""上下文压缩与观察者 REST API 适配器 (Context Compression REST Adapter)。

核心职责：
1. 管理上下文观察者 (Context Observer) 绑定的 LLM 执行策略组 (llm_policy_group_id)；
2. 校验所选策略组的有效性并提供解析出的首选模型状态；
3. 向前端提供 Task Note 观察者通用系统提示词 (system_note_update.md) 全文与工具清单元数据；
4. 彻底替代并弃用旧的多 Worker 方案接口。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent_runtime.context_compression.config import ContextCompressionConfig
from agent_runtime.context_compression.settings_repository import (
    ContextCompressionSettingsRepository,
)
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmModelRegistryGateway,
)
from agent_runtime.task_note.task_note_prompt_provider import (
    TASK_NOTE_TOOL_NAMES,
)

logger = logging.getLogger(__name__)


# ── Pydantic 传输模型 ─────────────────────────────────────────────


class ContextCompressionSettingsResponse(BaseModel):
    """上下文压缩设置响应。"""

    enabled: bool = True
    llm_policy_group_id: str | None = None
    llm_policy_group_status: str = "valid"
    llm_policy_group_status_message: str | None = None
    active_candidate: dict[str, Any] | None = None
    updated_at: str | None = None


class ContextCompressionSettingsUpdateBody(BaseModel):
    """上下文压缩设置更新请求体。"""

    llm_policy_group_id: str | None = None


class ObserverPromptDetailResponse(BaseModel):
    """上下文观察者系统提示词响应。"""

    template_id: str = "system_note_update"
    name: str = "上下文观察者（Context Observer）系统提示词"
    prompt_text: str
    tools: list[str]
    tool_count: int
    updated_at: str | None = None


# ── 辅助函数 ─────────────────────────────────────────────────────


def _load_system_note_update_prompt() -> tuple[str, str | None]:
    """读取官方内置的 system_note_update.md 文件全文及修改时间。"""
    prompt_path = (
        Path(__file__).resolve().parent.parent.parent
        / "task_note"
        / "prompts"
        / "system_note_update.md"
    )
    if not prompt_path.exists():
        return "# 上下文观察者系统提示词\n（未找到 prompts/system_note_update.md 文件）", None
    try:
        content = prompt_path.read_text(encoding="utf-8")
        mtime = datetime.fromtimestamp(prompt_path.stat().st_mtime, tz=timezone.utc).isoformat()
        return content, mtime
    except Exception as exc:
        logger.error("读取 system_note_update.md 失败: %s", exc)
        return f"# 读取提示词失败: {exc}", None


def _evaluate_policy_status(
    gateway: LlmModelRegistryGateway | None,
    policy_group_id: str | None,
) -> tuple[str, str | None, dict[str, Any] | None]:
    """评估指定的 LLM 策略组当前是否健康有效。"""
    if not policy_group_id:
        return "not_configured", "未配置观察者 LLM 执行策略组。", None
    if gateway is None:
        return "valid", None, None
    try:
        policy = gateway.get_execution_policy(policy_group_id)
        if policy is None:
            return "missing", f"策略组 '{policy_group_id}' 不存在。", None
        if not policy.enabled:
            return "disabled", f"策略组 '{policy_group_id}' 已被禁用。", None
        resolved = gateway.resolve_execution_policy(policy_group_id)
        candidate_info = {
            "provider_id": resolved.provider_id,
            "model_binding_id": resolved.binding_id,
            "model_id": resolved.model_id,
            "model_alias": resolved.model_alias,
        }
        return "valid", None, candidate_info
    except Exception as exc:
        return "error", str(exc), None


# ── 路由构造工厂 ─────────────────────────────────────────────────


def create_context_compression_router(
    conversation_repository: Any = None,
    *,
    prompt_repository: Any = None,
    settings_repository: ContextCompressionSettingsRepository | None = None,
    llm_registry_gateway: LlmModelRegistryGateway | None = None,
    archive_repository: Any = None,
    config_accessor: Callable[[], ContextCompressionConfig] | None = None,
) -> APIRouter:
    """创建上下文压缩与观察者 REST API 路由器。"""
    router = APIRouter()

    @router.get("/settings", response_model=ContextCompressionSettingsResponse)
    async def get_settings() -> ContextCompressionSettingsResponse:
        """获取当前上下文压缩与观察者策略设置。"""
        enabled = True
        if config_accessor is not None:
            try:
                enabled = config_accessor().enabled
            except Exception:
                pass

        policy_id = None
        updated_at = None
        if settings_repository is not None:
            settings = settings_repository.get_settings()
            policy_id = settings.llm_policy_group_id
            updated_at = settings.updated_at

        status, status_message, active_cand = _evaluate_policy_status(
            llm_registry_gateway, policy_id
        )

        return ContextCompressionSettingsResponse(
            enabled=enabled,
            llm_policy_group_id=policy_id,
            llm_policy_group_status=status,
            llm_policy_group_status_message=status_message,
            active_candidate=active_cand,
            updated_at=updated_at,
        )

    @router.put("/settings", response_model=ContextCompressionSettingsResponse)
    async def update_settings(
        body: ContextCompressionSettingsUpdateBody,
    ) -> ContextCompressionSettingsResponse:
        """更新上下文观察者绑定的 LLM 执行策略组。"""
        if settings_repository is None:
            raise HTTPException(status_code=500, detail="SettingsRepository 未配置。")

        new_policy_id = body.llm_policy_group_id
        if new_policy_id is not None:
            new_policy_id = new_policy_id.strip() or None

        if new_policy_id is not None and llm_registry_gateway is not None:
            status, status_message, _ = _evaluate_policy_status(
                llm_registry_gateway, new_policy_id
            )
            if status in ("missing", "disabled", "error"):
                raise HTTPException(
                    status_code=400,
                    detail=f"无效的 LLM 执行策略组 '{new_policy_id}': {status_message}",
                )

        settings_repository.set_settings(
            llm_policy_group_id=new_policy_id,
            input_mode="native_multimodal_history",
        )
        return await get_settings()

    @router.get("/observer-prompt", response_model=ObserverPromptDetailResponse)
    async def get_observer_prompt() -> ObserverPromptDetailResponse:
        """获取上下文观察者当前的系统提示词全文与工具清单元数据。"""
        prompt_text, mtime = _load_system_note_update_prompt()
        tools_list = list(TASK_NOTE_TOOL_NAMES)
        return ObserverPromptDetailResponse(
            template_id="system_note_update",
            name="上下文观察者（Context Observer）系统提示词",
            prompt_text=prompt_text,
            tools=tools_list,
            tool_count=len(tools_list),
            updated_at=mtime,
        )

    return router
