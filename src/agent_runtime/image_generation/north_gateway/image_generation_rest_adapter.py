from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_runtime.image_generation.service import ImageGenerationConfigurationError, ImageGenerationService


class ImageGenerationSettingsResponse(BaseModel):
    llm_policy_group_id: str | None = None
    llm_policy_group_status: str
    llm_policy_group_status_message: str | None = None
    default_request_params: dict[str, Any] = Field(default_factory=dict)
    updated_at: str | None = None


class ImageGenerationSettingsUpdateBody(BaseModel):
    llm_policy_group_id: str | None = None
    default_request_params: dict[str, Any] = Field(default_factory=dict)


def create_image_generation_router(service: ImageGenerationService) -> APIRouter:
    router = APIRouter()

    @router.get("/settings", response_model=ImageGenerationSettingsResponse)
    def get_settings() -> ImageGenerationSettingsResponse:
        settings = service.get_settings()
        status, status_message = _policy_status(service, settings.llm_policy_group_id)
        return ImageGenerationSettingsResponse(
            llm_policy_group_id=settings.llm_policy_group_id,
            llm_policy_group_status=status,
            llm_policy_group_status_message=status_message,
            default_request_params=dict(settings.default_request_params),
            updated_at=settings.updated_at,
        )

    @router.put("/settings", response_model=ImageGenerationSettingsResponse)
    def update_settings(body: ImageGenerationSettingsUpdateBody) -> ImageGenerationSettingsResponse:
        try:
            service.save_settings(
                llm_policy_group_id=body.llm_policy_group_id,
                default_request_params=dict(body.default_request_params),
            )
        except ImageGenerationConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return get_settings()

    return router


def _policy_status(service: ImageGenerationService, policy_group_id: str | None) -> tuple[str, str | None]:
    if not policy_group_id:
        return "unset", "Image generation LLM policy is not configured."
    try:
        service.validate_policy(policy_group_id)
    except KeyError:
        return "missing", f"LLM execution policy does not exist: {policy_group_id}"
    except ImageGenerationConfigurationError as exc:
        return "invalid", str(exc)
    except ValueError as exc:
        return "invalid", str(exc)
    return "ok", None
