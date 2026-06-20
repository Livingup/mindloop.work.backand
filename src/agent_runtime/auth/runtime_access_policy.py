# -*- coding: utf-8 -*-
"""Runtime LAN 暴露时的首次初始化访问策略。"""
from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address

LOCAL_BOOTSTRAP_REQUIRED_DETAIL = (
    "Runtime 当前允许局域网访问时，首次账号创建和初始化配置只能在运行 Runtime 的本机完成。"
    "请先回到这台电脑上的 Electron 窗口或本机浏览器完成初始化，再从手机或其他局域网设备登录。"
)
NO_ACCOUNT_API_RESTRICTED_DETAIL = (
    "Runtime 本地账号尚未初始化。出于安全原因，首次账号创建前只开放健康检查和账号初始化入口。"
)


@dataclass(frozen=True, slots=True)
class RuntimeBootstrapAccessPolicy:
    """判断首次 Runtime takeover 是否必须限制为本机来源。"""

    bind_host: str

    @property
    def requires_local_client_for_first_run(self) -> bool:
        """当前监听地址是否可能被局域网客户端访问。"""
        return host_exposes_remote_clients(self.bind_host)

    def is_local_request(self, request: object) -> bool:
        """请求来源是否来自 loopback 客户端。"""
        return is_loopback_client_host(request_client_host(request))

    def allows_first_run_request(self, request: object) -> bool:
        """首次初始化敏感请求是否允许继续。"""
        return (
            not self.requires_local_client_for_first_run
            or self.is_local_request(request)
        )


def host_exposes_remote_clients(bind_host: str | None) -> bool:
    """判断监听 host 是否不局限于本机 loopback。"""
    host = _normalize_host(bind_host)
    if not host:
        return True
    if host == "localhost":
        return False
    try:
        parsed = ip_address(host)
    except ValueError:
        return True
    return not parsed.is_loopback


def is_loopback_client_host(client_host: str | None) -> bool:
    """判断 HTTP 客户端地址是否为本机 loopback。"""
    host = _normalize_host(client_host)
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def request_client_host(request: object) -> str | None:
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return host if isinstance(host, str) else None


def is_no_account_api_allowed_path(path: str, method: str) -> bool:
    """本地账号不存在时仍允许访问的最小 API 表面。"""
    normalized_method = method.upper()
    if path == "/api/health":
        return True
    if path == "/api/auth/status":
        return True
    if path == "/api/auth/bootstrap" and normalized_method == "POST":
        return True
    return False


def is_first_run_local_only_path(path: str, method: str) -> bool:
    """首次初始化阶段必须限制为本机来源的敏感入口。"""
    normalized_method = method.upper()
    if path == "/api/auth/bootstrap" and normalized_method == "POST":
        return True
    if path == "/api/runtime-bootstrap/configure" and normalized_method in {"POST", "PUT", "PATCH"}:
        return True
    if path.startswith("/api/connection-channel/") and normalized_method in {"PUT", "PATCH", "POST"}:
        return True
    if path.startswith("/api/local-remote-access/") and normalized_method in {"PUT", "PATCH", "POST"}:
        return True
    if path.startswith("/api/system-settings"):
        return True
    return False


def _normalize_host(host: str | None) -> str:
    value = (host or "").strip().lower()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value
