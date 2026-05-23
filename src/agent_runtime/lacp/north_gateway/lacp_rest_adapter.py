from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_runtime.config import ConfigRegistry
from agent_runtime.lacp.bridge import AgentRuntimeLacpBridge
from agent_runtime.lacp.models import LacpClientStatus, LacpInstanceRegistration


class LacpRegistrationResponse(BaseModel):
    instance_id: str
    role_name: str
    description: str
    workspace: str | None = None
    enabled: bool
    status: str
    error_code: str | None = None
    error_message: str | None = None
    updated_at: str


class LacpInstanceEnableBody(BaseModel):
    enabled: bool = True


class LacpRegistrationListResponse(BaseModel):
    registrations: list[LacpRegistrationResponse] = Field(default_factory=list)


class LacpSettingsResponse(BaseModel):
    enabled: bool
    registry_url: str
    api_key_configured: bool
    config_path: str
    env_file_path: str


class LacpSettingsUpdateBody(BaseModel):
    enabled: bool
    registry_url: str = ""
    api_key: str | None = None


class LacpClientStatusResponse(BaseModel):
    enabled: bool
    running: bool
    connected: bool
    registry_url: str
    ws_endpoint: str | None = None
    last_error: str | None = None
    last_connected_at: str | None = None
    last_disconnected_at: str | None = None
    reconnect_attempt: int = 0
    registration_count: int
    registered_count: int


def create_lacp_router(bridge: AgentRuntimeLacpBridge) -> APIRouter:
    router = APIRouter()

    @router.get("/registrations", response_model=LacpRegistrationListResponse)
    def list_registrations() -> LacpRegistrationListResponse:
        return LacpRegistrationListResponse(
            registrations=[_registration_response(item) for item in bridge.list_registrations()],
        )

    @router.get("/status", response_model=LacpClientStatusResponse)
    def get_client_status() -> LacpClientStatusResponse:
        registrations = bridge.list_registrations()
        return _client_status_response(
            bridge.client_status(),
            registration_count=len(registrations),
            registered_count=sum(1 for item in registrations if item.enabled and item.status == "registered"),
        )

    @router.get("/settings", response_model=LacpSettingsResponse)
    def get_settings() -> LacpSettingsResponse:
        return _settings_response()

    @router.put("/settings", response_model=LacpSettingsResponse)
    async def update_settings(body: LacpSettingsUpdateBody) -> LacpSettingsResponse:
        registry_url = body.registry_url.strip()
        current = ConfigRegistry.instance().lacp
        next_api_key = current.api_key if body.api_key is None else body.api_key.strip()
        if body.enabled:
            if not registry_url:
                raise HTTPException(status_code=400, detail="LACP registry URL is required when LACP is enabled.")
            if not next_api_key:
                raise HTTPException(status_code=400, detail="LACP API key is required when LACP is enabled.")
        try:
            snapshot = ConfigRegistry.instance().snapshot()
            _write_lacp_yaml_section(snapshot.config_path, enabled=body.enabled, registry_url=registry_url)
            if body.api_key is not None:
                _write_lacp_env_key(snapshot.config_path, snapshot.env_file_path, next_api_key or "")
            ConfigRegistry.reload()
            await bridge.reload_client_from_config()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"LACP settings update failed: {exc}") from exc
        return _settings_response()

    @router.get("/instances/{instance_id}/registration", response_model=LacpRegistrationResponse)
    def get_instance_registration(instance_id: str) -> LacpRegistrationResponse:
        item = bridge.get_registration(instance_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"LACP registration not found: {instance_id}")
        return _registration_response(item)

    @router.put("/instances/{instance_id}/registration", response_model=LacpRegistrationResponse)
    async def set_instance_registration(instance_id: str, body: LacpInstanceEnableBody) -> LacpRegistrationResponse:
        try:
            if body.enabled:
                item = await bridge.enable_instance(instance_id)
            else:
                item = await bridge.disable_instance(instance_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Agent instance not found: {instance_id}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if item is None:
            raise HTTPException(status_code=404, detail=f"LACP registration not found: {instance_id}")
        return _registration_response(item)

    return router


def _registration_response(item: LacpInstanceRegistration) -> LacpRegistrationResponse:
    return LacpRegistrationResponse(
        instance_id=item.instance_id,
        role_name=item.role_name,
        description=item.description,
        workspace=item.workspace,
        enabled=item.enabled,
        status=item.status,
        error_code=item.error_code,
        error_message=item.error_message,
        updated_at=item.updated_at,
    )


def _client_status_response(
    item: LacpClientStatus,
    *,
    registration_count: int,
    registered_count: int,
) -> LacpClientStatusResponse:
    return LacpClientStatusResponse(
        enabled=item.enabled,
        running=item.running,
        connected=item.connected,
        registry_url=item.registry_url,
        ws_endpoint=item.ws_endpoint,
        last_error=item.last_error,
        last_connected_at=item.last_connected_at,
        last_disconnected_at=item.last_disconnected_at,
        reconnect_attempt=item.reconnect_attempt,
        registration_count=registration_count,
        registered_count=registered_count,
    )


def _settings_response() -> LacpSettingsResponse:
    registry = ConfigRegistry.instance()
    snapshot = registry.snapshot()
    config = registry.lacp
    env_path = _lacp_env_path(snapshot.config_path, snapshot.env_file_path)
    return LacpSettingsResponse(
        enabled=config.enabled,
        registry_url=config.registry_url,
        api_key_configured=bool((config.api_key or "").strip()),
        config_path=snapshot.config_path,
        env_file_path=str(env_path),
    )


def _write_lacp_yaml_section(config_path: str, *, enabled: bool, registry_url: str) -> None:
    path = Path(config_path)
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    start_index = next((index for index, line in enumerate(raw_lines) if line.strip() == "lacp:" and not line.startswith((" ", "\t"))), None)
    replacement = [
        "lacp:",
        f"  enabled: {'true' if enabled else 'false'}",
        f"  registry_url: {_yaml_double_quote(registry_url)}",
        '  api_key: "${LACP_API_KEY:-}"',
        "  data_dir: \"~/.spread-ai/lacp\"",
        "  connect_timeout_seconds: 10",
        "  request_timeout_seconds: 5",
        "  reconnect_initial_delay_seconds: 1",
        "  reconnect_max_delay_seconds: 30",
    ]
    if start_index is None:
        next_lines = raw_lines + ["", *replacement]
    else:
        end_index = start_index + 1
        while end_index < len(raw_lines):
            line = raw_lines[end_index]
            if line.strip() and not line.startswith((" ", "\t")):
                break
            end_index += 1
        next_lines = raw_lines[:start_index] + replacement + raw_lines[end_index:]
    path.write_text("\n".join(next_lines) + "\n", encoding="utf-8")


def _write_lacp_env_key(config_path: str, env_file_path: str | None, api_key: str) -> None:
    path = _lacp_env_path(config_path, env_file_path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacement = f"LACP_API_KEY={api_key}"
    for index, line in enumerate(lines):
        if line.startswith("LACP_API_KEY="):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if api_key:
        os.environ["LACP_API_KEY"] = api_key
    else:
        os.environ.pop("LACP_API_KEY", None)


def _lacp_env_path(config_path: str, env_file_path: str | None) -> Path:
    if env_file_path:
        return Path(env_file_path)
    return Path(config_path).resolve().parent / ".env"


def _yaml_double_quote(value: str) -> str:
    return "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"") + "\""
