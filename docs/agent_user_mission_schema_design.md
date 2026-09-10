# 业务任务单元表设计（agent_user_mission）

## 1. DDL 定义

```sql
CREATE TABLE agent_user_mission (
    mission_id              VARCHAR(64) PRIMARY KEY,      -- 业务任务唯一 ID
    task_id                 VARCHAR(64) NOT NULL,         -- 所属工作会期班次 ID
    scene_id                VARCHAR(64) NOT NULL,         -- 所属 Scene 场地 ID
    title                   VARCHAR(255) NOT NULL,        -- 任务标题
    raw_prompts             JSONB NOT NULL DEFAULT '[]',  -- 用户原始输入字符串数组（按顺序追加）
    refined_requirement     TEXT,                         -- 经 LLM 思考整理后的需求说明（需求/规范/约束/要求）
    status                  VARCHAR(32) NOT NULL DEFAULT 'in_progress', -- 任务状态
    sort_order              INT NOT NULL DEFAULT 1,       -- 班次内任务执行顺序
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at            TIMESTAMP WITH TIME ZONE      -- 任务完成时间
);

CREATE INDEX idx_mission_task_status ON agent_user_mission(task_id, status);
CREATE INDEX idx_mission_scene ON agent_user_mission(scene_id);
```

## 2. 字段说明

| 字段名 | 类型 | 必填 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `mission_id` | VARCHAR(64) | 是 | - | 主键，业务任务唯一标识（如 `mission-01`） |
| `task_id` | VARCHAR(64) | 是 | - | 外键，所属班次 ID，用于支撑同一个班次内发起多个独立任务 |
| `scene_id` | VARCHAR(64) | 是 | - | 所属 Scene 场景/场地 ID |
| `title` | VARCHAR(255) | 是 | - | 简短任务名称（如“优化登录模块”、“做数学作业”） |
| `raw_prompts` | JSONB | 是 | `'[]'` | 用户原始口语消息字符串数组，按时间发生顺序追加（如 `["优化下登录", "用JWT别改表结构"]`） |
| `refined_requirement` | TEXT | 否 | NULL | 经 LLM 结合项目代码、历史设计与用户输入后思考整理出的需求说明，表达需求、规范、约束、要求，格式自由 |
| `status` | VARCHAR(32) | 是 | `'in_progress'` | 状态枚举：`pending`（排队）、`in_progress`（执行中）、`completed`（已完成）、`abandoned`（废弃） |
| `sort_order` | INT | 是 | `1` | 当前班次内该任务的编排序号 |
| `created_at` | TIMESTAMP | 是 | `CURRENT_TIMESTAMP` | 任务创建时间戳 |
| `completed_at` | TIMESTAMP | 否 | NULL | 任务完成时间戳，未完成为 NULL |
