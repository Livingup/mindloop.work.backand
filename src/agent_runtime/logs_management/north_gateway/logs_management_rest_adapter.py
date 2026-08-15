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


class LogsCapabilitiesResponse(BaseModel):
    supported_space_release_modes: list[str] = Field(default_factory=list)
    release_only_retry_supported: bool = False


class LogsCleanupJobCreateRequest(BaseModel):
    mode: str
    lifecycle_filters: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    space_release_mode: str = "logical_only"
    release_only: bool = False


class LogsCleanupScopeResponse(BaseModel):
    task_count: int
    scene_count: int


class LogsCleanupCategoryResultResponse(BaseModel):
    category: str
    label: str
    matched_count: int
    deleted_count: int
    archive_files_deleted_count: int = 0


class LogsCleanupStoreSpaceReleaseResponse(BaseModel):
    store_id: str
    eligible: bool = False
    status: str
    vacuum_started: bool = False
    required_working_bytes: int | None = None
    available_bytes: int | None = None
    page_size_bytes: int | None = None
    page_count: int | None = None
    freelist_count: int | None = None
    before_size_bytes: int | None = None
    after_size_bytes: int | None = None
    reclaimed_size_bytes: int = 0
    net_size_change_bytes: int = 0
    before_db_size_bytes: int | None = None
    after_db_size_bytes: int | None = None
    before_wal_size_bytes: int | None = None
    after_wal_size_bytes: int | None = None
    before_shm_size_bytes: int | None = None
    after_shm_size_bytes: int | None = None
    error_code: str | None = None
    message: str | None = None


class LogsCleanupSpaceReleaseResponse(BaseModel):
    requested_mode: str
    status: str
    eligible_store_count: int = 0
    before_total_size_bytes: int | None = None
    after_total_size_bytes: int | None = None
    reclaimed_size_bytes: int = 0
    net_size_change_bytes: int = 0
    stores: list[LogsCleanupStoreSpaceReleaseResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class LogsCleanupResponse(BaseModel):
    mode: str
    space_release_mode: str = "logical_only"
    release_only: bool = False
    lifecycle_filters: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    scope: LogsCleanupScopeResponse
    category_results: list[LogsCleanupCategoryResultResponse] = Field(default_factory=list)
    total_matched_count: int
    total_deleted_count: int
    space_release: LogsCleanupSpaceReleaseResponse | None = None


class LogsCleanupJobResponse(BaseModel):
    job_id: str
    mode: str
    status: str
    space_release_mode: str = "logical_only"
    phase: str = "resolving_scope"
    active_store_id: str | None = None
    release_only: bool = False
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

    @router.get("/capabilities", response_model=LogsCapabilitiesResponse)
    def get_logs_capabilities() -> dict[str, Any]:
        return service.get_capabilities()

    @router.post("/cleanup/jobs", response_model=LogsCleanupJobResponse, status_code=202)
    def create_logs_cleanup_job(body: LogsCleanupJobCreateRequest) -> dict[str, Any]:
        try:
            return service.create_cleanup_job(
                mode=body.mode,
                lifecycle_filters=body.lifecycle_filters,
                categories=body.categories,
                space_release_mode=body.space_release_mode,
                release_only=body.release_only,
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
