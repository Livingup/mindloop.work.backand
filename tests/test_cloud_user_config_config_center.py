# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.cloud_user_config.north_gateway.config_center_rest_adapter import (
    create_config_center_router,
)
from agent_runtime.cloud_user_config.postgres_store import (
    CloudUserConfigChange,
    CloudUserConfigChangeSet,
    VERSIONED_CONFIG_NAMESPACES,
    is_versioned_config_namespace,
    versioned_config_namespaces,
)


class _FakeConfigCenterStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.versions: dict[tuple[str, str], dict[str, Any]] = {}
        self.current_revision = 0

    def save_payload(self, namespace: str, item_id: str, payload: dict[str, Any]) -> int | None:
        self.items[(namespace, item_id)] = dict(payload)
        if not is_versioned_config_namespace(namespace):
            return None
        self.current_revision += 1
        self.versions[(namespace, item_id)] = {
            "revision": self.current_revision,
            "deleted": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return self.current_revision

    def delete_payload(self, namespace: str, item_id: str) -> bool:
        existed = self.items.pop((namespace, item_id), None) is not None
        if existed and is_versioned_config_namespace(namespace):
            self.current_revision += 1
            self.versions[(namespace, item_id)] = {
                "revision": self.current_revision,
                "deleted": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        return existed

    def list_config_changes_since(self, last_seen_revision: int) -> CloudUserConfigChangeSet:
        changes: list[CloudUserConfigChange] = []
        for (namespace, item_id), version in sorted(
            self.versions.items(),
            key=lambda item: item[1]["revision"],
        ):
            if version["revision"] <= last_seen_revision:
                continue
            deleted = bool(version["deleted"])
            payload = None if deleted else dict(self.items[(namespace, item_id)])
            changes.append(
                CloudUserConfigChange(
                    namespace=namespace,
                    item_id=item_id,
                    revision=int(version["revision"]),
                    deleted=deleted,
                    payload=payload,
                    updated_at=str(version["updated_at"]),
                )
            )
        return CloudUserConfigChangeSet(
            current_revision=self.current_revision,
            changes=tuple(changes),
        )


def test_versioned_config_namespaces_are_only_user_shared_config_domains() -> None:
    assert set(versioned_config_namespaces()) == {
        "agent_definition",
        "context_compression.runtime_settings",
        "context_compression.worker_prompt",
        "file_resource.object_storage_settings",
        "image_generation.runtime_settings",
        "image_generation_llm_execution_policy",
        "image_generation_llm_model_metadata",
        "image_generation_llm_provider_definition",
        "lacp.registry_service_settings",
        "llm_execution_policy",
        "llm_model_metadata",
        "llm_provider_definition",
        "mcp.streamable_http_service_definition",
        "prompt_snippet",
        "web_tools.runtime_settings",
    }
    assert "conversation_history" not in VERSIONED_CONFIG_NAMESPACES
    assert "agent_runtime_state" not in VERSIONED_CONFIG_NAMESPACES
    assert "file_resource_archive" not in VERSIONED_CONFIG_NAMESPACES


def test_config_center_changes_endpoint_returns_latest_config_item_states() -> None:
    store = _FakeConfigCenterStore()
    store.save_payload("prompt_snippet", "shared-prompt", {"text": "v1"})
    store.save_payload(
        "file_resource.object_storage_settings",
        "default",
        {
            "gateway_provider": "aliyun_oss",
            "object_storage_bucket_name": "agent-runtime-resources",
        },
    )
    store.save_payload("conversation_history", "runtime-data", {"message": "not config"})
    store.delete_payload("prompt_snippet", "shared-prompt")

    app = FastAPI()
    app.include_router(
        create_config_center_router(store_accessor=lambda: store),
        prefix="/api/config-center",
    )

    response = TestClient(app).get("/api/config-center/changes?since=0")

    assert response.status_code == 200
    body = response.json()
    assert body["current_revision"] == 3
    assert body["poll_interval_seconds"] == 5
    assert "file_resource.object_storage_settings" in body["versioned_namespaces"]
    assert {
        (change["namespace"], change["item_id"], change["deleted"])
        for change in body["changes"]
    } == {
        ("file_resource.object_storage_settings", "default", False),
        ("prompt_snippet", "shared-prompt", True),
    }
    object_storage_change = next(
        change
        for change in body["changes"]
        if change["namespace"] == "file_resource.object_storage_settings"
    )
    assert object_storage_change["payload"]["gateway_provider"] == "aliyun_oss"

    since_two_response = TestClient(app).get("/api/config-center/changes?since=2")
    assert since_two_response.status_code == 200
    assert since_two_response.json()["changes"] == [
        {
            "namespace": "prompt_snippet",
            "item_id": "shared-prompt",
            "revision": 3,
            "deleted": True,
            "payload": None,
            "updated_at": body["changes"][1]["updated_at"],
        }
    ]
