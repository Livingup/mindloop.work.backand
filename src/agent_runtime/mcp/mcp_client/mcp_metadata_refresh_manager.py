# -*- coding: utf-8 -*-
"""MCP 元数据后台刷新管理器。

该管理器是 MCP 元数据通信模型中唯一负责实时网络刷新的一侧。Agent 执行、REST/CLI 元数据查询
和 MCP 元工具读取元数据时只访问内存缓存，不直接等待远端 MCP 服务。
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from agent_runtime.infrastructure.asyncio_diagnostics import monitored_to_thread
from agent_runtime.mcp.mcp_client.mcp_metadata_cache import (
    JsonMcpMetadataSnapshotRepository,
    McpCachedResource,
    McpCachedTool,
    McpMetadataRegistry,
    McpServiceMetadataSnapshot,
)

if TYPE_CHECKING:
    from agent_runtime.mcp.mcp_client.mcp_client_service import McpClientService

logger = logging.getLogger(__name__)

_DEFAULT_METADATA_POLL_INTERVAL_SECONDS = 30.0
_DEFAULT_METADATA_OPERATION_TIMEOUT_SECONDS = 10.0
_METADATA_DIAGNOSTIC_SLOW_PHASE_SECONDS = 1.0


def _duration_ms_since(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _current_task_name() -> str | None:
    task = asyncio.current_task()
    return task.get_name() if task is not None else None


def _slow_or_debug(duration_ms: int) -> int:
    return logging.WARNING if duration_ms >= int(_METADATA_DIAGNOSTIC_SLOW_PHASE_SECONDS * 1000) else logging.DEBUG


def _log_metadata_diagnostic(
    level: int,
    message: str,
    *,
    service_id: str,
    operation: str,
    phase: str,
    task_name: str | None,
    duration_ms: int | None = None,
    timeout_seconds: float | None = None,
    error_type: str | None = None,
    error: str | None = None,
) -> None:
    if not logger.isEnabledFor(level):
        return
    logger.log(
        level,
        "%s: trace_id=%s stream_id=%s event_type=%s service_id=%s operation=%s phase=%s task_name=%s duration_ms=%s timeout_seconds=%s error_type=%s error=%s",
        message,
        None,
        None,
        None,
        service_id,
        operation,
        phase,
        task_name,
        duration_ms,
        timeout_seconds,
        error_type,
        error,
    )


class McpMetadataRefreshManager:
    """后台刷新 MCP 服务能力、说明、工具和资源等元数据。

    刷新失败时保持旧内存快照，仅记录失败状态；刷新成功且快照变化时，先更新内存，
    再用后台写任务异步落盘。
    """

    def __init__(
        self,
        *,
        service: McpClientService,
        registry: McpMetadataRegistry,
        snapshot_repository: JsonMcpMetadataSnapshotRepository,
        poll_interval_seconds: float = _DEFAULT_METADATA_POLL_INTERVAL_SECONDS,
        operation_timeout_seconds: float = _DEFAULT_METADATA_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        self._service = service
        self._registry = registry
        self._snapshot_repository = snapshot_repository
        self._poll_interval_seconds = poll_interval_seconds
        self._operation_timeout_seconds = operation_timeout_seconds
        self._poll_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._persist_tasks: set[asyncio.Task[None]] = set()

    @property
    def running(self) -> bool:
        task = self._poll_task
        return task is not None and not task.done()

    async def load_persisted_metadata(self) -> None:
        """从本机持久化缓存加载元数据到进程内存。

        该方法必须在 start() 之前执行，保证后台刷新开始前所有消费者已有非网络基线。
        """
        snapshots = await monitored_to_thread(
            self._snapshot_repository.load_all,
            operation="mcp.metadata_cache.load_all",
            caller="McpMetadataRefreshManager.load_persisted_metadata",
            log=logger,
            diagnostic_context={"cache_operation": "load_all"},
        )
        self._registry.hydrate(snapshots)
        logger.info("已加载 MCP 元数据本地缓存：%d 个服务快照", len(snapshots))

    def start(self) -> None:
        """启动后台轮询刷新。重复调用是幂等的。"""
        if self.running:
            return
        self._stop_event = asyncio.Event()
        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name="mcp-metadata-refresh-poller",
        )

    async def stop(self) -> None:
        """停止后台轮询并等待已排队的持久化任务尽量收敛。"""
        stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        task = self._poll_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(Exception, asyncio.CancelledError):
                await task
        self._poll_task = None
        self._stop_event = None

        persist_tasks = tuple(self._persist_tasks)
        if persist_tasks:
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.wait(persist_tasks, timeout=5.0)
        self._persist_tasks.clear()

    async def refresh_once(self) -> None:
        """执行一轮完整元数据刷新。单个服务失败不影响其他服务。"""
        definitions = {definition.service_id: definition for definition in self._service.list_services()}
        current_service_ids = set(definitions)

        for service_id in self._registry.service_ids():
            definition = definitions.get(service_id)
            if service_id not in current_service_ids or definition is None or not definition.enabled:
                if self._registry.remove(service_id):
                    self._schedule_delete(service_id)

        for definition in definitions.values():
            if not definition.enabled:
                continue
            await self.refresh_service(definition.service_id)

    async def refresh_service(self, service_id: str) -> None:
        """刷新单个 MCP 服务的元数据。"""
        try:
            snapshot = await self._build_success_snapshot(service_id)
        except Exception as exc:  # noqa: BLE001 - 刷新异常只影响该服务本轮新鲜度
            logger.warning(
                "MCP 元数据刷新失败，保留旧缓存: service_id=%s, error_type=%s, error=%s",
                service_id,
                exc.__class__.__name__,
                exc,
            )
            previous = self._registry.get_snapshot(service_id)
            snapshot = McpServiceMetadataSnapshot.failed(
                service_id=service_id,
                error=str(exc),
                failed_at=time.time(),
                previous=previous,
            )

        if self._registry.upsert(snapshot):
            self._schedule_save(snapshot)

    async def _build_success_snapshot(self, service_id: str) -> McpServiceMetadataSnapshot:
        detail = await self._with_operation_timeout(
            self._service.get_service_detail(service_id),
            service_id=service_id,
            operation="get_service_detail",
        )

        tool_items: list[McpCachedTool] = []
        if bool(detail.get("supports_tools")):
            tool_summaries = await self._with_operation_timeout(
                self._service.list_tools(service_id),
                service_id=service_id,
                operation="list_tools",
            )
            tool_names = [item.name for item in tool_summaries]
            tool_details = []
            if tool_names:
                tool_details = await self._with_operation_timeout(
                    self._service.get_tool_detail(service_id, tool_names),
                    service_id=service_id,
                    operation="get_tool_detail",
                )
            detail_by_name = {item.name: item for item in tool_details}
            for summary in tool_summaries:
                tool_detail = detail_by_name.get(summary.name)
                if tool_detail is not None:
                    tool_items.append(McpCachedTool.from_tool_info(tool_detail))
                else:
                    tool_items.append(McpCachedTool.from_tool_summary(summary))

        resource_items: list[McpCachedResource] = []
        if bool(detail.get("supports_resources")):
            resources = await self._with_operation_timeout(
                self._service.list_resources(service_id),
                service_id=service_id,
                operation="list_resources",
            )
            resource_items = [McpCachedResource.from_resource_info(item) for item in resources]

        return McpServiceMetadataSnapshot.successful(
            service_id=service_id,
            supports_tools=bool(detail.get("supports_tools", False)),
            supports_resources=bool(detail.get("supports_resources", False)),
            server_name=str(detail.get("server_name") or ""),
            server_version=str(detail.get("server_version") or ""),
            instructions=detail.get("instructions"),
            tools=tool_items,
            resources=resource_items,
            refreshed_at=time.time(),
        )

    async def _with_operation_timeout(
        self,
        awaitable,
        *,
        service_id: str,
        operation: str,
    ) -> Any:
        task_name = f"mcp-metadata-refresh-{service_id}-{operation}"
        create_started_at = time.perf_counter()

        async def _run_operation() -> Any:
            operation_task_name = _current_task_name()
            start_delay_ms = int((time.perf_counter() - create_started_at) * 1000)
            _log_metadata_diagnostic(
                _slow_or_debug(start_delay_ms),
                "MCP metadata operation task started",
                service_id=service_id,
                operation=operation,
                phase="operation_task.started",
                task_name=operation_task_name,
                duration_ms=start_delay_ms,
                timeout_seconds=self._operation_timeout_seconds,
            )
            operation_started_at = time.perf_counter()
            try:
                result = await awaitable
            except asyncio.CancelledError:
                _log_metadata_diagnostic(
                    logging.WARNING,
                    "MCP metadata operation task cancelled",
                    service_id=service_id,
                    operation=operation,
                    phase="operation_task.cancelled",
                    task_name=operation_task_name,
                    duration_ms=_duration_ms_since(operation_started_at),
                    timeout_seconds=self._operation_timeout_seconds,
                    error_type="CancelledError",
                )
                raise
            except Exception as exc:
                _log_metadata_diagnostic(
                    logging.WARNING,
                    "MCP metadata operation task failed",
                    service_id=service_id,
                    operation=operation,
                    phase="operation_task.exception",
                    task_name=operation_task_name,
                    duration_ms=_duration_ms_since(operation_started_at),
                    timeout_seconds=self._operation_timeout_seconds,
                    error_type=exc.__class__.__name__,
                    error=str(exc),
                )
                raise
            _log_metadata_diagnostic(
                _slow_or_debug(_duration_ms_since(operation_started_at)),
                "MCP metadata operation task completed",
                service_id=service_id,
                operation=operation,
                phase="operation_task.completed",
                task_name=operation_task_name,
                duration_ms=_duration_ms_since(operation_started_at),
                timeout_seconds=self._operation_timeout_seconds,
            )
            return result

        task = asyncio.create_task(
            _run_operation(),
            name=task_name,
        )
        _log_metadata_diagnostic(
            logging.DEBUG,
            "MCP metadata operation task created",
            service_id=service_id,
            operation=operation,
            phase="operation_task.created",
            task_name=_current_task_name(),
            duration_ms=_duration_ms_since(create_started_at),
            timeout_seconds=self._operation_timeout_seconds,
        )
        wait_started_at = time.perf_counter()
        done, _pending = await asyncio.wait({task}, timeout=self._operation_timeout_seconds)
        if task in done:
            wait_ms = _duration_ms_since(wait_started_at)
            try:
                result = task.result()
            except Exception as exc:
                _log_metadata_diagnostic(
                    logging.WARNING,
                    "MCP metadata operation wait completed with exception",
                    service_id=service_id,
                    operation=operation,
                    phase="operation_wait.exception",
                    task_name=_current_task_name(),
                    duration_ms=wait_ms,
                    timeout_seconds=self._operation_timeout_seconds,
                    error_type=exc.__class__.__name__,
                    error=str(exc),
                )
                raise
            _log_metadata_diagnostic(
                _slow_or_debug(wait_ms),
                "MCP metadata operation wait completed",
                service_id=service_id,
                operation=operation,
                phase="operation_wait.completed",
                task_name=_current_task_name(),
                duration_ms=wait_ms,
                timeout_seconds=self._operation_timeout_seconds,
            )
            return result

        wait_ms = _duration_ms_since(wait_started_at)
        timeout_started_at = time.perf_counter()
        _log_metadata_diagnostic(
            logging.WARNING,
            "MCP metadata operation wait timed out",
            service_id=service_id,
            operation=operation,
            phase="operation_wait.timeout",
            task_name=_current_task_name(),
            duration_ms=wait_ms,
            timeout_seconds=self._operation_timeout_seconds,
            error_type="TimeoutError",
            error=f"operation did not complete within {self._operation_timeout_seconds}s",
        )
        task.cancel()
        _log_metadata_diagnostic(
            logging.DEBUG,
            "MCP metadata operation cancel requested after timeout",
            service_id=service_id,
            operation=operation,
            phase="operation_task.cancel_requested",
            task_name=_current_task_name(),
            duration_ms=0,
            timeout_seconds=self._operation_timeout_seconds,
        )
        task.add_done_callback(
            lambda done_task: self._consume_timed_out_operation(
                done_task,
                service_id=service_id,
                operation=operation,
                timeout_started_at=timeout_started_at,
                timeout_seconds=self._operation_timeout_seconds,
            )
        )
        raise TimeoutError(
            f"MCP 服务 '{service_id}' 元数据刷新操作超时: {operation}"
        )

    @staticmethod
    def _consume_timed_out_operation(
        task: asyncio.Task[Any],
        *,
        service_id: str,
        operation: str,
        timeout_started_at: float,
        timeout_seconds: float,
    ) -> None:
        recovery_ms = _duration_ms_since(timeout_started_at)
        try:
            task.result()
        except asyncio.CancelledError:
            _log_metadata_diagnostic(
                _slow_or_debug(recovery_ms),
                "MCP metadata timed-out operation cancellation recovered",
                service_id=service_id,
                operation=operation,
                phase="operation_task.cancel_recovered",
                task_name=None,
                duration_ms=recovery_ms,
                timeout_seconds=timeout_seconds,
                error_type="CancelledError",
            )
        except Exception as exc:
            _log_metadata_diagnostic(
                logging.WARNING,
                "MCP metadata timed-out operation completed with exception after timeout",
                service_id=service_id,
                operation=operation,
                phase="operation_task.exception_after_timeout",
                task_name=None,
                duration_ms=recovery_ms,
                timeout_seconds=timeout_seconds,
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
        else:
            _log_metadata_diagnostic(
                logging.WARNING,
                "MCP metadata timed-out operation completed after timeout",
                service_id=service_id,
                operation=operation,
                phase="operation_task.completed_after_timeout",
                task_name=None,
                duration_ms=recovery_ms,
                timeout_seconds=timeout_seconds,
            )

    async def _poll_loop(self) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            return
        try:
            while not stop_event.is_set():
                await self.refresh_once()
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self._poll_interval_seconds,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MCP 元数据后台刷新循环异常退出。")

    def _schedule_save(self, snapshot: McpServiceMetadataSnapshot) -> None:
        task = asyncio.create_task(
            monitored_to_thread(
                self._snapshot_repository.save,
                snapshot,
                operation="mcp.metadata_cache.save",
                caller="McpMetadataRefreshManager._schedule_save",
                log=logger,
                diagnostic_context={"service_id": snapshot.service_id, "cache_operation": "save"},
            ),
            name=f"mcp-metadata-cache-save-{snapshot.service_id}",
        )
        self._persist_tasks.add(task)
        task.add_done_callback(self._on_persist_task_done)

    def _schedule_delete(self, service_id: str) -> None:
        task = asyncio.create_task(
            monitored_to_thread(
                self._snapshot_repository.delete,
                service_id,
                operation="mcp.metadata_cache.delete",
                caller="McpMetadataRefreshManager._schedule_delete",
                log=logger,
                diagnostic_context={"service_id": service_id, "cache_operation": "delete"},
            ),
            name=f"mcp-metadata-cache-delete-{service_id}",
        )
        self._persist_tasks.add(task)
        task.add_done_callback(self._on_persist_task_done)

    def _on_persist_task_done(self, task: asyncio.Task[None]) -> None:
        self._persist_tasks.discard(task)
        with suppress(asyncio.CancelledError):
            try:
                task.result()
            except Exception:
                logger.exception("MCP 元数据缓存持久化任务失败。")
