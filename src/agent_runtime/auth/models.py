# -*- coding: utf-8 -*-
"""运行时本地账号与设备会话领域模型。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeAccount:
    """可登录当前运行时的本地账号。"""

    account_id: str
    username: str
    password_hash: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class RuntimeDeviceSession:
    """某个前端设备访问当前运行时的登录会话。"""

    session_id: str
    account_id: str
    token_hash: str
    device_id: str | None
    client_kind: str
    user_agent: str | None
    created_at: str
    last_seen_at: str
    expires_at: str
    revoked_at: str | None = None


@dataclass(frozen=True, slots=True)
class AuthenticatedRuntimeSession:
    """已通过认证的账号与设备会话组合。"""

    account: RuntimeAccount
    session: RuntimeDeviceSession
