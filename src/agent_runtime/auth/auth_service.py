# -*- coding: utf-8 -*-
"""运行时本地账号密码认证领域服务。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from agent_runtime.auth.infrastructure.sqlite_auth_repository import SqliteRuntimeAuthRepository
from agent_runtime.auth.models import (
    AuthenticatedRuntimeSession,
    RuntimeAccount,
    RuntimeAuthCheckResult,
    RuntimeAuthFailureReason,
    RuntimeDeviceSession,
)

ACCOUNT_STATUS_ACTIVE = "active"
PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 200_000
SESSION_IDLE_TTL_DAYS = 30
SESSION_ABSOLUTE_TTL_DAYS = 90
MIN_RUNTIME_PASSWORD_LENGTH = 12
PASSWORD_COMPLEXITY_MIN_CLASSES = 3


class RuntimeAuthService:
    """管理访问当前运行时的本地账号、密码和设备会话。

    第一阶段认证只保护“多端访问同一台运行时”，账号与会话数据保存在本机运行时
    SQLite 数据库中，不代表云账号，也不做跨运行时同步。
    """

    def __init__(self, repository: SqliteRuntimeAuthRepository) -> None:
        self._repository = repository

    def has_accounts(self) -> bool:
        return self._repository.has_accounts()

    def bootstrap(
        self,
        *,
        username: str,
        password: str,
        device_id: str | None,
        client_kind: str,
        user_agent: str | None,
    ) -> tuple[AuthenticatedRuntimeSession, str]:
        """首次初始化本地运行时账号，并创建当前设备登录会话。"""
        if self.has_accounts():
            raise ValueError("运行时账号已初始化，请直接登录。")
        account = self._new_account(username=username, password=password)
        self._repository.create_account(account)
        token, session = self._new_session(
            account_id=account.account_id,
            device_id=device_id,
            client_kind=client_kind,
            user_agent=user_agent,
        )
        self._repository.create_session(session)
        return AuthenticatedRuntimeSession(account=account, session=session), token

    def login(
        self,
        *,
        username: str,
        password: str,
        device_id: str | None,
        client_kind: str,
        user_agent: str | None,
    ) -> tuple[AuthenticatedRuntimeSession, str]:
        account = self._repository.get_account_by_username(_normalize_username(username))
        if account is None or account.status != ACCOUNT_STATUS_ACTIVE:
            raise ValueError("账号或密码错误。")
        if not _verify_password(password, account.password_hash):
            raise ValueError("账号或密码错误。")
        token, session = self._new_session(
            account_id=account.account_id,
            device_id=device_id,
            client_kind=client_kind,
            user_agent=user_agent,
        )
        self._repository.create_session(session)
        return AuthenticatedRuntimeSession(account=account, session=session), token

    def check_token(self, token: str | None, *, renew: bool) -> RuntimeAuthCheckResult:
        if not token:
            return RuntimeAuthCheckResult(session=None, failure_reason=RuntimeAuthFailureReason.TOKEN_MISSING)
        session = self._repository.get_session_by_token_hash(_hash_token(token))
        if session is None:
            return RuntimeAuthCheckResult(session=None, failure_reason=RuntimeAuthFailureReason.TOKEN_INVALID)
        if session.revoked_at is not None:
            return RuntimeAuthCheckResult(session=None, failure_reason=RuntimeAuthFailureReason.SESSION_REVOKED)
        now = _now()
        if _parse_datetime(session.expires_at) <= now or _parse_datetime(session.created_at) + timedelta(days=SESSION_ABSOLUTE_TTL_DAYS) <= now:
            return RuntimeAuthCheckResult(session=None, failure_reason=RuntimeAuthFailureReason.SESSION_EXPIRED)
        account = self._repository.get_account_by_id(session.account_id)
        if account is None or account.status != ACCOUNT_STATUS_ACTIVE:
            return RuntimeAuthCheckResult(session=None, failure_reason=RuntimeAuthFailureReason.TOKEN_INVALID)
        if not renew:
            return RuntimeAuthCheckResult(
                session=AuthenticatedRuntimeSession(account=account, session=session),
                failure_reason=None,
            )
        new_expires = _renewed_expires_at(session, now)
        now_text = now.isoformat()
        new_expires_text = new_expires.isoformat()
        self._repository.touch_session(session.session_id, now_text, new_expires_text)
        updated_session = replace(
            session,
            last_seen_at=now_text,
            expires_at=new_expires_text,
        )
        return RuntimeAuthCheckResult(
            session=AuthenticatedRuntimeSession(account=account, session=updated_session),
            failure_reason=None,
        )

    def authenticate_token(self, token: str | None) -> AuthenticatedRuntimeSession | None:
        return self.check_token(token, renew=True).session

    def revoke_token(self, token: str | None) -> bool:
        if not token:
            return False
        session = self._repository.get_session_by_token_hash(_hash_token(token))
        if session is None or session.revoked_at is not None:
            return False
        self._repository.revoke_session(session.session_id, _now_text())
        return True

    def _new_account(self, *, username: str, password: str) -> RuntimeAccount:
        normalized_username = _normalize_username(username)
        if not normalized_username:
            raise ValueError("账号名不能为空。")
        validate_runtime_password_policy(password)
        now = _now_text()
        return RuntimeAccount(
            account_id=f"acct-{uuid4()}",
            username=normalized_username,
            password_hash=_hash_password(password),
            status=ACCOUNT_STATUS_ACTIVE,
            created_at=now,
            updated_at=now,
        )

    def _new_session(
        self,
        *,
        account_id: str,
        device_id: str | None,
        client_kind: str,
        user_agent: str | None,
    ) -> tuple[str, RuntimeDeviceSession]:
        token = secrets.token_urlsafe(32)
        now = _now()
        now_text = now.isoformat()
        expires_at = (now + timedelta(days=SESSION_IDLE_TTL_DAYS)).isoformat()
        session = RuntimeDeviceSession(
            session_id=f"sess-{uuid4()}",
            account_id=account_id,
            token_hash=_hash_token(token),
            device_id=device_id.strip() if isinstance(device_id, str) and device_id.strip() else None,
            client_kind=(client_kind or "unknown").strip() or "unknown",
            user_agent=user_agent,
            created_at=now_text,
            last_seen_at=now_text,
            expires_at=expires_at,
        )
        return token, session


def _renewed_expires_at(session: RuntimeDeviceSession, now: datetime) -> datetime:
    created_at = _parse_datetime(session.created_at)
    return min(
        now + timedelta(days=SESSION_IDLE_TTL_DAYS),
        created_at + timedelta(days=SESSION_ABSOLUTE_TTL_DAYS),
    )


def _normalize_username(value: str) -> str:
    return value.strip().lower()


def validate_runtime_password_policy(password: str) -> None:
    """校验 Runtime 本地首账号密码强度。"""
    if len(password) < MIN_RUNTIME_PASSWORD_LENGTH:
        raise ValueError(f"密码长度至少需要 {MIN_RUNTIME_PASSWORD_LENGTH} 位。")
    character_class_count = sum(
        (
            any(ch.islower() for ch in password),
            any(ch.isupper() for ch in password),
            any(ch.isdigit() for ch in password),
            any(not ch.isalnum() for ch in password),
        )
    )
    if character_class_count < PASSWORD_COMPLEXITY_MIN_CLASSES:
        raise ValueError(
            "密码至少需要包含小写字母、大写字母、数字、符号中的 "
            f"{PASSWORD_COMPLEXITY_MIN_CLASSES} 类。"
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_text() -> str:
    return _now().isoformat()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    return "$".join(
        (
            PASSWORD_HASH_ALGORITHM,
            str(PASSWORD_HASH_ITERATIONS),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = password_hash.split("$", 3)
        if algorithm != PASSWORD_HASH_ALGORITHM:
            return False
        iterations = int(iterations_text)
        salt = base64.b64decode(salt_text.encode("ascii"))
        expected_digest = base64.b64decode(digest_text.encode("ascii"))
    except Exception:
        return False
    actual_digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual_digest, expected_digest)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
