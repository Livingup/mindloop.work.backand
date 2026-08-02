# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_runtime.codex_oauth.diagnostics import proxy_diagnostic
from agent_runtime.codex_oauth.service import CodexOAuthService


class CodexProxyInput(BaseModel):
    proxy_url: Any = None


class CodexBrowserAuthStartResponse(BaseModel):
    attempt_id: str
    authorization_url: str
    callback_url: str
    expires_at: datetime


class CodexDeviceAuthStartResponse(BaseModel):
    attempt_id: str
    verification_url: str
    user_code: str
    interval_seconds: float
    expires_at: datetime


class CodexBrowserCallbackBody(BaseModel):
    callback_url: Any = None


class CodexProxySummaryResponse(BaseModel):
    configured: bool
    scheme: str | None = None
    host: str | None = None
    port: int | None = None


class CodexErrorSummaryResponse(BaseModel):
    code: str
    message: str
    retryable: bool = False
    http_status: int | None = None
    retry_at: datetime | None = None


class CodexQuotaWindowResponse(BaseModel):
    kind: str
    used_percent: float
    remaining_percent: float
    resets_at: datetime | None
    exhausted: bool


class CodexQuotaResponse(BaseModel):
    account_id: str
    five_hour: CodexQuotaWindowResponse | None
    weekly: CodexQuotaWindowResponse | None
    fetched_at: datetime
    source: str


class CodexAccountStatusResponse(BaseModel):
    health: str
    reason_code: str
    reason_message: str | None
    since: datetime
    retry_at: datetime | None
    last_error: CodexErrorSummaryResponse | None
    exhausted_windows: list[str]
    failure_count: int
    last_http_status: int | None


class CodexAccountResponse(BaseModel):
    account_id: str
    display_label: str
    alias: str | None
    enabled: bool
    status: CodexAccountStatusResponse
    priority: int
    order: int
    proxy: CodexProxySummaryResponse
    plan_type: str | None
    quota: CodexQuotaResponse | None
    active_requests: int
    created_at: datetime
    updated_at: datetime
    version: int


class CodexAuthResultResponse(BaseModel):
    attempt_id: str
    account: CodexAccountResponse
    created: bool


class CodexAuthAttemptResponse(BaseModel):
    attempt_id: str
    kind: str
    status: str
    created_at: datetime
    expires_at: datetime
    result: CodexAuthResultResponse | None
    last_error: CodexErrorSummaryResponse | None


class CodexDevicePollResponse(BaseModel):
    attempt_id: str
    status: str
    next_poll_at: datetime | None
    auth_result: CodexAuthResultResponse | None
    error: CodexErrorSummaryResponse | None


class CodexProxyPatch(BaseModel):
    action: Literal["replace", "clear"]
    proxy_url: Any = None


class CodexAccountPatchBody(BaseModel):
    expected_version: int = Field(ge=0)
    alias: str | None = Field(default=None, max_length=200)
    priority: int | None = None
    proxy: CodexProxyPatch | None = None


class CodexAccountsReorderBody(BaseModel):
    account_ids: list[str] = Field(min_length=1)
    expected_versions: dict[str, int] | None = None


class CodexForceBody(BaseModel):
    force: bool = False


class CodexOperationResponse(BaseModel):
    operation_id: str
    kind: str
    account_id: str
    status: str
    force: bool
    started_at: datetime
    completed_at: datetime | None
    remaining_active_requests: int
    error: CodexErrorSummaryResponse | None


class CodexRecoveryCheckResponse(BaseModel):
    kind: str
    passed: bool
    error: CodexErrorSummaryResponse | None


class CodexRecoveryResponse(BaseModel):
    account: CodexAccountResponse
    recovered: bool
    checks: list[CodexRecoveryCheckResponse]


class CodexRoutingResponse(BaseModel):
    strategy: Literal["priority_drain", "round_robin"]


