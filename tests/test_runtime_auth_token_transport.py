from types import SimpleNamespace

from agent_runtime.server import _runtime_auth_token_from_request


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
