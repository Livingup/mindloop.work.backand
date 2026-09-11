# -*- coding: utf-8 -*-
"""Task Note 客观物理事实程序自动记录器。

负责在底层工具执行成功后，以客观物理快照形式自动写入 PostgreSQL 数据表：
1. task_artifact_records: 文件新增、修改、删除、移动产物；
2. task_command_records: 终端执行命令反查索引；
3. task_agent_relationships: 子 Agent 协作关系（父子双向记录）；
4. task_agent_messages: 父子 Agent 专线消息（发收双向单条记录）；
5. task_remote_agent_messages: 远程对等 Agent 通信消息（单条记录）。

所有记录遵循以下原则：
- mission_id 初始为 NULL（由后续 Agent 调用 bind_resources_to_mission 延迟归属绑定）；
- task_id 与 agent_instance_id 自动提取注入；
- 记录逻辑异常做静默容错保护，严禁因审计/记录异常阻断核心业务工具执行。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Callable, ContextManager

logger = logging.getLogger("agent_runtime.task_note.event_recorder")


def _generate_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class TaskNoteEventRecorder:
    """Task Note 物理事实记录器。"""

    def __init__(
        self,
        connection_factory: Callable[[], ContextManager[Any]] | None = None,
    ) -> None:
        self._connection_factory = connection_factory

    @classmethod
    def from_config_registry(cls) -> "TaskNoteEventRecorder":
        """从全局配置中心创建记录器实例。"""
        try:
            from agent_runtime.config import ConfigRegistry

            cfg = ConfigRegistry.instance()
            if cfg.cloud_user_config_database.is_postgresql_enabled:
                from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore

                store = PostgresUserConfigStore(lambda: cfg.cloud_user_config_database)
                return cls(connection_factory=store._connect)
        except Exception as exc:
            logger.warning("无法从 ConfigRegistry 初始化 TaskNoteEventRecorder: %s", exc)
        return cls(connection_factory=None)

    def record_file_artifact(
        self,
        *,
        task_id: str | None,
        agent_instance_id: str | None,
        op_type: str,
        file_path: str | None = None,
        edits: list[dict[str, Any]] | None = None,
        content_lines: list[str] | None = None,
        source_path: str | None = None,
        target_path: str | None = None,
        content: str | None = None,
        mission_id: str | None = None,
    ) -> str | None:
        """记录文件操作物理产物到 task_artifact_records。"""
        if not task_id or not agent_instance_id or self._connection_factory is None:
            return None

        artifact_id = _generate_id("art")
        try:
            from psycopg.types.json import Jsonb

            edits_val = Jsonb(edits) if edits is not None else None
            content_lines_val = Jsonb(content_lines) if content_lines is not None else None

            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO task_artifact_records (
                            artifact_id, task_id, mission_id, agent_instance_id,
                            type, file_path, edits, content_lines,
                            source_path, target_path, content
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s
                        )
                        """,
                        (
                            artifact_id,
                            task_id,
                            mission_id,
                            agent_instance_id,
                            op_type,
                            file_path,
                            edits_val,
                            content_lines_val,
                            source_path,
                            target_path,
                            content,
                        ),
                    )
                conn.commit()
            return artifact_id
        except Exception as exc:
            logger.warning("记录 task_artifact_records 异常 (已忽略): %s", exc)
            return None

    def record_terminal_command(
        self,
        *,
        task_id: str | None,
        agent_instance_id: str | None,
        command_id: str | None,
        command_line: str,
        mission_id: str | None = None,
    ) -> bool:
        """记录终端命令索引到 task_command_records。"""
        if not task_id or not agent_instance_id or not command_id or self._connection_factory is None:
            return False

        try:
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO task_command_records (
                            command_id, task_id, mission_id, agent_instance_id, command_line
                        ) VALUES (
                            %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (command_id) DO NOTHING
                        """,
                        (
                            command_id,
                            task_id,
                            mission_id,
                            agent_instance_id,
                            command_line,
                        ),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.warning("记录 task_command_records 异常 (已忽略): %s", exc)
            return False

    async def async_record_terminal_command(
        self,
        *,
        task_id: str | None,
        agent_instance_id: str | None,
        command_id: str | None,
        command_line: str,
        mission_id: str | None = None,
    ) -> bool:
        """异步记录终端命令索引。"""
        return await asyncio.to_thread(
            self.record_terminal_command,
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            command_id=command_id,
            command_line=command_line,
            mission_id=mission_id,
        )

    def record_subagent_relationship(
        self,
        *,
        task_id: str | None,
        parent_instance_id: str | None,
        child_instance_id: str | None,
        mission_id: str | None = None,
    ) -> bool:
        """记录子 Agent 协作拓扑关系到 task_agent_relationships（面向父子双方双向记录）。"""
        if not task_id or not parent_instance_id or not child_instance_id or self._connection_factory is None:
            return False

        try:
            rel_id_parent = _generate_id("rel")
            rel_id_child = _generate_id("rel")

            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    # 1. 父视角: 目标是 child
                    cur.execute(
                        """
                        INSERT INTO task_agent_relationships (
                            relationship_id, task_id, mission_id, agent_instance_id,
                            target_agent_type, target_agent_instance_id, status
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s
                        )
                        ON CONFLICT (task_id, agent_instance_id, target_agent_instance_id) DO NOTHING
                        """,
                        (
                            rel_id_parent,
                            task_id,
                            mission_id,
                            parent_instance_id,
                            "child",
                            child_instance_id,
                            "unreleased",
                        ),
                    )
                    # 2. 子视角: 目标是 parent
                    cur.execute(
                        """
                        INSERT INTO task_agent_relationships (
                            relationship_id, task_id, mission_id, agent_instance_id,
                            target_agent_type, target_agent_instance_id, status
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s
                        )
                        ON CONFLICT (task_id, agent_instance_id, target_agent_instance_id) DO NOTHING
                        """,
                        (
                            rel_id_child,
                            task_id,
                            mission_id,
                            child_instance_id,
                            "parent",
                            parent_instance_id,
                            "unreleased",
                        ),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.warning("记录 task_agent_relationships 异常 (已忽略): %s", exc)
            return False

    async def async_record_subagent_relationship(
        self,
        *,
        task_id: str | None,
        parent_instance_id: str | None,
        child_instance_id: str | None,
        mission_id: str | None = None,
    ) -> bool:
        """异步记录子 Agent 协作拓扑关系。"""
        return await asyncio.to_thread(
            self.record_subagent_relationship,
            task_id=task_id,
            parent_instance_id=parent_instance_id,
            child_instance_id=child_instance_id,
            mission_id=mission_id,
        )

    def record_agent_message(
        self,
        *,
        task_id: str | None,
        from_instance_id: str | None,
        to_instance_id: str | None,
        target_agent_type: str,
        message: str,
        mission_id: str | None = None,
    ) -> bool:
        """记录父子 Agent 专线消息到 task_agent_messages（双向单条记录）。"""
        if not task_id or not from_instance_id or not to_instance_id or self._connection_factory is None:
            return False

        try:
            msg_id_sender = _generate_id("msg")
            msg_id_receiver = _generate_id("msg")
            reverse_target_type = "parent" if target_agent_type == "child" else "child"

            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    # 1. 发送方: message_type='send'
                    cur.execute(
                        """
                        INSERT INTO task_agent_messages (
                            message_id, task_id, mission_id, agent_instance_id,
                            target_agent_instance_id, target_agent_type, message_type, message
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s
                        )
                        """,
                        (
                            msg_id_sender,
                            task_id,
                            mission_id,
                            from_instance_id,
                            to_instance_id,
                            target_agent_type,
                            "send",
                            message,
                        ),
                    )
                    # 2. 接收方: message_type='receive'
                    cur.execute(
                        """
                        INSERT INTO task_agent_messages (
                            message_id, task_id, mission_id, agent_instance_id,
                            target_agent_instance_id, target_agent_type, message_type, message
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s
                        )
                        """,
                        (
                            msg_id_receiver,
                            task_id,
                            mission_id,
                            to_instance_id,
                            from_instance_id,
                            reverse_target_type,
                            "receive",
                            message,
                        ),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.warning("记录 task_agent_messages 异常 (已忽略): %s", exc)
            return False

    async def async_record_agent_message(
        self,
        *,
        task_id: str | None,
        from_instance_id: str | None,
        to_instance_id: str | None,
        target_agent_type: str,
        message: str,
        mission_id: str | None = None,
    ) -> bool:
        """异步记录父子 Agent 专线消息。"""
        return await asyncio.to_thread(
            self.record_agent_message,
            task_id=task_id,
            from_instance_id=from_instance_id,
            to_instance_id=to_instance_id,
            target_agent_type=target_agent_type,
            message=message,
            mission_id=mission_id,
        )

    def record_remote_agent_message(
        self,
        *,
        task_id: str | None,
        agent_instance_id: str | None,
        remote_agent_instance_id: str | None,
        message_type: str,
        message: str,
        mission_id: str | None = None,
    ) -> bool:
        """记录远程对等 Agent 通信消息到 task_remote_agent_messages（单条记录）。"""
        if not task_id or not agent_instance_id or not remote_agent_instance_id or self._connection_factory is None:
            return False

        try:
            remote_msg_id = _generate_id("rmsg")
            with self._connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO task_remote_agent_messages (
                            remote_message_id, task_id, mission_id, agent_instance_id,
                            remote_agent_instance_id, message_type, message
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s
                        )
                        """,
                        (
                            remote_msg_id,
                            task_id,
                            mission_id,
                            agent_instance_id,
                            remote_agent_instance_id,
                            message_type,
                            message,
                        ),
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.warning("记录 task_remote_agent_messages 异常 (已忽略): %s", exc)
            return False

    async def async_record_remote_agent_message(
        self,
        *,
        task_id: str | None,
        agent_instance_id: str | None,
        remote_agent_instance_id: str | None,
        message_type: str,
        message: str,
        mission_id: str | None = None,
    ) -> bool:
        """异步记录远程对等 Agent 通信消息。"""
        return await asyncio.to_thread(
            self.record_remote_agent_message,
            task_id=task_id,
            agent_instance_id=agent_instance_id,
            remote_agent_instance_id=remote_agent_instance_id,
            message_type=message_type,
            message=message,
            mission_id=mission_id,
        )


_global_event_recorder: TaskNoteEventRecorder | None = None


def get_task_note_event_recorder() -> TaskNoteEventRecorder:
    """获取全局 TaskNoteEventRecorder 单例。"""
    global _global_event_recorder
    if _global_event_recorder is None:
        _global_event_recorder = TaskNoteEventRecorder.from_config_registry()
    return _global_event_recorder


def set_task_note_event_recorder(recorder: TaskNoteEventRecorder | None) -> None:
    """设置或重置全局 TaskNoteEventRecorder 单例（用于测试与依赖注入）。"""
    global _global_event_recorder
    _global_event_recorder = recorder
