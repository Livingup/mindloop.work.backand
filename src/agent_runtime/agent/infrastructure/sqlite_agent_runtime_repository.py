# -*- coding: utf-8 -*-
"""Agent 任务与实例运行态 SQLite 仓储实现。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from agent_runtime.agent.agent_instance import (
    AGENT_INSTANCE_STATUS_READY,
    AGENT_INSTANCE_STATUS_RUNNING,
    AgentInstance,
    PendingAgentUserMessage,
    PendingUserMessageAttachment,
)
from agent_runtime.agent.agent_task import (
    AgentParentChildRelation,
    AgentTask,
)


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id                TEXT PRIMARY KEY,
    root_agent_instance_id TEXT NOT NULL,
    agent_instance_ids     TEXT NOT NULL,
    status                 TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    started_at             TEXT,
    paused_at              TEXT,
    finished_at            TEXT,
    failed_at              TEXT,
    failure_message        TEXT,
    scene_id               TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status);
-- idx_agent_tasks_scene 索引依赖 scene_id 列，统一由 _migrate_schema 在确认列存在后创建，
-- 这里不放在初始 schema，避免历史 DB（agent_tasks 表先于本次新增已存在）执行 schema 时
-- 报 "no such column: scene_id"。

CREATE TABLE IF NOT EXISTS agent_instances (
    instance_id          TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL,
    thread_id            TEXT NOT NULL,
    current_working_directory TEXT NOT NULL,
    agent_definition_id  TEXT NOT NULL,
    llm_provider_id      TEXT NOT NULL,
    llm_model_binding_id TEXT NOT NULL,
    llm_policy_group_id  TEXT,
    conversation_id      TEXT,
    trace_id             TEXT NOT NULL,
    status               TEXT NOT NULL,
    pending_user_messages_json TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    started_at           TEXT,
    paused_at            TEXT,
    finished_at          TEXT,
    failed_at            TEXT,
    failure_message      TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_instances_task   ON agent_instances(task_id);
CREATE INDEX IF NOT EXISTS idx_agent_instances_thread ON agent_instances(thread_id);
CREATE INDEX IF NOT EXISTS idx_agent_instances_status ON agent_instances(status);

CREATE TABLE IF NOT EXISTS agent_parent_child_relations (
    relation_id             TEXT PRIMARY KEY,
    task_id                 TEXT NOT NULL,
    parent_instance_id      TEXT NOT NULL,
    child_instance_id       TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    released_at             TEXT,
    release_reason          TEXT,
    released_by_instance_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_relations_task   ON agent_parent_child_relations(task_id);
CREATE INDEX IF NOT EXISTS idx_agent_relations_parent ON agent_parent_child_relations(parent_instance_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_relations_child_once
    ON agent_parent_child_relations(task_id, child_instance_id);
"""