def create_codex_oauth_router(service: CodexOAuthService) -> APIRouter:
    router = APIRouter()

    @router.post("/auth/browser/start", response_model=CodexBrowserAuthStartResponse)
    async def start_browser_auth(body: CodexProxyInput | None = None) -> CodexBrowserAuthStartResponse:
        try:
            proxy_url = _sensitive_text(
                None if body is None else body.proxy_url,
                field_name="proxy_url",
                max_length=2048,
                required=False,
            )
            result = await service.begin_browser_auth(proxy_url)
            return CodexBrowserAuthStartResponse(
                attempt_id=result.attempt_id,
                authorization_url=result.authorization_url,
                callback_url=result.callback_url,
                expires_at=result.expires_at,
            )
        except Exception as exc:
            raise _http_error(exc) from None

    @router.post("/auth/device/start", response_model=CodexDeviceAuthStartResponse)
    async def start_device_auth(body: CodexProxyInput | None = None) -> CodexDeviceAuthStartResponse:
        try:
            proxy_url = _sensitive_text(
                None if body is None else body.proxy_url,
                field_name="proxy_url",
                max_length=2048,
                required=False,
            )
            result = await service.begin_device_auth(proxy_url)
            return CodexDeviceAuthStartResponse(
                attempt_id=result.attempt_id,
                verification_url=result.verification_url,
                user_code=result.user_code,
                interval_seconds=result.interval_seconds,
                expires_at=result.expires_at,
            )
        except Exception as exc:
            raise _http_error(exc) from None

    @router.get("/auth/attempts/{attempt_id}", response_model=CodexAuthAttemptResponse)
    async def get_auth_attempt(attempt_id: str) -> CodexAuthAttemptResponse:
        try:
            return _attempt_response(await service.get_auth_attempt(attempt_id))
        except Exception as exc:
            raise _http_error(exc) from None

    @router.post("/auth/device/{attempt_id}/poll", response_model=CodexDevicePollResponse)
    async def poll_device_auth(attempt_id: str) -> CodexDevicePollResponse:
        try:
            result = await service.poll_device_auth(attempt_id)
            return CodexDevicePollResponse(
                attempt_id=result.attempt_id,
                status=_enum_value(result.status),
                next_poll_at=result.next_poll_at,
                auth_result=_auth_result_response(result.auth_result),
                error=_error_response(result.error),
            )
        except Exception as exc:
            raise _http_error(exc) from None

    @router.post("/auth/browser/{attempt_id}/callback", response_model=CodexAuthResultResponse)
    async def complete_browser_callback(
        attempt_id: str, body: CodexBrowserCallbackBody
    ) -> CodexAuthResultResponse:
        try:
            callback_url = _sensitive_text(
                body.callback_url,
                field_name="callback_url",
                max_length=8192,
                required=True,
            )
            assert callback_url is not None
            return _auth_result_response(
                await service.complete_browser_callback(attempt_id, callback_url)
            )  # type: ignore[return-value]
        except Exception as exc:
            raise _http_error(exc) from None

    @router.post("/auth/attempts/{attempt_id}/cancel", response_model=CodexAuthAttemptResponse)
    async def cancel_auth_attempt(attempt_id: str) -> CodexAuthAttemptResponse:
        try:
            return _attempt_response(await service.cancel_auth_attempt(attempt_id))
        except Exception as exc:
            raise _http_error(exc) from None

    @router.get("/accounts", response_model=list[CodexAccountResponse])
    async def list_accounts(include_disabled: bool = True) -> list[CodexAccountResponse]:
        try:
            return [_account_response(item) for item in await service.list_accounts(include_disabled=include_disabled)]
        except Exception as exc:
            raise _http_error(exc) from None

    @router.get("/accounts/{account_id}", response_model=CodexAccountResponse)
    async def get_account(account_id: str) -> CodexAccountResponse:
        try:
            return _account_response(await service.get_account(account_id))
        except Exception as exc:
            raise _http_error(exc) from None

    @router.patch("/accounts/{account_id}", response_model=CodexAccountResponse)
    async def update_account(account_id: str, body: CodexAccountPatchBody) -> CodexAccountResponse:
        try:
            proxy_present = body.proxy is not None
            proxy_url = None
            if body.proxy is not None and body.proxy.action == "replace":
                proxy_url = _sensitive_text(
                    body.proxy.proxy_url,
                    field_name="proxy_url",
                    max_length=2048,
                    required=True,
                )
            elif body.proxy is not None and body.proxy.proxy_url is not None:
                raise ValueError("clear proxy 不应提供 proxy_url。")
            account = await service.update_account(
                account_id,
                alias_present="alias" in body.model_fields_set,
                alias=body.alias,
                priority_present="priority" in body.model_fields_set,
                priority=body.priority,
                proxy_present=proxy_present,
                proxy_url=proxy_url,
                expected_version=body.expected_version,
            )
            return _account_response(account)
        except Exception as exc:
            raise _http_error(exc) from None

    @router.post("/accounts/reorder", response_model=list[CodexAccountResponse])
    async def reorder_accounts(body: CodexAccountsReorderBody) -> list[CodexAccountResponse]:
        try:
            return [
                _account_response(item)
                for item in await service.reorder_accounts(
                    body.account_ids, expected_versions=body.expected_versions
                )
            ]
        except Exception as exc:
            raise _http_error(exc) from None

    @router.post("/accounts/{account_id}/enable", response_model=CodexAccountResponse)
    async def enable_account(account_id: str) -> CodexAccountResponse:
        try:
            return _account_response(await service.enable_account(account_id))
        except Exception as exc:
            raise _http_error(exc) from None

    @router.post("/accounts/{account_id}/disable", response_model=CodexOperationResponse)
    async def disable_account(
        account_id: str, body: CodexForceBody | None = None
    ) -> CodexOperationResponse:
        try:
            return _operation_response(
                await service.disable_account(account_id, force=bool(body and body.force))
            )
        except Exception as exc:
            raise _http_error(exc) from None

    @router.post("/accounts/{account_id}/recover", response_model=CodexRecoveryResponse)
    async def recover_account(account_id: str) -> CodexRecoveryResponse:
        try:
            result = await service.recover_account(account_id)
            return CodexRecoveryResponse(
                account=_account_response(result.account),
                recovered=result.recovered,
                checks=[
                    CodexRecoveryCheckResponse(
                        kind=_enum_value(check.kind),
                        passed=check.passed,
                        error=_error_response(check.error),
                    )
                    for check in result.checks
                ],
            )
        except Exception as exc:
            raise _http_error(exc) from None

    @router.get("/accounts/{account_id}/quota", response_model=CodexQuotaResponse | None)
    async def get_cached_quota(account_id: str) -> CodexQuotaResponse | None:
        try:
            return _quota_response(await service.get_cached_quota(account_id))
        except Exception as exc:
            raise _http_error(exc) from None

    @router.post("/accounts/{account_id}/quota/refresh", response_model=CodexQuotaResponse)
    async def refresh_quota(account_id: str) -> CodexQuotaResponse:
        try:
            return _quota_response(await service.refresh_quota(account_id))  # type: ignore[return-value]
        except Exception as exc:
            raise _http_error(exc) from None

    @router.delete("/accounts/{account_id}", response_model=CodexOperationResponse)
    async def delete_account(account_id: str, force: bool = False) -> CodexOperationResponse:
        try:
            return _operation_response(await service.delete_account(account_id, force=force))
        except Exception as exc:
            raise _http_error(exc) from None

    @router.get("/operations/{operation_id}", response_model=CodexOperationResponse)
    async def get_operation(operation_id: str) -> CodexOperationResponse:
        try:
            return _operation_response(await service.get_operation(operation_id))
        except Exception as exc:
            raise _http_error(exc) from None

    @router.post("/operations/{operation_id}/wait", response_model=CodexOperationResponse)
    async def wait_operation(operation_id: str, timeout_seconds: float = 30.0) -> CodexOperationResponse:
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise HTTPException(status_code=400, detail="timeout_seconds 必须在 (0, 120] 范围内。")
        try:
            return _operation_response(
                await service.wait_operation(operation_id, timeout_seconds=timeout_seconds)
            )
        except Exception as exc:
            raise _http_error(exc) from None

    @router.get("/routing", response_model=CodexRoutingResponse)
    async def get_routing() -> CodexRoutingResponse:
        try:
            return CodexRoutingResponse(strategy=_enum_value(await service.get_routing_strategy()))
        except Exception as exc:
            raise _http_error(exc) from None

    @router.put("/routing", response_model=CodexRoutingResponse)
    async def update_routing(body: CodexRoutingResponse) -> CodexRoutingResponse:
        try:
            return CodexRoutingResponse(strategy=_enum_value(await service.set_routing_strategy(body.strategy)))
        except Exception as exc:
            raise _http_error(exc) from None

    @router.post("/provider/ensure", status_code=204)
    async def ensure_provider() -> None:
        try:
            await service.ensure_builtin_provider()
        except Exception as exc:
            raise _http_error(exc) from None

    return router


