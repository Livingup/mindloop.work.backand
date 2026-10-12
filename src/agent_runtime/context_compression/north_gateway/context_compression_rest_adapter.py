# -*- coding: utf-8 -*-
"""上下文压缩与观察者 REST API 适配器 (Context Compression REST Adapter)."""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from agent_runtime.context_compression.config import ContextCompressionConfig
from agent_runtime.context_compression.settings_repository import (
    ContextCompressionSettingsRepository,
)

logger = logging.getLogger(__name__)


class ContextCompressionSettingsResponse(BaseModel):
    """上下文压缩设置响应。"""

    enabled: bool = True
    llm_policy_group_id: str | None = None
    updated_at: str | None = None


class ContextCompressionSettingsUpdateBody(BaseModel):
    """上下文压缩设置更新请求体。"""

    llm_policy_group_id: str | None = None


class ObserverPromptResponse(BaseModel):
    """上下文观察者系统提示词响应。"""

    prompt_text: str


def _load_system_note_update_prompt() -> str:
    """读取官方内置的 system_note_update.md 文件全文。"""
    prompt_path = (
        Path(__file__).resolve().parent.parent.parent
        / "task_note"
        / "prompts"
        / "system_note_update.md"
    )
    if not prompt_path.exists():
        return "# 上下文观察者系统提示词\n（未找到 prompts/system_note_update.md 文件）"
    try:
        return prompt_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.error("读取 system_note_update.md 失败: %s", exc)
        return f"# 读取提示词失败: {exc}"


def create_context_compression_router(
    conversation_repository: Any = None,
    *,
    prompt_repository: Any = None,
    settings_repository: ContextCompressionSettingsRepository | None = None,
    llm_registry_gateway: Any = None,
    archive_repository: Any = None,
    config_accessor: Callable[[], ContextCompressionConfig] | None = None,
) -> APIRouter:
    """创建上下文压缩与观察者 REST API 路由器。"""
    del conversation_repository, prompt_repository, llm_registry_gateway, archive_repository
    router = APIRouter()

    @router.get("/settings", response_model=ContextCompressionSettingsResponse)
    async def get_settings() -> ContextCompressionSettingsResponse:
        """获取当前观察者 LLM 驱动策略设置。"""
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

        return ContextCompressionSettingsResponse(
            enabled=enabled,
            llm_policy_group_id=policy_id,
            updated_at=updated_at,
        )

    @router.put("/settings", response_model=ContextCompressionSettingsResponse)
    async def update_settings(
        body: ContextCompressionSettingsUpdateBody,
    ) -> ContextCompressionSettingsResponse:
        """更新观察者绑定的 LLM 执行策略组。"""
        policy_id = body.llm_policy_group_id.strip() if body.llm_policy_group_id else None
        if settings_repository is not None:
            saved = settings_repository.update_settings(llm_policy_group_id=policy_id)
            return ContextCompressionSettingsResponse(
                enabled=saved.enabled,
                llm_policy_group_id=saved.llm_policy_group_id,
                updated_at=saved.updated_at,
            )

        return ContextCompressionSettingsResponse(
            enabled=True,
            llm_policy_group_id=policy_id,
            updated_at=None,
        )

    @router.get("/observer-prompt", response_model=ObserverPromptResponse)
    async def get_observer_prompt() -> ObserverPromptResponse:
        """获取 Task Note 观察者系统提示词全文。"""
        prompt_text = _load_system_note_update_prompt()
        return ObserverPromptResponse(prompt_text=prompt_text)

    return router
