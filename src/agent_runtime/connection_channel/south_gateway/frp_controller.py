from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agent_runtime.connection_channel.settings_repository import ConnectionChannelSettings

TunnelState = Literal[
    "not_configured",
    "disabled",
    "starting",
    "enabled",
    "start_failed",
    "stopping",
    "stop_failed",
    "unsupported",
    "unmanaged",
    "restart_required",
]

ControllerKind = Literal["none", "app_managed_frp", "external_unmanaged", "unsupported"]


@dataclass(frozen=True, slots=True)
class FrpControllerStatus:
    controller: ControllerKind
    tunnel_state: TunnelState
    tunnel_state_message: str
    last_started_at: str | None = None
    last_stopped_at: str | None = None
    last_error_message: str | None = None


class UnsupportedFrpController:
    """Safe placeholder FRP controller that never touches OS processes or external FRP tasks."""

    controller: ControllerKind = "unsupported"

    def status(self, settings: ConnectionChannelSettings) -> FrpControllerStatus:
        if not settings.configured:
            return FrpControllerStatus(
                controller=self.controller,
                tunnel_state="not_configured",
                tunnel_state_message="请先保存有效的连接通道云服务 Host 和端口。",
                last_started_at=settings.last_started_at,
                last_stopped_at=settings.last_stopped_at,
                last_error_message=settings.last_error_message,
            )
        if not settings.remote_access_enabled:
            return FrpControllerStatus(
                controller=self.controller,
                tunnel_state="disabled",
                tunnel_state_message="远程访问模式已关闭；本应用不会启动 FRP 通道。",
                last_started_at=settings.last_started_at,
                last_stopped_at=settings.last_stopped_at,
                last_error_message=settings.last_error_message,
            )
        return FrpControllerStatus(
            controller=self.controller,
            tunnel_state="unsupported",
            tunnel_state_message="当前 Runtime 尚未配置安全的应用托管 FRP controller；已记录开启意图，但不会启动或接管任何现有 frpc 进程/计划任务。",
            last_started_at=settings.last_started_at,
            last_stopped_at=settings.last_stopped_at,
            last_error_message=settings.last_error_message,
        )

    def start(self, settings: ConnectionChannelSettings) -> FrpControllerStatus:
        return FrpControllerStatus(
            controller=self.controller,
            tunnel_state="unsupported",
            tunnel_state_message="当前 Runtime 尚未配置安全的应用托管 FRP controller；不会启动或接管任何现有 frpc 进程/计划任务。",
            last_started_at=settings.last_started_at,
            last_stopped_at=settings.last_stopped_at,
            last_error_message=settings.last_error_message,
        )

    def stop(self, settings: ConnectionChannelSettings) -> FrpControllerStatus:
        return FrpControllerStatus(
            controller=self.controller,
            tunnel_state="disabled" if settings.configured else "not_configured",
            tunnel_state_message="远程访问模式已关闭；unsupported controller 未执行任何 OS/FRP 操作。",
            last_started_at=settings.last_started_at,
            last_stopped_at=settings.last_stopped_at,
            last_error_message=settings.last_error_message,
        )