def _account_response(account: Any) -> CodexAccountResponse:
    return CodexAccountResponse(
        account_id=account.account_id,
        display_label=_account_display_label(account),
        alias=account.alias,
        enabled=account.enabled,
        status=CodexAccountStatusResponse(
            health=_enum_value(account.status.health),
            reason_code=_enum_value(account.status.reason_code),
            reason_message=_account_reason_message(account.status),
            since=account.status.since,
            retry_at=account.status.retry_at,
            last_error=_error_response(account.status.last_error),
            exhausted_windows=sorted(account.status.exhausted_windows),
            failure_count=account.status.failure_count,
            last_http_status=account.status.last_http_status,
        ),
        priority=account.priority,
        order=account.order,
        proxy=_proxy_response(account.proxy),
        plan_type=account.plan_type,
        quota=_quota_response(account.quota),
        active_requests=account.active_requests,
        created_at=account.created_at,
        updated_at=account.updated_at,
        version=account.version,
    )


def _account_display_label(account: Any) -> str:
    if isinstance(account.alias, str) and account.alias.strip():
        return account.alias.strip()
    masked = _masked_account_id(str(account.account_id))
    return f"{masked} · {account.plan_type}" if account.plan_type else masked


def _masked_account_id(account_id: str) -> str:
    if len(account_id) <= 4:
        return "••••"
    if len(account_id) <= 8:
        return f"{account_id[:2]}…{account_id[-2:]}"
    return f"{account_id[:6]}…{account_id[-4:]}"


