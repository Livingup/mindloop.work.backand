from __future__ import annotations

import asyncio
import inspect
import json
import logging
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from uuid import uuid4

from agent_runtime.lacp.config import LacpConfig
from agent_runtime.lacp.models import (
    LacpClientStatus,
    LacpExecutionObservation,
    LacpInboundMessage,
    LacpInstanceRegistration,
    utc_now_iso,
)
from agent_runtime.lacp.registration_registry import LacpRegistrationRegistry

logger = logging.getLogger(__name__)

InboundMessageHandler = Callable[[LacpInboundMessage], Awaitable[bool]]


class LacpClient:
    def __init__(
        self,
        *,
        config_accessor: Callable[[], LacpConfig],
        registration_registry: LacpRegistrationRegistry,
    ) -> None:
        self._config_accessor = config_accessor
        self._registration_registry = registration_registry
        self._message_handler: InboundMessageHandler | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._pending_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._last_ws_endpoint: str | None = None
        self._last_error: str | None = None
        self._last_connected_at: str | None = None
        self._last_disconnected_at: str | None = None
        self._reconnect_attempt = 0

    def set_message_handler(self, handler: InboundMessageHandler) -> None:
        self._message_handler = handler

    @property
    def enabled(self) -> bool:
        return self._config_accessor().enabled

    def status(self) -> LacpClientStatus:
        config = self._config_accessor()
        return LacpClientStatus(
            enabled=config.enabled,
            running=self._running,
            connected=self._ws is not None,
            registry_url=config.registry_url,
            ws_endpoint=self._last_ws_endpoint,
            last_error=self._last_error,
            last_connected_at=self._last_connected_at,
            last_disconnected_at=self._last_disconnected_at,
            reconnect_attempt=self._reconnect_attempt,
        )

    async def start(self) -> None:
        if not self.enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def shutdown(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib_suppress(Exception):
                await ws.close()
        if task is not None and not task.done():
            task.cancel()
            with contextlib_suppress(Exception, asyncio.CancelledError):
                await task
        self._fail_pending(RuntimeError("LACP client stopped."))

    async def register_instance(self, registration: LacpInstanceRegistration) -> LacpInstanceRegistration:
        updated = self._registration_registry.upsert_enabled(registration)
        await self.start()
        if self._ws is not None:
            await self._register_one(updated)
            current = self._registration_registry.get_registration(updated.instance_id)
            return current or updated
        return updated

    async def unregister_instance(self, instance_id: str) -> None:
        current = self._registration_registry.get_registration(instance_id)
        if current is None:
            return
        self._registration_registry.mark_registering(instance_id)
        try:
            if self._ws is not None:
                await self._send_request({"type": "unregister", "instance_id": instance_id}, expected_type="unregister_ack")
        finally:
            self._registration_registry.mark_unregistered(instance_id)

    async def report_execution_observation(self, observation: LacpExecutionObservation) -> bool:
        if not self._registration_registry.is_registered(observation.instance_id):
            return False
        ws = self._ws
        if ws is None:
            return False
        request_id = self._new_request_id("obs")
        await self._send_json(observation.to_frame(request_id))
        return True

    async def _run_loop(self) -> None:
        retry_count = 0
        while self._running:
            config = self._config_accessor()
            if not config.enabled:
                await asyncio.sleep(1)
                continue
            try:
                ws_endpoint = await self._resolve_ws_endpoint(config)
                self._last_ws_endpoint = ws_endpoint
                headers = self._auth_headers(config)
                import websockets

                connect_kwargs: dict[str, Any] = {
                    "open_timeout": config.connect_timeout_seconds,
                }
                if headers:
                    header_param = "additional_headers" if "additional_headers" in inspect.signature(websockets.connect).parameters else "extra_headers"
                    connect_kwargs[header_param] = headers
                async with websockets.connect(
                    ws_endpoint,
                    **connect_kwargs,
                ) as ws:
                    self._ws = ws
                    self._last_error = None
                    self._last_connected_at = utc_now_iso()
                    retry_count = 0
                    self._reconnect_attempt = 0
                    reader_task = asyncio.create_task(self._reader_loop(ws))
                    await self._register_desired_instances()
                    await reader_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                logger.exception("LACP WebSocket connection failed.")
            finally:
                self._ws = None
                self._last_disconnected_at = utc_now_iso()
                self._registration_registry.mark_disconnected()
                self._fail_pending(RuntimeError("LACP WebSocket disconnected."))
            delay = min(
                config.reconnect_initial_delay_seconds * (2 ** retry_count),
                config.reconnect_max_delay_seconds,
            )
            retry_count += 1
            self._reconnect_attempt = retry_count
            await asyncio.sleep(delay)

    async def _reader_loop(self, ws: Any) -> None:
        async for raw_message in ws:
            try:
                frame = json.loads(raw_message)
            except json.JSONDecodeError:
                continue
            if not isinstance(frame, dict):
                continue
            request_id = frame.get("request_id")
            if isinstance(request_id, str) and request_id in self._pending_requests:
                future = self._pending_requests.pop(request_id)
                if not future.done():
                    future.set_result(frame)
                continue
            if frame.get("type") == "message":
                await self._handle_message_frame(frame)

    async def _handle_message_frame(self, frame: dict[str, Any]) -> None:
        message = LacpInboundMessage.from_frame(frame)
        received = False
        if self._message_handler is not None:
            try:
                received = await self._message_handler(message)
            except Exception:
                logger.exception("LACP inbound message handling failed: message_id=%s", message.message_id)
        if message.message_id:
            await self._send_json({"type": "ack", "message_id": message.message_id, "received": received})

    async def _register_desired_instances(self) -> None:
        for registration in self._registration_registry.desired_enabled():
            try:
                await self._register_one(registration)
            except Exception:
                logger.exception("LACP instance registration failed: instance_id=%s", registration.instance_id)

    async def _register_one(self, registration: LacpInstanceRegistration) -> None:
        self._registration_registry.mark_registering(registration.instance_id)
        frame = {
            "type": "register",
            "instance_id": registration.instance_id,
            "role_name": registration.role_name,
            "description": registration.description,
        }
        if registration.workspace:
            frame["workspace"] = registration.workspace
        try:
            ack = await self._send_request(frame, expected_type="register_ack")
        except Exception as exc:
            self._registration_registry.mark_failed(registration.instance_id, error_message=str(exc))
            raise
        if bool(ack.get("success")):
            self._registration_registry.mark_registered(registration.instance_id)
            return
        error_code = str(ack.get("error_code") or "REGISTER.FAILED")
        error_message = str(ack.get("error_message") or "LACP register failed.")
        self._registration_registry.mark_failed(
            registration.instance_id,
            error_code=error_code,
            error_message=error_message,
        )
        raise RuntimeError(error_message)

    async def _send_request(self, frame: dict[str, Any], *, expected_type: str) -> dict[str, Any]:
        ws = self._ws
        if ws is None:
            raise RuntimeError("LACP WebSocket is not connected.")
        request_id = self._new_request_id("req")
        frame = {"request_id": request_id, **frame}
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future
        await self._send_json(frame)
        timeout = self._config_accessor().request_timeout_seconds
        response = await asyncio.wait_for(future, timeout=timeout)
        if response.get("type") != expected_type:
            raise RuntimeError(f"Unexpected LACP response type: {response.get('type')}")
        return response

    async def _send_json(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("LACP WebSocket is not connected.")
        async with self._send_lock:
            await ws.send(json.dumps(frame, ensure_ascii=False))

    async def _resolve_ws_endpoint(self, config: LacpConfig) -> str:
        configured = config.registry_url.rstrip("/")
        parsed = urlparse(configured)
        if parsed.scheme in {"ws", "wss"}:
            return configured if parsed.path else f"{configured}/ws"
        metadata = await asyncio.to_thread(self._fetch_metadata, configured, config.connect_timeout_seconds)
        ws_path = "/ws"
        if isinstance(metadata, dict):
            endpoints = metadata.get("endpoints")
            if isinstance(endpoints, dict) and endpoints.get("ws"):
                ws_path = str(endpoints["ws"])
        return self._to_ws_url(urljoin(f"{configured}/", ws_path.lstrip("/")))

    @staticmethod
    def _fetch_metadata(base_url: str, timeout: float) -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(f"{base_url}/.well-known/lacp", timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
                return data if isinstance(data, dict) else None
        except Exception:
            return None

    @staticmethod
    def _to_ws_url(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme == "https":
            return urlunparse(("wss", *parsed[1:]))
        if parsed.scheme == "http":
            return urlunparse(("ws", *parsed[1:]))
        return url

    @staticmethod
    def _auth_headers(config: LacpConfig) -> dict[str, str]:
        if not config.api_key:
            return {}
        return {"Authorization": f"Bearer {config.api_key}"}

    @staticmethod
    def _new_request_id(prefix: str) -> str:
        return f"lacp-{prefix}-{uuid4().hex}"

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(exc)
        self._pending_requests.clear()


class contextlib_suppress:
    def __init__(self, *exceptions: type[BaseException]) -> None:
        self._exceptions = exceptions

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return exc_type is not None and issubclass(exc_type, self._exceptions)
