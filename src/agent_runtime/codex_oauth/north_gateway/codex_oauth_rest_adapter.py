# -*- coding: utf-8 -*-
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from agent_runtime.codex_oauth.models import CodexOAuthAccount
from agent_runtime.codex_oauth.service import CodexOAuthError, CodexOAuthService


class CodexOAuthLoginStartBody(BaseModel):
    redirect_uri: str | None = Field(default=None)


class CodexOAuthLoginStartResponse(BaseModel):
    authorization_url: str
    state: str
    redirect_uri: str
    expires_at: str


class CodexOAuthAccountResponse(BaseModel):
    credential_id: str
    account_id: str | None = None
    email: str | None = None
    display_name: str | None = None
    label: str
    expires_at: int
    scopes: str | None = None
    originator: str
    session_id: str
    disabled: bool
    created_at: str
    updated_at: str
    last_used_at: str | None = None


_SUCCESS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Codex Authorization Successful</title></head>
<body style="font-family: system-ui; background: #0f172a; color: #e2e8f0; display: grid; place-items: center; height: 100vh; margin: 0;">
  <main style="text-align: center;"><h1>Codex 授权成功</h1><p>你可以关闭此窗口并返回 Spread AI。</p></main>
  <script>setTimeout(() => window.close(), 1500)</script>
</body></html>"""


def _error_html(message: str) -> str:
    escaped = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Codex Authorization Failed</title></head>
<body style="font-family: system-ui; background: #0f172a; color: #e2e8f0; display: grid; place-items: center; height: 100vh; margin: 0;">
  <main style="text-align: center; max-width: 720px;"><h1 style="color: #f87171;">Codex 授权失败</h1><p style="color: #cbd5e1;">{escaped}</p></main>
</body></html>"""


def create_codex_oauth_router(service: CodexOAuthService) -> APIRouter:
    router = APIRouter()

    @router.post("/login/start", response_model=CodexOAuthLoginStartResponse)
    def start_login(
        request: Request,
        body: CodexOAuthLoginStartBody | None = None,
    ) -> CodexOAuthLoginStartResponse:
        try:
            redirect_uri = (
                body.redirect_uri
                if body and body.redirect_uri
                else service.default_redirect_uri(str(request.base_url))
            )
            result = service.start_login(redirect_uri)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return CodexOAuthLoginStartResponse(
            authorization_url=result.authorization_url,
            state=result.state,
            redirect_uri=result.redirect_uri,
            expires_at=result.expires_at,
        )

    @router.get("/login/callback", response_class=HTMLResponse)
    def complete_login(
        state: str | None = None,
        code: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
    ) -> HTMLResponse:
        if error:
            return HTMLResponse(_error_html(error_description or error), status_code=400)
        if not state or not code:
            return HTMLResponse(_error_html("缺少 Codex OAuth state 或 code。"), status_code=400)
        try:
            service.complete_login(state=state, code=code)
        except CodexOAuthError as exc:
            return HTMLResponse(_error_html(str(exc)), status_code=400)
        except Exception as exc:
            return HTMLResponse(_error_html(str(exc)), status_code=500)
        return HTMLResponse(_SUCCESS_HTML)

    @router.get("/accounts", response_model=list[CodexOAuthAccountResponse])
    def list_accounts() -> list[CodexOAuthAccountResponse]:
        return [_account_to_response(account) for account in service.list_accounts()]

    @router.post("/accounts/{credential_id}/refresh", response_model=CodexOAuthAccountResponse)
    def refresh_account(credential_id: str) -> CodexOAuthAccountResponse:
        try:
            return _account_to_response(service.refresh_account(credential_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CodexOAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/accounts/{credential_id}", status_code=204)
    def delete_account(credential_id: str) -> None:
        service.delete_account(credential_id)

    @router.post("/provider/ensure", status_code=204)
    def ensure_provider() -> None:
        service.ensure_builtin_provider()

    return router


def _account_to_response(account: CodexOAuthAccount) -> CodexOAuthAccountResponse:
    return CodexOAuthAccountResponse(
        credential_id=account.credential_id,
        account_id=account.account_id,
        email=account.email,
        display_name=account.display_name,
        label=account.label,
        expires_at=account.expires_at,
        scopes=account.scopes,
        originator=account.originator,
        session_id=account.session_id,
        disabled=account.disabled,
        created_at=account.created_at,
        updated_at=account.updated_at,
        last_used_at=account.last_used_at,
    )