def _proxy_response(proxy: Any) -> CodexProxySummaryResponse:
    safe = proxy_diagnostic(proxy)
    return CodexProxySummaryResponse(
        configured=bool(safe.get("proxy_configured")),
        scheme=safe.get("proxy_scheme"),
        host=safe.get("proxy_host"),
        port=safe.get("proxy_port"),
    )


def _quota_response(quota: Any | None) -> CodexQuotaResponse | None:
    if quota is None:
        return None
    return CodexQuotaResponse(
        account_id=quota.account_id,
        five_hour=_quota_window_response(quota.five_hour),
        weekly=_quota_window_response(quota.weekly),
        fetched_at=quota.fetched_at,
        source=quota.source,
    )


def _quota_window_response(window: Any | None) -> CodexQuotaWindowResponse | None:
    if window is None:
        return None
    return CodexQuotaWindowResponse(
        kind=window.kind,
        used_percent=window.used_percent,
        remaining_percent=window.remaining_percent,
        resets_at=window.resets_at,
        exhausted=window.exhausted,
    )


def _error_response(error: Any | None) -> CodexErrorSummaryResponse | None:
    if error is None:
        return None
    code = str(_enum_value(error.code))
    return CodexErrorSummaryResponse(
        code=code,
        message=_safe_error_message(code),
        retryable=error.retryable,
        http_status=error.http_status,
        retry_at=error.retry_at,
    )


def _auth_result_response(result: Any | None) -> CodexAuthResultResponse | None:
    if result is None:
        return None
    return CodexAuthResultResponse(
        attempt_id=result.attempt_id,
        account=_account_response(result.account),
        created=result.created,
    )


def _attempt_response(attempt: Any) -> CodexAuthAttemptResponse:
    return CodexAuthAttemptResponse(
        attempt_id=attempt.attempt_id,
        kind=_enum_value(attempt.kind),
        status=_enum_value(attempt.status),
        created_at=attempt.created_at,
        expires_at=attempt.expires_at,
        result=_auth_result_response(attempt.result),
        last_error=_error_response(attempt.last_error),
    )


