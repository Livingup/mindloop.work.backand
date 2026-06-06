from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles

logger = logging.getLogger("agent_runtime.frontend_static")


def mount_frontend_static(app: FastAPI, frontend_dist_dir: str | Path | None) -> bool:
    """将 release SPA 前端资源挂载到 Runtime HTTP origin。

    Electron packaged renderer 继续使用 app://renderer；该挂载只服务 desktop-web / mobile-web
    通过 HTTP 访问同一 Runtime origin 的 release 前端。/api/* 始终保留给 Runtime API，
    非 /api 路径优先返回 dist 中的真实文件，找不到时回退到 index.html 支持前端路由。
    """

    if frontend_dist_dir is None:
        return False

    dist_dir = Path(frontend_dist_dir).expanduser().resolve()
    index_file = dist_dir / "index.html"
    if not dist_dir.is_dir() or not index_file.is_file():
        raise FileNotFoundError(
            f"Release frontend dist directory is invalid or missing index.html: {dist_dir}"
        )

    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=str(assets_dir), html=False, check_dir=True),
            name="release-frontend-assets",
        )

    @app.get("/", include_in_schema=False)
    async def release_frontend_index():
        return FileResponse(index_file)

    @app.get("/{frontend_path:path}", include_in_schema=False)
    async def release_frontend_fallback(frontend_path: str):
        normalized_path = frontend_path.lstrip("/")
        if normalized_path == "api" or normalized_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")

        requested_file = _resolve_dist_file(dist_dir, normalized_path)
        if requested_file is not None and requested_file.is_file():
            return FileResponse(requested_file)
        return FileResponse(index_file)

    logger.info("Release frontend SPA mounted from %s", dist_dir)
    return True


def _resolve_dist_file(dist_dir: Path, request_path: str) -> Path | None:
    if not request_path:
        return None
    candidate = (dist_dir / request_path).resolve()
    try:
        candidate.relative_to(dist_dir)
    except ValueError:
        return None
    return candidate
