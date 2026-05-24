# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from agent_runtime.codex_oauth.config import CodexOAuthConfig
from agent_runtime.codex_oauth.infrastructure.sqlite_codex_oauth_repository import (
    SqliteCodexOAuthRepository,
)
from agent_runtime.codex_oauth.models import (
    CODEX_PROVIDER_ID,
    CodexOAuthAccount,
    CodexOAuthLoginStart,
    CodexPendingOAuthLogin,
    CodexResolvedToken,
    codex_api_key_ref,
    parse_codex_api_key_ref,
)
from agent_runtime.input_modality import INPUT_MODALITY_TEXT
from agent_runtime.llm_api_protocol import API_FORMAT_CODEX_RESPONSES
from agent_runtime.llm_model_registry.llm_model_metadata import LlmModelMetadata
from agent_runtime.llm_model_registry.llm_provider_definition import (
    LlmApiKey,
    LlmProviderDefinition,
    LlmProviderModelBinding,
    LlmRetryPolicy,
)
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmModelMetadataSaveRequest,
    LlmModelRegistryGateway,
    LlmProviderSaveRequest,
)

_DEFAULT_CODEX_MODELS: tuple[str, ...] = (
    "gpt-5.5",
    "gpt-5.2",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.4",
    "gpt-5.4-mini",
)


class CodexOAuthError(OSError):
    pass


