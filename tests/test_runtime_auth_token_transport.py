import re
from types import SimpleNamespace

import pytest

from agent_runtime.auth.auth_service import (
    RuntimeAuthService,
    validate_runtime_password_policy,
)
from agent_runtime.auth.infrastructure.sqlite_auth_repository import SqliteRuntimeAuthRepository
from agent_runtime.auth.rate_limiter import InMemoryFixedWindowRateLimiter
from agent_runtime.auth.runtime_access_policy import (
    RuntimeBootstrapAccessPolicy,
    host_exposes_remote_clients,
    is_no_account_api_allowed_path,
)
from agent_runtime.server import (
    _is_runtime_auth_exempt_path,
    _is_runtime_bootstrap_exempt_path,
    _runtime_auth_token_from_request,
    _runtime_cors_allow_origin_regex,
    _runtime_cors_allow_origins,
    _runtime_security_response_headers,
)


class _Request:
    def __init__(
        self,
        *,
        path: str,
        method: str = "GET",
        authorization: str | None = None,
        access_token: str | None = None,
    ) -> None:
        self.url = SimpleNamespace(path=path)
        self.method = method
        self.headers = {}
        if authorization is not None:
            self.headers["authorization"] = authorization
        self.query_params = {}
        if access_token is not None:
            self.query_params["access_token"] = access_token


def test_bearer_auth_token_is_accepted_on_normal_rest_request() -> None:
    request = _Request(
        path="/api/agent-runtime/scenes/scene-a/attach",
        method="POST",
        authorization="Bearer bearer-token",
        access_token="query-token",
    )

    assert _runtime_auth_token_from_request(request) == "bearer-token"


def test_query_token_is_accepted_for_sse_eventsource_transport() -> None:
    request = _Request(
        path="/api/communication/streams/stream-a/events",
        method="GET",
        access_token="query-token",
    )

    assert _runtime_auth_token_from_request(request) == "query-token"


def test_query_token_is_accepted_for_file_resource_upload_task_sse_transport() -> None:
    request = _Request(
        path="/api/file-resources/upload-tasks/file-upload-a/events",
        method="GET",
        access_token="query-token",
    )

    assert _runtime_auth_token_from_request(request) == "query-token"


def test_query_token_is_accepted_for_file_resource_content_transport() -> None:
    request = _Request(
        path="/api/file-resources/resource-a/content",
        method="GET",
        access_token="query-token",
    )

    assert _runtime_auth_token_from_request(request) == "query-token"


def test_query_token_is_accepted_for_beacon_detach_transport() -> None:
    request = _Request(
        path="/api/agent-runtime/scenes/scene-a/detach",
        method="POST",
        access_token="query-token",
    )

    assert _runtime_auth_token_from_request(request) == "query-token"


def test_query_token_is_rejected_for_normal_rest_request() -> None:
    request = _Request(
        path="/api/agent-runtime/scenes/scene-a/attach",
        method="POST",
        access_token="query-token",
    )

    assert _runtime_auth_token_from_request(request) is None


def test_query_token_is_rejected_for_scene_stop_request() -> None:
    request = _Request(
        path="/api/agent-runtime/scenes/scene-a/stop",
        method="POST",
        access_token="query-token",
    )

    assert _runtime_auth_token_from_request(request) is None


def test_query_token_is_rejected_for_file_resource_metadata_request() -> None:
    request = _Request(
        path="/api/file-resources/resource-a",
        method="GET",
        access_token="query-token",
    )

    assert _runtime_auth_token_from_request(request) is None


def test_query_token_is_rejected_for_object_storage_settings_request() -> None:
    request = _Request(
        path="/api/file-resources/object-storage-settings",
        method="GET",
        access_token="query-token",
    )

    assert _runtime_auth_token_from_request(request) is None


@pytest.mark.parametrize(
    "password",
    [
        "shortA1!",
        "abcdefghijkl",
        "ABCDEFGHIJKL",
        "123456789012",
        "lowercase1234",
    ],
)
def test_runtime_bootstrap_password_policy_rejects_weak_passwords(password: str) -> None:
    with pytest.raises(ValueError):
        validate_runtime_password_policy(password)


def test_runtime_bootstrap_password_policy_accepts_strong_password() -> None:
    validate_runtime_password_policy("StrongerPassw0rd!")


def test_runtime_auth_service_does_not_create_account_for_weak_bootstrap_password(tmp_path) -> None:
    service = RuntimeAuthService(SqliteRuntimeAuthRepository(lambda: str(tmp_path)))

    with pytest.raises(ValueError):
        service.bootstrap(
            username="admin",
            password="weak12",
            device_id="device-a",
            client_kind="desktop-web",
            user_agent="pytest",
        )

    assert service.has_accounts() is False


