from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from agent_runtime.connection_channel.service import (
    ConnectionChannelSettingsService,
    ConnectionChannelSettingsView,
    ConnectionChannelStatusView,
)


class ConnectionChannelSettingsResponse(BaseModel):
    cloud_service_host: str | None = None
    cloud_service_port: int | None = None
    configured: bool
    remote_access_enabled: bool
    authority: str | None = None
    updated_at: str | None = None
    db_path: str
    warning_message: str | None = None


class ConnectionChannelSettingsUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cloud_service_host: str | None = None
    cloud_service_port: int | str | None = None


class ConnectionChannelStatusResponse(BaseModel):
    cloud_service_host: str | None = None
    cloud_service_port: int | None = None
    configured: bool
    remote_access_enabled: bool
    authority: str | None = None
    tunnel_state: str
    tunnel_state_message: str
    controller: str
    owner_id: str | None = None
    managed_tunnel_id: str | None = None
    managed_config_path: str | None = None
    managed_task_name: str | None = None
    managed_pid: int | None = None
    controller_owner_marker: str | None = None
    applied_config_version: str | None = None
    current_config_version: str | None = None
    last_started_at: str | None = None
    last_stopped_at: str | None = None
    last_error_message: str | None = None
    updated_at: str | None = None
    db_path: str
    warning_message: str | None = None


def create_connection_channel_router(service: ConnectionChannelSettingsService) -> APIRouter:
    router = APIRouter()

    @router.get("/settings", response_model=ConnectionChannelSettingsResponse)
    def get_settings() -> ConnectionChannelSettingsResponse:
        return _settings_response(service.get_settings())

    @router.put("/settings", response_model=ConnectionChannelSettingsResponse)
    def update_settings(body: ConnectionChannelSettingsUpdateBody) -> ConnectionChannelSettingsResponse:
        try:
            settings = service.save_settings(
                cloud_service_host=body.cloud_service_host,
                cloud_service_port=body.cloud_service_port,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _settings_response(settings)

    @router.get("/status", response_model=ConnectionChannelStatusResponse)
    def get_status() -> ConnectionChannelStatusResponse:
        return _status_response(service.get_status())

    @router.post("/actions/enable", response_model=ConnectionChannelStatusResponse)
    def enable_remote_access() -> ConnectionChannelStatusResponse:
        try:
            status = service.enable_remote_access()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _status_response(status)

    @router.post("/actions/disable", response_model=ConnectionChannelStatusResponse)
    def disable_remote_access() -> ConnectionChannelStatusResponse:
        return _status_response(service.disable_remote_access())

    @router.get("/effective-endpoint", response_model=ConnectionChannelSettingsResponse)
    def get_effective_endpoint() -> ConnectionChannelSettingsResponse:
        status = service.get_status()
        if not status.configured:
            raise HTTPException(
                status_code=400,
                detail="当前电脑远程访问 Host 和端口尚未配置，远程浏览器连接入口不可用。",
            )
        if not status.remote_access_enabled:
            raise HTTPException(
                status_code=400,
                detail="当前电脑远程访问模式未开启，远程浏览器连接入口不可用。",
            )
        return _settings_response(service.get_settings())

    return router


def _settings_response(settings: ConnectionChannelSettingsView) -> ConnectionChannelSettingsResponse:
    return ConnectionChannelSettingsResponse(
        cloud_service_host=settings.cloud_service_host,
        cloud_service_port=settings.cloud_service_port,
        configured=settings.configured,
        remote_access_enabled=settings.remote_access_enabled,
        authority=settings.authority,
        updated_at=settings.updated_at,
        db_path=settings.db_path,
        warning_message=settings.warning_message,
    )


def _status_response(status: ConnectionChannelStatusView) -> ConnectionChannelStatusResponse:
    return ConnectionChannelStatusResponse(
        cloud_service_host=status.cloud_service_host,
        cloud_service_port=status.cloud_service_port,
        configured=status.configured,
        remote_access_enabled=status.remote_access_enabled,
        authority=status.authority,
        tunnel_state=status.tunnel_state,
        tunnel_state_message=status.tunnel_state_message,
        controller=status.controller,
        owner_id=status.owner_id,
        managed_tunnel_id=status.managed_tunnel_id,
        managed_config_path=status.managed_config_path,
        managed_task_name=status.managed_task_name,
        managed_pid=status.managed_pid,
        controller_owner_marker=status.controller_owner_marker,
        applied_config_version=status.applied_config_version,
        current_config_version=status.current_config_version,
        last_started_at=status.last_started_at,
        last_stopped_at=status.last_stopped_at,
        last_error_message=status.last_error_message,
        updated_at=status.updated_at,
        db_path=status.db_path,
        warning_message=status.warning_message,
    )
