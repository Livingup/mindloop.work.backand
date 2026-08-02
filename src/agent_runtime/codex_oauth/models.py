# -*- coding: utf-8 -*-
"""Public product constants for the managed louyi-codex integration."""

CODEX_PROVIDER_ID = "openai_codex"
CODEX_SESSION_NAMESPACE = "mindloop.spread-ai.openai_codex"
CODEX_SESSION_HEADER_NAME = "session-id"
CODEX_DISPLAY_API_BASE_URL = "https://chatgpt.com/backend-api/codex"

__all__ = [
    "CODEX_DISPLAY_API_BASE_URL",
    "CODEX_PROVIDER_ID",
    "CODEX_SESSION_HEADER_NAME",
    "CODEX_SESSION_NAMESPACE",
]
