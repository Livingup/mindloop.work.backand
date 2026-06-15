from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
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


class LogsCleanupJobCreateRequest(BaseModel):
    mode: str
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


class LogsCleanupJobResponse(BaseModel):
    job_id: str
    mode: str
    status: str
    lifecycle_filters: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None
    result: LogsCleanupResponse | None = None
    error_message: str | None = None


class LogsCleanupJobsResponse(BaseModel):
    items: list[LogsCleanupJobResponse] = Field(default_factory=list)
    active_count: int
    volatile: bool = True
    retention_note: str


def create_logs_management_router(service: LogsManagementService) -> APIRouter:
    router = APIRouter()

    @router.get("/overview", response_model=LogsOverviewResponse)
    def get_logs_overview() -> dict[str, Any]:
        return service.get_overview()

    @router.post("/cleanup/jobs", response_model=LogsCleanupJobResponse, status_code=202)
    def create_logs_cleanup_job(body: LogsCleanupJobCreateRequest) -> dict[str, Any]:
        try:
            return service.create_cleanup_job(
                mode=body.mode,
                lifecycle_filters=body.lifecycle_filters,
                categories=body.categories,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/cleanup/jobs", response_model=LogsCleanupJobsResponse)
    def list_logs_cleanup_jobs(
        limit: int = Query(default=10, ge=1, le=50),
        active_only: bool = Query(default=False),
    ) -> dict[str, Any]:
        return service.list_cleanup_jobs(limit=limit, active_only=active_only)

    @router.get("/cleanup/jobs/{job_id}", response_model=LogsCleanupJobResponse)
    def get_logs_cleanup_job(job_id: str) -> dict[str, Any]:
        job = service.get_cleanup_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="日志清理任务不存在。")
        return job

    return router