class SqliteAgentRuntimeRepository:
    """Agent 运行态 SQLite 仓储。

    该仓储把 task、Agent 实例和 task 内父子关系树写入同一个 SQLite 数据库。
    SQLite 开启 WAL，适合本机 FastAPI 多 worker / 多 Python 进程共享运行态。
    仓储方法内部使用进程内 RLock 保护同一连接上的并发访问；跨进程并发由 SQLite 事务保障。
    """

    def __init__(self, data_dir: str | Path) -> None:
        # Agent 运行态数据目录。这里保存 agent_runtime.db，不保存用户静态定义或 LLM 注册数据。
        self._data_dir = Path(data_dir).expanduser()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        db_path = self._data_dir / "agent_runtime.db"

        # check_same_thread=False 允许同一进程内不同执行线程复用仓储连接；实际操作由 _lock 串行化。
        self._conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate_schema()
        self._conn.commit()
        self._lock = RLock()

    def close(self) -> None:
        """关闭底层 SQLite 连接。"""
        with self._lock:
            self._conn.close()

    def reset_runtime_state(self) -> int:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT task_id FROM agent_instances WHERE status = ?",
                (AGENT_INSTANCE_STATUS_RUNNING,),
            ).fetchall()
            affected_task_ids = [str(row[0]) for row in rows]
            if not affected_task_ids:
                return 0
            now = _datetime_to_text(datetime.now(timezone.utc))
            placeholders = ",".join("?" for _ in affected_task_ids)
            instance_message = "AgentRuntime restarted; running execution carrier state was recovered."
            instance_result = self._conn.execute(
                f"""
                UPDATE agent_instances
                SET status = ?,
                    updated_at = ?,
                    paused_at = ?,
                    failed_at = ?,
                    failure_message = ?
                WHERE task_id IN ({placeholders})
                """,
                (
                    AGENT_INSTANCE_STATUS_READY,
                    now,
                    now,
                    now,
                    instance_message,
                    *affected_task_ids,
                ),
            )
            task_result = self._conn.execute(
                f"""
                UPDATE agent_tasks
                SET updated_at = ?,
                    paused_at = ?
                WHERE task_id IN ({placeholders})
                """,
                (
                    now,
                    now,
                    *affected_task_ids,
                ),
            )
            self._conn.commit()
            return int(instance_result.rowcount or 0) + int(task_result.rowcount or 0)

    def save_task(self, task: AgentTask) -> None:
        """保存或覆盖任务运行态，并同步该任务的父子关系树。"""
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO agent_tasks (
                    task_id, root_agent_instance_id, agent_instance_ids, status,
                    created_at, updated_at, started_at, paused_at, finished_at,
                    failed_at, failure_message, scene_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.root_agent_instance_id,
                    json.dumps(list(task.agent_instance_ids), ensure_ascii=False),
                    task.status,
                    _datetime_to_text(task.created_at),
                    _datetime_to_text(task.updated_at),
                    _optional_datetime_to_text(task.started_at),
                    _optional_datetime_to_text(task.paused_at),
                    _optional_datetime_to_text(task.finished_at),
                    _optional_datetime_to_text(task.failed_at),
                    task.failure_message,
                    task.scene_id,
                ),
            )
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO agent_parent_child_relations (
                    relation_id, task_id, parent_instance_id, child_instance_id, created_at,
                    released_at, release_reason, released_by_instance_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        relation.relation_id,
                        relation.task_id,
                        relation.parent_instance_id,
                        relation.child_instance_id,
                        _datetime_to_text(relation.created_at),
                        _optional_datetime_to_text(relation.released_at),
                        relation.release_reason,
                        relation.released_by_instance_id,
                    )
                    for relation in task.parent_child_relations
                ],
            )
            self._conn.commit()

    def get_task(self, task_id: str) -> AgentTask | None:
        """按 task_id 获取任务运行态。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT task_id, root_agent_instance_id, agent_instance_ids, status,
                       created_at, updated_at, started_at, paused_at, finished_at,
                       failed_at, failure_message, scene_id
                FROM agent_tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            relations = self._load_task_relations(task_id)
            instance_ids = self._load_task_instance_ids(
                task_id=task_id,
                root_agent_instance_id=row[1],
                stored_instance_ids=tuple(json.loads(row[2])),
                relations=relations,
            )
            live_relations = self._filter_task_relations_for_live_instances(relations, instance_ids)
            return _row_to_task(row, live_relations, instance_ids)

    def list_tasks(self) -> list[AgentTask]:
        """列出当前仓储中的全部任务。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT task_id, root_agent_instance_id, agent_instance_ids, status,
                       created_at, updated_at, started_at, paused_at, finished_at,
                       failed_at, failure_message, scene_id
                FROM agent_tasks ORDER BY created_at ASC
                """
            ).fetchall()
            tasks: list[AgentTask] = []
            for row in rows:
                relations = self._load_task_relations(row[0])
                instance_ids = self._load_task_instance_ids(
                    task_id=row[0],
                    root_agent_instance_id=row[1],
                    stored_instance_ids=tuple(json.loads(row[2])),
                    relations=relations,
                )
                live_relations = self._filter_task_relations_for_live_instances(relations, instance_ids)
                tasks.append(_row_to_task(row, live_relations, instance_ids))
            return tasks

    def save_agent_instance(self, instance: AgentInstance) -> None:
        """保存或覆盖 Agent 实例运行态。"""
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO agent_instances (
                    instance_id, task_id, thread_id, current_working_directory, agent_definition_id,
                    llm_provider_id, llm_model_binding_id, llm_policy_group_id,
                    conversation_id, trace_id,
                    status, pending_user_messages_json, created_at, updated_at, started_at, paused_at,
                    finished_at, failed_at, failure_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instance.instance_id,
                    instance.task_id,
                    instance.thread_id,
                    instance.current_working_directory,
                    instance.agent_definition_id,
                    instance.llm_provider_id,
                    instance.llm_model_binding_id,
                    instance.llm_policy_group_id,
                    instance.conversation_id,
                    instance.trace_id,
                    instance.status,
                    _pending_user_messages_to_json(instance.pending_user_messages),
                    _datetime_to_text(instance.created_at),
                    _datetime_to_text(instance.updated_at),
                    _optional_datetime_to_text(instance.started_at),
                    _optional_datetime_to_text(instance.paused_at),
                    _optional_datetime_to_text(instance.finished_at),
                    _optional_datetime_to_text(instance.failed_at),
                    instance.failure_message,
                ),
            )
            self._conn.commit()

    def get_agent_instance(self, instance_id: str) -> AgentInstance | None:
        """按 instance_id 获取 Agent 实例运行态。"""
        with self._lock:
            row = self._conn.execute(
                _INSTANCE_SELECT_SQL + " WHERE instance_id = ?",
                (instance_id,),
            ).fetchone()
            return _row_to_instance(row) if row else None

    def delete_agent_instance(self, instance_id: str) -> None:
        """从运行态仓储中删除一次性 Agent 实例。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM agent_instances WHERE instance_id = ?",
                (instance_id,),
            )
            self._conn.commit()

    def list_agent_instances_by_task(self, task_id: str) -> list[AgentInstance]:
        """列出某个任务下的全部 Agent 实例。"""
        with self._lock:
            rows = self._conn.execute(
                _INSTANCE_SELECT_SQL + " WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
            return [_row_to_instance(row) for row in rows]

    def list_agent_instances_by_thread(self, thread_id: str) -> list[AgentInstance]:
        """列出某个线程下的全部 Agent 实例。"""
        with self._lock:
            rows = self._conn.execute(
                _INSTANCE_SELECT_SQL + " WHERE thread_id = ? ORDER BY created_at ASC",
                (thread_id,),
            ).fetchall()
            return [_row_to_instance(row) for row in rows]

    def list_tasks_by_scene_id(self, scene_id: str) -> list[AgentTask]:
        """列出指定 scene 下的全部任务，按 created_at 倒序（最近在前）。

        利用 idx_agent_tasks_scene 索引；若 scene 下无任务返回空列表。
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT task_id, root_agent_instance_id, agent_instance_ids, status,
                       created_at, updated_at, started_at, paused_at, finished_at,
                       failed_at, failure_message, scene_id
                FROM agent_tasks WHERE scene_id = ? ORDER BY created_at DESC
                """,
                (scene_id,),
            ).fetchall()
            tasks: list[AgentTask] = []
            for row in rows:
                relations = self._load_task_relations(row[0])
                instance_ids = self._load_task_instance_ids(
                    task_id=row[0],
                    root_agent_instance_id=row[1],
                    stored_instance_ids=tuple(json.loads(row[2])),
                    relations=relations,
                )
                live_relations = self._filter_task_relations_for_live_instances(relations, instance_ids)
                tasks.append(_row_to_task(row, live_relations, instance_ids))
            return tasks

    def contains_task(self, task_id: str) -> bool:
        """判断任务是否存在。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM agent_tasks WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone()
            return row is not None

    def contains_agent_instance(self, instance_id: str) -> bool:
        """判断 Agent 实例是否存在。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM agent_instances WHERE instance_id = ? LIMIT 1",
                (instance_id,),
            ).fetchone()
            return row is not None

    def _migrate_schema(self) -> None:
        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(agent_instances)").fetchall()
        }
        if "current_working_directory" not in columns:
            self._conn.execute(
                "ALTER TABLE agent_instances "
                "ADD COLUMN current_working_directory TEXT NOT NULL "
                "DEFAULT '__legacy_missing_current_working_directory__'"
            )
        if "llm_policy_group_id" not in columns:
            self._conn.execute(
                "ALTER TABLE agent_instances "
                "ADD COLUMN llm_policy_group_id TEXT"
            )
        if "pending_user_messages_json" not in columns:
            self._conn.execute(
                "ALTER TABLE agent_instances "
                "ADD COLUMN pending_user_messages_json TEXT NOT NULL DEFAULT '[]'"
            )
        # 读取 agent_tasks 表列集，补齐以往版本未包含的 scene_id 列（允许 NULL，表示旧任务未挂到场景）。
        task_columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(agent_tasks)").fetchall()
        }
        if "scene_id" not in task_columns:
            self._conn.execute("ALTER TABLE agent_tasks ADD COLUMN scene_id TEXT")
        # 列存在后再确保索引创建成功（IF NOT EXISTS 保证幂等）。
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_tasks_scene ON agent_tasks(scene_id)"
        )
        relation_columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(agent_parent_child_relations)").fetchall()
        }
        if "released_at" not in relation_columns:
            self._conn.execute(
                "ALTER TABLE agent_parent_child_relations "
                "ADD COLUMN released_at TEXT"
            )
        if "release_reason" not in relation_columns:
            self._conn.execute(
                "ALTER TABLE agent_parent_child_relations "
                "ADD COLUMN release_reason TEXT"
            )
        if "released_by_instance_id" not in relation_columns:
            self._conn.execute(
                "ALTER TABLE agent_parent_child_relations "
                "ADD COLUMN released_by_instance_id TEXT"
            )

    def _load_task_relations(self, task_id: str) -> tuple[AgentParentChildRelation, ...]:
        """加载指定 task 的父子关系树边集合。"""
        rows = self._conn.execute(
            """
            SELECT relation_id, task_id, parent_instance_id, child_instance_id, created_at,
                   released_at, release_reason, released_by_instance_id
            FROM agent_parent_child_relations
            WHERE task_id = ?
            ORDER BY created_at ASC
            """,
            (task_id,),
        ).fetchall()
        return tuple(
            AgentParentChildRelation(
                relation_id=row[0],
                task_id=row[1],
                parent_instance_id=row[2],
                child_instance_id=row[3],
                created_at=_text_to_datetime(row[4]),
                released_at=_optional_text_to_datetime(row[5]),
                release_reason=row[6],
                released_by_instance_id=row[7],
            )
            for row in rows
        )


    def _load_task_instance_ids(
        self,
        *,
        task_id: str,
        root_agent_instance_id: str,
        stored_instance_ids: tuple[str, ...],
        relations: tuple[AgentParentChildRelation, ...],
    ) -> tuple[str, ...]:
        """从多个事实来源合并 task 内实例 ID。

        agent_tasks.agent_instance_ids 是聚合快照；在多进程并发追加子 Agent 时可能被最后一次写入覆盖。
        因此 SQLite 仓储读取时还会合并 agent_instances 表和父子关系表，保证查询视图尽量反映所有已提交事实。
        """
        ordered: list[str] = []

        def add(instance_id: str) -> None:
            if instance_id not in ordered:
                ordered.append(instance_id)

        add(root_agent_instance_id)
        for instance_id in stored_instance_ids:
            add(instance_id)
        rows = self._conn.execute(
            "SELECT instance_id FROM agent_instances WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        ).fetchall()
        for row in rows:
            add(row[0])
        existing_ids = set(ordered)
        rows = self._conn.execute(
            "SELECT instance_id FROM agent_instances WHERE instance_id IN (%s)"
            % ",".join("?" for _ in ordered),
            tuple(ordered),
        ).fetchall() if ordered else []
        existing_ids = {row[0] for row in rows}
        return tuple(instance_id for instance_id in ordered if instance_id in existing_ids)

    def _filter_task_relations_for_live_instances(
        self,
        relations: tuple[AgentParentChildRelation, ...],
        instance_ids: tuple[str, ...],
    ) -> tuple[AgentParentChildRelation, ...]:
        """过滤 released 后 runtime instance 已删除的历史关系，保持 AgentTask 活跃关系树可重建。"""
        live_instance_ids = set(instance_ids)
        return tuple(
            relation
            for relation in relations
            if not relation.is_released
            or (
                relation.parent_instance_id in live_instance_ids
                and relation.child_instance_id in live_instance_ids
            )
        )


_INSTANCE_SELECT_SQL = """
SELECT instance_id, task_id, thread_id, current_working_directory, agent_definition_id,
       llm_provider_id, llm_model_binding_id, llm_policy_group_id,
       conversation_id, trace_id,
       status, pending_user_messages_json, created_at, updated_at, started_at, paused_at,
       finished_at, failed_at, failure_message
FROM agent_instances
"""


def _row_to_task(
    row: sqlite3.Row | tuple,
    relations: tuple[AgentParentChildRelation, ...],
    instance_ids: tuple[str, ...],
) -> AgentTask:
    """将 SQLite task 行还原为 AgentTask 聚合根。

    行列顺序（1-based）：
        0 task_id, 1 root_agent_instance_id, 2 agent_instance_ids, 3 status,
        4 created_at, 5 updated_at, 6 started_at, 7 paused_at,
        8 finished_at, 9 failed_at, 10 failure_message, 11 scene_id
    scene_id 列为以往版本补加，可能为 None。
    """
    scene_id = row[11] if len(row) > 11 else None
    return AgentTask(
        task_id=row[0],
        root_agent_instance_id=row[1],
        agent_instance_ids=instance_ids,
        status=row[3],
        created_at=_text_to_datetime(row[4]),
        updated_at=_text_to_datetime(row[5]),
        started_at=_optional_text_to_datetime(row[6]),
        paused_at=_optional_text_to_datetime(row[7]),
        finished_at=_optional_text_to_datetime(row[8]),
        failed_at=_optional_text_to_datetime(row[9]),
        failure_message=row[10],
        scene_id=scene_id,
        parent_child_relations=relations,
    )


def _row_to_instance(row: sqlite3.Row | tuple) -> AgentInstance:
    """将 SQLite instance 行还原为 AgentInstance 实体。"""
    return AgentInstance(
        instance_id=row[0],
        task_id=row[1],
        thread_id=row[2],
        current_working_directory=row[3],
        agent_definition_id=row[4],
        llm_provider_id=row[5],
        llm_model_binding_id=row[6],
        llm_policy_group_id=row[7],
        conversation_id=row[8],
        trace_id=row[9],
        status=row[10],
        pending_user_messages=_pending_user_messages_from_json(row[11]),
        created_at=_text_to_datetime(row[12]),
        updated_at=_text_to_datetime(row[13]),
        started_at=_optional_text_to_datetime(row[14]),
        paused_at=_optional_text_to_datetime(row[15]),
        finished_at=_optional_text_to_datetime(row[16]),
        failed_at=_optional_text_to_datetime(row[17]),
        failure_message=row[18],
    )


def _pending_user_messages_to_json(messages: tuple[PendingAgentUserMessage, ...]) -> str:
    """序列化 AgentInstance 待处理用户消息队列。"""
    return json.dumps(
        [
            {
                "message_id": message.message_id,
                "user_text": message.user_text,
                "client_message_id": message.client_message_id,
                "attachments": [
                    _pending_user_message_attachment_to_dict(attachment)
                    for attachment in message.attachments
                ],
                "received_at": _datetime_to_text(message.received_at),
                "metadata": message.metadata,
            }
            for message in messages
        ],
        ensure_ascii=False,
    )


def _pending_user_messages_from_json(value: str | None) -> tuple[PendingAgentUserMessage, ...]:
    """反序列化 AgentInstance 待处理用户消息队列。"""
    if not value:
        return ()
    raw_items = json.loads(value)
    if not isinstance(raw_items, list):
        return ()
    messages: list[PendingAgentUserMessage] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        user_text = str(item.get("user_text") or "")
        attachments = _pending_user_message_attachments_from_json(item.get("attachments"))
        if not user_text.strip() and not attachments:
            continue
        messages.append(
            PendingAgentUserMessage(
                message_id=str(item.get("message_id") or "legacy-pending-user-message"),
                user_text=user_text,
                attachments=attachments,
                client_message_id=item.get("client_message_id"),
                metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                received_at=_text_to_datetime(item["received_at"]) if item.get("received_at") else None,
            )
        )
    return tuple(messages)


def _pending_user_message_attachment_to_dict(
    attachment: PendingUserMessageAttachment,
) -> dict[str, object | None]:
    return {
        "kind": attachment.kind,
        "resource_id": attachment.resource_id,
        "name": attachment.name,
        "mime_type": attachment.mime_type,
        "size_in_bytes": attachment.size_in_bytes,
        "width": attachment.width,
        "height": attachment.height,
        "page_count": attachment.page_count,
    }


def _pending_user_message_attachments_from_json(
    value: object,
) -> tuple[PendingUserMessageAttachment, ...]:
    if not isinstance(value, list):
        return ()
    attachments: list[PendingUserMessageAttachment] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            attachments.append(
                PendingUserMessageAttachment(
                    kind=str(item.get("kind") or ""),
                    resource_id=str(item.get("resource_id") or ""),
                    name=_optional_str(item.get("name")),
                    mime_type=_optional_str(item.get("mime_type")),
                    size_in_bytes=_optional_int(item.get("size_in_bytes")),
                    width=_optional_int(item.get("width")),
                    height=_optional_int(item.get("height")),
                    page_count=_optional_int(item.get("page_count")),
                )
            )
        except ValueError:
            continue
    return tuple(attachments)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _datetime_to_text(value: datetime) -> str:
    """序列化必填 datetime。"""
    return value.isoformat()


def _optional_datetime_to_text(value: datetime | None) -> str | None:
    """序列化可选 datetime。"""
    return value.isoformat() if value is not None else None


def _text_to_datetime(value: str) -> datetime:
    """反序列化必填 datetime。"""
    return datetime.fromisoformat(value)


def _optional_text_to_datetime(value: str | None) -> datetime | None:
    """反序列化可选 datetime。"""
    return datetime.fromisoformat(value) if value else None
