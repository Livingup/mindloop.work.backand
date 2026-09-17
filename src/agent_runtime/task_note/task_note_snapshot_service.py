# -*- coding: utf-8 -*-
"""Agent Task Note 状态快照服务。

负责从 PostgreSQL 中查询并组装当前 (task_id, agent_instance_id) 维度下的
完整 Note 状态快照 JSON。该快照作为：
1. 业务 Agent 切换到 Note 更新态时的确定性基准线（Baseline）；
2. note_update_end 退出 Note 状态后创建新 Conversation 的初始上下文。
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, ContextManager, Iterator

logger = logging.getLogger("agent_runtime.task_note.snapshot_service")


def _serialize_value(val: Any) -> Any:
    """序列化 datetime 或特殊类型为 JSON 兼容对象。"""
    if isinstance(val, datetime):
        return val.isoformat()
    return val


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    """序列化数据库查询行。"""
    return {k: _serialize_value(v) for k, v in row.items()}


def get_empty_task_note_snapshot(task_id: str, agent_instance_id: str) -> dict[str, Any]:
    """返回空 Task Note 基线快照结构。"""
    return {
        "task_id": task_id,
        "agent_instance_id": agent_instance_id,
        "missions": [],
        "workspace_context": [],
        "solution": None,
        "decisions": [],
        "todos": [],
        "progress": None,
        "discoveries": [],
        "issues": [],
        "artifacts": [],
        "validations": [],
        "external_states": [],
        "commands": [],
        "agent_relationships": [],
        "agent_messages": [],
        "remote_agent_messages": [],
    }


_SENTINEL = object()


class TaskNoteSnapshotService:
    """Task Note 快照服务。"""

    def __init__(
        self,
        connection_factory: Callable[[], ContextManager[Any]] | None | object = _SENTINEL,
    ) -> None:
        if connection_factory is _SENTINEL:
            self._connection_factory = None
            self._allow_auto_resolve = True
        else:
            self._connection_factory = connection_factory  # type: ignore[assignment]
            self._allow_auto_resolve = False

    def _get_connection_factory(self) -> Callable[[], ContextManager[Any]] | None:
        """获取或自动延迟解析数据库连接工厂。"""
        if self._connection_factory is not None:
            return self._connection_factory
        if not self._allow_auto_resolve:
            return None

        # 1. 尝试从 get_task_note_store() 获取连接工厂
        try:
            from agent_runtime.task_note.task_note_store import get_task_note_store

            store = get_task_note_store()
            if store._connection_factory is not None:
                self._connection_factory = store._connection_factory
                return self._connection_factory
        except Exception:
            pass

        # 2. 尝试从 ConfigRegistry 动态初始化
        try:
            from agent_runtime.config import ConfigRegistry

            cfg = ConfigRegistry.instance()
            if cfg.cloud_user_config_database.is_postgresql_enabled:
                from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore

                store = PostgresUserConfigStore(lambda: cfg.cloud_user_config_database)
                self._connection_factory = store._connect
                return self._connection_factory
        except Exception as exc:
            logger.warning("动态解析 TaskNoteSnapshotService 连接工厂失败: %s", exc)
        return None

    @classmethod
    def from_config_registry(cls) -> "TaskNoteSnapshotService":
        """从全局配置中心创建 Task Note 快照服务实例。"""
        try:
            from agent_runtime.task_note.task_note_store import get_task_note_store

            store = get_task_note_store()
            if store._connection_factory is not None:
                return cls(connection_factory=store._connection_factory)
        except Exception:
            pass

        try:
            from agent_runtime.config import ConfigRegistry

            cfg = ConfigRegistry.instance()
            if cfg.cloud_user_config_database.is_postgresql_enabled:
                from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore

                store = PostgresUserConfigStore(lambda: cfg.cloud_user_config_database)
                return cls(connection_factory=store._connect)
        except Exception as exc:
            logger.warning("无法从 ConfigRegistry 初始化 TaskNoteSnapshotService 连接工厂: %s", exc)
        return cls()

    def build_snapshot(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
    ) -> dict[str, Any]:
        """构建指定 task_id 和 agent_instance_id 的完整 Note 快照 JSON。"""
        snapshot = get_empty_task_note_snapshot(task_id, agent_instance_id)
        conn_factory = self._get_connection_factory()
        if conn_factory is None:
            return snapshot

        try:
            with conn_factory() as conn:
                with conn.cursor() as cur:
                    def _query_rows(table_name: str, sql: str, params: tuple) -> list[dict[str, Any]]:
                        try:
                            cur.execute(sql, params)
                            return [_serialize_row(r) for r in cur.fetchall()]
                        except Exception as exc:
                            logger.warning(
                                "构建 Task Note 快照时查询 %s 失败 (已降级为空): %s",
                                table_name,
                                exc,
                            )
                            if hasattr(conn, "rollback"):
                                try:
                                    conn.rollback()
                                except Exception:
                                    pass
                            return []

                    def _query_one(table_name: str, sql: str, params: tuple) -> dict[str, Any] | None:
                        try:
                            cur.execute(sql, params)
                            row = cur.fetchone()
                            return _serialize_row(row) if row else None
                        except Exception as exc:
                            logger.warning(
                                "构建 Task Note 快照时查询 %s 失败 (已降级为空): %s",
                                table_name,
                                exc,
                            )
                            if hasattr(conn, "rollback"):
                                try:
                                    conn.rollback()
                                except Exception:
                                    pass
                            return None

                    # 1. agent_user_mission (当前 agent 实例在该 task 下创建和维护的 mission)
                    snapshot["missions"] = _query_rows(
                        "agent_user_mission",
                        """
                        SELECT mission_id, task_id, scene_id, agent_instance_id, title,
                               raw_prompts, refined_requirement, status, sort_order, created_at
                        FROM agent_user_mission
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY sort_order ASC, created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )

                    # 2. task_workspace_context
                    snapshot["workspace_context"] = _query_rows(
                        "task_workspace_context",
                        """
                        SELECT context_id, task_id, mission_id, agent_instance_id, source_type,
                               description, path, line_ranges, directory_structure, search_content,
                               core_result_content, reference_links, url_list, core_content,
                               command_id, important_content, content, created_at
                        FROM task_workspace_context
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )

                    # 3. task_solution_content (当前 agent 的活跃方案)
                    snapshot["solution"] = _query_one(
                        "task_solution_content",
                        """
                        SELECT solution_id, task_id, mission_id, agent_instance_id,
                               solution_content, solution_status, created_at
                        FROM task_solution_content
                        WHERE task_id = %s AND agent_instance_id = %s AND solution_status = 'active'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (task_id, agent_instance_id),
                    )

                    # 4. task_decision_records
                    snapshot["decisions"] = _query_rows(
                        "task_decision_records",
                        """
                        SELECT decision_id, task_id, mission_id, agent_instance_id,
                               decision_content, rationale, alternatives, impact, decision_status, created_at
                        FROM task_decision_records
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )

                    # 5. task_todo_items
                    snapshot["todos"] = _query_rows(
                        "task_todo_items",
                        """
                        SELECT todo_id, task_id, mission_id, agent_instance_id,
                               priority, todo_content, status, created_at
                        FROM task_todo_items
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY priority ASC, created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )

                    # 6. task_progress_content (当前 agent 唯一恢复快照)
                    snapshot["progress"] = _query_one(
                        "task_progress_content",
                        """
                        SELECT progress_id, task_id, mission_id, agent_instance_id,
                               completed_summary, current_work, current_blockers,
                               recent_validation, next_step, handoff_notes, created_at
                        FROM task_progress_content
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (task_id, agent_instance_id),
                    )

                    # 7. task_discovery_records
                    snapshot["discoveries"] = _query_rows(
                        "task_discovery_records",
                        """
                        SELECT discovery_id, task_id, mission_id, agent_instance_id,
                               discovery_content, impact, source_type, source_path,
                               line_ranges, command_id, reference_url, created_at
                        FROM task_discovery_records
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )

                    # 8. task_issue_records
                    snapshot["issues"] = _query_rows(
                        "task_issue_records",
                        """
                        SELECT issue_id, task_id, mission_id, agent_instance_id,
                               issue_content, impact, status, next_action, closure_condition,
                               resolution, source_type, command_id, created_at
                        FROM task_issue_records
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )

                    # 9. task_artifact_records
                    snapshot["artifacts"] = _query_rows(
                        "task_artifact_records",
                        """
                        SELECT artifact_id, task_id, mission_id, agent_instance_id,
                               type, file_path, edits, content_lines, source_path,
                               target_path, content, created_at
                        FROM task_artifact_records
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )

                    # 10. task_validation_records
                    snapshot["validations"] = _query_rows(
                        "task_validation_records",
                        """
                        SELECT validation_id, task_id, mission_id, agent_instance_id,
                               target_type, target_record_id, validation_method,
                               validation_scope, conclusion, uncovered_scope, validation_content,
                               source_type, command_id, source_path, line_ranges, created_at
                        FROM task_validation_records
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )

                    # 11. task_external_state_records
                    snapshot["external_states"] = _query_rows(
                        "task_external_state_records",
                        """
                        SELECT external_state_id, task_id, mission_id, agent_instance_id,
                               target_type, target_identifier, change_content, current_state,
                               reversibility, rollback_info, command_id, created_at
                        FROM task_external_state_records
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )

                    # 12. task_command_records
                    snapshot["commands"] = _query_rows(
                        "task_command_records",
                        """
                        SELECT command_id, task_id, agent_instance_id,
                               command_line, created_at
                        FROM task_command_records
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )

                    # 13. task_agent_relationships
                    snapshot["agent_relationships"] = _query_rows(
                        "task_agent_relationships",
                        """
                        SELECT relationship_id, task_id, mission_id, agent_instance_id,
                               target_agent_type, target_agent_instance_id, status, created_at
                        FROM task_agent_relationships
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )

                    # 14. task_agent_messages
                    snapshot["agent_messages"] = _query_rows(
                        "task_agent_messages",
                        """
                        SELECT message_id, task_id, mission_id, agent_instance_id,
                               target_agent_instance_id, target_agent_type, message_type,
                               message, created_at
                        FROM task_agent_messages
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )

                    # 15. task_remote_agent_messages
                    snapshot["remote_agent_messages"] = _query_rows(
                        "task_remote_agent_messages",
                        """
                        SELECT remote_message_id, task_id, mission_id, agent_instance_id,
                               remote_agent_instance_id, message_type, message, created_at
                        FROM task_remote_agent_messages
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )

        except Exception as exc:
            logger.exception("构建 Task Note 快照失败: task_id=%s, agent_instance_id=%s, error=%s", task_id, agent_instance_id, exc)
            return get_empty_task_note_snapshot(task_id, agent_instance_id)

        return snapshot

    async def build_task_note_snapshot_json(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
    ) -> str:
        """异步构建指定 (task_id, agent_instance_id) 维度的完整 Note 快照 JSON。"""
        loop = asyncio.get_running_loop()
        snapshot = await loop.run_in_executor(
            None,
            lambda: self.build_snapshot(task_id=task_id, agent_instance_id=agent_instance_id),
        )
        return json.dumps(snapshot, ensure_ascii=False, indent=2)

    async def query_latest_note_snapshot_json(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
    ) -> str:
        """查询最新 Note 快照 JSON。"""
        return await self.build_task_note_snapshot_json(
            task_id=task_id,
            agent_instance_id=agent_instance_id,
        )

    def cleanup_agent_instance_note(
        self,
        task_id: str,
        agent_instance_id: str,
    ) -> int:
        """物理清除指定子 Agent 实例的全部 Note 记录。"""
        from agent_runtime.task_note.task_note_store import TaskNoteStore

        store = TaskNoteStore(connection_factory=self._get_connection_factory())
        return store.cleanup_agent_instance_note(
            task_id=task_id, agent_instance_id=agent_instance_id
        )

    def cleanup_task_note(
        self,
        task_id: str,
    ) -> int:
        """物理清除指定 Task 下所有 Agent 实例的全部 Note 记录（含高频辅助表 task_command_records）。"""
        from agent_runtime.task_note.task_note_store import TaskNoteStore

        store = TaskNoteStore(connection_factory=self._get_connection_factory())
        return store.cleanup_task_note(task_id=task_id)

    async def async_cleanup_agent_instance_note(
        self,
        task_id: str,
        agent_instance_id: str,
    ) -> int:
        """异步物理清除指定子 Agent 实例的全部 Note 记录。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.cleanup_agent_instance_note(
                task_id=task_id, agent_instance_id=agent_instance_id
            ),
        )

    async def async_cleanup_task_note(
        self,
        task_id: str,
    ) -> int:
        """异步物理清除指定 Task 下所有 Agent 实例的全部 Note 记录。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.cleanup_task_note(task_id=task_id),
        )


_global_task_note_snapshot_service: TaskNoteSnapshotService | None = None


def get_task_note_snapshot_service() -> TaskNoteSnapshotService:
    """获取全局 TaskNoteSnapshotService 单例。"""
    global _global_task_note_snapshot_service
    if _global_task_note_snapshot_service is None:
        _global_task_note_snapshot_service = TaskNoteSnapshotService.from_config_registry()
    return _global_task_note_snapshot_service


def set_task_note_snapshot_service(service: TaskNoteSnapshotService | None) -> None:
    """设置或重置全局 TaskNoteSnapshotService 单例（用于测试与依赖注入）。"""
    global _global_task_note_snapshot_service
    _global_task_note_snapshot_service = service


