# -*- coding: utf-8 -*-
"""Scene 多客户端会话注册表。

该组件追踪多个前端设备同时连接同一个 scene 的 presence。它只管理“哪个前端
还在连接/查看/控制 scene”的运行时事实，不代表 Agent 任务本身生命周期。单个
移动端、浏览器 Tab 或 Electron 窗口断开时，只移除对应 client session，不能误伤
其他仍在线的前端。

注册表纯内存运行；服务重启后所有前端连接自然失效，前端按现有 attach/replay
链路重新连接即可。线程安全：所有读写操作由 RLock 保护。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import RLock, Thread
from typing import Callable
from uuid import uuid4

logger = logging.getLogger(__name__)

# 默认心跳过期超时（秒）。前端应以 STALE_TIMEOUT / 3 的间隔发送心跳。
# 设为 120s 以容忍浏览器对后台 Tab 的 setInterval 节流（通常降至 ~1 次/分钟）。
DEFAULT_STALE_TIMEOUT_SECONDS: float = 120.0

# 后台扫描间隔（秒）。
DEFAULT_SWEEP_INTERVAL_SECONDS: float = 5.0


@dataclass(slots=True)
class SceneClientSession:
    """单个前端设备/窗口连接 scene 的会话状态。"""

    client_session_id: str
    device_id: str | None = None
    client_kind: str = "unknown"
    user_id: str | None = None
    opened_at: float = field(default_factory=time.time)
    last_heartbeat_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class SceneSession:
    """一个 scene 下所有前端连接的聚合状态。"""

    scene_id: str
    workspace_path: str
    opened_at: float = field(default_factory=time.time)
    last_heartbeat_at: float = field(default_factory=time.time)
    vacant_since_at: float | None = None
    clients: dict[str, SceneClientSession] = field(default_factory=dict)

    @property
    def active_client_count(self) -> int:
        return len(self.clients)


class SceneSessionRegistry:
    """Scene 前端连接会话注册表。

    on_expired 回调只在某个 scene 的全部客户端都离开/过期，并且调用方 can_expire
    策略确认可自动释放后触发。注册表会保留无客户端的空置 scene 以记录空置起点；
    单个 client 过期不会触发 scene 级 detach。
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

    def open_session(
        self,
        scene_id: str,
        workspace_path: str,
        *,
        client_session_id: str | None = None,
        device_id: str | None = None,
        client_kind: str = "unknown",
        user_id: str | None = None,
    ) -> str:
        """注册或刷新一个前端 client 对 scene 的连接，返回有效 client_session_id。"""
        now = time.time()
        effective_client_session_id = _normalize_client_session_id(client_session_id)
        with self._lock:
            session = self._sessions.get(scene_id)
            if session is None:
                session = SceneSession(
                    scene_id=scene_id,
                    workspace_path=workspace_path,
                    opened_at=now,
                    last_heartbeat_at=now,
                )
                self._sessions[scene_id] = session
            else:
                session.workspace_path = workspace_path
                session.last_heartbeat_at = now
                session.vacant_since_at = None

            client = session.clients.get(effective_client_session_id)
            if client is None:
                session.clients[effective_client_session_id] = SceneClientSession(
                    client_session_id=effective_client_session_id,
                    device_id=device_id,
                    client_kind=client_kind or "unknown",
                    user_id=user_id,
                    opened_at=now,
                    last_heartbeat_at=now,
                )
            else:
                client.device_id = device_id
                client.client_kind = client_kind or client.client_kind or "unknown"
                client.user_id = user_id
                client.last_heartbeat_at = now
            return effective_client_session_id

    def heartbeat(self, scene_id: str, client_session_id: str | None = None) -> bool:
        """刷新前端 client 心跳。返回 False 表示该连接未注册，前端应重新 attach。"""
        now = time.time()
        with self._lock:
            session = self._sessions.get(scene_id)
            if session is None:
                return False
            if client_session_id is None:
                # 兼容旧调用方：未传 client_session_id 时，如果 scene 下仍有客户端，刷新整个 scene。
                if not session.clients:
                    return False
                for client in session.clients.values():
                    client.last_heartbeat_at = now
                session.last_heartbeat_at = now
                return True
            client = session.clients.get(client_session_id)
            if client is None:
                return False
            client.last_heartbeat_at = now
            session.last_heartbeat_at = now
            return True

    def close_session(self, scene_id: str, client_session_id: str | None = None) -> SceneSession | None:
        """关闭前端 client 对 scene 的连接。

        传入 client_session_id 时只释放该前端连接；未传时兼容旧调用方，清空整个 scene
        presence。最后一个前端离开后只记录 vacant_since_at，由后台 sweep 在 can_expire
        策略允许后再触发 on_expired，避免关闭前端直接停止后台 Agent 工作。
        """
        now = time.time()
        with self._lock:
            session = self._sessions.get(scene_id)
            if session is None:
                return None
            if client_session_id is None:
                session.clients.clear()
            else:
                session.clients.pop(client_session_id, None)
            if session.clients:
                session.last_heartbeat_at = max(
                    (client.last_heartbeat_at for client in session.clients.values()),
                    default=session.last_heartbeat_at,
                )
                session.vacant_since_at = None
                return None
            session.last_heartbeat_at = now
            session.vacant_since_at = session.vacant_since_at or now
            return None

    def is_open(self, scene_id: str) -> bool:
        """判断 scene 是否有任一前端 client 处于打开状态。"""
        with self._lock:
            session = self._sessions.get(scene_id)
            return bool(session and session.clients)

    def list_sessions(self) -> list[SceneSession]:
        """列出所有活跃 scene session（快照）。"""
        with self._lock:
            return list(self._sessions.values())

    def active_client_count(self, scene_id: str) -> int:
        with self._lock:
            session = self._sessions.get(scene_id)
            return session.active_client_count if session is not None else 0

    def get_session_diagnostics(self, scene_id: str) -> dict[str, object]:
        now = time.time()
        with self._lock:
            session = self._sessions.get(scene_id)
            if session is None:
                return {
                    "scene_id": scene_id,
                    "is_open": False,
                    "active_client_count": 0,
                    "stale_timeout_seconds": self._stale_timeout,
                    "last_heartbeat_age_seconds": None,
                    "vacant_since_at": None,
                    "vacant_since_at_iso": None,
                    "vacant_age_seconds": None,
                    "is_stale": None,
                    "clients": [],
                }
            last_heartbeat_age = now - session.last_heartbeat_at
            vacant_age = now - session.vacant_since_at if session.vacant_since_at is not None else None
            return {
                "scene_id": scene_id,
                "workspace_path": session.workspace_path,
                "is_open": bool(session.clients),
                "active_client_count": session.active_client_count,
                "opened_at": session.opened_at,
                "last_heartbeat_at": session.last_heartbeat_at,
                "vacant_since_at": session.vacant_since_at,
                "opened_at_iso": _iso_from_timestamp(session.opened_at),
                "last_heartbeat_at_iso": _iso_from_timestamp(session.last_heartbeat_at),
                "vacant_since_at_iso": _iso_from_timestamp(session.vacant_since_at) if session.vacant_since_at is not None else None,
                "stale_timeout_seconds": self._stale_timeout,
                "last_heartbeat_age_seconds": last_heartbeat_age,
                "vacant_age_seconds": vacant_age,
                "is_stale": last_heartbeat_age > self._stale_timeout,
                "clients": [
                    {
                        "client_session_id": client.client_session_id,
                        "device_id": client.device_id,
                        "client_kind": client.client_kind,
                        "user_id": client.user_id,
                        "opened_at": client.opened_at,
                        "last_heartbeat_at": client.last_heartbeat_at,
                        "opened_at_iso": _iso_from_timestamp(client.opened_at),
                        "last_heartbeat_at_iso": _iso_from_timestamp(client.last_heartbeat_at),
                        "last_heartbeat_age_seconds": now - client.last_heartbeat_at,
                        "is_stale": now - client.last_heartbeat_at > self._stale_timeout,
                    }
                    for client in session.clients.values()
                ],
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
        """守护线程主循环：周期性检查过期 client 并在 scene 空置后触发回调。"""
        while not self._stopped:
            try:
                self._sweep_once()
            except Exception:
                logger.exception("SceneSessionRegistry sweep 异常")
            time.sleep(self._sweep_interval)

    def _sweep_once(self) -> None:
        """执行一轮过期扫描。"""
        now = time.time()
        expired_scenes: list[SceneSession] = []
        with self._lock:
            for scene_id, session in list(self._sessions.items()):
                for client_id, client in list(session.clients.items()):
                    if now - client.last_heartbeat_at > self._stale_timeout:
                        logger.info(
                            "Scene client session 过期: scene_id=%s client_session_id=%s client_kind=%s",
                            scene_id,
                            client_id,
                            client.client_kind,
                        )
                        del session.clients[client_id]
                if session.clients:
                    session.last_heartbeat_at = max(
                        client.last_heartbeat_at for client in session.clients.values()
                    )
                    session.vacant_since_at = None
                    continue
                if session.vacant_since_at is None:
                    session.vacant_since_at = now
                    session.last_heartbeat_at = now
                if self._can_expire is not None:
                    try:
                        if not self._can_expire(session):
                            logger.debug(
                                "Scene 已无前端连接但不满足自动释放条件，"
                                "继续保留空置状态: scene_id=%s vacant_age=%.1fs",
                                scene_id,
                                now - session.vacant_since_at,
                            )
                            continue
                    except Exception:
                        logger.exception(
                            "Scene session can_expire 判断异常，按可过期处理: scene_id=%s",
                            scene_id,
                        )
                expired_scenes.append(session)
                del self._sessions[scene_id]
        for session in expired_scenes:
            logger.info(
                "Scene 已无活跃客户端，触发空置收尾: scene_id=%s, workspace=%s",
                session.scene_id,
                session.workspace_path,
            )
            try:
                self._on_expired(session)
            except Exception:
                logger.exception(
                    "Scene session 过期回调失败: scene_id=%s", session.scene_id
                )


def _normalize_client_session_id(value: str | None) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return f"client-{uuid4()}"


def _iso_from_timestamp(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))