class CodexOAuthService:
    def __init__(
        self,
        repository: SqliteCodexOAuthRepository,
        config_accessor: Any,
        llm_registry_gateway: LlmModelRegistryGateway | None = None,
    ) -> None:
        self._repository = repository
        self._config_accessor = config_accessor
        self._llm_registry_gateway = llm_registry_gateway

    def start_login(self, redirect_uri: str) -> CodexOAuthLoginStart:
        config = self._config()
        now = _utc_now()
        expires_at = now + timedelta(seconds=config.login_timeout_seconds)
        code_verifier = _generate_code_verifier()
        code_challenge = _code_challenge(code_verifier)
        state = _generate_state()
        pending = CodexPendingOAuthLogin(
            state=state,
            code_verifier=code_verifier,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            originator=config.client_originator,
            created_at=_iso(now),
            expires_at=_iso(expires_at),
        )
        self._repository.delete_expired_pending_logins(_iso(now))
        self._repository.save_pending_login(pending)
        params = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid profile email offline_access",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
            "originator": config.oauth_originator,
        })
        return CodexOAuthLoginStart(
            authorization_url=f"{config.issuer.rstrip('/')}/oauth/authorize?{params}",
            state=state,
            redirect_uri=redirect_uri,
            expires_at=_iso(expires_at),
        )

    def default_redirect_uri(self, request_base_url: str) -> str:
        config = self._config()
        if not config.oauth_redirect_base_url:
            return self.local_callback_redirect_uri()
        base_url = (config.oauth_redirect_base_url or request_base_url).rstrip("/")
        return f"{base_url}/api/codex-oauth/login/callback"

    def local_callback_redirect_uri(self) -> str:
        config = self._config()
        path = _normalize_callback_path(config.oauth_callback_path)
        return f"http://{config.oauth_callback_host}:{config.oauth_callback_port}{path}"

    def local_callback_bind_address(self) -> tuple[str, int, str]:
        config = self._config()
        return (
            config.oauth_callback_host,
            config.oauth_callback_port,
            _normalize_callback_path(config.oauth_callback_path),
        )

    def complete_login(self, *, state: str, code: str) -> CodexOAuthAccount:
        pending = self._repository.find_pending_login(state)
        if pending is None:
            raise CodexOAuthError("Codex OAuth 登录状态不存在或已过期。")
        if _parse_iso(pending.expires_at) <= _utc_now():
            self._repository.delete_pending_login(state)
            raise CodexOAuthError("Codex OAuth 登录已超时，请重新发起授权。")
        self._repository.delete_pending_login(state)
        tokens = self._exchange_code_for_tokens(code, pending.redirect_uri, pending.code_verifier)
        account = self._account_from_tokens(tokens, pending.originator)
        self._repository.save_account(account)
        self.ensure_builtin_provider()
        return account

    def list_accounts(self) -> list[CodexOAuthAccount]:
        return self._repository.find_all_accounts()

    def refresh_account(self, credential_id: str) -> CodexOAuthAccount:
        account = self._repository.find_account_by_id(credential_id)
        if account is None:
            raise KeyError(f"Codex 授权账号不存在: {credential_id}")
        refreshed = self._refresh_account(account)
        self.ensure_builtin_provider()
        return refreshed

    def delete_account(self, credential_id: str) -> bool:
        deleted = self._repository.delete_account(credential_id)
        if deleted:
            self.ensure_builtin_provider()
        return deleted

    def resolve_api_key_ref(self, api_key_ref: str | None) -> CodexResolvedToken:
        credential_id = parse_codex_api_key_ref(api_key_ref)
        if credential_id is None:
            raise CodexOAuthError("Codex provider 的 API Key 必须是 codex-oauth-ref:<credential_id>。")
        account = self._repository.find_account_by_id(credential_id)
        if account is None:
            raise CodexOAuthError(f"Codex 授权账号不存在: {credential_id}")
        if account.disabled:
            raise CodexOAuthError(f"Codex 授权账号已禁用: {credential_id}")
        if account.expires_at <= int(time.time()) + 60:
            account = self._refresh_account(account)
        now = _iso(_utc_now())
        account = replace(account, last_used_at=now, updated_at=now)
        self._repository.save_account(account)
        config = self._config()
        return CodexResolvedToken(
            credential_id=account.credential_id,
            access_token=account.access_token,
            account_id=account.account_id,
            session_id=account.session_id,
            originator=account.originator or config.client_originator,
            user_agent=config.user_agent,
        )

    def ensure_builtin_provider(self) -> None:
        gateway = self._llm_registry_gateway
        if gateway is None:
            return
        config = self._config()
        for model_id in _DEFAULT_CODEX_MODELS:
            try:
                gateway.get_model_metadata(model_id)
            except KeyError:
                gateway.save_model_metadata(
                    LlmModelMetadataSaveRequest.from_metadata(_default_model_metadata(model_id))
                )
        accounts = [account for account in self.list_accounts() if not account.disabled]
        account_by_ref = {
            codex_api_key_ref(account.credential_id): account
            for account in accounts
        }
        default_bindings = tuple(_default_binding(model_id) for model_id in _DEFAULT_CODEX_MODELS)
        try:
            existing = gateway.get_provider(CODEX_PROVIDER_ID)
        except KeyError:
            account_keys = tuple(
                LlmApiKey(key=codex_api_key_ref(account.credential_id), name=account.label)
                for account in accounts
            )
            provider = LlmProviderDefinition(
                provider_id=CODEX_PROVIDER_ID,
                group="official",
                api_base_url=config.codex_api_base_url,
                custom_headers={},
                api_keys=account_keys,
                retry=LlmRetryPolicy(max_retries_per_key=1),
                models=default_bindings,
            )
        else:
            ordered_refs: list[str] = []
            for item in existing.api_keys:
                if item.key in account_by_ref and item.key not in ordered_refs:
                    ordered_refs.append(item.key)
            for account in accounts:
                item_ref = codex_api_key_ref(account.credential_id)
                if item_ref not in ordered_refs:
                    ordered_refs.append(item_ref)
            account_keys = tuple(
                LlmApiKey(key=item_ref, name=account_by_ref[item_ref].label)
                for item_ref in ordered_refs
                if item_ref in account_by_ref
            )
            default_ids = {binding.id for binding in default_bindings}
            custom_bindings = tuple(
                binding for binding in existing.models if binding.id not in default_ids
            )
            provider = LlmProviderDefinition(
                provider_id=existing.provider_id,
                group=existing.group or "official",
                api_base_url=config.codex_api_base_url,
                custom_headers=existing.custom_headers,
                api_keys=account_keys,
                retry=existing.retry,
                models=default_bindings + custom_bindings,
            )
        gateway.save_provider(LlmProviderSaveRequest.from_provider(provider))

    def _refresh_account(self, account: CodexOAuthAccount) -> CodexOAuthAccount:
        tokens = self._refresh_tokens(account.refresh_token)
        now = _utc_now()
        claims = _combined_claims(tokens)
        refreshed = replace(
            account,
            account_id=_extract_account_id(claims) or account.account_id,
            email=_extract_email(claims) or account.email,
            display_name=_extract_display_name(claims) or account.display_name,
            access_token=str(tokens.get("access_token") or account.access_token),
            refresh_token=str(tokens.get("refresh_token") or account.refresh_token),
            id_token=(str(tokens["id_token"]) if tokens.get("id_token") else account.id_token),
            expires_at=int(time.time()) + int(tokens.get("expires_in") or 3600),
            scopes=str(tokens.get("scope") or account.scopes or "openid profile email offline_access"),
            updated_at=_iso(now),
        )
        self._repository.save_account(refreshed)
        return refreshed

    def _account_from_tokens(self, tokens: dict[str, Any], originator: str) -> CodexOAuthAccount:
        now = _utc_now()
        claims = _combined_claims(tokens)
        credential_id = f"codex-{uuid.uuid4().hex}"
        return CodexOAuthAccount(
            credential_id=credential_id,
            account_id=_extract_account_id(claims),
            email=_extract_email(claims),
            display_name=_extract_display_name(claims),
            access_token=str(tokens.get("access_token") or ""),
            refresh_token=str(tokens.get("refresh_token") or ""),
            id_token=(str(tokens["id_token"]) if tokens.get("id_token") else None),
            expires_at=int(time.time()) + int(tokens.get("expires_in") or 3600),
            scopes=str(tokens.get("scope") or "openid profile email offline_access"),
            originator=originator,
            session_id=str(uuid.uuid4()),
            disabled=False,
            created_at=_iso(now),
            updated_at=_iso(now),
        )

    def _exchange_code_for_tokens(self, code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
        config = self._config()
        return _post_form(
            f"{config.issuer.rstrip('/')}/oauth/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": config.client_id,
                "code_verifier": code_verifier,
            },
        )

    def _refresh_tokens(self, refresh_token: str) -> dict[str, Any]:
        config = self._config()
        return _post_form(
            f"{config.issuer.rstrip('/')}/oauth/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": config.client_id,
            },
        )

    def _config(self) -> CodexOAuthConfig:
        return self._config_accessor()


