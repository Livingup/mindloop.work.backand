from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_runtime.logs_management.service import LogsManagementService


class LogsOverviewItemResponse(BaseModel):
    store_id: str
    title: str
    size_bytes: int
    record_count: int
    path: str
    available: bool = False


class LogsOverviewResponse(BaseModel):
    items: list[LogsOverviewItemResponse] = Field(default_factory=list)
    total_size_bytes: int


class LogsCleanupRequest(BaseModel):
    lifecycle_filters: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)


class LogsCleanupScopeResponse(BaseModel):
    task_count: int
    scene_count: int


class LogsCleanupCategoryResultResponse(BaseModel):
    category: str
    label: str
    matched_count: int
    deleted_count: int


class LogsCleanupResponse(BaseModel):
    mode: str
    lifecycle_filters: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    scope: LogsCleanupScopeResponse
    category_results: list[LogsCleanupCategoryResultResponse] = Field(default_factory=list)
    total_matched_count: int
    total_deleted_count: int


def create_logs_management_router(service: LogsManagementService) -> APIRouter:
    router = APIRouter()

    @router.get("/overview", response_model=LogsOverviewResponse)
    def get_logs_overview() -> dict[str, Any]:
        return service.get_overview()

    @router.post("/cleanup/preview", response_model=LogsCleanupResponse)
    def preview_logs_cleanup(body: LogsCleanupRequest) -> dict[str, Any]:
        try:
            return service.preview_cleanup(
                lifecycle_filters=body.lifecycle_filters,
                categories=body.categories,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/cleanup/execute", response_model=LogsCleanupResponse)
    def execute_logs_cleanup(body: LogsCleanupRequest) -> dict[str, Any]:
        try:
            return service.execute_cleanup(
                lifecycle_filters=body.lifecycle_filters,
                categories=body.categories,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