def _operation_response(result: Any) -> CodexOperationResponse:
    return CodexOperationResponse(
        operation_id=result.operation_id,
        kind=_enum_value(result.kind),
        account_id=result.account_id,
        status=_enum_value(result.status),
        force=result.force,
        started_at=result.started_at,
        completed_at=result.completed_at,
        remaining_active_requests=result.remaining_active_requests,
        error=_error_response(result.error),
    )


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _sensitive_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
    required: bool,
 ) -> str | None:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise TypeError(f"{field_name} 必须是字符串。")
    if required and not text:
        raise ValueError(f"{field_name} 不能为空。")
    if len(text) > max_length:
        raise ValueError(f"{field_name} 超过允许长度。")
    return text or None


def _http_error(exc: Exception) -> HTTPException:
    code = str(_enum_value(getattr(exc, "code", exc.__class__.__name__)))
    if code in {"account_not_found", "auth_attempt_not_found", "operation_not_found"}:
        status = 404
    elif code in {
        "account_conflict",
        "auth_attempt_conflict",
        "auth_attempt_already_completed",
    }:
        status = 409
    elif code in {
        "validation_error",
        "configuration_error",
        "reserved_response_field",
        "unsupported_response_parameter",
        "oauth_state_mismatch",
        "auth_attempt_expired",
        "auth_attempt_cancelled",
        "ValueError",
        "TypeError",
    }:
        status = 400
    elif code == "operation_wait_timeout":
        status = 408
    elif code in {"no_available_account", "account_disabled"}:
        status = 503
    elif code in {"proxy_error", "connect_error", "request_timeout", "oauth_exchange", "upstream_http_error"}:
        status = 502
    else:
        status = 500
    return HTTPException(
        status_code=status,
        detail={
            "code": code,
            "message": _safe_error_message(code),
            "retryable": bool(getattr(exc, "retryable", False)),
            "http_status": getattr(exc, "http_status", None),
        },
    )


def _account_reason_message(status: Any) -> str | None:
    error = getattr(status, "last_error", None)
    if error is not None:
        return _safe_error_message(str(_enum_value(getattr(error, "code", "codex_sdk_error"))))
    reason_code = str(_enum_value(getattr(status, "reason_code", "none")))
    if reason_code in {"", "none"}:
        return None
    return f"Codex 账号健康状态：{reason_code}"


def _safe_error_message(code: str) -> str:
    messages = {
        "account_not_found": "Codex 账号不存在。",
        "account_conflict": "Codex 账号版本已变化，请刷新后重试。",
        "account_disabled": "Codex 账号已停用。",
        "no_available_account": "当前没有可用的 Codex 账号。",
        "auth_attempt_not_found": "授权尝试不存在。",
        "auth_attempt_expired": "授权尝试已过期。",
        "auth_attempt_cancelled": "授权尝试已取消。",
        "auth_attempt_already_completed": "授权尝试已经完成。",
        "auth_attempt_conflict": "授权尝试状态已变化，请刷新后重试。",
        "oauth_state_mismatch": "授权回调校验失败，请重新发起授权。",
        "oauth_exchange": "Codex OAuth 交换失败。",
        "proxy_error": "账号代理连接失败。",
        "connect_error": "Codex 网络连接失败。",
        "request_timeout": "Codex 请求超时。",
        "upstream_http_error": "Codex 上游请求失败。",
        "operation_not_found": "账号操作不存在。",
        "operation_wait_timeout": "等待账号操作完成超时。",
        "request_cancelled": "Codex 请求已取消。",
        "validation_error": "Codex 请求参数无效。",
        "configuration_error": "louyi-codex SDK 配置无效。",
        "reserved_response_field": "请求包含 SDK 保留字段。",
        "unsupported_response_parameter": "请求包含当前 SDK 不支持的 Responses 参数。",
        "ValueError": "请求参数无效。",
        "TypeError": "请求参数类型无效。",
    }
    return messages.get(code, f"louyi-codex 操作失败（{code}）。")
