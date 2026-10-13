# -*- coding: utf-8 -*-
"""Agent Task Note 数据库 Schema 定义与初始化工具。"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("agent_runtime.task_note.infrastructure.schema")

TASK_NOTE_SCHEMA_SQL = """\
-- 1. 业务任务单元表 (agent_user_mission)
CREATE TABLE IF NOT EXISTS agent_user_mission (
    mission_id              VARCHAR(64) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    scene_id                VARCHAR(64) NOT NULL,
    agent_instance_id       VARCHAR(64) NOT NULL,
    title                   VARCHAR(255) NOT NULL,
    raw_prompts             JSONB NOT NULL DEFAULT '[]',  -- 用户或父 Agent 委派输入字符串数组 (可优化措辞使需求更清晰明确，保证核心含义与需求不变)
    refined_requirement     TEXT,
    status                  VARCHAR(32) NOT NULL DEFAULT 'in_progress',
    outcome_summary         TEXT,                         -- 闭环结果摘要 (prune_mission 写入；裁剪后任务以卡片形式保留)
    pruned_at               TIMESTAMP WITH TIME ZONE,     -- 裁剪时间；非 NULL 表示该任务已被裁剪为卡片
    sort_order              INT NOT NULL DEFAULT 1,
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mission_task_status ON agent_user_mission(task_id, status);
CREATE INDEX IF NOT EXISTS idx_mission_agent ON agent_user_mission(task_id, agent_instance_id);
CREATE INDEX IF NOT EXISTS idx_mission_scene ON agent_user_mission(scene_id);

-- 2. 任务背景素材表 (task_workspace_context)
CREATE TABLE IF NOT EXISTS task_workspace_context (
    context_id              VARCHAR(64) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    mission_id              VARCHAR(64) NOT NULL,
    agent_instance_id       VARCHAR(64) NOT NULL,
    source_type             VARCHAR(32) NOT NULL,
    description             TEXT,
    path                    TEXT,
    line_ranges             JSONB,
    directory_structure     TEXT,
    search_content          TEXT,
    core_result_content     TEXT,
    reference_links         JSONB,
    url_list                JSONB,
    core_content            TEXT,
    command_id              VARCHAR(64),
    important_content       TEXT,
    content                 TEXT,
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_workspace_context_mission ON task_workspace_context(task_id, mission_id);

-- 3. 当前宏观方案表 (task_solution_content)
CREATE TABLE IF NOT EXISTS task_solution_content (
    solution_id             VARCHAR(64) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    mission_id              VARCHAR(64) NOT NULL,
    agent_instance_id       VARCHAR(64) NOT NULL,
    solution_content        TEXT NOT NULL,
    solution_status         VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_solution_mission_agent UNIQUE (mission_id, agent_instance_id)
);

-- 4. 关键决策记录表 (task_decision_records)
CREATE TABLE IF NOT EXISTS task_decision_records (
    decision_id             VARCHAR(64) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    mission_id              VARCHAR(64) NOT NULL,
    agent_instance_id       VARCHAR(64) NOT NULL,
    decision_content        TEXT NOT NULL,
    rationale               TEXT NOT NULL,
    alternatives            TEXT,
    impact                  TEXT,
    decision_status         VARCHAR(32) NOT NULL DEFAULT 'active',
    superseded_reason       TEXT,
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_decision_mission ON task_decision_records(task_id, mission_id);

-- 5. 计划工作项表 (task_todo_items)
CREATE TABLE IF NOT EXISTS task_todo_items (
    todo_id                 VARCHAR(64) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    mission_id              VARCHAR(64) NOT NULL,
    agent_instance_id       VARCHAR(64) NOT NULL,
    priority                INT NOT NULL DEFAULT 1,
    todo_content            TEXT NOT NULL,
    status                  VARCHAR(32) NOT NULL DEFAULT '未开始',
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_todo_mission_status ON task_todo_items(mission_id, status);

-- 6. 任务恢复快照表 (task_progress_content)
CREATE TABLE IF NOT EXISTS task_progress_content (
    progress_id             VARCHAR(64) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    mission_id              VARCHAR(64) NOT NULL,
    agent_instance_id       VARCHAR(64) NOT NULL,
    completed_summary       TEXT NOT NULL,
    current_work            TEXT NOT NULL,
    current_blockers        TEXT,
    recent_validation       TEXT,
    next_step               TEXT NOT NULL,
    handoff_notes           TEXT,
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_progress_mission_agent UNIQUE (mission_id, agent_instance_id)
);

-- 7. 客观认知发现表 (task_discovery_records)
CREATE TABLE IF NOT EXISTS task_discovery_records (
    discovery_id            VARCHAR(64) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    mission_id              VARCHAR(64) NOT NULL,
    agent_instance_id       VARCHAR(64) NOT NULL,
    discovery_content       TEXT NOT NULL,
    impact                  TEXT NOT NULL,
    source_type             VARCHAR(32),
    source_path             TEXT,
    line_ranges             JSONB,
    command_id              VARCHAR(64),
    reference_url           TEXT,
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_discovery_mission ON task_discovery_records(task_id, mission_id);

-- 8. 问题与阻塞记录表 (task_issue_records)
CREATE TABLE IF NOT EXISTS task_issue_records (
    issue_id                VARCHAR(64) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    mission_id              VARCHAR(64) NOT NULL,
    agent_instance_id       VARCHAR(64) NOT NULL,
    issue_content           TEXT NOT NULL,
    impact                  TEXT NOT NULL,
    status                  VARCHAR(32) NOT NULL DEFAULT 'open',
    next_action             TEXT,
    closure_condition       TEXT,
    resolution              TEXT,
    source_type             VARCHAR(32),
    command_id              VARCHAR(64),
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_issue_mission_status ON task_issue_records(mission_id, status);

-- 9. 交付产物记录表 (task_artifact_records)
CREATE TABLE IF NOT EXISTS task_artifact_records (
    artifact_id             VARCHAR(64) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    mission_id              VARCHAR(64),
    agent_instance_id       VARCHAR(64) NOT NULL,
    type                    VARCHAR(32) NOT NULL,
    file_path               TEXT,
    edits                   JSONB,
    content_lines           JSONB,
    source_path             TEXT,
    target_path             TEXT,
    content                 TEXT,
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_artifact_mission ON task_artifact_records(task_id, mission_id);

-- 10. 验证与检查结论表 (task_validation_records)
CREATE TABLE IF NOT EXISTS task_validation_records (
    validation_id           VARCHAR(64) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    mission_id              VARCHAR(64) NOT NULL,
    agent_instance_id       VARCHAR(64) NOT NULL,
    target_type             VARCHAR(32) NOT NULL,
    target_record_id        VARCHAR(64),
    validation_method       VARCHAR(32) NOT NULL,
    validation_scope        TEXT NOT NULL,
    conclusion              VARCHAR(32) NOT NULL,
    uncovered_scope         TEXT,
    validation_content      TEXT NOT NULL,
    source_type             VARCHAR(32) NOT NULL,
    command_id              VARCHAR(64),
    source_path             TEXT,
    line_ranges             JSONB,
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_validation_mission ON task_validation_records(task_id, mission_id);

-- 11. 外部状态变更表 (task_external_state_records)
CREATE TABLE IF NOT EXISTS task_external_state_records (
    external_state_id       VARCHAR(64) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    mission_id              VARCHAR(64) NOT NULL,
    agent_instance_id       VARCHAR(64) NOT NULL,
    target_type             VARCHAR(32) NOT NULL,
    target_identifier       TEXT NOT NULL,
    change_content          TEXT NOT NULL,
    current_state           TEXT NOT NULL,
    reversibility           VARCHAR(32) NOT NULL DEFAULT 'reversible',
    rollback_info           TEXT,
    command_id              VARCHAR(64),
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_external_state_mission ON task_external_state_records(task_id, mission_id);

-- 12. 命令执行索引表 (task_command_records)
CREATE TABLE IF NOT EXISTS task_command_records (
    command_id              VARCHAR(64) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    agent_instance_id       VARCHAR(64) NOT NULL,
    command_line            TEXT NOT NULL,
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_command_tenant ON task_command_records(task_id, agent_instance_id);

-- 13. 子 Agent 协作关系表 (task_agent_relationships)
-- 面向 agent_instance_id 双向记录，无 updated_at
CREATE TABLE IF NOT EXISTS task_agent_relationships (
    relationship_id          VARCHAR(64) PRIMARY KEY,
    task_id                  VARCHAR(64) NOT NULL,
    mission_id               VARCHAR(64),
    agent_instance_id        VARCHAR(64) NOT NULL,
    target_agent_type        VARCHAR(32) NOT NULL,        -- 'child' / 'parent'
    target_agent_instance_id VARCHAR(64) NOT NULL,
    delegation_content       TEXT,                         -- 创建子 Agent 时的委派任务原文 (create_subagent.initial_message，程序自动记录)
    fork_message_id          VARCHAR(64),                  -- 子 Agent 对话分叉边界消息 ID (仅 target_agent_type='parent' 的行有值)
    status                   VARCHAR(32) NOT NULL DEFAULT 'unreleased', -- unreleased, released
    created_at               TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_agent_rel UNIQUE (task_id, agent_instance_id, target_agent_instance_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_rel_tenant ON task_agent_relationships(task_id, agent_instance_id);

-- 14. 父子 Agent 专线消息表 (task_agent_messages)
-- 一条消息一行记录，面向 agent_instance_id，无 updated_at
CREATE TABLE IF NOT EXISTS task_agent_messages (
    message_id               VARCHAR(64) PRIMARY KEY,
    task_id                  VARCHAR(64) NOT NULL,
    mission_id               VARCHAR(64),
    agent_instance_id        VARCHAR(64) NOT NULL,
    target_agent_instance_id VARCHAR(64) NOT NULL,
    target_agent_type        VARCHAR(32) NOT NULL,        -- 'child' / 'parent'
    message_type             VARCHAR(32) NOT NULL,        -- 'send' / 'receive'
    message                  TEXT NOT NULL,
    created_at               TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_msg_tenant ON task_agent_messages(task_id, agent_instance_id);
CREATE INDEX IF NOT EXISTS idx_agent_msg_target ON task_agent_messages(task_id, agent_instance_id, target_agent_instance_id);

-- 15. 远程对等 Agent 消息表 (task_remote_agent_messages)
-- 一条消息一行记录，面向 agent_instance_id，无 updated_at
CREATE TABLE IF NOT EXISTS task_remote_agent_messages (
    remote_message_id        VARCHAR(64) PRIMARY KEY,
    task_id                  VARCHAR(64) NOT NULL,
    mission_id               VARCHAR(64),
    agent_instance_id        VARCHAR(64) NOT NULL,
    remote_agent_instance_id VARCHAR(64) NOT NULL,
    message_type             VARCHAR(32) NOT NULL,        -- 'send' / 'receive'
    message                  TEXT NOT NULL,
    created_at               TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_remote_msg_tenant ON task_remote_agent_messages(task_id, agent_instance_id);
CREATE INDEX IF NOT EXISTS idx_remote_msg_target ON task_remote_agent_messages(task_id, agent_instance_id, remote_agent_instance_id);

-- 16. 观察断点表 (task_note_watermark)
CREATE TABLE IF NOT EXISTS task_note_watermark (
    agent_instance_id        VARCHAR(64) PRIMARY KEY,     -- 被观察的业务 Agent 实例 ID
    task_id                  VARCHAR(64) NOT NULL,
    covered_until_seq        BIGINT NOT NULL DEFAULT 0,   -- 快照覆盖到的合法断点 seq (含)
    covered_until_message_id VARCHAR(64),                 -- 该断点对应的业务 Agent 消息 ID
    note_version             INT NOT NULL DEFAULT 0,      -- 快照版本号，每次成功观察 +1
    inherited_processed      BOOLEAN NOT NULL DEFAULT FALSE, -- 子 Agent 继承段是否已在首次观察中处理
    updated_at               TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_watermark_task ON task_note_watermark(task_id);
"""

TASK_NOTE_MIGRATIONS_SQL = """\
ALTER TABLE agent_user_mission ADD COLUMN IF NOT EXISTS outcome_summary TEXT;
ALTER TABLE agent_user_mission ADD COLUMN IF NOT EXISTS pruned_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE task_agent_relationships ADD COLUMN IF NOT EXISTS delegation_content TEXT;
ALTER TABLE task_agent_relationships ADD COLUMN IF NOT EXISTS fork_message_id VARCHAR(64);
ALTER TABLE task_decision_records ADD COLUMN IF NOT EXISTS superseded_reason TEXT;
DROP INDEX IF EXISTS idx_command_mission;
ALTER TABLE task_command_records DROP COLUMN IF EXISTS mission_id;
"""


def init_task_note_schema(conn: Any) -> None:
    """初始化 Task Note 相关的 16 张表结构、索引及迁移升级。"""
    logger.info("正在执行 Task Note 数据库表与索引初始化...")
    with conn.cursor() as cur:
        cur.execute(TASK_NOTE_SCHEMA_SQL)
        cur.execute(TASK_NOTE_MIGRATIONS_SQL)
    conn.commit()
    logger.info("Task Note 16 张表与索引初始化/升级完成。")


REQUIRED_MIGRATION_COLUMNS = frozenset({
    ("agent_user_mission", "outcome_summary"),
    ("agent_user_mission", "pruned_at"),
    ("task_agent_relationships", "delegation_content"),
    ("task_agent_relationships", "fork_message_id"),
    ("task_decision_records", "superseded_reason"),
})


def ensure_task_note_schema(config_registry: Any = None) -> bool:
    """检查并执行 Task Note 16 张表与迁移。

    1. PG 未启用返回 False；
    2. 获取连接调用 init_task_note_schema(conn)；
    3. 查询 information_schema.columns 断言 outcome_summary、pruned_at、delegation_content、fork_message_id、superseded_reason 存在；
    4. 成功返回 True；任何异常记 error 日志（含异常类型）并返回 False，不抛出。
    """
    try:
        from agent_runtime.config import ConfigRegistry

        cfg = config_registry
        if cfg is None:
            if getattr(ConfigRegistry, "_instance", None) is not None:
                cfg = ConfigRegistry.instance()
            else:
                logger.info("ConfigRegistry 尚未初始化，跳过 Task Note schema 初始化。")
                return False

        cloud_db = getattr(cfg, "cloud_user_config_database", None)
        if cloud_db is None or not cloud_db.is_postgresql_enabled:
            logger.info("PostgreSQL 未启用，跳过 Task Note schema 初始化。")
            return False

        from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore

        store = PostgresUserConfigStore(lambda: cfg.cloud_user_config_database)
        with store._connect() as conn:
            init_task_note_schema(conn)
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT table_name, column_name 
                    FROM information_schema.columns 
                    WHERE table_schema = 'public' 
                      AND (
                        (table_name = 'agent_user_mission' AND column_name IN ('outcome_summary', 'pruned_at')) OR
                        (table_name = 'task_agent_relationships' AND column_name IN ('delegation_content', 'fork_message_id')) OR
                        (table_name = 'task_decision_records' AND column_name IN ('superseded_reason'))
                      );
                """)
                existing_cols = frozenset((row[0], row[1]) for row in cur.fetchall())
                missing = REQUIRED_MIGRATION_COLUMNS - existing_cols
                if missing:
                    logger.error("Task Note 迁移断言失败，缺失必需列: %s", missing)
                    return False

        logger.info("Task Note 16 张表与索引初始化/升级完成。")
        return True
    except Exception as exc:
        logger.error("Task Note schema 初始化执行失败 (%s): %s", type(exc).__name__, exc, exc_info=True)
        return False

