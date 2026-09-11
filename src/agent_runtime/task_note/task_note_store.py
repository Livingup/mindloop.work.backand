# -*- coding: utf-8 -*-
"""Agent Task Note 领域数据访问与持久化存储服务 (TaskNoteStore)。

负责 Note 更新 Agent 调用的 31 个维护工具对 PostgreSQL 11 张主题表的持久化 CRUD 操作：
1. agent_user_mission: create_mission, append_mission_prompt, update_mission_requirement, update_mission_status
2. task_workspace_context: create, update, delete
3. task_solution_content: upsert, supersede
4. task_decision_records: create, supersede, update
5. task_todo_items: create, batch_create, update_status, update_content, delete
6. task_progress_content: upsert
7. task_discovery_records: create, update, delete
8. task_issue_records: create, update, resolve
9. task_artifact_records (通用产物): create_other, delete_other
10. task_validation_records: create, update
11. task_external_state_records: create, update
12. 物理事实延迟归属绑定: bind_resources_to_mission
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, ContextManager

logger = logging.getLogger("agent_runtime.task_note.store")


def _generate_id(prefix: str) -> str:
    """生成带业务前缀的紧凑唯一标识。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _to_json_compatible(val: Any) -> Any:
    """递归将 Pydantic 模型、dataclass 等对象转换为原生 JSON 可序列化类型。"""
    if val is None:
        return None
    if hasattr(val, "model_dump") and callable(val.model_dump):
        return val.model_dump(mode="json")
    if hasattr(val, "dict") and callable(val.dict):
        return val.dict()
    if isinstance(val, (list, tuple, set)):
        return [_to_json_compatible(item) for item in val]
    if isinstance(val, dict):
        return {k: _to_json_compatible(v) for k, v in val.items()}
    return val


def _dump_json(val: Any) -> Any:
    """序列化为数据库驱动支持的 JSONB 或 JSON 字符串。"""
    if val is None:
        return None
    normalized = _to_json_compatible(val)
    try:
        from psycopg.types.json import Jsonb

        return Jsonb(normalized)
    except ImportError:
        return json.dumps(normalized, ensure_ascii=False)


