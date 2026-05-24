# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from agent_runtime.codex_oauth.models import CodexOAuthAccount, CodexPendingOAuthLogin

logger = logging.getLogger(__name__)

CODEX_OAUTH_DB_FILENAME = "codex_oauth.sqlite3"

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS codex_oauth_pending_logins (
    state           TEXT PRIMARY KEY,
    code_verifier   TEXT NOT NULL,
    code_challenge  TEXT NOT NULL,
    redirect_uri    TEXT NOT NULL,
    originator      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codex_oauth_accounts (
    credential_id   TEXT PRIMARY KEY,
    account_id      TEXT,
    email           TEXT,
    display_name    TEXT,
    access_token    TEXT NOT NULL,
    refresh_token   TEXT NOT NULL,
    id_token        TEXT,
    expires_at      INTEGER NOT NULL,
    scopes          TEXT,
    originator      TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    disabled        INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_used_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_codex_oauth_accounts_account_id ON codex_oauth_accounts(account_id);
CREATE INDEX IF NOT EXISTS idx_codex_oauth_accounts_email ON codex_oauth_accounts(email);
"""


def codex_oauth_db_path(data_dir: str) -> Path:
    return Path(data_dir).expanduser() / CODEX_OAUTH_DB_FILENAME


class SqliteCodexOAuthRepository:
    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor
        self._lock = RLock()
        self._conn: sqlite3.Connection | None = None
        self._db_path: Path | None = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def save_pending_login(self, pending: CodexPendingOAuthLogin) -> None:
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT INTO codex_oauth_pending_logins (
                    state, code_verifier, code_challenge, redirect_uri,
                    originator, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(state) DO UPDATE SET
                    code_verifier = excluded.code_verifier,
                    code_challenge = excluded.code_challenge,
                    redirect_uri = excluded.redirect_uri,
                    originator = excluded.originator,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (
                    pending.state,
                    pending.code_verifier,
                    pending.code_challenge,
                    pending.redirect_uri,
                    pending.originator,
                    pending.created_at,
                    pending.expires_at,
                ),
            )
            conn.commit()

    def find_pending_login(self, state: str) -> CodexPendingOAuthLogin | None:
        with self._lock:
            row = self._connection().execute(
                """
                SELECT state, code_verifier, code_challenge, redirect_uri,
                       originator, created_at, expires_at
                FROM codex_oauth_pending_logins
                WHERE state = ?
                """,
                (state,),
            ).fetchone()
            return _row_to_pending(row) if row else None

    def delete_pending_login(self, state: str) -> None:
        with self._lock:
            conn = self._connection()
            conn.execute("DELETE FROM codex_oauth_pending_logins WHERE state = ?", (state,))
            conn.commit()

    def delete_expired_pending_logins(self, now_iso: str) -> None:
        with self._lock:
            conn = self._connection()
            conn.execute("DELETE FROM codex_oauth_pending_logins WHERE expires_at <= ?", (now_iso,))
            conn.commit()

    def save_account(self, account: CodexOAuthAccount) -> None:
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT INTO codex_oauth_accounts (
                    credential_id, account_id, email, display_name,
                    access_token, refresh_token, id_token, expires_at,
                    scopes, originator, session_id, disabled,
                    created_at, updated_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(credential_id) DO UPDATE SET
                    account_id = excluded.account_id,
                    email = excluded.email,
                    display_name = excluded.display_name,
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    id_token = excluded.id_token,
                    expires_at = excluded.expires_at,
                    scopes = excluded.scopes,
                    originator = excluded.originator,
                    session_id = excluded.session_id,
                    disabled = excluded.disabled,
                    updated_at = excluded.updated_at,
                    last_used_at = excluded.last_used_at
                """,
                _account_to_row(account),
            )
            conn.commit()

    def find_all_accounts(self) -> list[CodexOAuthAccount]:
        with self._lock:
            rows = self._connection().execute(
                """
                SELECT credential_id, account_id, email, display_name,
                       access_token, refresh_token, id_token, expires_at,
                       scopes, originator, session_id, disabled,
                       created_at, updated_at, last_used_at
                FROM codex_oauth_accounts
                ORDER BY created_at ASC, credential_id ASC
                """
            ).fetchall()
            return [_row_to_account(row) for row in rows]

    def find_account_by_id(self, credential_id: str) -> CodexOAuthAccount | None:
        with self._lock:
            row = self._connection().execute(
                """
                SELECT credential_id, account_id, email, display_name,
                       access_token, refresh_token, id_token, expires_at,
                       scopes, originator, session_id, disabled,
                       created_at, updated_at, last_used_at
                FROM codex_oauth_accounts
                WHERE credential_id = ?
                """,
                (credential_id,),
            ).fetchone()
            return _row_to_account(row) if row else None

    def delete_account(self, credential_id: str) -> bool:
        with self._lock:
            conn = self._connection()
            cursor = conn.execute(
                "DELETE FROM codex_oauth_accounts WHERE credential_id = ?",
                (credential_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def _connection(self) -> sqlite3.Connection:
        with self._lock:
            db_path = codex_oauth_db_path(self._data_dir_accessor())
            if self._conn is not None and self._db_path == db_path:
                return self._conn
            if self._conn is not None:
                self._close_locked()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
            self._db_path = db_path
            logger.info("已打开 Codex OAuth SQLite 数据库 path=%s", db_path)
            return conn

    def _close_locked(self) -> None:
        if self._conn is None:
            self._db_path = None
            return
        try:
            self._conn.close()
        except sqlite3.Error as exc:
            logger.warning("关闭 Codex OAuth SQLite 连接失败 err=%s", exc)
        finally:
            self._conn = None
            self._db_path = None


def _row_to_pending(row: tuple[Any, ...]) -> CodexPendingOAuthLogin:
    return CodexPendingOAuthLogin(
        state=str(row[0]),
        code_verifier=str(row[1]),
        code_challenge=str(row[2]),
        redirect_uri=str(row[3]),
        originator=str(row[4]),
        created_at=str(row[5]),
        expires_at=str(row[6]),
    )


def _account_to_row(account: CodexOAuthAccount) -> tuple[Any, ...]:
    return (
        account.credential_id,
        account.account_id,
        account.email,
        account.display_name,
        account.access_token,
        account.refresh_token,
        account.id_token,
        int(account.expires_at),
        account.scopes,
        account.originator,
        account.session_id,
        1 if account.disabled else 0,
        account.created_at,
        account.updated_at,
        account.last_used_at,
    )


def _row_to_account(row: tuple[Any, ...]) -> CodexOAuthAccount:
    return CodexOAuthAccount(
        credential_id=str(row[0]),
        account_id=row[1],
        email=row[2],
        display_name=row[3],
        access_token=str(row[4]),
        refresh_token=str(row[5]),
        id_token=row[6],
        expires_at=int(row[7]),
        scopes=row[8],
        originator=str(row[9]),
        session_id=str(row[10]),
        disabled=bool(row[11]),
        created_at=str(row[12]),
        updated_at=str(row[13]),
        last_used_at=row[14],
    )
