from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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