class TaskNoteStore:
    """Task Note 数据库持久化存储服务。"""

    def __init__(
        self,
        connection_factory: Callable[[], ContextManager[Any]] | None = None,
    ) -> None:
        self._connection_factory = connection_factory

    @classmethod
    def from_config_registry(cls) -> "TaskNoteStore":
        """从全局配置中心创建 Task Note 存储服务实例。"""
        try:
            from agent_runtime.config import ConfigRegistry

            cfg = ConfigRegistry.instance()
            if cfg.cloud_user_config_database.is_postgresql_enabled:
                from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore

                store = PostgresUserConfigStore(lambda: cfg.cloud_user_config_database)
                return cls(connection_factory=store._connect)
        except Exception as exc:
            logger.warning("无法从 ConfigRegistry 初始化 TaskNoteStore 连接工厂: %s", exc)
        return cls(connection_factory=None)

    # ── 1. 业务任务单元 (agent_user_mission) ──────────────────────────

    def create_mission(
        self,
        *,
        task_id: str,
        scene_id: str,
        agent_instance_id: str,
        title: str,
        raw_prompt: str | None = None,
        refined_requirement: str | None = None,
        status: str = "in_progress",
        sort_order: int = 1,
        mission_id: str | None = None,
    ) -> str | None:
        """创建新业务任务单元（保存标题、用户/委派输入与提炼需求，支持优化措辞使需求更清晰明确）。"""
        resolved_mission_id = mission_id or f"mission-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        if self._connection_factory is None:
            logger.debug("TaskNoteStore 未配置数据库连接，跳过 create_mission")
            return resolved_mission_id

        initial_prompts = []
        if raw_prompt and raw_prompt.strip():
            initial_prompts.append({
                "prompt": raw_prompt.strip(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO agent_user_mission (
                            mission_id, task_id, scene_id, agent_instance_id,
                            title, raw_prompts, refined_requirement, status, sort_order
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            resolved_mission_id,
                            task_id,
                            scene_id,
                            agent_instance_id,
                            title,
                            _dump_json(initial_prompts),
                            refined_requirement,
                            status,
                            sort_order,
                        ),
                    )
                conn.commit()
            return resolved_mission_id
        except Exception as exc:
            logger.error("create_mission 写入数据库失败: %s", exc)
            raise

    def append_mission_prompt(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        mission_id: str,
        prompt: str,
    ) -> bool:
        """向业务任务追加用户发言或委派指令（支持在保持原意前提下优化措辞使需求更明确）。"""
        if self._connection_factory is None:
            return True

        new_item = [{
            "prompt": prompt,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE agent_user_mission
                        SET raw_prompts = raw_prompts || %s
                        WHERE task_id = %s AND mission_id = %s AND agent_instance_id = %s
                        """,
                        (_dump_json(new_item), task_id, mission_id, agent_instance_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("append_mission_prompt 更新失败: %s", exc)
            raise

    def update_mission_requirement(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        mission_id: str,
        refined_requirement: str,
    ) -> bool:
        """覆盖更新正式需求规格书。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE agent_user_mission
                        SET refined_requirement = %s
                        WHERE task_id = %s AND mission_id = %s AND agent_instance_id = %s
                        """,
                        (refined_requirement, task_id, mission_id, agent_instance_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("update_mission_requirement 更新失败: %s", exc)
            raise

    def update_mission_status(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        mission_id: str,
        status: str,
    ) -> bool:
        """推进业务任务状态。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE agent_user_mission
                        SET status = %s
                        WHERE task_id = %s AND mission_id = %s AND agent_instance_id = %s
                        """,
                        (status, task_id, mission_id, agent_instance_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("update_mission_status 更新失败: %s", exc)
            raise

    # ── 2. 任务背景素材 (task_workspace_context) ──────────────────────

    def create_workspace_context(
        self,
        *,
        task_id: str,
        mission_id: str,
        agent_instance_id: str,
        source_type: str,
        description: str | None = None,
        path: str | None = None,
        line_ranges: list[Any] | None = None,
        directory_structure: str | None = None,
        search_content: str | None = None,
        core_result_content: str | None = None,
        reference_links: list[Any] | None = None,
        url_list: list[Any] | None = None,
        core_content: str | None = None,
        command_id: str | None = None,
        important_content: str | None = None,
        content: str | None = None,
    ) -> str:
        """新增背景素材记录。"""
        context_id = _generate_id("ctx")
        if self._connection_factory is None:
            return context_id

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO task_workspace_context (
                            context_id, task_id, mission_id, agent_instance_id,
                            source_type, description, path, line_ranges,
                            directory_structure, search_content, core_result_content,
                            reference_links, url_list, core_content, command_id,
                            important_content, content
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s
                        )
                        """,
                        (
                            context_id,
                            task_id,
                            mission_id,
                            agent_instance_id,
                            source_type,
                            description,
                            path,
                            _dump_json(line_ranges),
                            directory_structure,
                            search_content,
                            core_result_content,
                            _dump_json(reference_links),
                            _dump_json(url_list),
                            core_content,
                            command_id,
                            important_content,
                            content,
                        ),
                    )
                conn.commit()
            return context_id
        except Exception as exc:
            logger.error("create_workspace_context 写入失败: %s", exc)
            raise

    def update_workspace_context(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        context_id: str,
        description: str | None = None,
        path: str | None = None,
        line_ranges: list[Any] | None = None,
        directory_structure: str | None = None,
        search_content: str | None = None,
        core_result_content: str | None = None,
        reference_links: list[Any] | None = None,
        url_list: list[Any] | None = None,
        core_content: str | None = None,
        command_id: str | None = None,
        important_content: str | None = None,
        content: str | None = None,
    ) -> bool:
        """更新背景素材记录。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE task_workspace_context SET
                            description = COALESCE(%s, description),
                            path = COALESCE(%s, path),
                            line_ranges = COALESCE(%s, line_ranges),
                            directory_structure = COALESCE(%s, directory_structure),
                            search_content = COALESCE(%s, search_content),
                            core_result_content = COALESCE(%s, core_result_content),
                            reference_links = COALESCE(%s, reference_links),
                            url_list = COALESCE(%s, url_list),
                            core_content = COALESCE(%s, core_content),
                            command_id = COALESCE(%s, command_id),
                            important_content = COALESCE(%s, important_content),
                            content = COALESCE(%s, content)
                        WHERE task_id = %s AND context_id = %s AND agent_instance_id = %s
                        """,
                        (
                            description,
                            path,
                            _dump_json(line_ranges) if line_ranges is not None else None,
                            directory_structure,
                            search_content,
                            core_result_content,
                            _dump_json(reference_links) if reference_links is not None else None,
                            _dump_json(url_list) if url_list is not None else None,
                            core_content,
                            command_id,
                            important_content,
                            content,
                            task_id,
                            context_id,
                            agent_instance_id,
                        ),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("update_workspace_context 更新失败: %s", exc)
            raise

    def delete_workspace_context(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        context_id: str,
    ) -> bool:
        """删除背景素材记录。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM task_workspace_context
                        WHERE task_id = %s AND context_id = %s AND agent_instance_id = %s
                        """,
                        (task_id, context_id, agent_instance_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("delete_workspace_context 删除失败: %s", exc)
            raise

    # ── 3. 当前宏观方案 (task_solution_content) ───────────────────────

    def upsert_solution(
        self,
        *,
        task_id: str,
        mission_id: str,
        agent_instance_id: str,
        solution_content: str,
    ) -> str:
        """全量覆盖更新宏观技术方案快照 (基于 mission_id, agent_instance_id 唯一约束)。"""
        solution_id = _generate_id("sol")
        if self._connection_factory is None:
            return solution_id

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO task_solution_content (
                            solution_id, task_id, mission_id, agent_instance_id,
                            solution_content, solution_status
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, 'active'
                        )
                        ON CONFLICT (mission_id, agent_instance_id)
                        DO UPDATE SET
                            solution_content = EXCLUDED.solution_content,
                            solution_status = 'active'
                        """,
                        (
                            solution_id,
                            task_id,
                            mission_id,
                            agent_instance_id,
                            solution_content,
                        ),
                    )
                conn.commit()
            return solution_id
        except Exception as exc:
            logger.error("upsert_solution 写入失败: %s", exc)
            raise

    def supersede_solution(
        self,
        *,
        task_id: str,
        mission_id: str,
        agent_instance_id: str,
    ) -> str | None:
        """软废弃当前宏观技术方案。"""
        if self._connection_factory is None:
            return f"sol-superseded-{task_id}"

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE task_solution_content
                        SET solution_status = 'superseded'
                        WHERE task_id = %s AND mission_id = %s AND agent_instance_id = %s
                        RETURNING solution_id
                        """,
                        (task_id, mission_id, agent_instance_id),
                    )
                    row = cur.fetchone()
                conn.commit()
            return row[0] if row else f"sol-superseded-{task_id}"
        except Exception as exc:
            logger.error("supersede_solution 更新失败: %s", exc)
            raise

    # ── 4. 关键架构决策 (task_decision_records) ───────────────────────

    def create_decision(
        self,
        *,
        task_id: str,
        mission_id: str,
        agent_instance_id: str,
        decision_content: str,
        rationale: str,
        alternatives: str | None = None,
        impact: str | None = None,
        decision_status: str = "active",
    ) -> str:
        """新增关键决策记录。"""
        decision_id = _generate_id("dec")
        if self._connection_factory is None:
            return decision_id

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO task_decision_records (
                            decision_id, task_id, mission_id, agent_instance_id,
                            decision_content, rationale, alternatives, impact, decision_status
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            decision_id,
                            task_id,
                            mission_id,
                            agent_instance_id,
                            decision_content,
                            rationale,
                            alternatives,
                            impact,
                            decision_status,
                        ),
                    )
                conn.commit()
            return decision_id
        except Exception as exc:
            logger.error("create_decision 写入失败: %s", exc)
            raise

    def supersede_decision(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        decision_id: str,
    ) -> bool:
        """软废弃决策记录。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE task_decision_records
                        SET decision_status = 'superseded'
                        WHERE task_id = %s AND decision_id = %s AND agent_instance_id = %s
                        """,
                        (task_id, decision_id, agent_instance_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("supersede_decision 更新失败: %s", exc)
            raise

    def update_decision(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        decision_id: str,
        decision_content: str | None = None,
        rationale: str | None = None,
        alternatives: str | None = None,
        impact: str | None = None,
    ) -> bool:
        """更新决策记录。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE task_decision_records SET
                            decision_content = COALESCE(%s, decision_content),
                            rationale = COALESCE(%s, rationale),
                            alternatives = COALESCE(%s, alternatives),
                            impact = COALESCE(%s, impact)
                        WHERE task_id = %s AND decision_id = %s AND agent_instance_id = %s
                        """,
                        (decision_content, rationale, alternatives, impact, task_id, decision_id, agent_instance_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("update_decision 更新失败: %s", exc)
            raise

    # ── 5. 计划工作项清单 (task_todo_items) ───────────────────────────

    def create_todo(
        self,
        *,
        task_id: str,
        mission_id: str,
        agent_instance_id: str,
        todo_content: str,
        priority: int = 1,
        status: str = "未开始",
    ) -> str:
        """新增待办工作项。"""
        todo_id = _generate_id("todo")
        if self._connection_factory is None:
            return todo_id

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO task_todo_items (
                            todo_id, task_id, mission_id, agent_instance_id,
                            priority, todo_content, status
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s
                        )
                        """,
                        (
                            todo_id,
                            task_id,
                            mission_id,
                            agent_instance_id,
                            priority,
                            todo_content,
                            status,
                        ),
                    )
                conn.commit()
            return todo_id
        except Exception as exc:
            logger.error("create_todo 写入失败: %s", exc)
            raise

    def batch_create_todos(
        self,
        *,
        task_id: str,
        mission_id: str,
        agent_instance_id: str,
        todos: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """批量创建待办工作项。"""
        results: list[dict[str, Any]] = []
        if not todos:
            return results

        if self._connection_factory is None:
            for item in todos:
                results.append({
                    "todo_id": _generate_id("todo"),
                    "todo_content": item.get("todo_content", ""),
                })
            return results

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    for item in todos:
                        tid = _generate_id("todo")
                        content = item.get("todo_content", "")
                        prio = int(item.get("priority", 1))
                        stat = item.get("status", "未开始")
                        cur.execute(
                            """
                            INSERT INTO task_todo_items (
                                todo_id, task_id, mission_id, agent_instance_id,
                                priority, todo_content, status
                            ) VALUES (
                                %s, %s, %s, %s,
                                %s, %s, %s
                            )
                            """,
                            (tid, task_id, mission_id, agent_instance_id, prio, content, stat),
                        )
                        results.append({"todo_id": tid, "todo_content": content})
                conn.commit()
            return results
        except Exception as exc:
            logger.error("batch_create_todos 批量写入失败: %s", exc)
            raise

    def update_todo_status(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        todo_id: str,
        status: str,
    ) -> bool:
        """极简推进待办状态。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE task_todo_items
                        SET status = %s
                        WHERE task_id = %s AND todo_id = %s AND agent_instance_id = %s
                        """,
                        (status, task_id, todo_id, agent_instance_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("update_todo_status 更新失败: %s", exc)
            raise

    def update_todo_content(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        todo_id: str,
        todo_content: str | None = None,
        priority: int | None = None,
    ) -> bool:
        """修改待办内容或优先级。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE task_todo_items SET
                            todo_content = COALESCE(%s, todo_content),
                            priority = COALESCE(%s, priority)
                        WHERE task_id = %s AND todo_id = %s AND agent_instance_id = %s
                        """,
                        (todo_content, priority, task_id, todo_id, agent_instance_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("update_todo_content 更新失败: %s", exc)
            raise

    def delete_todo(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        todo_id: str,
    ) -> bool:
        """物理删除失效待办。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM task_todo_items
                        WHERE task_id = %s AND todo_id = %s AND agent_instance_id = %s
                        """,
                        (task_id, todo_id, agent_instance_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("delete_todo 删除失败: %s", exc)
            raise

    # ── 6. 任务恢复快照 (task_progress_content) ───────────────────────

    def upsert_progress(
        self,
        *,
        task_id: str,
        mission_id: str,
        agent_instance_id: str,
        completed_summary: str,
        current_work: str,
        current_blockers: str | None = None,
        recent_validation: str | None = None,
        next_step: str,
        handoff_notes: str | None = None,
    ) -> str:
        """全量覆盖写入当前唯一的恢复快照 PC 指针。"""
        progress_id = _generate_id("prog")
        if self._connection_factory is None:
            return progress_id

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO task_progress_content (
                            progress_id, task_id, mission_id, agent_instance_id,
                            completed_summary, current_work, current_blockers,
                            recent_validation, next_step, handoff_notes, created_at
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s, CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (mission_id, agent_instance_id)
                        DO UPDATE SET
                            completed_summary = EXCLUDED.completed_summary,
                            current_work = EXCLUDED.current_work,
                            current_blockers = EXCLUDED.current_blockers,
                            recent_validation = EXCLUDED.recent_validation,
                            next_step = EXCLUDED.next_step,
                            handoff_notes = EXCLUDED.handoff_notes,
                            created_at = CURRENT_TIMESTAMP
                        """,
                        (
                            progress_id,
                            task_id,
                            mission_id,
                            agent_instance_id,
                            completed_summary,
                            current_work,
                            current_blockers,
                            recent_validation,
                            next_step,
                            handoff_notes,
                        ),
                    )
                conn.commit()
            return progress_id
        except Exception as exc:
            logger.error("upsert_progress 写入失败: %s", exc)
            raise

    # ── 7. 客观认知发现 (task_discovery_records) ───────────────────────

    def create_discovery(
        self,
        *,
        task_id: str,
        mission_id: str,
        agent_instance_id: str,
        discovery_content: str,
        impact: str,
        source_type: str | None = None,
        source_path: str | None = None,
        line_ranges: list[Any] | None = None,
        command_id: str | None = None,
        reference_url: str | None = None,
    ) -> str:
        """新增客观认知发现记录。"""
        discovery_id = _generate_id("disc")
        if self._connection_factory is None:
            return discovery_id

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO task_discovery_records (
                            discovery_id, task_id, mission_id, agent_instance_id,
                            discovery_content, impact, source_type, source_path,
                            line_ranges, command_id, reference_url
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s
                        )
                        """,
                        (
                            discovery_id,
                            task_id,
                            mission_id,
                            agent_instance_id,
                            discovery_content,
                            impact,
                            source_type,
                            source_path,
                            _dump_json(line_ranges),
                            command_id,
                            reference_url,
                        ),
                    )
                conn.commit()
            return discovery_id
        except Exception as exc:
            logger.error("create_discovery 写入失败: %s", exc)
            raise

    def update_discovery(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        discovery_id: str,
        discovery_content: str | None = None,
        impact: str | None = None,
        source_type: str | None = None,
        source_path: str | None = None,
        line_ranges: list[Any] | None = None,
        command_id: str | None = None,
        reference_url: str | None = None,
    ) -> bool:
        """更新客观认知记录。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE task_discovery_records SET
                            discovery_content = COALESCE(%s, discovery_content),
                            impact = COALESCE(%s, impact),
                            source_type = COALESCE(%s, source_type),
                            source_path = COALESCE(%s, source_path),
                            line_ranges = COALESCE(%s, line_ranges),
                            command_id = COALESCE(%s, command_id),
                            reference_url = COALESCE(%s, reference_url)
                        WHERE task_id = %s AND discovery_id = %s AND agent_instance_id = %s
                        """,
                        (
                            discovery_content,
                            impact,
                            source_type,
                            source_path,
                            _dump_json(line_ranges) if line_ranges is not None else None,
                            command_id,
                            reference_url,
                            task_id,
                            discovery_id,
                            agent_instance_id,
                        ),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("update_discovery 更新失败: %s", exc)
            raise

    def delete_discovery(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        discovery_id: str,
    ) -> bool:
        """物理删除证伪认知噪音。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM task_discovery_records
                        WHERE task_id = %s AND discovery_id = %s AND agent_instance_id = %s
                        """,
                        (task_id, discovery_id, agent_instance_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("delete_discovery 删除失败: %s", exc)
            raise

    # ── 8. 问题与阻塞跟踪 (task_issue_records) ─────────────────────────

    def create_issue(
        self,
        *,
        task_id: str,
        mission_id: str,
        agent_instance_id: str,
        issue_content: str,
        impact: str,
        status: str = "open",
        next_action: str | None = None,
        closure_condition: str | None = None,
        resolution: str | None = None,
        source_type: str | None = None,
        command_id: str | None = None,
    ) -> str:
        """新增问题与阻塞记录。"""
        issue_id = _generate_id("issue")
        if self._connection_factory is None:
            return issue_id

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO task_issue_records (
                            issue_id, task_id, mission_id, agent_instance_id,
                            issue_content, impact, status, next_action,
                            closure_condition, resolution, source_type, command_id
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s, %s
                        )
                        """,
                        (
                            issue_id,
                            task_id,
                            mission_id,
                            agent_instance_id,
                            issue_content,
                            impact,
                            status,
                            next_action,
                            closure_condition,
                            resolution,
                            source_type,
                            command_id,
                        ),
                    )
                conn.commit()
            return issue_id
        except Exception as exc:
            logger.error("create_issue 写入失败: %s", exc)
            raise

    def update_issue(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        issue_id: str,
        issue_content: str | None = None,
        impact: str | None = None,
        status: str | None = None,
        next_action: str | None = None,
        closure_condition: str | None = None,
        resolution: str | None = None,
        source_type: str | None = None,
        command_id: str | None = None,
    ) -> bool:
        """更新问题记录。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE task_issue_records SET
                            issue_content = COALESCE(%s, issue_content),
                            impact = COALESCE(%s, impact),
                            status = COALESCE(%s, status),
                            next_action = COALESCE(%s, next_action),
                            closure_condition = COALESCE(%s, closure_condition),
                            resolution = COALESCE(%s, resolution),
                            source_type = COALESCE(%s, source_type),
                            command_id = COALESCE(%s, command_id)
                        WHERE task_id = %s AND issue_id = %s AND agent_instance_id = %s
                        """,
                        (
                            issue_content,
                            impact,
                            status,
                            next_action,
                            closure_condition,
                            resolution,
                            source_type,
                            command_id,
                            task_id,
                            issue_id,
                            agent_instance_id,
                        ),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("update_issue 更新失败: %s", exc)
            raise

    def resolve_issue(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        issue_id: str,
        resolution: str,
    ) -> bool:
        """闭环解决问题。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE task_issue_records
                        SET status = 'resolved', resolution = %s
                        WHERE task_id = %s AND issue_id = %s AND agent_instance_id = %s
                        """,
                        (resolution, task_id, issue_id, agent_instance_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("resolve_issue 解决失败: %s", exc)
            raise

    # ── 9. 通用交付产物 (task_artifact_records) ───────────────────────

    def create_artifact_other(
        self,
        *,
        task_id: str,
        mission_id: str,
        agent_instance_id: str,
        artifact_type: str,
        content: str,
    ) -> str:
        """登记通用成果交付产物 (非 file 类型)。"""
        artifact_id = _generate_id("art")
        if self._connection_factory is None:
            return artifact_id

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO task_artifact_records (
                            artifact_id, task_id, mission_id, agent_instance_id,
                            type, content
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s
                        )
                        """,
                        (
                            artifact_id,
                            task_id,
                            mission_id,
                            agent_instance_id,
                            artifact_type,
                            content,
                        ),
                    )
                conn.commit()
            return artifact_id
        except Exception as exc:
            logger.error("create_artifact_other 写入失败: %s", exc)
            raise

    def delete_artifact_other(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        artifact_id: str,
    ) -> bool:
        """删除非代码文件的通用交付物 (严禁删除底层文件产物)。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM task_artifact_records
                        WHERE task_id = %s AND artifact_id = %s AND agent_instance_id = %s AND type != 'file'
                        """,
                        (task_id, artifact_id, agent_instance_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("delete_artifact_other 删除失败: %s", exc)
            raise

    # ── 10. 验证与检查结论 (task_validation_records) ──────────────────

    def create_validation(
        self,
        *,
        task_id: str,
        mission_id: str,
        agent_instance_id: str,
        target_type: str,
        validation_method: str,
        validation_scope: str,
        conclusion: str,
        validation_content: str,
        source_type: str,
        target_record_id: str | None = None,
        uncovered_scope: str | None = None,
        command_id: str | None = None,
        source_path: str | None = None,
        line_ranges: list[Any] | None = None,
    ) -> str:
        """登记测试与验证结论。"""
        validation_id = _generate_id("val")
        if self._connection_factory is None:
            return validation_id

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO task_validation_records (
                            validation_id, task_id, mission_id, agent_instance_id,
                            target_type, target_record_id, validation_method, validation_scope,
                            conclusion, uncovered_scope, validation_content, source_type,
                            command_id, source_path, line_ranges
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s
                        )
                        """,
                        (
                            validation_id,
                            task_id,
                            mission_id,
                            agent_instance_id,
                            target_type,
                            target_record_id,
                            validation_method,
                            validation_scope,
                            conclusion,
                            uncovered_scope,
                            validation_content,
                            source_type,
                            command_id,
                            source_path,
                            _dump_json(line_ranges),
                        ),
                    )
                conn.commit()
            return validation_id
        except Exception as exc:
            logger.error("create_validation 写入失败: %s", exc)
            raise

    def update_validation(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        validation_id: str,
        conclusion: str | None = None,
        uncovered_scope: str | None = None,
        validation_content: str | None = None,
        source_type: str | None = None,
        command_id: str | None = None,
        source_path: str | None = None,
        line_ranges: list[Any] | None = None,
    ) -> bool:
        """修正验证结论。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE task_validation_records SET
                            conclusion = COALESCE(%s, conclusion),
                            uncovered_scope = COALESCE(%s, uncovered_scope),
                            validation_content = COALESCE(%s, validation_content),
                            source_type = COALESCE(%s, source_type),
                            command_id = COALESCE(%s, command_id),
                            source_path = COALESCE(%s, source_path),
                            line_ranges = COALESCE(%s, line_ranges)
                        WHERE task_id = %s AND validation_id = %s AND agent_instance_id = %s
                        """,
                        (
                            conclusion,
                            uncovered_scope,
                            validation_content,
                            source_type,
                            command_id,
                            source_path,
                            _dump_json(line_ranges) if line_ranges is not None else None,
                            task_id,
                            validation_id,
                            agent_instance_id,
                        ),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("update_validation 更新失败: %s", exc)
            raise

    # ── 11. 外部持久状态变更 (task_external_state_records) ────────────

    def create_external_state(
        self,
        *,
        task_id: str,
        mission_id: str,
        agent_instance_id: str,
        target_type: str,
        target_identifier: str,
        change_content: str,
        current_state: str,
        reversibility: str = "reversible",
        rollback_info: str | None = None,
        command_id: str | None = None,
    ) -> str:
        """记录外部环境状态变更与回滚指引。"""
        external_state_id = _generate_id("ext")
        if self._connection_factory is None:
            return external_state_id

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO task_external_state_records (
                            external_state_id, task_id, mission_id, agent_instance_id,
                            target_type, target_identifier, change_content, current_state,
                            reversibility, rollback_info, command_id
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s
                        )
                        """,
                        (
                            external_state_id,
                            task_id,
                            mission_id,
                            agent_instance_id,
                            target_type,
                            target_identifier,
                            change_content,
                            current_state,
                            reversibility,
                            rollback_info,
                            command_id,
                        ),
                    )
                conn.commit()
            return external_state_id
        except Exception as exc:
            logger.error("create_external_state 写入失败: %s", exc)
            raise

    def update_external_state(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        external_state_id: str,
        change_content: str | None = None,
        current_state: str | None = None,
        reversibility: str | None = None,
        rollback_info: str | None = None,
        command_id: str | None = None,
    ) -> bool:
        """更新外部状态记录。"""
        if self._connection_factory is None:
            return True

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE task_external_state_records SET
                            change_content = COALESCE(%s, change_content),
                            current_state = COALESCE(%s, current_state),
                            reversibility = COALESCE(%s, reversibility),
                            rollback_info = COALESCE(%s, rollback_info),
                            command_id = COALESCE(%s, command_id)
                        WHERE task_id = %s AND external_state_id = %s AND agent_instance_id = %s
                        """,
                        (
                            change_content,
                            current_state,
                            reversibility,
                            rollback_info,
                            command_id,
                            task_id,
                            external_state_id,
                            agent_instance_id,
                        ),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("update_external_state 更新失败: %s", exc)
            raise

    # ── 12. 物理资源延迟归属绑定 ───────────────────────────────────────

    def bind_resources_to_mission(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        mission_id: str,
        resource_type: str,
        resource_ids: list[str],
    ) -> int:
        """将底层物理事实记录批量延迟绑定到指定业务任务。"""
        if not resource_ids:
            return 0
        if self._connection_factory is None:
            return len(resource_ids)

        bound_count = 0
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    if resource_type in ("artifact", "artifacts"):
                        cur.execute(
                            """
                            UPDATE task_artifact_records
                            SET mission_id = %s
                            WHERE task_id = %s AND agent_instance_id = %s AND artifact_id = ANY(%s)
                            """,
                            (mission_id, task_id, agent_instance_id, resource_ids),
                        )
                        bound_count = cur.rowcount
                    elif resource_type in ("agent_relationship", "agent_relationships"):
                        cur.execute(
                            """
                            UPDATE task_agent_relationships
                            SET mission_id = %s
                            WHERE task_id = %s AND agent_instance_id = %s AND relationship_id = ANY(%s)
                            """,
                            (mission_id, task_id, agent_instance_id, resource_ids),
                        )
                        bound_count = cur.rowcount
                        # 级联更新关联的目标子 Agent 专线消息
                        cur.execute(
                            """
                            UPDATE task_agent_messages
                            SET mission_id = %s
                            WHERE task_id = %s AND agent_instance_id = %s
                              AND target_agent_instance_id IN (
                                  SELECT target_agent_instance_id
                                  FROM task_agent_relationships
                                  WHERE relationship_id = ANY(%s)
                              )
                            """,
                            (mission_id, task_id, agent_instance_id, resource_ids),
                        )
                    else:
                        logger.warning("未知的 resource_type: %s", resource_type)
                conn.commit()
            return bound_count
        except Exception as exc:
            logger.error("bind_resources_to_mission 绑定失败: %s", exc)
            raise

    def get_command_lines_by_ids(
        self,
        *,
        task_id: str,
        command_ids: list[str],
    ) -> dict[str, str]:
        """按 command_id 列表批量反查对应的 命令行指令 (command_line)。"""
        if not command_ids or self._connection_factory is None:
            return {}
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT command_id, command_line
                        FROM task_command_records
                        WHERE task_id = %s AND command_id = ANY(%s)
                        """,
                        (task_id, list(command_ids)),
                    )
                    return {row["command_id"]: row["command_line"] for row in cur.fetchall()}
        except Exception as exc:
            logger.error("get_command_lines_by_ids 查询失败: %s", exc)
            return {}

    def get_all_command_lines_for_tenant(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
    ) -> dict[str, str]:
        """反查当前租户 (task_id, agent_instance_id) 下的所有 命令行执行 ID 与 命令行指令映射。"""
        if self._connection_factory is None:
            return {}
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT command_id, command_line
                        FROM task_command_records
                        WHERE task_id = %s AND agent_instance_id = %s
                        """,
                        (task_id, agent_instance_id),
                    )
                    return {row["command_id"]: row["command_line"] for row in cur.fetchall()}
        except Exception as exc:
            logger.error("get_all_command_lines_for_tenant 查询失败: %s", exc)
            return {}

    def cleanup_agent_instance_note(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
    ) -> int:
        """物理清除指定子 Agent 实例的全部 Note 记录（含高频辅助表 task_command_records）。

        用于非 Root Agent 调用 end_task 完成会话时的垃圾回收。
        """
        if not task_id or not agent_instance_id or self._connection_factory is None:
            return 0

        total_deleted = 0
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    for table in ALL_NOTE_TABLES:
                        cur.execute(
                            f"DELETE FROM {table} WHERE task_id = %s AND agent_instance_id = %s",
                            (task_id, agent_instance_id),
                        )
                        total_deleted += cur.rowcount if cur.rowcount > 0 else 0
                conn.commit()
            logger.info(
                "物理清理子 Agent Note 记录完成: task_id=%s, agent_instance_id=%s, deleted_rows=%d",
                task_id,
                agent_instance_id,
                total_deleted,
            )
            return total_deleted
        except Exception as exc:
            logger.error(
                "物理清理子 Agent Note 记录失败 (忽略异常): task_id=%s, agent_instance_id=%s, error=%s",
                task_id,
                agent_instance_id,
                exc,
            )
            return 0

    def cleanup_task_note(
        self,
        *,
        task_id: str,
    ) -> int:
        """物理清除指定 Task 下所有 Agent 实例的全部 Note 记录（含高频辅助表 task_command_records）。

        用于 Root Agent 调用 end_task 结束全任务时的垃圾回收。
        """
        if not task_id or self._connection_factory is None:
            return 0

        total_deleted = 0
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    for table in ALL_NOTE_TABLES:
                        cur.execute(
                            f"DELETE FROM {table} WHERE task_id = %s",
                            (task_id,),
                        )
                        total_deleted += cur.rowcount if cur.rowcount > 0 else 0
                conn.commit()
            logger.info(
                "物理清理 Task 全量 Note 记录完成: task_id=%s, deleted_rows=%d",
                task_id,
                total_deleted,
            )
            return total_deleted
        except Exception as exc:
            logger.error(
                "物理清理 Task 全量 Note 记录失败 (忽略异常): task_id=%s, error=%s",
                task_id,
                exc,
            )
            return 0


ALL_NOTE_TABLES: tuple[str, ...] = (
    "task_mission_records",
    "task_active_solution",
    "task_decision_records",
    "task_progress_content",
    "task_todo_items",
    "task_workspace_context",
    "task_discovery_records",
    "task_issue_records",
    "task_artifact_records",
    "task_validation_records",
    "task_external_state_records",
    "task_command_records",
    "task_agent_relationships",
    "task_agent_messages",
    "task_remote_agent_messages",
)



_global_task_note_store: TaskNoteStore | None = None


def get_task_note_store() -> TaskNoteStore:
    """获取全局 TaskNoteStore 单例。"""
    global _global_task_note_store
    if _global_task_note_store is None:
        _global_task_note_store = TaskNoteStore.from_config_registry()
    return _global_task_note_store


def set_task_note_store(store: TaskNoteStore | None) -> None:
    """设置或重置全局 TaskNoteStore 单例（用于测试与依赖注入）。"""
    global _global_task_note_store
    _global_task_note_store = store
