# -*- coding: utf-8 -*-
"""Agent Task Note 领域数据访问与持久化存储服务 (TaskNoteStore)。

负责 Note 更新 Agent 调用的 36 个维护工具对 PostgreSQL 16 张表的持久化 CRUD 操作：
1. agent_user_mission: create_mission, append_mission_prompt, update_mission_prompts, update_mission_title, update_mission_requirement, update_mission_status, prune_mission, delete_mission
2. task_workspace_context: create, update, delete
3. task_solution_content: upsert, supersede
4. task_decision_records: create, supersede, update
5. task_todo_items: create, batch_create, update_status, update_content, delete
6. task_progress_content: upsert
7. task_discovery_records: create, update, delete
8. task_issue_records: create, update, resolve
9. task_artifact_records: create_other, delete_task_artifacts
10. task_validation_records: create, update
11. task_external_state_records: create, update
12. task_command_records: 终端命令索引记录
13. task_agent_relationships: 子 Agent 拓扑关系
14. task_agent_messages: 父子 Agent 专线消息
15. task_remote_agent_messages: 远程对等 Agent 通信消息
16. task_note_watermark: 观察断点水位记录
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable, ContextManager

logger = logging.getLogger("agent_runtime.task_note.store")

_task_note_available: bool = False


class TaskNoteStorageUnavailableError(RuntimeError):
    """Task Note 存储不可用（PG 未启用、未配置连接工厂或底层连接失败）。"""
    pass


def set_task_note_available(available: bool) -> None:
    """设置 Task Note 底层存储可用状态。"""
    global _task_note_available
    _task_note_available = bool(available)


def is_task_note_available() -> bool:
    """查询 Task Note 底层存储当前是否可用。"""
    global _task_note_available
    return _task_note_available



def _generate_id(prefix: str) -> str:
    """生成带业务前缀的紧凑唯一标识。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _to_json_compatible(val: Any) -> Any:
    """递归将 Pydantic 模型、dataclass、datetime 等对象转换为原生 JSON 可序列化类型。"""
    if val is None:
        return None
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, uuid.UUID):
        return str(val)
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


def _assert_mission_writable(cur: Any, mission_id: str, agent_instance_id: str) -> None:
    """断言任务存在、归属于目标 Agent 且未被裁剪，否则抛出异常拒绝写入。"""
    if not mission_id or not str(mission_id).strip():
        raise ValueError("缺失必填任务 ID (mission_id)")
    cur.execute(
        "SELECT pruned_at, agent_instance_id FROM agent_user_mission WHERE mission_id = %s AND agent_instance_id = %s",
        (str(mission_id).strip(), agent_instance_id),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"目标任务不存在: mission_id={mission_id}")
    pruned_at = row.get("pruned_at") if isinstance(row, dict) else row[0]
    owner_agent_id = (row.get("agent_instance_id") or agent_instance_id) if isinstance(row, dict) else (row[1] if len(row) > 1 else agent_instance_id)
    if owner_agent_id != agent_instance_id:
        raise ValueError(f"任务不属于目标 Agent: mission_id={mission_id}")
    if pruned_at is not None:
        raise ValueError("任务已裁剪为卡片，只读")


def _assert_record_mission_writable(
    cur: Any,
    table_name: str,
    id_col: str,
    record_id: str,
    task_id: str,
    agent_instance_id: str,
) -> None:
    """断言待修改/删除的记录所挂载的任务未被裁剪为卡片。"""
    cur.execute(
        f"SELECT mission_id FROM {table_name} WHERE task_id = %s AND {id_col} = %s AND agent_instance_id = %s",
        (task_id, record_id, agent_instance_id),
    )
    row = cur.fetchone()
    if row:
        mid = row.get("mission_id") if isinstance(row, dict) else row[0]
        if mid:
            _assert_mission_writable(cur, mid, agent_instance_id)


