from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agent_runtime.web_tools.models import WebToolPolicyCandidate
from agent_runtime.web_tools.service import WebToolsConfigurationError, WebToolsService


class WebToolPolicyCandidateBody(BaseModel):
    id: str
    provider_id: str
    model_binding_id: str
    enabled: bool = True


class WebToolsSettingsResponse(BaseModel):
    candidates: list[WebToolPolicyCandidateBody]
    status: str
    status_message: str | None = None
    supported_api_formats: list[str]
    updated_at: str | None = None


class WebToolsSettingsUpdateBody(BaseModel):
    candidates: list[WebToolPolicyCandidateBody]


def create_web_tools_router(service: WebToolsService) -> APIRouter:
    router = APIRouter()

    @router.get("/settings", response_model=WebToolsSettingsResponse)
    def get_settings() -> WebToolsSettingsResponse:
        settings = service.get_settings()
        status, status_message = _settings_status(service)
        return WebToolsSettingsResponse(
            candidates=[_candidate_to_body(item) for item in settings.candidates],
            status=status,
            status_message=status_message,
            supported_api_formats=list(service.supported_api_formats()),
            updated_at=settings.updated_at,
        )

    @router.put("/settings", response_model=WebToolsSettingsResponse)
    def update_settings(body: WebToolsSettingsUpdateBody) -> WebToolsSettingsResponse:
        try:
            service.save_settings(
                candidates=tuple(_body_to_candidate(item) for item in body.candidates),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, WebToolsConfigurationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return get_settings()

    return router


def _settings_status(service: WebToolsService) -> tuple[str, str | None]:
    try:
        service.validate_settings()
    except WebToolsConfigurationError as exc:
        return "unset", str(exc)
    except KeyError as exc:
        return "missing", str(exc)
    except ValueError as exc:
        return "invalid", str(exc)
    return "ok", None


def _candidate_to_body(candidate: WebToolPolicyCandidate) -> WebToolPolicyCandidateBody:
    return WebToolPolicyCandidateBody(
        id=candidate.id,
        provider_id=candidate.provider_id,
        model_binding_id=candidate.model_binding_id,
        enabled=candidate.enabled,
    )


def _body_to_candidate(body: WebToolPolicyCandidateBody) -> WebToolPolicyCandidate:
    return WebToolPolicyCandidate(
        id=body.id,
        provider_id=body.provider_id,
        model_binding_id=body.model_binding_id,
        enabled=body.enabled,
    )
