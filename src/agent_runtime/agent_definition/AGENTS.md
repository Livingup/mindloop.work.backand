# Agent 定义模块（agent_definition）

负责管理 Agent 的静态模板数据，描述一个 Agent 是什么、什么时候使用、启动时需要哪些系统提示词，以及允许调用哪些工具、MCP 工具、Skill 和子 Agent。

---

## 业务职责

- 以 `agent_id` 为唯一标识管理 Agent 定义。
- 每个 Agent 定义一个 YAML 文件，文件名即 `agent_id`。
- 存储目录由 `config/agent_runtime.yaml` 中的 `agent_definition.definitions_dir` 配置，默认是 `~/.spread-ai/agents`。
- 支持 `name`、`description`、`group`、`builtin_background_agent`、`disabled` 等管理元数据。
- 支持系统提示词混合片段数组：`text` 与 `template_id` 可交叉排列并保持顺序。
- 只保存引用 ID，不跨模块强校验引用是否存在；后续 Agent 执行引擎负责解析提示词片段、工具、MCP 服务、Skill 和子 Agent。

---

## YAML 格式

```yaml
agent_id: coding-agent
name: 编程 Agent
description: 负责代码阅读、修改、测试和提交
group: development
system_prompt:
  - text: 你是一个严格遵循项目架构规范的编程 Agent。
  - template_id: common-development-rules
  - text: ..........。
  - template_id: ............
tool_ids:
  - file_read_as_lines
  - text_search
mcp_tools:
  - service_id: local-filesystem
    tool_ids:
      - read_file
      - write_file
skill_ids:
  - python-development
available_sub_agent_ids:
  - code-review-agent
builtin_background_agent: false
disabled: false
```

---

## 字段说明

| 字段 | 说明 |
|---|---|
| `agent_id` | Agent 定义唯一标识，同时作为 YAML 文件名主名。 |
| `name` | Agent 展示名，可选。 |
| `description` | Agent 用途说明，可选。 |
| `group` | Agent 分组，默认 `default`。 |
| `system_prompt` | 系统提示词片段数组，支持 `text` 与 `template_id` 交叉混合。 |
| `tool_ids` | 本地业务工具授权列表，对应 tool 模块的 `tool_name`；`category="business"` 的本地工具必须写入此列表才可见/可调，`lifecycle` 与 `system` 工具默认可见，不需要写入。 |
| `mcp_tools` | MCP 工具绑定，记录 `service_id` 和该服务下允许使用的 `tool_ids`；本地 MCP 元工具默认可见不代表放宽这里的远端服务/工具白名单。 |
| `skill_ids` | Skill ID 列表，对应 skill 模块的 `skill_id`。 |
| `available_sub_agent_ids` | 当前 Agent 可调度的子 Agent 定义 ID 列表。 |
| `builtin_background_agent` | 是否为系统内置后台 Agent。 |
| `disabled` | 是否禁用。 |

---

## 业务规则

- `agent_id` 只能包含英文字母、数字、下划线和中划线，必须以英文字母或数字开头，长度不超过 64。
- `agent_id` 不能使用 Windows 保留文件名，例如 `CON`、`PRN`、`AUX`、`NUL`、`COM1`、`LPT1`。
- `system_prompt` 每个元素必须且只能包含 `text` 或 `template_id` 之一。
- `tool_ids`、`skill_ids`、`available_sub_agent_ids` 内部不允许重复。
- `available_sub_agent_ids` 不能包含当前 Agent 自身。
- `mcp_tools` 中同一个 `service_id` 不能重复，同一服务下 `tool_ids` 不能重复。
- Agent 定义模块只做定义自身结构校验，不直接调用 prompt_template、tool、mcp、skill 模块校验引用存在性。

---

## 分层结构

```text
agent_definition/
├── north_gateway/
│   ├── agent_definition_contracts.py      # 北向请求模型与 Protocol
│   ├── agent_definition_gateway_impl.py   # 北向网关实现
│   ├── agent_definition_cli_adapter.py    # spread-ai agent CLI
│   └── agent_definition_rest_adapter.py   # /api/agents REST 路由
├── infrastructure/
│   └── yaml_agent_definition_repository.py # YAML 文件仓储实现
├── agent_definition.py                    # 聚合根和值对象
├── agent_definition_repository.py         # 仓储接口
├── agent_definition_service.py            # 领域服务
├── agent_definition_config.py             # 配置对象
└── agent_definition_factory.py            # 依赖装配工厂
```

依赖方向：北向网关 → 领域服务 → 仓储接口 ← YAML 仓储实现。工厂是唯一知道 `ConfigRegistry` 和具体仓储实现的装配位置。

---

## CLI

```bash
spread-ai agent list
spread-ai agent list --group development --exclude-disabled
spread-ai agent get coding-agent
spread-ai agent create --file coding-agent.yaml
spread-ai agent update coding-agent --file coding-agent-update.yaml
spread-ai agent delete coding-agent
spread-ai agent enable coding-agent
spread-ai agent disable coding-agent
```

CLI 输入复杂字段时使用 YAML 文件，输出统一为 JSON，方便用户和外部 Agent 调用。

---

## REST

挂载前缀：`/api/agents`

- `GET /api/agents`
- `GET /api/agents?group=development&include_disabled=false`
- `GET /api/agents/{agent_id}`
- `POST /api/agents`
- `PUT /api/agents/{agent_id}`
- `DELETE /api/agents/{agent_id}`
- `POST /api/agents/{agent_id}/enable`
- `POST /api/agents/{agent_id}/disable`