class TaskNoteStore:
    """Task Note 数据库持久化存储服务。"""

    def __init__(
        self,
        connection_factory: Callable[[], ContextManager[Any]] | None = None,
    ) -> None:
        self._connection_factory = connection_factory

    def is_available(self) -> bool:
        """检查底层数据库是否可用。"""
        if self._connection_factory is None:
            return False
        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            return True
        except Exception:
            return False

    def _get_connection(self) -> ContextManager[Any]:
        """获取数据库连接，若未配置或不可用则显式抛出 TaskNoteStorageUnavailableError。"""
        try:
            return self._connection_factory()
        except Exception as exc:
            raise TaskNoteStorageUnavailableError(f"Task Note 存储不可用 (连接失败): {exc}") from exc

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
        initial_prompts = []
        if raw_prompt and raw_prompt.strip():
            initial_prompts.append({
                "prompt": raw_prompt.strip(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        try:
            with self._get_connection() as conn:
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
        new_item = [{
            "prompt": prompt,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]

        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
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

    def update_mission_prompts(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        mission_id: str,
        raw_prompts: list[str],
    ) -> bool:
        """整组全量覆盖替换任务原始诉求列表（支持在保持核心含义与需求不变的前提下优化措辞）。"""
        items = [
            {
                "prompt": p.strip(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            for p in raw_prompts
            if p and p.strip()
        ]

        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
                    cur.execute(
                        """
                        UPDATE agent_user_mission
                        SET raw_prompts = %s
                        WHERE task_id = %s AND mission_id = %s AND agent_instance_id = %s
                        """,
                        (_dump_json(items), task_id, mission_id, agent_instance_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("update_mission_prompts 更新失败: %s", exc)
            raise

    def update_mission_title(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        mission_id: str,
        title: str,
    ) -> bool:
        """更新业务任务简明标题。"""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
                    cur.execute(
                        """
                        UPDATE agent_user_mission
                        SET title = %s
                        WHERE task_id = %s AND mission_id = %s AND agent_instance_id = %s
                        """,
                        (title, task_id, mission_id, agent_instance_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("update_mission_title 更新失败: %s", exc)
            raise

    def prune_mission(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        mission_id: str,
        outcome_summary: str,
        keep_record_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """闭环任务卡片化裁剪：清空庞大需求与过程描述，写入结果摘要与裁剪时间，级联清理过程记录。"""
        keep_set = set(keep_record_ids or [])
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT status, agent_instance_id, pruned_at
                        FROM agent_user_mission
                        WHERE task_id = %s AND mission_id = %s
                        """,
                        (task_id, mission_id),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise ValueError(f"目标任务不存在: mission_id={mission_id}")

                    status = row.get("status") if isinstance(row, dict) else row[0]
                    owner_agent_id = row.get("agent_instance_id") if isinstance(row, dict) else (row[1] if len(row) > 1 else agent_instance_id)
                    pruned_at = row.get("pruned_at") if isinstance(row, dict) else (row[2] if len(row) > 2 else None)

                    if owner_agent_id != agent_instance_id:
                        raise ValueError(f"任务不属于目标 Agent: mission_id={mission_id}")

                    if pruned_at is not None:
                        raise ValueError("任务已裁剪为卡片，只读")

                    if status not in ("completed", "abandoned"):
                        raise ValueError(f"任务状态为 {status}，仅允许裁剪已闭环任务(completed, abandoned)")

                    # 校验 task_agent_relationships 中该 mission 下不存在 status='unreleased' 的行
                    cur.execute(
                        """
                        SELECT 1 FROM task_agent_relationships
                        WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s AND status = 'unreleased'
                        LIMIT 1
                        """,
                        (task_id, agent_instance_id, mission_id),
                    )
                    if cur.fetchone():
                        raise ValueError("任务存在未释放的子 Agent (status='unreleased')，禁止裁剪")

                    # 同一事务内：写 outcome_summary、pruned_at；清空 raw_prompts、refined_requirement
                    cur.execute(
                        """
                        UPDATE agent_user_mission
                        SET outcome_summary = %s,
                            pruned_at = CURRENT_TIMESTAMP,
                            refined_requirement = NULL,
                            raw_prompts = '[]'::jsonb
                        WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s
                        """,
                        (outcome_summary, task_id, agent_instance_id, mission_id),
                    )

                    table_pk_map = [
                        ("task_workspace_context", "context_id"),
                        ("task_solution_content", "solution_id"),
                        ("task_decision_records", "decision_id"),
                        ("task_todo_items", "todo_id"),
                        ("task_progress_content", "progress_id"),
                        ("task_discovery_records", "discovery_id"),
                        ("task_issue_records", "issue_id"),
                        ("task_artifact_records", "artifact_id"),
                        ("task_validation_records", "validation_id"),
                        ("task_external_state_records", "external_state_id"),
                    ]

                    deleted_count = 0
                    kept_count = 0

                    for table_name, pk_col in table_pk_map:
                        if keep_set:
                            cur.execute(
                                f"""
                                SELECT COUNT(*) FROM {table_name}
                                WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s
                                  AND {pk_col} = ANY(%s)
                                """,
                                (task_id, agent_instance_id, mission_id, list(keep_set)),
                            )
                            cnt_row = cur.fetchone()
                            if cnt_row:
                                kept_count += cnt_row.get("count") if isinstance(cnt_row, dict) else cnt_row[0]

                            cur.execute(
                                f"""
                                DELETE FROM {table_name}
                                WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s
                                  AND NOT ({pk_col} = ANY(%s))
                                """,
                                (task_id, agent_instance_id, mission_id, list(keep_set)),
                            )
                        else:
                            cur.execute(
                                f"""
                                DELETE FROM {table_name}
                                WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s
                                """,
                                (task_id, agent_instance_id, mission_id),
                            )
                        deleted_count += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

                    # 已释放的 task_agent_relationships
                    if keep_set:
                        cur.execute(
                            """
                            SELECT COUNT(*) FROM task_agent_relationships
                            WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s
                              AND relationship_id = ANY(%s)
                            """,
                            (task_id, agent_instance_id, mission_id, list(keep_set)),
                        )
                        cnt_row = cur.fetchone()
                        if cnt_row:
                            kept_count += cnt_row.get("count") if isinstance(cnt_row, dict) else cnt_row[0]

                        cur.execute(
                            """
                            DELETE FROM task_agent_relationships
                            WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s
                              AND status = 'released'
                              AND NOT (relationship_id = ANY(%s))
                            """,
                            (task_id, agent_instance_id, mission_id, list(keep_set)),
                        )
                    else:
                        cur.execute(
                            """
                            DELETE FROM task_agent_relationships
                            WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s
                              AND status = 'released'
                            """,
                            (task_id, agent_instance_id, mission_id),
                        )
                    deleted_count += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

                    # 对应 task_agent_messages
                    if keep_set:
                        cur.execute(
                            """
                            SELECT COUNT(*) FROM task_agent_messages
                            WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s
                              AND message_id = ANY(%s)
                            """,
                            (task_id, agent_instance_id, mission_id, list(keep_set)),
                        )
                        cnt_row = cur.fetchone()
                        if cnt_row:
                            kept_count += cnt_row.get("count") if isinstance(cnt_row, dict) else cnt_row[0]

                        cur.execute(
                            """
                            DELETE FROM task_agent_messages
                            WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s
                              AND NOT (message_id = ANY(%s))
                            """,
                            (task_id, agent_instance_id, mission_id, list(keep_set)),
                        )
                    else:
                        cur.execute(
                            """
                            DELETE FROM task_agent_messages
                            WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s
                            """,
                            (task_id, agent_instance_id, mission_id),
                        )
                    deleted_count += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

                conn.commit()

            return {
                "mission_id": mission_id,
                "deleted_count": deleted_count,
                "kept_count": kept_count,
                "message": "已完成任务卡片化裁剪，过程细节已安全压缩清理",
            }
        except Exception as exc:
            logger.error("prune_mission 执行失败: %s", exc)
            raise

    def delete_mission(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        mission_id: str,
        reason: str,
        reassign_to_mission_id: str | None = None,
    ) -> dict[str, Any]:
        """物理删除业务任务：支持将挂载的记录转移到目标任务，或直接级联物理删除。"""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT status, agent_instance_id, raw_prompts FROM agent_user_mission WHERE task_id = %s AND mission_id = %s",
                        (task_id, mission_id),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise ValueError(f"目标任务不存在: mission_id={mission_id}")
                    status = row.get("status") if isinstance(row, dict) else row[0]
                    owner_agent_id = row.get("agent_instance_id") if isinstance(row, dict) else (row[1] if len(row) > 1 else agent_instance_id)
                    src_raw_prompts = row.get("raw_prompts") if isinstance(row, dict) else (row[2] if len(row) > 2 else [])

                    if owner_agent_id != agent_instance_id:
                        raise ValueError(f"任务不属于目标 Agent: mission_id={mission_id}")

                    reassigned_count = 0
                    deleted_count = 0

                    if not reassign_to_mission_id:
                        if status not in ("completed", "abandoned"):
                            raise ValueError(f"未指定转移目标任务时，仅允许删除已闭环任务 (当前状态: {status})")

                        cur.execute(
                            """
                            SELECT 1 FROM task_agent_relationships
                            WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s AND status = 'unreleased'
                            LIMIT 1
                            """,
                            (task_id, agent_instance_id, mission_id),
                        )
                        if cur.fetchone():
                            raise ValueError("任务存在未释放的子 Agent，禁止删除")

                        cascade_tables = [
                            "task_workspace_context",
                            "task_solution_content",
                            "task_decision_records",
                            "task_todo_items",
                            "task_progress_content",
                            "task_discovery_records",
                            "task_issue_records",
                            "task_artifact_records",
                            "task_validation_records",
                            "task_external_state_records",
                            "task_agent_relationships",
                            "task_agent_messages",
                        ]
                        for table_name in cascade_tables:
                            cur.execute(
                                f"DELETE FROM {table_name} WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s",
                                (task_id, agent_instance_id, mission_id),
                            )
                            deleted_count += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

                        cur.execute(
                            "DELETE FROM agent_user_mission WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s",
                            (task_id, agent_instance_id, mission_id),
                        )
                        deleted_count += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                        conn.commit()
                        logger.info("已成功删除任务: mission_id=%s, reason=%s", mission_id, reason)
                        return {
                            "mission_id": mission_id,
                            "reassigned_count": 0,
                            "deleted_count": deleted_count,
                            "message": f"任务已安全删除 (理由: {reason})",
                        }
                    else:
                        if reassign_to_mission_id == mission_id:
                            raise ValueError("不能转移给任务自身")

                        _assert_mission_writable(cur, reassign_to_mission_id, agent_instance_id)

                        # raw_prompts 按时间顺序并入目标任务
                        cur.execute(
                            "SELECT raw_prompts FROM agent_user_mission WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s",
                            (task_id, agent_instance_id, reassign_to_mission_id),
                        )
                        dst_row = cur.fetchone()
                        dst_prompts = dst_row.get("raw_prompts") if isinstance(dst_row, dict) else (dst_row[0] if dst_row else [])
                        if not isinstance(dst_prompts, list):
                            dst_prompts = []
                        if not isinstance(src_raw_prompts, list):
                            src_raw_prompts = []
                        merged_prompts = dst_prompts + src_raw_prompts
                        cur.execute(
                            """
                            UPDATE agent_user_mission
                            SET raw_prompts = %s
                            WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s
                            """,
                            (_dump_json(merged_prompts), task_id, agent_instance_id, reassign_to_mission_id),
                        )

                        reassign_tables = [
                            "task_workspace_context",
                            "task_decision_records",
                            "task_todo_items",
                            "task_discovery_records",
                            "task_issue_records",
                            "task_artifact_records",
                            "task_validation_records",
                            "task_external_state_records",
                            "task_agent_relationships",
                            "task_agent_messages",
                        ]
                        for table_name in reassign_tables:
                            cur.execute(
                                f"""
                                UPDATE {table_name}
                                SET mission_id = %s
                                WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s
                                """,
                                (reassign_to_mission_id, task_id, agent_instance_id, mission_id),
                            )
                            reassigned_count += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

                        # solution/progress
                        cur.execute(
                            "SELECT 1 FROM task_solution_content WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s",
                            (task_id, agent_instance_id, reassign_to_mission_id),
                        )
                        if cur.fetchone():
                            cur.execute(
                                "DELETE FROM task_solution_content WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s",
                                (task_id, agent_instance_id, mission_id),
                            )
                            deleted_count += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                        else:
                            cur.execute(
                                "UPDATE task_solution_content SET mission_id = %s WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s",
                                (reassign_to_mission_id, task_id, agent_instance_id, mission_id),
                            )
                            reassigned_count += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

                        cur.execute(
                            "SELECT 1 FROM task_progress_content WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s",
                            (task_id, agent_instance_id, reassign_to_mission_id),
                        )
                        if cur.fetchone():
                            cur.execute(
                                "DELETE FROM task_progress_content WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s",
                                (task_id, agent_instance_id, mission_id),
                            )
                            deleted_count += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                        else:
                            cur.execute(
                                "UPDATE task_progress_content SET mission_id = %s WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s",
                                (reassign_to_mission_id, task_id, agent_instance_id, mission_id),
                            )
                            reassigned_count += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

                        cur.execute(
                            "DELETE FROM agent_user_mission WHERE task_id = %s AND agent_instance_id = %s AND mission_id = %s",
                            (task_id, agent_instance_id, mission_id),
                        )
                        deleted_count += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

                        conn.commit()
                        logger.info("已成功删除任务: mission_id=%s, reason=%s", mission_id, reason)
                        return {
                            "mission_id": mission_id,
                            "reassigned_count": reassigned_count,
                            "deleted_count": deleted_count,
                            "message": f"任务已安全删除并转移记录至 {reassign_to_mission_id} (理由: {reason})",
                        }
        except Exception as exc:
            logger.error("delete_mission 执行失败: %s", exc)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_record_mission_writable(cur, "task_workspace_context", "context_id", context_id, task_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_record_mission_writable(cur, "task_workspace_context", "context_id", context_id, task_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
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
                            task_id = EXCLUDED.task_id,
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
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
            if row:
                return row.get("solution_id") if isinstance(row, dict) else row[0]
            return f"sol-superseded-{task_id}"
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
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
        superseded_reason: str | None = None,
    ) -> bool:
        """软废弃决策记录。"""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_record_mission_writable(cur, "task_decision_records", "decision_id", decision_id, task_id, agent_instance_id)
                    if superseded_reason is not None:
                        cur.execute(
                            """
                            UPDATE task_decision_records
                            SET decision_status = 'superseded',
                                superseded_reason = %s
                            WHERE task_id = %s AND decision_id = %s AND agent_instance_id = %s
                            """,
                            (superseded_reason, task_id, decision_id, agent_instance_id),
                        )
                    else:
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_record_mission_writable(cur, "task_decision_records", "decision_id", decision_id, task_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
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

        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_record_mission_writable(cur, "task_todo_items", "todo_id", todo_id, task_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_record_mission_writable(cur, "task_todo_items", "todo_id", todo_id, task_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_record_mission_writable(cur, "task_todo_items", "todo_id", todo_id, task_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
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
                            task_id = EXCLUDED.task_id,
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_record_mission_writable(cur, "task_discovery_records", "discovery_id", discovery_id, task_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_record_mission_writable(cur, "task_discovery_records", "discovery_id", discovery_id, task_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_record_mission_writable(cur, "task_issue_records", "issue_id", issue_id, task_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_record_mission_writable(cur, "task_issue_records", "issue_id", issue_id, task_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
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
        """删除非代码文件的通用交付物 (严格限定 type='other')。"""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM task_artifact_records
                        WHERE task_id = %s AND artifact_id = %s AND agent_instance_id = %s AND type = 'other'
                        """,
                        (task_id, artifact_id, agent_instance_id),
                    )
                conn.commit()
                return cur.rowcount > 0
        except Exception as exc:
            logger.error("delete_artifact_other 执行失败: %s", exc)
            raise

    def delete_task_artifacts(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
        artifact_ids: list[str],
    ) -> int:
        """根据主键批量物理删除产物记录。"""
        if not artifact_ids:
            return 0
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT DISTINCT mission_id
                        FROM task_artifact_records
                        WHERE task_id = %s AND agent_instance_id = %s AND artifact_id = ANY(%s) AND mission_id IS NOT NULL
                        """,
                        (task_id, agent_instance_id, list(artifact_ids)),
                    )
                    for r in cur.fetchall():
                        mid = r.get("mission_id") if isinstance(r, dict) else r[0]
                        if mid:
                            _assert_mission_writable(cur, mid, agent_instance_id)
                    cur.execute(
                        """
                        DELETE FROM task_artifact_records
                        WHERE task_id = %s AND agent_instance_id = %s AND artifact_id = ANY(%s)
                        """,
                        (task_id, agent_instance_id, list(artifact_ids)),
                    )
                    deleted_count = cur.rowcount
                conn.commit()
            return deleted_count
        except Exception as exc:
            logger.error("delete_task_artifacts 删除失败: %s", exc)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_record_mission_writable(cur, "task_validation_records", "validation_id", validation_id, task_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
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
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_record_mission_writable(cur, "task_external_state_records", "external_state_id", external_state_id, task_id, agent_instance_id)
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
        if resource_type not in ("artifact", "agent_relationship"):
            raise ValueError(f"无效的 resource_type: {resource_type}，仅支持 'artifact' 或 'agent_relationship'")

        bound_count = 0
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    _assert_mission_writable(cur, mission_id, agent_instance_id)
                    if resource_type == "artifact":
                        cur.execute(
                            """
                            UPDATE task_artifact_records
                            SET mission_id = %s
                            WHERE task_id = %s AND agent_instance_id = %s AND artifact_id = ANY(%s)
                            """,
                            (mission_id, task_id, agent_instance_id, resource_ids),
                        )
                        bound_count = cur.rowcount
                    elif resource_type == "agent_relationship":
                        cur.execute(
                            """
                            UPDATE task_agent_relationships
                            SET mission_id = %s
                            WHERE task_id = %s AND agent_instance_id = %s AND relationship_id = ANY(%s)
                            """,
                            (mission_id, task_id, agent_instance_id, resource_ids),
                        )
                        bound_count = cur.rowcount
                        # 级联更新关联的目标子 Agent 专线消息（双向）
                        cur.execute(
                            """
                            UPDATE task_agent_messages
                            SET mission_id = %s
                            WHERE task_id = %s
                              AND (
                                  (agent_instance_id = %s AND target_agent_instance_id IN (
                                      SELECT target_agent_instance_id
                                      FROM task_agent_relationships
                                      WHERE task_id = %s AND agent_instance_id = %s AND relationship_id = ANY(%s)
                                  ))
                                  OR
                                  (target_agent_instance_id = %s AND agent_instance_id IN (
                                      SELECT target_agent_instance_id
                                      FROM task_agent_relationships
                                      WHERE task_id = %s AND agent_instance_id = %s AND relationship_id = ANY(%s)
                                  ))
                              )
                            """,
                            (
                                mission_id,
                                task_id,
                                agent_instance_id,
                                task_id,
                                agent_instance_id,
                                resource_ids,
                                agent_instance_id,
                                task_id,
                                agent_instance_id,
                                resource_ids,
                            ),
                        )
                conn.commit()
            return bound_count
        except (ValueError, PermissionError, TaskNoteStorageUnavailableError):
            raise
        except Exception as exc:
            logger.error("bind_resources_to_mission 绑定失败: %s", exc)
            raise TaskNoteStorageUnavailableError(f"bind_resources_to_mission 失败: {exc}") from exc

    def get_command_lines_by_ids(
        self,
        *,
        task_id: str,
        command_ids: list[str],
    ) -> dict[str, str]:
        """按 command_id 列表批量反查对应的 命令行指令 (command_line)。"""
        if not command_ids:
            return {}
        try:
            with self._get_connection() as conn:
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
        try:
            with self._get_connection() as conn:
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

    # ── 13. 观察断点与快照状态 (task_note_watermark) ─────────────────

    def get_watermark(
        self,
        *,
        agent_instance_id: str,
        task_id: str | None = None,
    ) -> dict[str, Any] | None:
        """获取指定业务 Agent 实例的最新观察断点记录。"""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT agent_instance_id, task_id, covered_until_seq,
                               covered_until_message_id, note_version,
                               inherited_processed, updated_at
                        FROM task_note_watermark
                        WHERE agent_instance_id = %s
                        """,
                        (agent_instance_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
                    if isinstance(row, dict):
                        return _to_json_compatible(dict(row))
                    if row and len(row) >= 7:
                        return {
                            "agent_instance_id": row[0],
                            "task_id": row[1],
                            "covered_until_seq": row[2],
                            "covered_until_message_id": row[3],
                            "note_version": row[4],
                            "inherited_processed": row[5],
                            "updated_at": row[6].isoformat() if hasattr(row[6], "isoformat") else str(row[6]) if row[6] else None,
                        }
                    return None
        except Exception as exc:
            logger.error("get_watermark 查询失败: %s", exc)
            return None

    def update_watermark(
        self,
        *,
        agent_instance_id: str,
        task_id: str,
        covered_until_seq: int,
        covered_until_message_id: str | None = None,
        note_version_inc: bool = True,
        inherited_processed: bool | None = None,
    ) -> dict[str, Any]:
        """原子插入或推进观察断点水位。"""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO task_note_watermark (
                            agent_instance_id, task_id, covered_until_seq,
                            covered_until_message_id, note_version,
                            inherited_processed, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (agent_instance_id) DO UPDATE
                        SET covered_until_seq = EXCLUDED.covered_until_seq,
                            covered_until_message_id = COALESCE(EXCLUDED.covered_until_message_id, task_note_watermark.covered_until_message_id),
                            note_version = CASE WHEN %s THEN task_note_watermark.note_version + 1 ELSE task_note_watermark.note_version END,
                            inherited_processed = CASE WHEN %s IS NOT NULL THEN %s ELSE task_note_watermark.inherited_processed END,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING agent_instance_id, task_id, covered_until_seq, covered_until_message_id, note_version, inherited_processed, updated_at
                        """,
                        (
                            agent_instance_id,
                            task_id,
                            covered_until_seq,
                            covered_until_message_id,
                            1 if note_version_inc else 0,
                            bool(inherited_processed),
                            note_version_inc,
                            inherited_processed,
                            inherited_processed,
                        ),
                    )
                    row = cur.fetchone()
                conn.commit()
                if isinstance(row, dict):
                    return dict(row)
                if row and len(row) >= 7:
                    return {
                        "agent_instance_id": row[0],
                        "task_id": row[1],
                        "covered_until_seq": row[2],
                        "covered_until_message_id": row[3],
                        "note_version": row[4],
                        "inherited_processed": row[5],
                        "updated_at": row[6].isoformat() if hasattr(row[6], "isoformat") else str(row[6]) if row[6] else None,
                    }
                return {
                    "agent_instance_id": agent_instance_id,
                    "task_id": task_id,
                    "covered_until_seq": covered_until_seq,
                    "covered_until_message_id": covered_until_message_id,
                    "note_version": 1 if note_version_inc else 0,
                    "inherited_processed": bool(inherited_processed),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
        except Exception as exc:
            logger.error("update_watermark 写入失败: %s", exc)
            raise

    def set_inherited_processed(
        self,
        *,
        agent_instance_id: str,
        task_id: str,
    ) -> bool:
        """标记子 Agent 继承段已在首次观察中处理完成。"""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO task_note_watermark (
                            agent_instance_id, task_id, inherited_processed, updated_at
                        ) VALUES (
                            %s, %s, TRUE, CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (agent_instance_id) DO UPDATE
                        SET inherited_processed = TRUE,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (agent_instance_id, task_id),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("set_inherited_processed 失败: %s", exc)
            raise

    def reset_watermark(
        self,
        *,
        agent_instance_id: str,
        task_id: str | None = None,
    ) -> bool:
        """重置指定 Agent 实例的观察断点水位为初始状态（用于会话重置/清空时同步）。"""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    if task_id:
                        cur.execute(
                            """
                            UPDATE task_note_watermark
                            SET covered_until_seq = 0,
                                covered_until_message_id = NULL,
                                note_version = 0,
                                inherited_processed = FALSE,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE agent_instance_id = %s AND task_id = %s
                            """,
                            (agent_instance_id, task_id),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE task_note_watermark
                            SET covered_until_seq = 0,
                                covered_until_message_id = NULL,
                                note_version = 0,
                                inherited_processed = FALSE,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE agent_instance_id = %s
                            """,
                            (agent_instance_id,),
                        )
                conn.commit()
            return True
        except Exception as exc:
            logger.error("reset_watermark 失败: %s", exc)
            return False

    def get_note_snapshot(
        self,
        *,
        task_id: str,
        agent_instance_id: str,
    ) -> dict[str, Any]:
        """获取当前业务 Agent 实例在当前 Task 下的完整 Note 结构化快照。"""
        snapshot: dict[str, Any] = {
            "watermark": self.get_watermark(agent_instance_id=agent_instance_id),
            "missions": [],
            "unbound_artifacts": [],
            "relationships": [],
            "agent_messages": [],
        }
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT mission_id, task_id, scene_id, agent_instance_id,
                               title, raw_prompts, refined_requirement, status,
                               outcome_summary, pruned_at, sort_order, created_at
                        FROM agent_user_mission
                        WHERE task_id = %s AND agent_instance_id = %s
                        ORDER BY sort_order ASC, created_at ASC
                        """,
                        (task_id, agent_instance_id),
                    )
                    mission_rows = cur.fetchall()
                    missions_by_id: dict[str, dict[str, Any]] = {}
                    for row in mission_rows:
                        m_data = dict(row) if isinstance(row, dict) else {
                            "mission_id": row[0],
                            "task_id": row[1],
                            "scene_id": row[2],
                            "agent_instance_id": row[3],
                            "title": row[4],
                            "raw_prompts": row[5],
                            "refined_requirement": row[6],
                            "status": row[7],
                            "outcome_summary": row[8],
                            "pruned_at": row[9].isoformat() if row[9] else None,
                            "sort_order": row[10],
                            "created_at": row[11].isoformat() if row[11] else None,
                        }
                        m_data["workspace_contexts"] = []
                        m_data["solution"] = None
                        m_data["decisions"] = []
                        m_data["todos"] = []
                        m_data["progress"] = None
                        m_data["discoveries"] = []
                        m_data["issues"] = []
                        m_data["artifacts"] = []
                        m_data["validations"] = []
                        m_data["external_states"] = []
                        missions_by_id[m_data["mission_id"]] = m_data

                    mission_ids = list(missions_by_id.keys())
                    if mission_ids:
                        cur.execute(
                            "SELECT * FROM task_workspace_context WHERE task_id = %s AND agent_instance_id = %s AND mission_id = ANY(%s) ORDER BY created_at ASC",
                            (task_id, agent_instance_id, mission_ids),
                        )
                        for r in cur.fetchall():
                            d = dict(r)
                            if d["mission_id"] in missions_by_id:
                                missions_by_id[d["mission_id"]]["workspace_contexts"].append(d)

                        cur.execute(
                            "SELECT * FROM task_solution_content WHERE task_id = %s AND agent_instance_id = %s AND mission_id = ANY(%s)",
                            (task_id, agent_instance_id, mission_ids),
                        )
                        for r in cur.fetchall():
                            d = dict(r)
                            if d["mission_id"] in missions_by_id:
                                missions_by_id[d["mission_id"]]["solution"] = d

                        cur.execute(
                            "SELECT * FROM task_decision_records WHERE task_id = %s AND agent_instance_id = %s AND mission_id = ANY(%s) ORDER BY created_at ASC",
                            (task_id, agent_instance_id, mission_ids),
                        )
                        for r in cur.fetchall():
                            d = dict(r)
                            if d["mission_id"] in missions_by_id:
                                missions_by_id[d["mission_id"]]["decisions"].append(d)

                        cur.execute(
                            "SELECT * FROM task_todo_items WHERE task_id = %s AND agent_instance_id = %s AND mission_id = ANY(%s) ORDER BY priority ASC, created_at ASC",
                            (task_id, agent_instance_id, mission_ids),
                        )
                        for r in cur.fetchall():
                            d = dict(r)
                            if d["mission_id"] in missions_by_id:
                                missions_by_id[d["mission_id"]]["todos"].append(d)

                        cur.execute(
                            "SELECT * FROM task_progress_content WHERE task_id = %s AND agent_instance_id = %s AND mission_id = ANY(%s)",
                            (task_id, agent_instance_id, mission_ids),
                        )
                        for r in cur.fetchall():
                            d = dict(r)
                            if d["mission_id"] in missions_by_id:
                                missions_by_id[d["mission_id"]]["progress"] = d

                        cur.execute(
                            "SELECT * FROM task_discovery_records WHERE task_id = %s AND agent_instance_id = %s AND mission_id = ANY(%s) ORDER BY created_at ASC",
                            (task_id, agent_instance_id, mission_ids),
                        )
                        for r in cur.fetchall():
                            d = dict(r)
                            if d["mission_id"] in missions_by_id:
                                missions_by_id[d["mission_id"]]["discoveries"].append(d)

                        cur.execute(
                            "SELECT * FROM task_issue_records WHERE task_id = %s AND agent_instance_id = %s AND mission_id = ANY(%s) ORDER BY created_at ASC",
                            (task_id, agent_instance_id, mission_ids),
                        )
                        for r in cur.fetchall():
                            d = dict(r)
                            if d["mission_id"] in missions_by_id:
                                missions_by_id[d["mission_id"]]["issues"].append(d)

                        cur.execute(
                            "SELECT * FROM task_artifact_records WHERE task_id = %s AND agent_instance_id = %s AND mission_id = ANY(%s) ORDER BY created_at ASC",
                            (task_id, agent_instance_id, mission_ids),
                        )
                        for r in cur.fetchall():
                            d = dict(r)
                            if d["mission_id"] in missions_by_id:
                                missions_by_id[d["mission_id"]]["artifacts"].append(d)

                        cur.execute(
                            "SELECT * FROM task_validation_records WHERE task_id = %s AND agent_instance_id = %s AND mission_id = ANY(%s) ORDER BY created_at ASC",
                            (task_id, agent_instance_id, mission_ids),
                        )
                        for r in cur.fetchall():
                            d = dict(r)
                            if d["mission_id"] in missions_by_id:
                                missions_by_id[d["mission_id"]]["validations"].append(d)

                        cur.execute(
                            "SELECT * FROM task_external_state_records WHERE task_id = %s AND agent_instance_id = %s AND mission_id = ANY(%s) ORDER BY created_at ASC",
                            (task_id, agent_instance_id, mission_ids),
                        )
                        for r in cur.fetchall():
                            d = dict(r)
                            if d["mission_id"] in missions_by_id:
                                missions_by_id[d["mission_id"]]["external_states"].append(d)

                    snapshot["missions"] = list(missions_by_id.values())

                    cur.execute(
                        "SELECT * FROM task_artifact_records WHERE task_id = %s AND agent_instance_id = %s AND mission_id IS NULL ORDER BY created_at ASC",
                        (task_id, agent_instance_id),
                    )
                    snapshot["unbound_artifacts"] = [dict(r) for r in cur.fetchall()]

                    cur.execute(
                        "SELECT * FROM task_agent_relationships WHERE task_id = %s AND agent_instance_id = %s ORDER BY created_at ASC",
                        (task_id, agent_instance_id),
                    )
                    snapshot["relationships"] = [dict(r) for r in cur.fetchall()]

                    cur.execute(
                        "SELECT * FROM task_agent_messages WHERE task_id = %s AND agent_instance_id = %s ORDER BY created_at ASC",
                        (task_id, agent_instance_id),
                    )
                    snapshot["agent_messages"] = [dict(r) for r in cur.fetchall()]

            return _to_json_compatible(snapshot)
        except Exception as exc:
            # 读取失败必须抛错，不得返回空骨架：替换路径据此不替换，观察路径据此按失败处理（TASK_NOTE-19）
            logger.error("get_note_snapshot 查询失败: %s", exc)
            raise TaskNoteStorageUnavailableError(f"读取 Note 快照失败: {exc}") from exc

    def cleanup_agent_instance_note(
        self,
        task_id: str,
        agent_instance_id: str,
    ) -> int:
        """物理清除指定子 Agent 实例的全部 Note 记录（含高频辅助表 task_command_records 与水位记录）。

        用于非 Root Agent 调用 end_task 完成会话时的垃圾回收。
        """
        if not task_id or not agent_instance_id:
            return 0

        total_deleted = 0
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    for table in ALL_NOTE_TABLES:
                        if table == "task_note_watermark":
                            cur.execute(
                                "DELETE FROM task_note_watermark WHERE task_id = %s AND agent_instance_id = %s",
                                (task_id, agent_instance_id),
                            )
                        else:
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
        task_id: str,
    ) -> int:
        """物理清除指定 Task 下所有 Agent 实例的全部 Note 记录（含高频辅助表 task_command_records 与水位记录）。

        用于 Root Agent 调用 end_task 结束全任务时的垃圾回收。
        """
        if not task_id:
            return 0

        total_deleted = 0
        try:
            with self._get_connection() as conn:
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
    "agent_user_mission",
    "task_solution_content",
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
    "task_note_watermark",
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
