-- =============================================================================
-- Agent Task Note 15 张核心表结构与索引定义 (PostgreSQL DDL)
-- 包含：业务任务实体、5大执行 Note 主题、4大客观事实记录与协作拓扑通信表
-- =============================================================================

-- 1. 业务任务单元表 (agent_user_mission)
CREATE TABLE IF NOT EXISTS agent_user_mission (
    mission_id              VARCHAR(64) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    scene_id                VARCHAR(64) NOT NULL,
    agent_instance_id       VARCHAR(64) NOT NULL,
    title                   VARCHAR(255) NOT NULL,
    raw_prompts             JSONB NOT NULL DEFAULT '[]',
    refined_requirement     TEXT,
    status                  VARCHAR(32) NOT NULL DEFAULT 'in_progress',
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
    mission_id              VARCHAR(64),
    agent_instance_id       VARCHAR(64) NOT NULL,
    command_content         TEXT NOT NULL,
    execution_status        VARCHAR(32) NOT NULL,
    execution_result        TEXT,
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_command_mission ON task_command_records(task_id, mission_id);

-- 13. 子 Agent 协作关系表 (task_agent_relationships)
CREATE TABLE IF NOT EXISTS task_agent_relationships (
    relationship_id         VARCHAR(64) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    mission_id              VARCHAR(64),
    parent_agent_instance_id VARCHAR(64) NOT NULL,
    child_agent_instance_id  VARCHAR(64) NOT NULL,
    status                  VARCHAR(32) NOT NULL DEFAULT 'unreleased',
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_agent_rel UNIQUE (task_id, parent_agent_instance_id, child_agent_instance_id)
);

-- 14. 父子 Agent 专线消息表 (task_agent_messages)
CREATE TABLE IF NOT EXISTS task_agent_messages (
    conversation_scope_id   VARCHAR(128) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    mission_id              VARCHAR(64),
    parent_agent_instance_id VARCHAR(64) NOT NULL,
    child_agent_instance_id  VARCHAR(64) NOT NULL,
    messages                JSONB NOT NULL DEFAULT '[]',
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_agent_msg UNIQUE (task_id, parent_agent_instance_id, child_agent_instance_id)
);

-- 15. 远程对等 Agent 消息表 (task_remote_agent_messages)
CREATE TABLE IF NOT EXISTS task_remote_agent_messages (
    remote_conversation_scope_id VARCHAR(128) PRIMARY KEY,
    task_id                 VARCHAR(64) NOT NULL,
    mission_id              VARCHAR(64),
    agent_instance_id       VARCHAR(64) NOT NULL,
    remote_agent_instance_id VARCHAR(64) NOT NULL,
    messages                JSONB NOT NULL DEFAULT '[]',
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_remote_msg UNIQUE (task_id, agent_instance_id, remote_agent_instance_id)
);
