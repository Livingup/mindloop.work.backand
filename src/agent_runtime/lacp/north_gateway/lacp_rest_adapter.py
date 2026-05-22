from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_runtime.lacp.bridge import AgentRuntimeLacpBridge
from agent_runtime.lacp.models import LacpInstanceRegistration


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


def create_lacp_router(bridge: AgentRuntimeLacpBridge) -> APIRouter:
    router = APIRouter()

    @router.get("/registrations", response_model=LacpRegistrationListResponse)
    def list_registrations() -> LacpRegistrationListResponse:
        return LacpRegistrationListResponse(
            registrations=[_registration_response(item) for item in bridge.list_registrations()],
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
