# -*- coding: utf-8 -*-
"""运行时账号密码认证的 SQLite 仓储实现。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import RLock
from typing import Callable

from agent_runtime.auth.models import RuntimeAccount, RuntimeDeviceSession


class SqliteRuntimeAuthRepository:
    """把运行时本地账号与设备会话保存到运行时数据目录。

    该仓储只服务于“访问当前运行时”的账号密码认证，不承担云账号、跨运行时同步
    或组织级用户体系职责。数据文件跟随当前运行时数据根目录落本机。
    """

    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor
        self._lock = RLock()

    def has_accounts(self) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT 1 FROM runtime_accounts LIMIT 1").fetchone()
            return row is not None

    def create_account(self, account: RuntimeAccount) -> RuntimeAccount:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_accounts(
                    account_id, username, password_hash, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    account.account_id,
                    account.username,
                    account.password_hash,
                    account.status,
                    account.created_at,
                    account.updated_at,
                ),
            )
            conn.commit()
        return account

    def get_account_by_username(self, username: str) -> RuntimeAccount | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runtime_accounts WHERE username = ?",
                (username,),
            ).fetchone()
        return _account_from_row(row)

    def get_account_by_id(self, account_id: str) -> RuntimeAccount | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runtime_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return _account_from_row(row)

    def create_session(self, session: RuntimeDeviceSession) -> RuntimeDeviceSession:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_device_sessions(
                    session_id, account_id, token_hash, device_id, client_kind, user_agent,
                    created_at, last_seen_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.account_id,
                    session.token_hash,
                    session.device_id,
                    session.client_kind,
                    session.user_agent,
                    session.created_at,
                    session.last_seen_at,
                    session.expires_at,
                    session.revoked_at,
                ),
            )
            conn.commit()
        return session

    def get_session_by_token_hash(self, token_hash: str) -> RuntimeDeviceSession | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runtime_device_sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return _session_from_row(row)

    def touch_session(self, session_id: str, last_seen_at: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE runtime_device_sessions SET last_seen_at = ? WHERE session_id = ?",
                (last_seen_at, session_id),
            )
            conn.commit()

    def revoke_session(self, session_id: str, revoked_at: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE runtime_device_sessions SET revoked_at = ? WHERE session_id = ?",
                (revoked_at, session_id),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        db_path = self._db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema(conn)
        return conn

    def _db_path(self) -> Path:
        return Path(self._data_dir_accessor()).expanduser() / "runtime_auth.sqlite3"

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_accounts (
                account_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_device_sessions (
                session_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                device_id TEXT,
                client_kind TEXT NOT NULL,
                user_agent TEXT,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                FOREIGN KEY(account_id) REFERENCES runtime_accounts(account_id)
            );

            CREATE INDEX IF NOT EXISTS idx_runtime_device_sessions_account
                ON runtime_device_sessions(account_id);
            CREATE INDEX IF NOT EXISTS idx_runtime_device_sessions_token
                ON runtime_device_sessions(token_hash);
            """
        )
        conn.commit()


def _account_from_row(row: sqlite3.Row | None) -> RuntimeAccount | None:
    if row is None:
        return None
    return RuntimeAccount(
        account_id=row["account_id"],
        username=row["username"],
        password_hash=row["password_hash"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _session_from_row(row: sqlite3.Row | None) -> RuntimeDeviceSession | None:
    if row is None:
        return None
    return RuntimeDeviceSession(
        session_id=row["session_id"],
        account_id=row["account_id"],
        token_hash=row["token_hash"],
        device_id=row["device_id"],
        client_kind=row["client_kind"],
        user_agent=row["user_agent"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )
