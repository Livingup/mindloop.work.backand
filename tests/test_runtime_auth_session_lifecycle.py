from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from starlette.testclient import TestClient

from agent_runtime.auth.auth_service import (
    RuntimeAuthService,
    _hash_token,
)
from agent_runtime.auth.infrastructure.sqlite_auth_repository import SqliteRuntimeAuthRepository
from agent_runtime.auth.models import RuntimeAuthFailureReason
from agent_runtime.auth.north_gateway.auth_rest_adapter import create_runtime_auth_router


def _create_service(tmp_path: str) -> RuntimeAuthService:
    repository = SqliteRuntimeAuthRepository(lambda: str(tmp_path))
    return RuntimeAuthService(repository)


def test_session_renewal_extends_expires_at_by_30_days(tmp_path, monkeypatch) -> None:
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("agent_runtime.auth.auth_service._now", lambda: t0)

    service = _create_service(tmp_path)
    auth_session, token = service.bootstrap(
        username="admin",
        password="StrongerPassw0rd!",
        device_id="device-1",
        client_kind="desktop-web",
        user_agent="pytest",
    )
    initial_expires_at = datetime.fromisoformat(auth_session.session.expires_at)
    assert initial_expires_at == t0 + timedelta(days=30)

    # Advance clock by 10 days
    t1 = t0 + timedelta(days=10)
    monkeypatch.setattr("agent_runtime.auth.auth_service._now", lambda: t1)

    result = service.check_token(token, renew=True)
    assert result.session is not None
    assert result.failure_reason is None

    renewed_expires_at = datetime.fromisoformat(result.session.session.expires_at)
    assert renewed_expires_at == t1 + timedelta(days=30)

    # Verify repository in DB has updated expires_at
    db_session = service._repository.get_session_by_token_hash(_hash_token(token))
    assert db_session is not None
    assert datetime.fromisoformat(db_session.expires_at) == t1 + timedelta(days=30)


def test_session_renewal_capped_at_absolute_90_days(tmp_path, monkeypatch) -> None:
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("agent_runtime.auth.auth_service._now", lambda: t0)

    service = _create_service(tmp_path)
    _auth_session, token = service.bootstrap(
        username="admin",
        password="StrongerPassw0rd!",
        device_id="device-1",
        client_kind="desktop-web",
        user_agent="pytest",
    )

    # Keep session active at day 25 and day 50
    t_25 = t0 + timedelta(days=25)
    monkeypatch.setattr("agent_runtime.auth.auth_service._now", lambda: t_25)
    assert service.check_token(token, renew=True).session is not None

    t_50 = t0 + timedelta(days=50)
    monkeypatch.setattr("agent_runtime.auth.auth_service._now", lambda: t_50)
    assert service.check_token(token, renew=True).session is not None

    # Advance clock to day 75. 75d + 30d = 105d > 90d, so should be capped at 90d from created_at
    t_75 = t0 + timedelta(days=75)
    monkeypatch.setattr("agent_runtime.auth.auth_service._now", lambda: t_75)

    result = service.check_token(token, renew=True)
    assert result.session is not None
    capped_expires_at = datetime.fromisoformat(result.session.session.expires_at)
    assert capped_expires_at == t0 + timedelta(days=90)


def test_expired_session_returns_session_expired_and_does_not_resurrect(tmp_path, monkeypatch) -> None:
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("agent_runtime.auth.auth_service._now", lambda: t0)

    service = _create_service(tmp_path)
    auth_session, token = service.bootstrap(
        username="admin",
        password="StrongerPassw0rd!",
        device_id="device-1",
        client_kind="desktop-web",
        user_agent="pytest",
    )
    original_expires_at = auth_session.session.expires_at

    # Advance clock past expiration (day 31)
    t_31 = t0 + timedelta(days=31)
    monkeypatch.setattr("agent_runtime.auth.auth_service._now", lambda: t_31)

    result = service.check_token(token, renew=True)
    assert result.session is None
    assert result.failure_reason == RuntimeAuthFailureReason.SESSION_EXPIRED

    # Verify expires_at in SQLite was NOT updated
    db_session = service._repository.get_session_by_token_hash(_hash_token(token))
    assert db_session is not None
    assert db_session.expires_at == original_expires_at


def test_token_revoked_missing_and_invalid_reasons(tmp_path, monkeypatch) -> None:
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("agent_runtime.auth.auth_service._now", lambda: t0)

    service = _create_service(tmp_path)
    _auth_session, token = service.bootstrap(
        username="admin",
        password="StrongerPassw0rd!",
        device_id="device-1",
        client_kind="desktop-web",
        user_agent="pytest",
    )

    # Missing tokens
    assert service.check_token(None, renew=True).failure_reason == RuntimeAuthFailureReason.TOKEN_MISSING
    assert service.check_token("", renew=True).failure_reason == RuntimeAuthFailureReason.TOKEN_MISSING

    # Invalid token
    assert service.check_token("random-unknown-token", renew=True).failure_reason == RuntimeAuthFailureReason.TOKEN_INVALID

    # Revoked token
    assert service.revoke_token(token) is True
    assert service.check_token(token, renew=True).failure_reason == RuntimeAuthFailureReason.SESSION_REVOKED


def test_check_token_renew_false_does_not_touch_db(tmp_path, monkeypatch) -> None:
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("agent_runtime.auth.auth_service._now", lambda: t0)

    service = _create_service(tmp_path)
    auth_session, token = service.bootstrap(
        username="admin",
        password="StrongerPassw0rd!",
        device_id="device-1",
        client_kind="desktop-web",
        user_agent="pytest",
    )
    initial_last_seen = auth_session.session.last_seen_at
    initial_expires_at = auth_session.session.expires_at

    # Advance clock
    t1 = t0 + timedelta(days=5)
    monkeypatch.setattr("agent_runtime.auth.auth_service._now", lambda: t1)

    result = service.check_token(token, renew=False)
    assert result.session is not None
    assert result.failure_reason is None

    # DB records remain unchanged
    db_session = service._repository.get_session_by_token_hash(_hash_token(token))
    assert db_session is not None
    assert db_session.last_seen_at == initial_last_seen
    assert db_session.expires_at == initial_expires_at


def test_auth_me_endpoint_returns_code_when_unauthenticated_and_renewed_session(tmp_path, monkeypatch) -> None:
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("agent_runtime.auth.auth_service._now", lambda: t0)

    service = _create_service(tmp_path)
    _auth_session, token = service.bootstrap(
        username="admin",
        password="StrongerPassw0rd!",
        device_id="device-1",
        client_kind="desktop-web",
        user_agent="pytest",
    )

    app = FastAPI()
    app.include_router(create_runtime_auth_router(service), prefix="/api/auth")
    client = TestClient(app)

    # 1. Unauthenticated: missing token
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is False
    assert data["code"] == "auth_token_missing"

    # 2. Authenticated: valid token at day 5
    t1 = t0 + timedelta(days=5)
    monkeypatch.setattr("agent_runtime.auth.auth_service._now", lambda: t1)

    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is True
    assert data["code"] is None
    expected_renewed_expires_at = (t1 + timedelta(days=30)).isoformat()
    assert data["session"]["expires_at"] == expected_renewed_expires_at

    # 3. Unauthenticated: expired token at day 36
    t_expired = t0 + timedelta(days=36)
    monkeypatch.setattr("agent_runtime.auth.auth_service._now", lambda: t_expired)

    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is False
    assert data["code"] == "auth_session_expired"
