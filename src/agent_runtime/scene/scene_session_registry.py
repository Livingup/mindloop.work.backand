# -*- coding: utf-8 -*-
"""Scene 会话注册表：追踪前端连接状态并自动关闭过期 scene。

该组件纯内存运行，服务重启后所有 session 自然过期（前端 SSE 也同时断开，不存在残留）。
主要职责：
- 记录每个 scene 的 "打开" 时间和最近一次心跳时间；
- 后台守护线程周期性扫描过期 session；
- 过期判定：last_heartbeat_at + stale_timeout < now；
- 过期后回调 on_expired 闭包（由外层注入，负责执行 detach 逻辑）。

线程安全：所有读写操作由 RLock 保护。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import RLock, Thread
from typing import Callable

logger = logging.getLogger(__name__)

# 默认心跳过期超时（秒）。前端应以 STALE_TIMEOUT / 3 的间隔发送心跳。
# 设为 120s 以容忍浏览器对后台 Tab 的 setInterval 节流（通常降至 ~1 次/分钟）。
DEFAULT_STALE_TIMEOUT_SECONDS: float = 120.0

# 后台扫描间隔（秒）。
DEFAULT_SWEEP_INTERVAL_SECONDS: float = 5.0


@dataclass(slots=True)
class SceneSession:
    """单个 scene 的会话状态。"""

    scene_id: str
    workspace_path: str
    opened_at: float = field(default_factory=time.time)
    last_heartbeat_at: float = field(default_factory=time.time)


class SceneSessionRegistry:
    """Scene 前端连接会话注册表。

    Parameters
    ----------
    on_expired : Callable[[SceneSession], None]
        session 过期时回调。由外层注入 detach_scene 逻辑。
        回调在 sweep 守护线程中同步执行，不应抛出异常（内部应 try-except 兜底）。
    can_expire : Callable[[SceneSession], bool] | None
        可选的过期前置条件判断。返回 True 表示可以安全过期；返回 False 表示 scene
        下仍有活跃执行（如 running agent），应延迟过期直到下一轮扫描。
        为 None 时视为始终允许过期。
    stale_timeout : float
        心跳过期超时秒数。超过该时间未收到心跳视为断连。
    sweep_interval : float
        后台扫描间隔秒数。
    """

    def __init__(
        self,
        *,
        on_expired: Callable[[SceneSession], None],
        can_expire: Callable[[SceneSession], bool] | None = None,
        stale_timeout: float = DEFAULT_STALE_TIMEOUT_SECONDS,
        sweep_interval: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self._on_expired = on_expired
        self._can_expire = can_expire
        self._stale_timeout = stale_timeout
        self._sweep_interval = sweep_interval
        self._lock = RLock()
        self._sessions: dict[str, SceneSession] = {}
        self._stopped = False
        self._sweep_thread: Thread | None = None

    # ── 公开 API ──────────────────────────────────────────────────

    def open_session(self, scene_id: str, workspace_path: str) -> None:
        """注册或刷新一个 scene session。attach_scene 时调用。"""
        now = time.time()
        with self._lock:
            existing = self._sessions.get(scene_id)
            if existing is not None:
                existing.last_heartbeat_at = now
                existing.workspace_path = workspace_path
            else:
                self._sessions[scene_id] = SceneSession(
                    scene_id=scene_id,
                    workspace_path=workspace_path,
                    opened_at=now,
                    last_heartbeat_at=now,
                )

    def heartbeat(self, scene_id: str) -> bool:
        """刷新 scene session 心跳。返回 False 表示该 scene 未注册（前端应重新 attach）。"""
        now = time.time()
        with self._lock:
            session = self._sessions.get(scene_id)
            if session is None:
                return False
            session.last_heartbeat_at = now
            return True

    def close_session(self, scene_id: str) -> SceneSession | None:
        """显式关闭 scene session（前端主动 detach 或 beforeunload 时调用）。

        返回被移除的 session，不存在则返回 None。
        注意：本方法只移除注册表记录，不执行 detach 业务逻辑——调用方负责 detach。
        """
        with self._lock:
            return self._sessions.pop(scene_id, None)

    def is_open(self, scene_id: str) -> bool:
        """判断 scene 是否处于打开状态（有活跃 session）。"""
        with self._lock:
            return scene_id in self._sessions

    def list_sessions(self) -> list[SceneSession]:
        """列出所有活跃 session（快照）。"""
        with self._lock:
            return list(self._sessions.values())

    def get_session_diagnostics(self, scene_id: str) -> dict[str, object]:
        now = time.time()
        with self._lock:
            session = self._sessions.get(scene_id)
            if session is None:
                return {
                    "scene_id": scene_id,
                    "is_open": False,
                    "stale_timeout_seconds": self._stale_timeout,
                    "last_heartbeat_age_seconds": None,
                    "is_stale": None,
                }
            last_heartbeat_age = now - session.last_heartbeat_at
            return {
                "scene_id": scene_id,
                "workspace_path": session.workspace_path,
                "is_open": True,
                "opened_at": session.opened_at,
                "last_heartbeat_at": session.last_heartbeat_at,
                "opened_at_iso": _iso_from_timestamp(session.opened_at),
                "last_heartbeat_at_iso": _iso_from_timestamp(session.last_heartbeat_at),
                "stale_timeout_seconds": self._stale_timeout,
                "last_heartbeat_age_seconds": last_heartbeat_age,
                "is_stale": last_heartbeat_age > self._stale_timeout,
            }

    # ── 生命周期 ─────────────────────────────────────────────────

    def start(self) -> None:
        """启动后台过期扫描守护线程。"""
        if self._sweep_thread is not None and self._sweep_thread.is_alive():
            return
        self._stopped = False
        self._sweep_thread = Thread(
            target=self._sweep_loop,
            name="scene-session-sweep",
            daemon=True,
        )
        self._sweep_thread.start()
        logger.info(
            "SceneSessionRegistry sweep 启动: stale_timeout=%.1fs, sweep_interval=%.1fs",
            self._stale_timeout,
            self._sweep_interval,
        )

    def stop(self) -> None:
        """停止后台扫描线程。"""
        self._stopped = True
        if self._sweep_thread is not None:
            self._sweep_thread.join(timeout=self._sweep_interval * 2)
            self._sweep_thread = None
        logger.info("SceneSessionRegistry sweep 已停止。")

    # ── 私有 ─────────────────────────────────────────────────────

    def _sweep_loop(self) -> None:
        """守护线程主循环：周期性检查过期 session 并触发回调。"""
        while not self._stopped:
            try:
                self._sweep_once()
            except Exception:
                logger.exception("SceneSessionRegistry sweep 异常")
            time.sleep(self._sweep_interval)

    def _sweep_once(self) -> None:
        """执行一轮过期扫描。"""
        now = time.time()
        expired: list[SceneSession] = []
        with self._lock:
            for scene_id, session in list(self._sessions.items()):
                if now - session.last_heartbeat_at > self._stale_timeout:
                    # 检查是否可以安全过期（如 scene 下无 running agent）
                    if self._can_expire is not None:
                        try:
                            if not self._can_expire(session):
                                logger.debug(
                                    "Scene session 已超时但不满足过期条件（可能有活跃执行），"
                                    "延迟到下一轮: scene_id=%s",
                                    scene_id,
                                )
                                continue
                        except Exception:
                            logger.exception(
                                "Scene session can_expire 判断异常，按可过期处理: scene_id=%s",
                                scene_id,
                            )
                    expired.append(session)
                    del self._sessions[scene_id]
        for session in expired:
            logger.info(
                "Scene session 过期，触发自动关闭: scene_id=%s, workspace=%s, "
                "last_heartbeat=%.1fs ago",
                session.scene_id,
                session.workspace_path,
                now - session.last_heartbeat_at,
            )
            try:
                self._on_expired(session)
            except Exception:
                logger.exception(
                    "Scene session 过期回调失败: scene_id=%s", session.scene_id
                )


def _iso_from_timestamp(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))
