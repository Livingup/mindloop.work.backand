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

from agent_runtime.infrastructure.asyncio_diagnostics import BlockingPool, monitored_to_thread
from agent_runtime.infrastructure.runtime_task_registry import get_runtime_task_registry
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


def _safe_ws_owner_id(ws_endpoint: str | None) -> str:
    if not ws_endpoint:
        return "unknown"
    parsed = urlparse(ws_endpoint)
    host = parsed.hostname or "unknown"
    if parsed.port is not None:
        host = f"{host}-{parsed.port}"
    path = parsed.path.strip("/").replace("/", "-")
    raw = f"{host}-{path}" if path else host
    safe = "".join(char if char.isalnum() or char in {".", "_", "-"} else "-" for char in raw)
    return (safe.strip("-._") or "unknown")[:80]


def _runtime_task_id_for(task: asyncio.Task[Any] | None) -> str | None:
    if task is None:
        return None
    return get_runtime_task_registry().task_id_for(task)


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
        self._task = get_runtime_task_registry().create_task(
            self._run_loop(),
            task_name="lacp-client-run-loop",
            owner_type="lacp_client",
            owner_id="global",
            task_class="client_run_loop",
            diagnostic_context={"enabled": True, "phase": "start"},
        )

    async def reload_from_config(self) -> None:
        await self.shutdown()
        await self.start()

    async def shutdown(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        ws = self._ws
        self._ws = None
        if ws is not None:
            with contextlib_suppress(Exception):
                await ws.close()
        self._fail_pending(RuntimeError("LACP client stopped."), reason="client_stopped", task=task)
        if task is not None and not task.done():
            get_runtime_task_registry().cancel(
                task,
                message="lacp client stopped",
                diagnostic_context={"phase": "shutdown"},
            )
            with contextlib_suppress(Exception, asyncio.CancelledError):
                await task

    async def register_instance(self, registration: LacpInstanceRegistration) -> LacpInstanceRegistration:
        self._validate_registration_config()
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
        registry = get_runtime_task_registry()
        run_task = asyncio.current_task()
        run_task_or_id = run_task or "lacp-client-run-loop"
        run_task_id = _runtime_task_id_for(run_task)
        registry.mark_progress(
            run_task_or_id,
            diagnostic_context={"phase": "client_run_loop.started", "task_id": run_task_id},
        )
        while self._running:
            config = self._config_accessor()
            if not config.enabled:
                registry.mark_progress(
                    run_task_or_id,
                    diagnostic_context={
                        "phase": "config.disabled",
                        "enabled": False,
                        "task_id": run_task_id,
                        "reason": "config_disabled",
                    },
                )
                await asyncio.sleep(1)
                continue
            config_error = self._connection_config_error(config)
            if config_error is not None:
                self._last_error = config_error
                registry.mark_progress(
                    run_task_or_id,
                    diagnostic_context={
                        "phase": "config.error",
                        "enabled": True,
                        "task_id": run_task_id,
                        "reason": "config_error",
                        "error_type": "ConfigError",
                    },
                )
                await asyncio.sleep(1)
                continue
            endpoint_owner_id = "unknown"
            connection_exit_reason = "websocket_disconnected"
            try:
                registry.mark_progress(
                    run_task_or_id,
                    diagnostic_context={
                        "phase": "resolve_ws_endpoint",
                        "task_id": run_task_id,
                        "reconnect_attempt": retry_count,
                    },
                )
                ws_endpoint = await self._resolve_ws_endpoint(config)
                self._last_ws_endpoint = ws_endpoint
                endpoint_owner_id = _safe_ws_owner_id(ws_endpoint)
                headers = self._auth_headers(config)
                registry.mark_progress(
                    run_task_or_id,
                    diagnostic_context={
                        "phase": "websocket.connecting",
                        "task_id": run_task_id,
                        "endpoint_owner_id": endpoint_owner_id,
                        "reconnect_attempt": retry_count,
                        "timeout_seconds": config.connect_timeout_seconds,
                    },
                )
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
                    registry.mark_progress(
                        run_task_or_id,
                        diagnostic_context={
                            "phase": "websocket.connected",
                            "task_id": run_task_id,
                            "endpoint_owner_id": endpoint_owner_id,
                            "reconnect_attempt": retry_count,
                        },
                    )
                    reader_task = registry.create_task(
                        self._reader_loop(ws, endpoint_owner_id=endpoint_owner_id),
                        task_name=f"lacp-websocket-reader-{endpoint_owner_id}",
                        owner_type="lacp_client",
                        owner_id=endpoint_owner_id,
                        task_class="websocket_reader",
                        diagnostic_context={"phase": "reader.started", "endpoint_owner_id": endpoint_owner_id},
                    )
                    reader_task_id = _runtime_task_id_for(reader_task)
                    registry.mark_progress(
                        run_task_or_id,
                        diagnostic_context={
                            "phase": "reader_task.created",
                            "task_id": run_task_id,
                            "reader_task_id": reader_task_id,
                            "endpoint_owner_id": endpoint_owner_id,
                        },
                    )
                    registry.mark_progress(
                        reader_task,
                        diagnostic_context={
                            "phase": "reader.started",
                            "task_id": reader_task_id,
                            "owner_task_id": run_task_id,
                            "endpoint_owner_id": endpoint_owner_id,
                        },
                    )
                    try:
                        await self._register_desired_instances()
                        await reader_task
                        connection_exit_reason = "reader_completed"
                    finally:
                        if not reader_task.done():
                            registry.cancel(
                                reader_task,
                                message="lacp websocket connection closing",
                                diagnostic_context={
                                    "phase": "reader.connection_closing",
                                    "reason": "connection_closing",
                                    "task_id": reader_task_id,
                                    "owner_task_id": run_task_id,
                                    "endpoint_owner_id": endpoint_owner_id,
                                },
                            )
                            with contextlib_suppress(Exception, asyncio.CancelledError):
                                await reader_task
                            connection_exit_reason = "reader_cancelled"
            except asyncio.CancelledError:
                connection_exit_reason = "client_stopped"
                registry.mark_progress(
                    run_task_or_id,
                    diagnostic_context={
                        "phase": "client_run_loop.cancelled",
                        "reason": "client_stopped",
                        "task_id": run_task_id,
                        "endpoint_owner_id": endpoint_owner_id,
                        "error_type": "CancelledError",
                    },
                )
                raise
            except Exception as exc:
                connection_exit_reason = "websocket_error"
                self._last_error = str(exc)
                registry.mark_progress(
                    run_task_or_id,
                    diagnostic_context={
                        "phase": "websocket.exception",
                        "reason": "websocket_exception",
                        "task_id": run_task_id,
                        "endpoint_owner_id": endpoint_owner_id,
                        "error_type": exc.__class__.__name__,
                    },
                )
                logger.warning("LACP WebSocket connection failed: endpoint_owner_id=%s error_type=%s", endpoint_owner_id, exc.__class__.__name__)
            finally:
                self._ws = None
                self._last_disconnected_at = utc_now_iso()
                self._registration_registry.mark_disconnected()
                pending_reason = "client_stopped" if not self._running else "websocket_disconnected"
                self._fail_pending(RuntimeError("LACP client stopped." if pending_reason == "client_stopped" else "LACP WebSocket disconnected."), reason=pending_reason)
                registry.mark_progress(
                    run_task_or_id,
                    diagnostic_context={
                        "phase": "websocket.disconnected",
                        "reason": connection_exit_reason,
                        "task_id": run_task_id,
                        "endpoint_owner_id": endpoint_owner_id,
                        "pending_request_count": len(self._pending_requests),
                    },
                )
            delay = min(
                config.reconnect_initial_delay_seconds * (2 ** retry_count),
                config.reconnect_max_delay_seconds,
            )
            retry_count += 1
            self._reconnect_attempt = retry_count
            registry.mark_progress(
                run_task_or_id,
                diagnostic_context={
                    "phase": "reconnect.sleeping",
                    "task_id": run_task_id,
                    "reconnect_attempt": retry_count,
                    "delay_seconds": delay,
                },
            )
            await asyncio.sleep(delay)

    async def _reader_loop(self, ws: Any, *, endpoint_owner_id: str) -> None:
        registry = get_runtime_task_registry()
        reader_task = asyncio.current_task()
        reader_task_or_id = reader_task or f"lacp-websocket-reader-{endpoint_owner_id}"
        reader_task_id = _runtime_task_id_for(reader_task)
        pending_count = len(self._pending_requests)
        registry.mark_progress(
            reader_task_or_id,
            diagnostic_context={
                "phase": "reader.waiting",
                "task_id": reader_task_id,
                "endpoint_owner_id": endpoint_owner_id,
                "pending_request_count": pending_count,
            },
        )
        try:
            async for raw_message in ws:
                registry.mark_progress(
                    reader_task_or_id,
                    diagnostic_context={
                        "phase": "reader.frame_received",
                        "task_id": reader_task_id,
                        "endpoint_owner_id": endpoint_owner_id,
                        "pending_request_count": len(self._pending_requests),
                    },
                )
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
                    registry.mark_progress(
                        reader_task_or_id,
                        diagnostic_context={
                            "phase": "reader.pending_response",
                            "task_id": reader_task_id,
                            "endpoint_owner_id": endpoint_owner_id,
                            "pending_request_count": len(self._pending_requests),
                        },
                    )
                    continue
                if frame.get("type") == "message":
                    registry.mark_progress(
                        reader_task_or_id,
                        diagnostic_context={
                            "phase": "reader.message_frame",
                            "task_id": reader_task_id,
                            "endpoint_owner_id": endpoint_owner_id,
                            "pending_request_count": len(self._pending_requests),
                        },
                    )
                    await self._handle_message_frame(frame)
            registry.mark_progress(
                reader_task_or_id,
                diagnostic_context={
                    "phase": "reader.exit",
                    "reason": "reader_completed",
                    "task_id": reader_task_id,
                    "endpoint_owner_id": endpoint_owner_id,
                    "pending_request_count": len(self._pending_requests),
                },
            )
        except asyncio.CancelledError:
            registry.mark_progress(
                reader_task_or_id,
                diagnostic_context={
                    "phase": "reader.exit",
                    "reason": "reader_cancelled",
                    "task_id": reader_task_id,
                    "endpoint_owner_id": endpoint_owner_id,
                    "pending_request_count": len(self._pending_requests),
                    "error_type": "CancelledError",
                },
            )
            raise
        except Exception as exc:
            registry.mark_progress(
                reader_task_or_id,
                diagnostic_context={
                    "phase": "reader.exit",
                    "reason": "reader_error",
                    "task_id": reader_task_id,
                    "endpoint_owner_id": endpoint_owner_id,
                    "pending_request_count": len(self._pending_requests),
                    "error_type": exc.__class__.__name__,
                },
            )
            raise

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
            self._mark_client_run_loop_progress(
                {
                    "phase": "pending_request.failed",
                    "reason": "websocket_disconnected",
                    "expected_type": expected_type,
                    "pending_request_count": len(self._pending_requests),
                    "error_type": "RuntimeError",
                }
            )
            raise RuntimeError("LACP WebSocket is not connected.")
        request_id = self._new_request_id("req")
        frame = {"request_id": request_id, **frame}
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future
        try:
            await self._send_json(frame)
            timeout = self._config_accessor().request_timeout_seconds
            self._mark_client_run_loop_progress(
                {
                    "phase": "pending_request.waiting",
                    "expected_type": expected_type,
                    "timeout_seconds": timeout,
                    "pending_request_count": len(self._pending_requests),
                }
            )
            response = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            self._pending_requests.pop(request_id, None)
            self._mark_client_run_loop_progress(
                {
                    "phase": "pending_request.failed",
                    "reason": "request_timeout",
                    "expected_type": expected_type,
                    "timeout_seconds": timeout,
                    "pending_request_count": len(self._pending_requests),
                    "error_type": "TimeoutError",
                }
            )
            raise TimeoutError("LACP request timed out.") from exc
        except Exception:
            self._pending_requests.pop(request_id, None)
            raise
        response_type = response.get("type")
        if response_type != expected_type:
            response_type_label = response_type if isinstance(response_type, str) else type(response_type).__name__
            response_type_label = response_type_label[:80]
            self._mark_client_run_loop_progress(
                {
                    "phase": "pending_request.failed",
                    "reason": "unexpected_response_type",
                    "expected_type": expected_type,
                    "response_type": response_type_label,
                    "pending_request_count": len(self._pending_requests),
                    "error_type": "RuntimeError",
                }
            )
            raise RuntimeError(f"Unexpected LACP response type: {response_type_label}")
        self._mark_client_run_loop_progress(
            {
                "phase": "pending_request.completed",
                "expected_type": expected_type,
                "response_type": expected_type,
                "pending_request_count": len(self._pending_requests),
            }
        )
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
        metadata = await monitored_to_thread(
            self._fetch_metadata,
            configured,
            config.connect_timeout_seconds,
            operation="lacp.resolve_ws_endpoint.fetch_metadata",
            caller="LacpClient._resolve_ws_endpoint",
            pool=BlockingPool.NETWORK,
            log=logger,
            slow_execution_seconds=max(config.connect_timeout_seconds, 1.0),
            diagnostic_context={
                "registry_url": configured,
                "connect_timeout_seconds": config.connect_timeout_seconds,
            },
        )
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

    def _validate_registration_config(self) -> None:
        config = self._config_accessor()
        config_error = self._connection_config_error(config)
        if config_error is not None:
            raise ValueError(config_error)

    @staticmethod
    def _connection_config_error(config: LacpConfig) -> str | None:
        if not config.enabled:
            return "LACP service is disabled. Enable LACP before registering an agent instance."
        if not config.registry_url.strip():
            return "LACP registry URL is not configured."
        if not (config.api_key or "").strip():
            return "LACP API key is not configured."
        return None

    @staticmethod
    def _new_request_id(prefix: str) -> str:
        return f"lacp-{prefix}-{uuid4().hex}"

    def _mark_client_run_loop_progress(
        self,
        diagnostic_context: dict[str, Any],
        *,
        task: asyncio.Task[None] | None = None,
    ) -> None:
        target_task = task or self._task
        if target_task is None:
            return
        enriched_context = {"task_id": _runtime_task_id_for(target_task), **diagnostic_context}
        get_runtime_task_registry().mark_progress(target_task, diagnostic_context=enriched_context)

    def _fail_pending(
        self,
        exc: Exception,
        *,
        reason: str,
        task: asyncio.Task[None] | None = None,
    ) -> None:
        pending_count = len(self._pending_requests)
        self._mark_client_run_loop_progress(
            {
                "phase": "pending_requests.failed",
                "reason": reason,
                "pending_request_count": pending_count,
                "error_type": exc.__class__.__name__,
            },
            task=task,
        )
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
