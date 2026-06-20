from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import ip_address

from agent_runtime.connection_channel.settings_repository import (
    ConnectionChannelSettings,
    ConnectionChannelSettingsRepository,
)
from agent_runtime.connection_channel.south_gateway.frp_controller import (
    FrpControllerStatus,
    UnsupportedFrpController,
)


_DOMAIN_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_CONTROL_OR_WHITESPACE_PATTERN = re.compile(r"[\s\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class ConnectionChannelSettingsView:
    cloud_service_host: str | None
    cloud_service_port: int | None
    configured: bool
    remote_access_enabled: bool
    updated_at: str | None
    db_path: str
    warning_message: str | None = None

    @property
    def authority(self) -> str | None:
        if not self.configured or self.cloud_service_host is None or self.cloud_service_port is None:
            return None
        return f"{self.cloud_service_host}:{self.cloud_service_port}"


@dataclass(frozen=True, slots=True)
class ConnectionChannelStatusView:
    cloud_service_host: str | None
    cloud_service_port: int | None
    configured: bool
    remote_access_enabled: bool
    authority: str | None
    tunnel_state: str
    tunnel_state_message: str
    controller: str
    owner_id: str | None
    managed_tunnel_id: str | None
    managed_config_path: str | None
    managed_task_name: str | None
    managed_pid: int | None
    controller_owner_marker: str | None
    applied_config_version: str | None
    current_config_version: str | None
    last_started_at: str | None
    last_stopped_at: str | None
    last_error_message: str | None
    updated_at: str | None
    db_path: str
    warning_message: str | None = None


class ConnectionChannelSettingsService:
    """Domain service for current-machine remote browser access settings."""

    def __init__(
        self,
        repository: ConnectionChannelSettingsRepository,
        frp_controller: UnsupportedFrpController | None = None,
    ) -> None:
        self._repository = repository
        self._frp_controller = frp_controller or UnsupportedFrpController()

    def get_settings(self) -> ConnectionChannelSettingsView:
        return self._settings_view(self._repository.get_settings())

    def get_status(self) -> ConnectionChannelStatusView:
        settings = self._repository.get_settings()
        return self._status_view(settings, self._controller_status(settings))

    def save_settings(
        self,
        *,
        cloud_service_host: str | None,
        cloud_service_port: int | str | None,
    ) -> ConnectionChannelSettingsView:
        normalized_host = normalize_connection_channel_host(cloud_service_host)
        normalized_port = normalize_connection_channel_port(cloud_service_port)
        if normalized_host is None and normalized_port is not None:
            raise ValueError("当前电脑远程访问 Host 未配置时，端口也必须留空。")
        if normalized_host is not None and normalized_port is None:
            raise ValueError("当前电脑远程访问 Host 已配置时，必须填写端口。")
        current = self._repository.get_settings()
        if current.remote_access_enabled and (normalized_host is None or normalized_port is None):
            raise ValueError("远程访问模式已开启时不能清空 Host 或端口；请先关闭远程访问模式。")
        settings = self._repository.save_endpoint(
            cloud_service_host=normalized_host,
            cloud_service_port=normalized_port,
        )
        return self._settings_view(settings)

    def enable_remote_access(self) -> ConnectionChannelStatusView:
        current = self._repository.get_settings()
        if not current.configured:
            raise ValueError("请先保存当前电脑远程访问 Host 和端口，再开启远程访问模式。")
        settings = self._repository.set_remote_access_enabled(True)
        controller_status = self._frp_controller.start(settings)
        applied_config_version = (
            settings.effective_config_version
            if controller_status.tunnel_state == "enabled"
            else settings.applied_config_version
        )
        self._repository.record_controller_state(
            tunnel_controller=controller_status.controller,
            owner_id=settings.owner_id,
            managed_tunnel_id=settings.managed_tunnel_id,
            managed_config_path=settings.managed_config_path,
            managed_task_name=settings.managed_task_name,
            managed_pid=settings.managed_pid,
            controller_owner_marker=settings.controller_owner_marker,
            applied_config_version=applied_config_version,
            last_error_message=controller_status.last_error_message,
        )
        refreshed = self._repository.get_settings()
        return self._status_view(refreshed, controller_status)

    def disable_remote_access(self) -> ConnectionChannelStatusView:
        settings = self._repository.set_remote_access_enabled(False)
        controller_status = self._frp_controller.stop(settings)
        self._repository.record_controller_state(
            tunnel_controller=controller_status.controller,
            owner_id=settings.owner_id,
            managed_tunnel_id=settings.managed_tunnel_id,
            managed_config_path=settings.managed_config_path,
            managed_task_name=settings.managed_task_name,
            managed_pid=settings.managed_pid,
            controller_owner_marker=settings.controller_owner_marker,
            applied_config_version=settings.applied_config_version,
            last_error_message=controller_status.last_error_message,
        )
        refreshed = self._repository.get_settings()
        return self._status_view(refreshed, controller_status)

    def _settings_view(self, settings: ConnectionChannelSettings) -> ConnectionChannelSettingsView:
        warning = _host_warning(settings.cloud_service_host)
        return ConnectionChannelSettingsView(
            cloud_service_host=settings.cloud_service_host,
            cloud_service_port=settings.cloud_service_port,
            configured=settings.configured,
            remote_access_enabled=settings.remote_access_enabled,
            updated_at=settings.updated_at,
            db_path=str(self._repository.db_path()),
            warning_message=warning,
        )

    def _status_view(
        self,
        settings: ConnectionChannelSettings,
        controller_status: FrpControllerStatus,
    ) -> ConnectionChannelStatusView:
        authority = None if not settings.configured else f"{settings.cloud_service_host}:{settings.cloud_service_port}"
        tunnel_state = controller_status.tunnel_state
        tunnel_state_message = controller_status.tunnel_state_message
        if settings.remote_access_enabled and settings.configured and settings.tunnel_restart_required:
            tunnel_state = "restart_required"
            tunnel_state_message = "当前电脑远程访问 Host 或端口已变更；请关闭后重新开启远程访问，使 FRP 通道使用新配置。"
        return ConnectionChannelStatusView(
            cloud_service_host=settings.cloud_service_host,
            cloud_service_port=settings.cloud_service_port,
            configured=settings.configured,
            remote_access_enabled=settings.remote_access_enabled,
            authority=authority,
            tunnel_state=tunnel_state,
            tunnel_state_message=tunnel_state_message,
            controller=controller_status.controller,
            owner_id=settings.owner_id,
            managed_tunnel_id=settings.managed_tunnel_id,
            managed_config_path=settings.managed_config_path,
            managed_task_name=settings.managed_task_name,
            managed_pid=settings.managed_pid,
            controller_owner_marker=settings.controller_owner_marker,
            applied_config_version=settings.applied_config_version,
            current_config_version=settings.effective_config_version,
            last_started_at=controller_status.last_started_at,
            last_stopped_at=controller_status.last_stopped_at,
            last_error_message=controller_status.last_error_message,
            updated_at=settings.updated_at,
            db_path=str(self._repository.db_path()),
            warning_message=_host_warning(settings.cloud_service_host),
        )

    def _controller_status(self, settings: ConnectionChannelSettings) -> FrpControllerStatus:
        return self._frp_controller.status(settings)


def normalize_connection_channel_host(value: str | None) -> str | None:
    host = (value or "").strip()
    if not host:
        return None
    if "://" in host:
        raise ValueError("当前电脑远程访问 Host 只填写 IP 或域名，不要包含 http/https/ws/wss 协议。")
    if any(token in host for token in ("/", "?", "#")):
        raise ValueError("当前电脑远程访问 Host 不能包含路径、查询参数或片段。")
    if _CONTROL_OR_WHITESPACE_PATTERN.search(host):
        raise ValueError("当前电脑远程访问 Host 不能包含空白字符或控制字符。")
    if host.startswith("[") or host.endswith("]"):
        raise ValueError("当前电脑远程访问 Host 暂不支持 IPv6 地址，请填写 IPv4 或域名。")
    if ":" in host:
        raise ValueError("当前电脑远程访问端口请填写在独立端口字段，不要混写到 Host 中。")
    if len(host) > 253:
        raise ValueError("当前电脑远程访问 Host 长度不能超过 253 个字符。")

    try:
        parsed_ip = ip_address(host)
    except ValueError:
        _validate_domain_name(host)
    else:
        if parsed_ip.version != 4:
            raise ValueError("当前电脑远程访问 Host 暂不支持 IPv6 地址，请填写 IPv4 或域名。")
    return host.lower() if _looks_like_domain(host) else host


def normalize_connection_channel_port(value: int | str | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if not re.fullmatch(r"\d+", text):
            raise ValueError("当前电脑远程访问端口必须是 1 到 65535 之间的整数。")
        port = int(text, 10)
    elif isinstance(value, int) and not isinstance(value, bool):
        port = value
    else:
        raise ValueError("当前电脑远程访问端口必须是 1 到 65535 之间的整数。")
    if port < 1 or port > 65535:
        raise ValueError("当前电脑远程访问端口必须是 1 到 65535 之间的整数。")
    return port


def _validate_domain_name(host: str) -> None:
    if host == "localhost":
        return
    if host.endswith("."):
        raise ValueError("当前电脑远程访问域名不要以点号结尾。")
    labels = host.split(".")
    if len(labels) < 2:
        raise ValueError("当前电脑远程访问 Host 必须是合法 IPv4、域名或 localhost。")
    if not all(_DOMAIN_LABEL_PATTERN.fullmatch(label) for label in labels):
        raise ValueError("当前电脑远程访问 Host 必须是合法 IPv4 或域名。")


def _looks_like_domain(host: str) -> bool:
    try:
        ip_address(host)
        return False
    except ValueError:
        return True


def _host_warning(host: str | None) -> str | None:
    if not host:
        return None
    normalized = host.strip().lower()
    if normalized == "localhost":
        return "localhost 只适合本机开发，远程浏览器通常无法访问。"
    try:
        parsed = ip_address(normalized)
    except ValueError:
        return None
    if parsed.is_loopback:
        return "Loopback 地址只适合本机开发，远程浏览器通常无法访问。"
    if parsed.is_private:
        return "私有网段地址通常只能在局域网或隧道内访问，请确认远程浏览器可达。"
    return None
