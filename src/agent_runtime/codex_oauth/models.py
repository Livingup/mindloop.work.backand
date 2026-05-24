# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass

CODEX_PROVIDER_ID = "openai_codex"
CODEX_API_KEY_REF_PREFIX = "codex-oauth-ref:"


@dataclass(frozen=True, slots=True)
class CodexPendingOAuthLogin:
    state: str
    code_verifier: str
    code_challenge: str
    redirect_uri: str
    originator: str
    created_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class CodexOAuthAccount:
    credential_id: str
    account_id: str | None
    email: str | None
    display_name: str | None
    access_token: str
    refresh_token: str
    id_token: str | None
    expires_at: int
    scopes: str | None
    originator: str
    session_id: str
    disabled: bool
    created_at: str
    updated_at: str
    last_used_at: str | None = None

    @property
    def label(self) -> str:
        if self.display_name and self.display_name.strip():
            return self.display_name.strip()
        if self.email and self.email.strip():
            return self.email.strip()
        if self.account_id and self.account_id.strip():
            return self.account_id.strip()
        return self.credential_id


@dataclass(frozen=True, slots=True)
class CodexOAuthLoginStart:
    authorization_url: str
    state: str
    redirect_uri: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class CodexResolvedToken:
    credential_id: str
    access_token: str
    account_id: str | None
    session_id: str
    originator: str
    user_agent: str


def codex_api_key_ref(credential_id: str) -> str:
    return f"{CODEX_API_KEY_REF_PREFIX}{credential_id}"


def parse_codex_api_key_ref(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text.startswith(CODEX_API_KEY_REF_PREFIX):
        return None
    credential_id = text[len(CODEX_API_KEY_REF_PREFIX):].strip()
    return credential_id or None
