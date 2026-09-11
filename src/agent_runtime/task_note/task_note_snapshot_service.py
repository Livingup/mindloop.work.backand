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


class TaskNoteSnapshotService:
    """Task Note 快照服务。"""

    def __init__(
        self,
        connection_factory: Callable[[], ContextManager[Any]] | None = None,
    ) -> None:
        self._connection_factory = connection_factory

    @classmethod
    def from_config_registry(cls) -> "TaskNoteSnapshotService":
        """从全局配置中心创建 Task Note 快照服务实例。"""
        try:
            from agent_runtime.config import ConfigRegistry

            cfg = ConfigRegistry.instance()
            if cfg.cloud_user_config_database.is_postgresql_enabled:
                from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore

                store = PostgresUserConfigStore(lambda: cfg.cloud_user_config_database)
                return cls(connection_factory=store._connect)
        except Exception as exc:
            logger.warning("无法从 ConfigRegistry 初始化 TaskNoteSnapshotService 连接工厂: %s", exc)
        return cls(connection_factory=None)

    def build_snapshot(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
    ) -> dict[str, Any]:
        """构建指定 task_id 和 agent_instance_id 的完整 Note 快照 JSON。"""
        snapshot = get_empty_task_note_snapshot(task_id, agent_instance_id)
        if self._connection_factory is None:
            return snapshot

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    # 1. agent_user_mission (该 task 下所有关联或当前 agent 创建的 mission)
                    cur.execute(
                        """
                        SELECT mission_id, task_id, scene_id, agent_instance_id, title,
                               raw_prompts, refined_requirement, status, sort_order, created_at
                        FROM agent_user_mission
                        WHERE task_id = %s
                        ORDER BY sort_order ASC, created_at ASC
                        """,
                        (task_id,),
                    )
                    snapshot["missions"] = [_serialize_row(r) for r in cur.fetchall()]

                    # 2. task_workspace_context
                    cur.execute(
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
                    snapshot["workspace_context"] = [_serialize_row(r) for r in cur.fetchall()]

                    # 3. task_solution_content (当前 agent 的活跃方案)
                    cur.execute(
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
                    solution_row = cur.fetchone()
                    if solution_row:
                        snapshot["solution"] = _serialize_row(solution_row)

                    # 4. task_decision_records
                    cur.execute(
                        """
                        SELECT decision_id, task_id, mission_id, agent_instance_id,
                               decision_content, rationale, alternatives, impact, decision_status, created_at
                        FROM task_decision_records
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )
                    snapshot["decisions"] = [_serialize_row(r) for r in cur.fetchall()]

                    # 5. task_todo_items
                    cur.execute(
                        """
                        SELECT todo_id, task_id, mission_id, agent_instance_id,
                               priority, todo_content, status, created_at
                        FROM task_todo_items
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY priority ASC, created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )
                    snapshot["todos"] = [_serialize_row(r) for r in cur.fetchall()]

                    # 6. task_progress_content (当前 agent 唯一恢复快照)
                    cur.execute(
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
                    progress_row = cur.fetchone()
                    if progress_row:
                        snapshot["progress"] = _serialize_row(progress_row)

                    # 7. task_discovery_records
                    cur.execute(
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
                    snapshot["discoveries"] = [_serialize_row(r) for r in cur.fetchall()]

                    # 8. task_issue_records
                    cur.execute(
                        """
                        SELECT issue_id, task_id, mission_id, agent_instance_id,
                               issue_content, status, impact, resolution, related_context_id,
                               command_id, source_path, line_ranges, created_at
                        FROM task_issue_records
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )
                    snapshot["issues"] = [_serialize_row(r) for r in cur.fetchall()]

                    # 9. task_artifact_records
                    cur.execute(
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
                    snapshot["artifacts"] = [_serialize_row(r) for r in cur.fetchall()]

                    # 10. task_validation_records
                    cur.execute(
                        """
                        SELECT validation_id, task_id, mission_id, agent_instance_id,
                               validation_scope, conclusion, validation_content,
                               uncovered_scope, source_type, command_id, source_path,
                               line_ranges, created_at
                        FROM task_validation_records
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )
                    snapshot["validations"] = [_serialize_row(r) for r in cur.fetchall()]

                    # 11. task_external_state_records
                    cur.execute(
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
                    snapshot["external_states"] = [_serialize_row(r) for r in cur.fetchall()]

                    # 12. task_command_records
                    cur.execute(
                        """
                        SELECT command_id, task_id, agent_instance_id,
                               command_line, created_at
                        FROM task_command_records
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )
                    snapshot["commands"] = [_serialize_row(r) for r in cur.fetchall()]

                    # 13. task_agent_relationships
                    cur.execute(
                        """
                        SELECT relationship_id, task_id, mission_id, agent_instance_id,
                               target_agent_type, target_agent_instance_id, status, created_at
                        FROM task_agent_relationships
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )
                    snapshot["agent_relationships"] = [_serialize_row(r) for r in cur.fetchall()]

                    # 14. task_agent_messages
                    cur.execute(
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
                    snapshot["agent_messages"] = [_serialize_row(r) for r in cur.fetchall()]

                    # 15. task_remote_agent_messages
                    cur.execute(
                        """
                        SELECT remote_message_id, task_id, mission_id, agent_instance_id,
                               remote_agent_instance_id, message_type, message, created_at
                        FROM task_remote_agent_messages
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )
                    snapshot["remote_agent_messages"] = [_serialize_row(r) for r in cur.fetchall()]

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
        *,
        task_id: str,
        agent_instance_id: str,
    ) -> int:
        """物理清除指定子 Agent 实例的全部 Note 记录（含高频辅助表 task_command_records）。"""
        from agent_runtime.task_note.task_note_store import TaskNoteStore

        store = TaskNoteStore(connection_factory=self._connection_factory)
        return store.cleanup_agent_instance_note(
            task_id=task_id, agent_instance_id=agent_instance_id
        )

    def cleanup_task_note(
        self,
        *,
        task_id: str,
    ) -> int:
        """物理清除指定 Task 下所有 Agent 实例的全部 Note 记录（含高频辅助表 task_command_records）。"""
        from agent_runtime.task_note.task_note_store import TaskNoteStore

        store = TaskNoteStore(connection_factory=self._connection_factory)
        return store.cleanup_task_note(task_id=task_id)

    async def async_cleanup_agent_instance_note(
        self,
        *,
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
        *,
        task_id: str,
    ) -> int:
        """异步物理清除指定 Task 下所有 Agent 实例的全部 Note 记录。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.cleanup_task_note(task_id=task_id),
        )