def _default_model_metadata(model_id: str) -> LlmModelMetadata:
    return LlmModelMetadata(
        model_id=model_id,
        group="codex",
        context_window=400000,
        max_output_tokens=128000,
        compression_threshold=300000,
        supports_tool_calling=True,
        supports_structured_output=False,
        supported_input_modalities=(INPUT_MODALITY_TEXT,),
    )


def _default_binding(model_id: str) -> LlmProviderModelBinding:
    return LlmProviderModelBinding(
        id=model_id,
        model_id=model_id,
        model_alias=model_id,
        api_format=API_FORMAT_CODEX_RESPONSES,
        tool_choice=json.dumps({}, ensure_ascii=False),
        thinking=json.dumps({}, ensure_ascii=False),
        custom_request_params=json.dumps({
            "stream": True,
            "reasoning": {"effort": "xhigh"},
        }, ensure_ascii=False),
    )


def _post_form(url: str, form: dict[str, str]) -> dict[str, Any]:
    payload = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise CodexOAuthError(f"Codex OAuth HTTP {exc.code}: {body[:500]}") from exc
    except Exception as exc:
        raise CodexOAuthError(f"Codex OAuth 请求失败: {exc.__class__.__name__}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CodexOAuthError("Codex OAuth token 响应不是合法 JSON。") from exc
    if not isinstance(data, dict):
        raise CodexOAuthError("Codex OAuth token 响应结构无效。")
    if not data.get("access_token"):
        raise CodexOAuthError("Codex OAuth token 响应缺少 access_token。")
    if form.get("grant_type") == "authorization_code" and not data.get("refresh_token"):
        raise CodexOAuthError("Codex OAuth token 响应缺少 refresh_token。")
    return data


def _combined_claims(tokens: dict[str, Any]) -> dict[str, Any]:
    claims: dict[str, Any] = {}
    for key in ("id_token", "access_token"):
        token = tokens.get(key)
        if isinstance(token, str):
            parsed = _parse_jwt_claims(token)
            if parsed:
                claims.update(parsed)
    return claims


def _parse_jwt_claims(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode((payload + padding).encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _extract_account_id(claims: dict[str, Any]) -> str | None:
    direct = claims.get("chatgpt_account_id")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    auth_claims = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claims, dict):
        nested = auth_claims.get("chatgpt_account_id")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    organizations = claims.get("organizations")
    if isinstance(organizations, list) and organizations:
        first = organizations[0]
        if isinstance(first, dict):
            org_id = first.get("id")
            if isinstance(org_id, str) and org_id.strip():
                return org_id.strip()
    return None


def _extract_email(claims: dict[str, Any]) -> str | None:
    value = claims.get("email")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _extract_display_name(claims: dict[str, Any]) -> str | None:
    value = claims.get("name") or claims.get("preferred_username")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _generate_code_verifier() -> str:
    return _base64_url(secrets.token_bytes(32))


def _code_challenge(verifier: str) -> str:
    return _base64_url(hashlib.sha256(verifier.encode("ascii")).digest())


def _generate_state() -> str:
    return _base64_url(secrets.token_bytes(32))


def _base64_url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _normalize_callback_path(value: str) -> str:
    text = value.strip().replace("\\", "/") or "/auth/callback"
    return text if text.startswith("/") else f"/{text}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)
