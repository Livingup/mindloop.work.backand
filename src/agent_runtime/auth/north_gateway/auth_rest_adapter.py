# -*- coding: utf-8 -*-
"""运行时账号密码认证 REST 适配器。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from agent_runtime.auth.auth_service import RuntimeAuthService
from agent_runtime.auth.models import AuthenticatedRuntimeSession
from agent_runtime.auth.rate_limiter import InMemoryFixedWindowRateLimiter

LOGIN_RATE_LIMIT_MAX_ATTEMPTS = 8
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 60
BOOTSTRAP_RATE_LIMIT_MAX_ATTEMPTS = 5
BOOTSTRAP_RATE_LIMIT_WINDOW_SECONDS = 300


class RuntimeAuthStatusResponse(BaseModel):
    configured: bool
    bootstrap_required: bool
    auth_required: bool


class RuntimeAuthRequestBody(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    device_id: str | None = None
    client_kind: str = "unknown"


class RuntimeAuthAccountResponse(BaseModel):
    account_id: str
    username: str
    status: str
    created_at: str
    updated_at: str


class RuntimeAuthDeviceSessionResponse(BaseModel):
    session_id: str
    device_id: str | None = None
    client_kind: str
    created_at: str
    last_seen_at: str
    expires_at: str


class RuntimeAuthLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    account: RuntimeAuthAccountResponse
    session: RuntimeAuthDeviceSessionResponse


class RuntimeAuthMeResponse(BaseModel):
    authenticated: bool
    account: RuntimeAuthAccountResponse | None = None
    session: RuntimeAuthDeviceSessionResponse | None = None


def create_runtime_auth_router(
    auth_service: RuntimeAuthService,
    *,
    login_rate_limiter: InMemoryFixedWindowRateLimiter | None = None,
    bootstrap_rate_limiter: InMemoryFixedWindowRateLimiter | None = None,
) -> APIRouter:
    router = APIRouter()
    login_rate_limiter = login_rate_limiter or InMemoryFixedWindowRateLimiter(
        max_attempts=LOGIN_RATE_LIMIT_MAX_ATTEMPTS,
        window_seconds=LOGIN_RATE_LIMIT_WINDOW_SECONDS,
    )
    bootstrap_rate_limiter = bootstrap_rate_limiter or InMemoryFixedWindowRateLimiter(
        max_attempts=BOOTSTRAP_RATE_LIMIT_MAX_ATTEMPTS,
        window_seconds=BOOTSTRAP_RATE_LIMIT_WINDOW_SECONDS,
    )

    @router.get("/status", response_model=RuntimeAuthStatusResponse)
    def status() -> RuntimeAuthStatusResponse:
        configured = auth_service.has_accounts()
        return RuntimeAuthStatusResponse(
            configured=configured,
            bootstrap_required=not configured,
            auth_required=configured,
        )

    @router.post("/bootstrap", response_model=RuntimeAuthLoginResponse)
    def bootstrap(body: RuntimeAuthRequestBody, request: Request) -> RuntimeAuthLoginResponse:
        _enforce_rate_limit(
            bootstrap_rate_limiter,
            _rate_limit_key(request, "bootstrap", body.username),
            "初始化尝试过于频繁，请稍后重试。",
        )
        try:
            session, token = auth_service.bootstrap(
                username=body.username,
                password=body.password,
                device_id=body.device_id,
                client_kind=body.client_kind,
                user_agent=request.headers.get("user-agent"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return _login_response(session, token)

    @router.post("/login", response_model=RuntimeAuthLoginResponse)
    def login(body: RuntimeAuthRequestBody, request: Request) -> RuntimeAuthLoginResponse:
        _enforce_rate_limit(
            login_rate_limiter,
            _rate_limit_key(request, "login", body.username),
            "登录尝试过于频繁，请稍后重试。",
        )
        try:
            session, token = auth_service.login(
                username=body.username,
                password=body.password,
                device_id=body.device_id,
                client_kind=body.client_kind,
                user_agent=request.headers.get("user-agent"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        return _login_response(session, token)

    @router.post("/logout", status_code=204)
    def logout(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> None:
        auth_service.revoke_token(_bearer_token(authorization))

    @router.get("/me", response_model=RuntimeAuthMeResponse)
    def me(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> RuntimeAuthMeResponse:
        session = auth_service.authenticate_token(_bearer_token(authorization))
        if session is None:
            return RuntimeAuthMeResponse(authenticated=False)
        return RuntimeAuthMeResponse(
            authenticated=True,
            account=_account_response(session),
            session=_session_response(session),
        )

    return router


def _enforce_rate_limit(
    limiter: InMemoryFixedWindowRateLimiter,
    key: str,
    detail: str,
) -> None:
    decision = limiter.consume(key)
    if decision.allowed:
        return
    raise HTTPException(
        status_code=429,
        detail=detail,
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )


def _rate_limit_key(request: Request, action: str, username: str) -> str:
    client_host = request.client.host if request.client is not None else "unknown"
    normalized_username = username.strip().lower() or "unknown"
    return f"{action}:{client_host}:{normalized_username}"


def _login_response(
    authenticated_session: AuthenticatedRuntimeSession,
    token: str,
) -> RuntimeAuthLoginResponse:
    return RuntimeAuthLoginResponse(
        access_token=token,
        account=_account_response(authenticated_session),
        session=_session_response(authenticated_session),
    )


def _account_response(session: AuthenticatedRuntimeSession) -> RuntimeAuthAccountResponse:
    account = session.account
    return RuntimeAuthAccountResponse(
        account_id=account.account_id,
        username=account.username,
        status=account.status,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def _session_response(session: AuthenticatedRuntimeSession) -> RuntimeAuthDeviceSessionResponse:
    device_session = session.session
    return RuntimeAuthDeviceSessionResponse(
        session_id=device_session.session_id,
        device_id=device_session.device_id,
        client_kind=device_session.client_kind,
        created_at=device_session.created_at,
        last_seen_at=device_session.last_seen_at,
        expires_at=device_session.expires_at,
    )


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return None
    token = authorization[len(prefix):].strip()
    return token or None
