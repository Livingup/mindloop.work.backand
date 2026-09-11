# 业务任务单元表设计（agent_user_mission）

## 1. DDL 定义

```sql
CREATE TABLE agent_user_mission (
    mission_id              VARCHAR(64) PRIMARY KEY,      -- 业务任务唯一 ID
    task_id                 VARCHAR(64) NOT NULL,         -- 所属工作会期班次 ID
    scene_id                VARCHAR(64) NOT NULL,         -- 所属 Scene 场地 ID
    title                   VARCHAR(255) NOT NULL,        -- 任务标题
    raw_prompts             JSONB NOT NULL DEFAULT '[]',  -- 用户或父 Agent 委派输入字符串数组（按顺序追加，可优化措辞使需求更清晰，保持核心含义与需求不变）
    refined_requirement     TEXT,                         -- 经 LLM 思考整理后的需求说明（需求/规范/约束/要求）
    status                  VARCHAR(32) NOT NULL DEFAULT 'in_progress', -- 任务状态
    sort_order              INT NOT NULL DEFAULT 1,       -- 班次内任务执行顺序
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
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
| `raw_prompts` | JSONB | 是 | `'[]'` | 用户或父 Agent 委派消息字符串数组，按时间发生顺序追加。不要求一字不变的死板原文，Agent 可优化措辞使需求更清晰明确，但核心含义与需求严禁篡改（如 `["优化登录模块", "用JWT实现认证且别改表结构"]`） |
| `refined_requirement` | TEXT | 否 | NULL | 经 LLM 结合项目代码、历史设计与用户输入后思考整理出的需求说明，表达需求、规范、约束、要求，格式自由 |
| `status` | VARCHAR(32) | 是 | `'in_progress'` | 状态枚举：`pending`（排队）、`in_progress`（执行中）、`completed`（已完成）、`abandoned`（废弃） |
| `sort_order` | INT | 是 | `1` | 当前班次内该任务的编排序号 |
| `created_at` | TIMESTAMP | 是 | `CURRENT_TIMESTAMP` | 任务创建时间戳 |

## 3. 支持“一个班次内多个独立复杂任务”的机制说明

### 3.1 核心痛点与解耦模型
在实际交互中，用户在同一个对话班次（`task_id`）内往往会先后安排多个独立的复杂任务（例如：“先优化登录模块” $\rightarrow$ 完成后再说“去把数学作业做了”）。在用户或主 Agent 显式触发 `end_task` 终结班次之前，该工作会期持续存在。

如果将具体需求、计划与断点直接扁平绑定在 `task_id` 上，多任务先后执行时上下文和状态将发生严重串扰。`agent_user_mission` 通过 **`task_id` 1:N `mission_id`** 的层级结构实现了会期容器与业务任务单元的彻底解耦：

- **`task_id`（班次 / 会期层）**：负责管理物理生命周期、通信流（`stream-scene-{scene_id}`）、主/子 Agent 资源树与全局清场终态；
- **`mission_id`（业务任务层）**：负责界定具体业务目标边界，作为计划、执行断点、局部需求与产物账本的独立聚合锚点。

### 3.2 任务生命周期与轮转机制
1. **任务创建（Initiated）**：
   - 用户在班次内发起第一个任务（如“优化登录模块”），系统创建 `mission-01`，`status = 'in_progress'`，`sort_order = 1`；
   - 用户后续在该任务内的多轮追加与纠偏（或父 Agent 委派指令），按顺序追加至 `raw_prompts` 字符串数组（可适当提炼优化措辞以使需求更清晰）；
   - Agent 调研项目规范与代码后，将思考整理出的需求、规范、约束与要求写入 `refined_requirement`，指导后续执行。
2. **任务收敛（Completed）**：
   - 当 `mission-01` 的工作落地并验证完毕后，系统将其 `status` 更新为 `'completed'`；
   - 该任务沉淀出的物理资产（代码、文件）与环境事实自动归档。
3. **无缝切换（Transition）**：
   - 用户接着在同一会期内提出新任务（如“做数学作业”），系统识别出新目标，创建 `mission-02`，`status = 'in_progress'`，`sort_order = 2`；
   - 当前执行向量（微观断点 PC 指针）与计划拓扑立即切换到 `mission-02`；
   - **上下文隔离**：`mission-01` 的执行细节与中间过程被彻底隔绝，不污染当前工作记忆；
   - **资产继承**：`mission-01` 产出的客观事实可作为背景环境知识复用，班次级全局硬约束继续对 `mission-02` 生效。

