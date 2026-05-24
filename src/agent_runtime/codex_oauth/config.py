# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CodexOAuthConfig:
    data_dir: str = "~/.spread-ai/codex-oauth"
    client_id: str = "app_EMoamEEZ73f0CkXaXp7hrann"
    issuer: str = "https://auth.openai.com"
    codex_api_base_url: str = "https://chatgpt.com/backend-api/codex"
    client_originator: str = "mindloop.spread-ai"
    oauth_originator: str = "opencode"
    oauth_callback_host: str = "localhost"
    oauth_callback_port: int = 1455
    oauth_callback_path: str = "/auth/callback"
    user_agent: str = "mindloop.spread-ai"
    oauth_redirect_base_url: str | None = None
    login_timeout_seconds: int = 300