def test_runtime_auth_service_creates_account_for_strong_bootstrap_password(tmp_path) -> None:
    service = RuntimeAuthService(SqliteRuntimeAuthRepository(lambda: str(tmp_path)))

    authenticated_session, token = service.bootstrap(
        username="Admin",
        password="StrongerPassw0rd!",
        device_id="device-a",
        client_kind="desktop-web",
        user_agent="pytest",
    )

    assert token
    assert authenticated_session.account.username == "admin"
    assert service.has_accounts() is True


def test_runtime_bind_host_detection_marks_non_loopback_as_lan_reachable() -> None:
    assert host_exposes_remote_clients("0.0.0.0") is True
    assert host_exposes_remote_clients("::") is True
    assert host_exposes_remote_clients("192.168.1.20") is True
    assert host_exposes_remote_clients("spread-ai.local") is True
    assert host_exposes_remote_clients("127.0.0.1") is False
    assert host_exposes_remote_clients("::1") is False
    assert host_exposes_remote_clients("localhost") is False


def test_first_run_bootstrap_policy_blocks_remote_client_on_lan_bind() -> None:
    policy = RuntimeBootstrapAccessPolicy("0.0.0.0")
    remote_request = SimpleNamespace(client=SimpleNamespace(host="192.168.1.23"))
    local_request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

    assert policy.allows_first_run_request(remote_request) is False
    assert policy.allows_first_run_request(local_request) is True


def test_first_run_bootstrap_policy_does_not_block_loopback_only_bind() -> None:
    policy = RuntimeBootstrapAccessPolicy("127.0.0.1")
    remote_request = SimpleNamespace(client=SimpleNamespace(host="192.168.1.23"))

    assert policy.allows_first_run_request(remote_request) is True


def test_no_account_api_surface_is_minimal() -> None:
    assert is_no_account_api_allowed_path("/api/health", "GET") is True
    assert is_no_account_api_allowed_path("/api/auth/status", "GET") is True
    assert is_no_account_api_allowed_path("/api/auth/bootstrap", "POST") is True
    assert is_no_account_api_allowed_path("/api/auth/bootstrap", "GET") is False
    assert is_no_account_api_allowed_path("/api/system-settings", "GET") is False
    assert is_no_account_api_allowed_path("/api/agent-runtime/scenes", "GET") is False
    assert is_no_account_api_allowed_path("/api/runtime-diagnostics/concurrency", "GET") is False


def test_system_settings_and_runtime_diagnostics_are_not_bootstrap_guard_exempt() -> None:
    assert _is_runtime_bootstrap_exempt_path("/api/system-settings") is False
    assert _is_runtime_bootstrap_exempt_path("/api/runtime-diagnostics/concurrency") is False
    assert _is_runtime_bootstrap_exempt_path("/api/runtime-bootstrap/status") is True
    assert _is_runtime_auth_exempt_path("/api/health") is True
    assert _is_runtime_auth_exempt_path("/api/runtime-diagnostics/concurrency") is False


def test_in_memory_fixed_window_rate_limiter_blocks_after_limit() -> None:
    now = 100.0

    def clock() -> float:
        return now

    limiter = InMemoryFixedWindowRateLimiter(max_attempts=2, window_seconds=10, clock=clock)

    assert limiter.consume("login:client:user").allowed is True
    assert limiter.consume("login:client:user").allowed is True
    blocked = limiter.consume("login:client:user")
    assert blocked.allowed is False
    assert blocked.retry_after_seconds >= 1

    now = 111.0
    assert limiter.consume("login:client:user").allowed is True


def test_runtime_security_headers_include_browser_hardening() -> None:
    headers = _runtime_security_response_headers()

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_runtime_cors_is_not_wildcard_and_only_allows_trusted_cross_origins() -> None:
    assert "*" not in _runtime_cors_allow_origins()
    assert "app://renderer" in _runtime_cors_allow_origins()

    cors_regex = re.compile(_runtime_cors_allow_origin_regex())
    assert cors_regex.match("http://localhost:5173")
    assert cors_regex.match("http://127.0.0.1:5173")
    assert cors_regex.match("http://[::1]:5173")
    assert not cors_regex.match("http://192.168.1.23:5173")


def test_runtime_auth_failure_body_returns_fixed_detail_and_reason_code() -> None:
    from agent_runtime.auth.models import RuntimeAuthFailureReason
    from agent_runtime.server import _RUNTIME_AUTH_FAILURE_DETAIL, _runtime_auth_failure_body

    for reason in (
        RuntimeAuthFailureReason.TOKEN_MISSING,
        RuntimeAuthFailureReason.TOKEN_INVALID,
        RuntimeAuthFailureReason.SESSION_REVOKED,
        RuntimeAuthFailureReason.SESSION_EXPIRED,
    ):
        body = _runtime_auth_failure_body(reason)
        assert body["detail"] == _RUNTIME_AUTH_FAILURE_DETAIL
        assert body["code"] == reason.value

