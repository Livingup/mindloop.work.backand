# -*- coding: utf-8 -*-
"""louyi-codex application service used by the managed openai_codex provider."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, NoReturn, Sequence

from agent_runtime.codex_oauth.config import CodexOAuthConfig
from agent_runtime.codex_oauth.diagnostics import (
    CodexSdkRawEventSink,
    proxy_diagnostic,
    sdk_error_projection,
    short_identifier,
    stable_fingerprint,
 )
from agent_runtime.codex_oauth.models import (
    CODEX_DISPLAY_API_BASE_URL,
    CODEX_PROVIDER_ID,
    CODEX_SESSION_HEADER_NAME,
    CODEX_SESSION_NAMESPACE,
 )
from agent_runtime.input_modality import INPUT_MODALITY_TEXT
from agent_runtime.llm_api_protocol import API_FORMAT_CODEX_RESPONSES
from agent_runtime.llm_model_registry.llm_model_metadata import LlmModelMetadata
from agent_runtime.llm_model_registry.llm_provider_definition import (
    LlmProviderDefinition,
    LlmProviderModelBinding,
    LlmRetryPolicy,
 )
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmModelMetadataSaveRequest,
    LlmModelRegistryGateway,
    LlmProviderSaveRequest,
 )

logger = logging.getLogger("agent_runtime.codex_sdk")

_DEFAULT_CODEX_MODELS: tuple[str, ...] = (
    "gpt-5.5",
    "gpt-5.2",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.4",
    "gpt-5.4-mini",
 )


class CodexOAuthError(RuntimeError):
    """Stable Runtime-facing error for SDK management and lifecycle failures."""

    def __init__(
        self,
        message: str,
        *,
        sdk_error: BaseException | None = None,
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.sdk_error = sdk_error
        self.diagnostic = diagnostic or {}
        self.code = self.diagnostic.get("code", "codex_sdk_error")
        self.http_status = self.diagnostic.get("http_status")
        self.retryable = bool(self.diagnostic.get("retryable", False))
        self.retry_at = self.diagnostic.get("retry_at")
        self.partial_output = bool(self.diagnostic.get("partial_output", False))


class CodexOAuthService:
    """Owns the process-wide AsyncCodexClient and projects its Stable resources."""

    def __init__(
        self,
        config_accessor: Any,
        llm_registry_gateway: LlmModelRegistryGateway | None = None,
    ) -> None:
        self._config_accessor = config_accessor
        self._llm_registry_gateway = llm_registry_gateway
        self._client: Any | None = None
        self._storage_dir: Path | None = None

    @property
    def opened(self) -> bool:
        return self._client is not None

    @property
    def client(self) -> Any:
        if self._client is None:
            raise CodexOAuthError("louyi-codex SDK 尚未打开。")
        return self._client

    def set_llm_registry_gateway(self, gateway: LlmModelRegistryGateway | None) -> None:
        if gateway is not None:
            self._llm_registry_gateway = gateway

    async def open(self) -> None:
        if self._client is not None:
            return
        sdk = _sdk_module()
        config = self._config()
        storage_dir = Path(config.data_dir).expanduser().resolve()
        sdk_config = sdk.SDKConfig(
            storage_dir=storage_dir,
            session_header_name=CODEX_SESSION_HEADER_NAME,
            session_namespace=CODEX_SESSION_NAMESPACE,
        )
        client = sdk.AsyncCodexClient(sdk_config, event_sink=CodexSdkRawEventSink())
        started_at = time.perf_counter()
        try:
            await client.open()
        except Exception as exc:
            diagnostic = sdk_error_projection(exc, stage="lifecycle_open")
            logger.error(
                "codex_sdk.lifecycle.open_failed storage=%s details=%s",
                storage_dir,
                diagnostic,
            )
            with suppress(Exception, asyncio.CancelledError):
                await client.close()
            raise CodexOAuthError(
                diagnostic["message"],
                sdk_error=exc,
                diagnostic=diagnostic,
            ) from None
        self._client = client
        self._storage_dir = storage_dir
        logger.info(
            "codex_sdk.lifecycle.opened version=%s storage=%s namespace=%s duration_ms=%d",
            getattr(sdk, "__version__", "unknown"),
            storage_dir,
            CODEX_SESSION_NAMESPACE,
            int((time.perf_counter() - started_at) * 1000),
        )

    async def close(self) -> None:
        client = self._client
        if client is None:
            return
        started_at = time.perf_counter()
        try:
            await client.close()
        except Exception as exc:
            diagnostic = sdk_error_projection(exc, stage="lifecycle_close")
            logger.error("codex_sdk.lifecycle.close_failed details=%s", diagnostic)
            raise CodexOAuthError(
                diagnostic["message"],
                sdk_error=exc,
                diagnostic=diagnostic,
            ) from None
        finally:
            self._client = None
            self._storage_dir = None
        logger.info(
            "codex_sdk.lifecycle.closed duration_ms=%d",
            int((time.perf_counter() - started_at) * 1000),
        )

    async def ensure_builtin_provider(self) -> None:
        gateway = self._llm_registry_gateway
        if gateway is None:
            return
        for model_id in _DEFAULT_CODEX_MODELS:
            try:
                gateway.get_model_metadata(model_id)
            except KeyError:
                gateway.save_model_metadata(
                    LlmModelMetadataSaveRequest.from_metadata(_default_model_metadata(model_id))
                )
        default_bindings = tuple(_default_binding(model_id) for model_id in _DEFAULT_CODEX_MODELS)
        try:
            existing = gateway.get_provider(CODEX_PROVIDER_ID)
        except KeyError:
            provider = LlmProviderDefinition(
                provider_id=CODEX_PROVIDER_ID,
                group="official",
                api_base_url=CODEX_DISPLAY_API_BASE_URL,
                custom_headers={},
                api_keys=(),
                retry=_sdk_owned_retry_policy(),
                models=default_bindings,
            )
        else:
            existing_by_id = {binding.id: binding for binding in existing.models}
            default_ids = {binding.id for binding in default_bindings}
            managed_default_bindings = tuple(
                _sdk_owned_binding(existing_by_id[binding.id])
                if binding.id in existing_by_id
                else binding
                for binding in default_bindings
            )
            custom_bindings = tuple(
                _sdk_owned_binding(binding)
                for binding in existing.models
                if binding.id not in default_ids
            )
            provider = LlmProviderDefinition(
                provider_id=existing.provider_id,
                group=existing.group or "official",
                api_base_url=CODEX_DISPLAY_API_BASE_URL,
                custom_headers=dict(existing.custom_headers),
                api_keys=(),
                retry=_sdk_owned_retry_policy(),
                models=managed_default_bindings + custom_bindings,
            )
        gateway.save_provider(LlmProviderSaveRequest.from_provider(provider))
        logger.info(
            "codex_sdk.provider.synced provider_id=%s model_count=%d api_key_count=0",
            CODEX_PROVIDER_ID,
            len(provider.models),
        )

    async def begin_browser_auth(self, proxy_url: str | None = None) -> Any:
        """Start new-account browser authorization with one account proxy.

        louyi exposes separate technical parameters for authorization transport and
        successful-account defaults; both receive the same parsed product proxy.
        """
        sdk = _sdk_module()
        account_proxy = _proxy_config(sdk, proxy_url)
        started_at = time.perf_counter()
        result = await self._management_call(
            stage="oauth_browser_start",
            operation=lambda: self.client.auth.begin_browser_pkce(
                auth_proxy=account_proxy,
                account_defaults=sdk.NewAccountDefaults(proxy=account_proxy),
            ),
            proxy=account_proxy,
        )
        logger.info(
            "codex_sdk.oauth.browser_started attempt_id=%s expires_at=%s proxy=%s duration_ms=%d",
            short_identifier(result.attempt_id),
            result.expires_at,
            proxy_diagnostic(account_proxy),
            int((time.perf_counter() - started_at) * 1000),
        )
        return result

    async def begin_device_auth(self, proxy_url: str | None = None) -> Any:
        """Start new-account device authorization with one account proxy.

        The SDK parameter split is an implementation detail; the Runtime passes
        one identical parsed proxy to both technical stages of this account flow.
        """
        sdk = _sdk_module()
        account_proxy = _proxy_config(sdk, proxy_url)
        started_at = time.perf_counter()
        result = await self._management_call(
            stage="oauth_device_start",
            operation=lambda: self.client.auth.begin_device_code(
                auth_proxy=account_proxy,
                account_defaults=sdk.NewAccountDefaults(proxy=account_proxy),
            ),
            proxy=account_proxy,
        )
        logger.info(
            "codex_sdk.oauth.device_started attempt_id=%s expires_at=%s poll_interval=%s proxy=%s duration_ms=%d",
            short_identifier(result.attempt_id),
            result.expires_at,
            result.interval_seconds,
            proxy_diagnostic(account_proxy),
            int((time.perf_counter() - started_at) * 1000),
        )
        return result

    async def get_auth_attempt(self, attempt_id: str) -> Any:
        result = await self._management_call(
            stage="oauth_attempt_get",
            operation=lambda: self.client.auth.get_attempt(attempt_id),
            attempt_id=attempt_id,
        )
        logger.info(
            "codex_sdk.oauth.attempt_loaded attempt_id=%s kind=%s status=%s completed_account=%s",
            short_identifier(attempt_id),
            _enum_value(result.kind),
            _enum_value(result.status),
            short_identifier(getattr(getattr(result, "result", None), "account", None).account_id)
            if getattr(getattr(result, "result", None), "account", None) is not None
            else None,
        )
        return result

    async def poll_device_auth(self, attempt_id: str) -> Any:
        started_at = time.perf_counter()
        result = await self._management_call(
            stage="oauth_device_poll",
            operation=lambda: self.client.auth.poll_device_code_once(attempt_id),
            attempt_id=attempt_id,
        )
        logger.info(
            "codex_sdk.oauth.device_polled attempt_id=%s status=%s next_poll_at=%s account_id=%s duration_ms=%d",
            short_identifier(attempt_id),
            _enum_value(result.status),
            result.next_poll_at,
            short_identifier(getattr(getattr(result.auth_result, "account", None), "account_id", None)),
            int((time.perf_counter() - started_at) * 1000),
        )
        return result

    async def complete_browser_callback(self, attempt_id: str, callback_url: str) -> Any:
        started_at = time.perf_counter()
        result = await self._management_call(
            stage="oauth_browser_callback",
            operation=lambda: self.client.auth.complete_browser_callback_url(
                attempt_id,
                callback_url,
            ),
            attempt_id=attempt_id,
        )
        logger.info(
            "codex_sdk.oauth.browser_completed attempt_id=%s account_id=%s created=%s duration_ms=%d",
            short_identifier(attempt_id),
            short_identifier(result.account.account_id),
            result.created,
            int((time.perf_counter() - started_at) * 1000),
        )
        return result

    async def cancel_auth_attempt(self, attempt_id: str) -> Any:
        result = await self._management_call(
            stage="oauth_attempt_cancel",
            operation=lambda: self.client.auth.cancel_attempt(attempt_id),
            attempt_id=attempt_id,
        )
        logger.info(
            "codex_sdk.oauth.cancelled attempt_id=%s kind=%s status=%s",
            short_identifier(attempt_id),
            _enum_value(result.kind),
            _enum_value(result.status),
        )
        return result

    async def list_accounts(self, *, include_disabled: bool = True) -> list[Any]:
        accounts = await self._management_call(
            stage="account_list",
            operation=lambda: self.client.accounts.list(include_disabled=include_disabled),
        )
        logger.info(
            "codex_sdk.accounts.listed count=%d include_disabled=%s health=%s",
            len(accounts),
            include_disabled,
            _health_counts(accounts),
        )
        return accounts

    async def get_account(self, account_id: str) -> Any:
        account = await self._management_call(
            stage="account_get",
            operation=lambda: self.client.accounts.get(account_id),
            account_id=account_id,
        )
        logger.info(
            "codex_sdk.account.loaded account_id=%s enabled=%s health=%s version=%s proxy=%s",
            short_identifier(account_id),
            account.enabled,
            _enum_value(account.status.health),
            account.version,
            proxy_diagnostic(account.proxy),
        )
        return account

    async def update_account(
        self,
        account_id: str,
        *,
        alias_present: bool = False,
        alias: str | None = None,
        priority_present: bool = False,
        priority: int | None = None,
        proxy_present: bool = False,
        proxy_url: str | None = None,
        expected_version: int | None = None,
    ) -> Any:
        sdk = _sdk_module()
        patch_kwargs: dict[str, Any] = {}
        changed_fields: list[str] = []
        if alias_present:
            patch_kwargs["alias"] = alias
            changed_fields.append("alias")
        if priority_present:
            if priority is None:
                raise ValueError("priority 不能为 null。")
            patch_kwargs["priority"] = priority
            changed_fields.append("priority")
        if proxy_present:
            patch_kwargs["proxy"] = _proxy_config(sdk, proxy_url)
            changed_fields.append("proxy")
        account = await self._management_call(
            stage="account_update",
            operation=lambda: self.client.accounts.update(
                account_id,
                sdk.AccountPatch(**patch_kwargs),
                expected_version=expected_version,
            ),
            proxy=patch_kwargs.get("proxy") if proxy_present else None,
            account_id=account_id,
        )
        logger.info(
            "codex_sdk.account.updated account_id=%s version=%s fields=%s enabled=%s health=%s proxy=%s",
            short_identifier(account_id),
            account.version,
            changed_fields,
            account.enabled,
            _enum_value(account.status.health),
            proxy_diagnostic(account.proxy),
        )
        return account

    async def reorder_accounts(
        self,
        account_ids: Sequence[str],
        *,
        expected_versions: Mapping[str, int] | None = None,
    ) -> list[Any]:
        accounts = await self._management_call(
            stage="account_reorder",
            operation=lambda: self.client.accounts.reorder(
                account_ids,
                expected_versions=expected_versions,
            ),
        )
        logger.info(
            "codex_sdk.accounts.reordered account_ids=%s versions_checked=%s",
            [short_identifier(item) for item in account_ids],
            bool(expected_versions),
        )
        return accounts

    async def enable_account(self, account_id: str) -> Any:
        account = await self._management_call(
            stage="account_enable",
            operation=lambda: self.client.accounts.enable(account_id),
            account_id=account_id,
        )
        logger.info(
            "codex_sdk.account.enabled account_id=%s version=%s health=%s",
            short_identifier(account_id),
            account.version,
            _enum_value(account.status.health),
        )
        return account

    async def disable_account(self, account_id: str, *, force: bool = False) -> Any:
        result = await self._management_call(
            stage="account_disable",
            operation=lambda: self.client.accounts.disable(account_id, force=force),
            account_id=account_id,
        )
        _log_operation("disable", result)
        return result

    async def recover_account(self, account_id: str) -> Any:
        started_at = time.perf_counter()
        result = await self._management_call(
            stage="account_recover",
            operation=lambda: self.client.accounts.recover(account_id),
            account_id=account_id,
        )
        logger.info(
            "codex_sdk.account.recovered account_id=%s recovered=%s health=%s checks=%s duration_ms=%d",
            short_identifier(account_id),
            result.recovered,
            _enum_value(result.account.status.health),
            [{"kind": _enum_value(check.kind), "passed": check.passed, "error_code": getattr(check.error, "code", None)} for check in result.checks],
            int((time.perf_counter() - started_at) * 1000),
        )
        return result

    async def delete_account(self, account_id: str, *, force: bool = False) -> Any:
        result = await self._management_call(
            stage="account_delete",
            operation=lambda: self.client.accounts.delete(account_id, force=force),
            account_id=account_id,
        )
        _log_operation("delete", result)
        return result

    async def get_operation(self, operation_id: str) -> Any:
        result = await self._management_call(
            stage="operation_get",
            operation=lambda: self.client.operations.get(operation_id),
        )
        _log_operation("get", result)
        return result

    async def wait_operation(self, operation_id: str, *, timeout_seconds: float) -> Any:
        result = await self._management_call(
            stage="operation_wait",
            operation=lambda: self.client.operations.wait(
                operation_id,
                timeout=timeout_seconds,
            ),
        )
        _log_operation("wait", result)
        return result

    async def get_cached_quota(self, account_id: str) -> Any | None:
        snapshot = await self._management_call(
            stage="quota_cached_get",
            operation=lambda: self.client.quotas.get_cached(account_id),
            account_id=account_id,
        )
        logger.info(
            "codex_sdk.quota.cached account_id=%s present=%s",
            short_identifier(account_id),
            snapshot is not None,
        )
        return snapshot

    async def refresh_quota(self, account_id: str) -> Any:
        started_at = time.perf_counter()
        account = await self._management_call(
            stage="quota_refresh_account_get",
            operation=lambda: self.client.accounts.get(account_id),
            account_id=account_id,
        )
        account_proxy = getattr(account, "proxy", None)
        snapshot = await self._management_call(
            stage="quota_refresh",
            operation=lambda: self.client.quotas.refresh(account_id),
            proxy=account_proxy if account_proxy is not None else {"configured": False},
            account_id=account_id,
        )
        logger.info(
            "codex_sdk.quota.refreshed account_id=%s source=%s five_hour=%s weekly=%s duration_ms=%d",
            short_identifier(account_id),
            snapshot.source,
            _quota_window_diagnostic(snapshot.five_hour),
            _quota_window_diagnostic(snapshot.weekly),
            int((time.perf_counter() - started_at) * 1000),
        )
        return snapshot

    async def get_routing_strategy(self) -> Any:
        strategy = await self._management_call(
            stage="routing_get",
            operation=lambda: self.client.accounts.get_routing_strategy(),
        )
        logger.info("codex_sdk.routing.loaded strategy=%s", _enum_value(strategy))
        return strategy

    async def set_routing_strategy(self, strategy: str) -> Any:
        sdk = _sdk_module()
        updated = await self._management_call(
            stage="routing_set",
            operation=lambda: self.client.accounts.set_routing_strategy(
                sdk.RoutingStrategy(strategy)
            ),
        )
        logger.info("codex_sdk.routing.updated strategy=%s", _enum_value(updated))
        return updated

    async def delete_session_binding(self, conversation_id: str) -> bool:
        deleted = await self._management_call(
            stage="session_binding_delete",
            operation=lambda: self.client.sessions.delete_binding(conversation_id),
        )
        logger.info(
            "codex_sdk.session.binding_deleted session_fingerprint=%s deleted=%s",
            stable_fingerprint(conversation_id),
            deleted,
        )
        return deleted

    async def _management_call(
        self,
        *,
        stage: str,
        operation: Callable[[], Awaitable[Any]],
        proxy: Any = None,
        account_id: str | None = None,
        attempt_id: str | None = None,
    ) -> Any:
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._raise_management_error(
                exc,
                stage=stage,
                proxy=proxy,
                account_id=account_id,
                attempt_id=attempt_id,
            )

    @staticmethod
    def _raise_management_error(
        exc: BaseException,
        *,
        stage: str,
        proxy: Any = None,
        account_id: str | None = None,
        attempt_id: str | None = None,
    ) -> NoReturn:
        diagnostic = sdk_error_projection(
            exc,
            stage=stage,
            proxy=proxy,
            account_id=account_id,
            attempt_id=attempt_id,
        )
        logger.error("codex_sdk.management.failed details=%s", diagnostic)
        raise CodexOAuthError(
            diagnostic["message"],
            sdk_error=exc,
            diagnostic=diagnostic,
        ) from None

    def _config(self) -> CodexOAuthConfig:
        return self._config_accessor()


def _sdk_module() -> Any:
    try:
        import louyi_codex
    except ImportError:
        raise CodexOAuthError(
            "未安装 louyi-codex SDK。请先从当前 SDK 源码构建并安装 wheel。"
        ) from None
    return louyi_codex


def _proxy_config(sdk: Any, proxy_url: str | None) -> Any | None:
    from urllib.parse import urlsplit
    text = (proxy_url or "").strip()
    if not text:
        return None
    try:
        urlsplit(text).port
    except ValueError:
        raise ValueError("proxy_url 端口无效。") from None
    return sdk.ProxyConfig(text)



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
    import json
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


def _sdk_owned_retry_policy() -> LlmRetryPolicy:
    return LlmRetryPolicy(
        max_retries_per_key=0,
        initial_delay_seconds=0,
        max_delay_seconds=0,
        retryable_status_codes=(),
    )


def _sdk_owned_binding(binding: LlmProviderModelBinding) -> LlmProviderModelBinding:
    return LlmProviderModelBinding(
        id=binding.id,
        model_id=binding.model_id,
        model_alias=binding.model_alias,
        api_format=API_FORMAT_CODEX_RESPONSES,
        tool_choice=binding.tool_choice,
        thinking=binding.thinking,
        custom_request_params=binding.custom_request_params,
        api_base_url=None,
        custom_headers=dict(binding.custom_headers),
        api_keys=(),
    )


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _health_counts(accounts: Sequence[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for account in accounts:
        health = str(_enum_value(account.status.health))
        counts[health] = counts.get(health, 0) + 1
    return counts


def _quota_window_diagnostic(window: Any | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {
        "used_percent": window.used_percent,
        "remaining_percent": window.remaining_percent,
        "resets_at": window.resets_at,
        "exhausted": window.exhausted,
    }


def _log_operation(action: str, result: Any) -> None:
    logger.info(
        "codex_sdk.account.operation action=%s operation_id=%s account_id=%s kind=%s status=%s force=%s remaining_active_requests=%s error_code=%s",
        action,
        short_identifier(result.operation_id),
        short_identifier(result.account_id),
        _enum_value(result.kind),
        _enum_value(result.status),
        result.force,
        result.remaining_active_requests,
        getattr(result.error, "code", None),
    )
