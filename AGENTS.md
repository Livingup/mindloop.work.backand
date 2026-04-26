# Agent Runtime — 通用 Agent 运行时环境服务

为 AI Agent 提供完整的运行基础设施，包括 LLM 调用、工具执行、消息管理、文件资源管理等核心能力。

---

## 已完成模块

### 统一文件资源服务（file_resource）

负责 Agent 运行过程中所有文件资源的统一管理，包括图片、PDF 等文件的上传、存储、元数据管理和访问地址生成。支持本地存储和对象存储（阿里云 OSS）。

### 全局配置注册中心（config）

全局单例 ConfigRegistry，统一加载运行时 YAML 配置并分发给各业务域。支持环境变量占位符 `${ENV_VAR}` / `${ENV_VAR:-default}` 解析、`.env` 文件加载、watchdog 文件监听热更新。启动时只读取原始 YAML，各业务配置段在被访问时才解析环境变量并构建强类型配置对象，避免无关模块的必填环境变量阻塞 CLI 子命令。`spread-ai` 发布态 CLI 按 `--config` → `SPREAD_AI_CONFIG` → `~/.spread-ai/agent_runtime.yaml` 的顺序发现配置；若用户默认配置不存在，则首次运行自动从包内模板生成。仓库根目录的 `config/agent_runtime.yaml` 作为项目开发态配置存在。所有业务模块的配置（统一文件资源服务、会话持久化、提示词模板、Skill 服务、MCP 客户端、Agent 定义）均纳入同一配置体系，各模块只认 ConfigRegistry 内存数据，不关心配置文件的加载与维护。CLI 入口（`cli.py`）作为组合根，各模块网关通过工厂函数延迟创建（命令执行时 ConfigRegistry 已就绪），工厂函数以 `Callable` 形式传给 CLI adapter，adapter 在命令回调中调用。serve 命令同样从 ConfigRegistry 取配置创建网关。

### 会话消息模块（conversation_message）

统一历史对话消息管理，建立支持多平台（OpenAI / Claude / Gemini）的统一历史消息列表模型。包括统一消息模型、协议适配器、会话状态管理。不关心 LLM 调用本身，只关注消息的统一表示和协议转换。会话聚合根的持久化支持 InMemory 内存实现和 SQLite + JSONL 混合持久化实现，仓储接口定义在领域层（conversation_repository.py），实现类位于基础设施层（infrastructure/）。会话组织层级：task_id → thread_id → agent_id → agent_instance_id → conversation_id。

### 提示词模板服务（prompt_template）

负责提示词可复用片段的增删改查管理。片段以 YAML 文件存储在 `.spread-ai/prompt-snippets/` 下（文件名 UUID），每个片段自声明唯一描述名，启动时扫描建立索引。Agent 定义通过 `ref` 引用描述名拼接系统提示词。

### 统一工具模块（tool）

负责本地工具的定义、自动发现、注册、执行，以及各 LLM 平台工具 JSON Schema 的生成与缓存管理。通过 `@tool` 装饰器声明工具函数（支持 name、description、group、public、wiki 元数据），import 时自动注册到全局仓储。内置函数签名→JSON Schema 解析器（支持基本类型、Optional、List、Dict、Literal、Pydantic BaseModel），以及 OpenAI / Claude / Gemini 三个平台的 Schema 生成器实现。Schema 生成是本模块职责，LLM 统一调用客户端只取现成 schema。DDD 分层：领域实体（聚合根）+ 仓储接口/内存实现 + 领域服务（四个业务用例）+ 北向网关（Gateway 契约 + CLI adapter + REST adapter）。CLI 命令：`spread-ai tool list/get/groups`。工具执行不提供 CLI 命令（工具可返回多模态数据，不适合 CLI 场景）。

### MCP 模块（mcp）

拆分为两个子模块：

**mcp_client** — 通用 MCP 服务管理和调用能力。不关心程序如何使用 MCP 服务，面向 MCP 协议提供通用能力。DDD 分层：领域实体 McpServiceDefinition（聚合根，含嵌套传输配置值对象 StdioTransportConfig / StreamableHttpTransportConfig）+ 值对象 5 个（McpServiceCapabilities / McpToolInfo / McpToolSummary / McpResourceInfo / McpResourceContent / McpToolResult）+ 仓储接口/YAML 实现（~/.spread-ai/mcp-servers/{service_id}.yaml）+ McpSessionManager（MCP SDK 连接池，支持 stdio 和 streamable-http 传输）+ 领域服务 McpClientService（配置管理 + 协议操作）+ 北向网关（McpClientGateway Protocol + DTO + 网关实现）+ CLI 适配器 + REST 适配器 + 工厂。CLI 命令：`spread-ai mcp-config list/get/add/update/remove/enable/disable`（配置管理）+ `spread-ai mcp inspect/tools/tool-detail/resources/read-resource`（协议操作）。

**mcp_meta_tools** — 供 Agent 使用的 5 个 MCP 元工具（@tool 装饰器注册为本地工具，async 函数）：mcp_get_service_detail / mcp_list_resources / mcp_read_resource / mcp_get_tool_detail / mcp_call_tool。依赖 mcp_client 北向网关契约。

**工具注册机制**：各模块的 `@tool` 装饰工具函数需在 `cli.py` 中显式 import 对应模块来触发注册（不采用全量扫描，避免启动变慢）。新增工具模块时需在 `cli.py` 中添加一行 `import`。

**CLI 设计原则**：`spread-ai` 是给用户、脚本、CI 和外部 Agent 调用的统一脚本命令入口。命令按业务域组织，输入输出保持结构化；复杂输入使用文件；错误输出 JSON 且使用非 0 exit code；查询命令不产生业务副作用；子命令不能被无关模块配置阻塞。

### Skill 模块（skill）

Skill 发现与元数据管理。扫描用户配置的 Skill 仓库目录（通用公共目录，不绑定本项目），解析各子目录中 `SKILL.md` 的 YAML frontmatter，提供 Skill 列表查询和刷新能力。遵循 Agent Skills Open Standard 规范。Skill 仓库路径在 `config/agent_runtime.yaml` 的 `skill_service.skills_dir` 中配置。用户直接在仓库目录下创建/编辑/删除 Skill 子目录，无需通过 API。启动时自动扫描 + 接口触发重新扫描。只解析 frontmatter 元数据（name、description、license、compatibility、metadata、allowed-tools），不读取 Markdown 正文（Agent 拿到目录路径后自行按规范读取）。DDD 分层：领域实体 SkillDefinition（聚合根，含 name 格式校验）+ 仓储接口/文件系统实现（扫描目录 + 解析 SKILL.md）+ 领域服务 + 北向网关（SkillGateway Protocol + DTO + 网关实现）+ CLI 适配器 + REST 适配器 + 工厂。CLI 命令：`spread-ai skill list`（列出所有 Skill）/ `spread-ai skill refresh`（重新扫描）。REST：`GET /api/skills` / `POST /api/skills/refresh`。

### Agent 定义模块（agent_definition）

负责 Agent 静态模板数据管理。每个 Agent 定义一个 YAML 文件，文件名即 `agent_id`，默认存储在 `~/.spread-ai/agents/` 下，配置项为 `agent_definition.definitions_dir`。定义字段包括 `agent_id`、可选 `name` / `description`、`group`、系统提示词混合数组、本地工具 ID、MCP 服务工具绑定、Skill ID、可用子 Agent ID、是否系统内置后台 Agent、是否禁用。系统提示词数组支持 `text` 与 `template_id` 交叉混合并保持顺序，例如 `[{text: ...}, {template_id: ...}, {text: ...}]`。DDD 分层：领域实体 AgentDefinition（聚合根，携带文件名安全和引用列表去重校验）+ 值对象 SystemPromptSegment / McpToolBinding + 仓储接口/YAML 实现（`~/.spread-ai/agents/{agent_id}.yaml`）+ 领域服务 + 北向网关（Protocol + 请求模型 + 网关实现）+ CLI 适配器 + REST 适配器 + 工厂。CLI 命令：`spread-ai agent list/get/create/update/delete/enable/disable`。REST：`/api/agents` CRUD + enable/disable。

### LLM 服务商与模型注册模块（llm_model_registry）

负责用户自定义的 LLM 模型型号元数据、服务商定义和服务商模型接入项管理。主配置只保存注册数据根目录 `llm_model_registry.registry_dir`，默认 `~/.spread-ai/llm-model-registry`；具体业务数据不写死在系统配置或代码中，而是按 `models/{model_id}.yaml` 与 `providers/{provider_id}.yaml` 分目录存储。模型元数据字段包括 `model_id`、`name`、`context_window`、`max_output_tokens`、`compression_threshold`、`supports_tool_calling`、`supports_structured_output`、`supported_input_modalities`。服务商定义字段包括 `provider_id`、可选 `name`、默认 `api_base_url`、服务商级 `custom_headers`、默认 `api_keys`、`retry` 重试策略，以及 `models` 接入项列表；每个接入项包含 `id`、关联 `model_id`、请求实际模型名 `model_alias`、`api_format`、自定义 JSON 字符串 `tool_choice` / `thinking` / `custom_request_params`、模型专属 `api_base_url`、模型专属 `custom_headers` 和模型专属 `api_keys`。领域规则：ID 需要满足跨平台文件名安全格式；保存服务商时校验接入项引用的模型元数据必须存在；接入项 ID 在同一服务商内不能重复；`thinking` / `tool_choice` / `custom_request_params` 只校验 JSON 字符串合法性，不理解厂商语义；解析接入项时模型专属 base URL/API Keys 覆盖服务商默认值，headers 由服务商级与模型级合并且模型级同名覆盖。DDD 分层：领域实体 LlmModelMetadata / LlmProviderDefinition（聚合根）+ 值对象 LlmApiKey / LlmRetryPolicy / LlmProviderModelBinding + 仓储接口/YAML 实现 + 领域服务 + 北向网关（Protocol + 请求模型 + 网关实现）+ CLI 适配器 + REST 适配器 + 工厂。CLI 命令：`spread-ai llm-registry models list/get/save/delete`、`spread-ai llm-registry providers list/get/save/delete/models`、`spread-ai llm-registry resolve`。REST：`/api/llm-registry/models`、`/api/llm-registry/providers`、`/api/llm-registry/providers/{provider_id}/models/{binding_id}/resolved`。

---

## 规划模块

### 模块一：Agent 执行引擎

Agent 的核心编排层，负责 Agent 实例的创建和任务执行循环。

- 根据用户选择的 Agent 定义创建 Agent 实例
- 根据 Agent 定义和用户选择的 LLM 服务商与模型创建 LLM 请求参数
- 循环调用 LLM 执行任务
- 将 LLM 返回的工具调用请求转发给工具执行引擎
- 将工具执行结果生成工具结果消息，继续调用 LLM

### 模块二：LLM 统一调用客户端

统一的 LLM 调用入口，屏蔽不同平台协议差异。

- 接收统一的输入：用户消息/工具执行结果消息、会话 ID、系统提示词、tools 定义、服务商与模型 ID
- 根据目标平台协议组装规范请求参数
- HTTP 请求和响应的原始报文记录（根据配置控制记录详细程度：是否打印 tools schema、是否打印系统提示词等）
- 调用目标平台 SDK 请求 LLM，将结果返回给 Agent 执行引擎

### 模块三：工具执行引擎

负责工具定义的实现与调用执行。

- 支持本地工具和 MCP 工具两种类型
- MCP 工具通过封装转发 MCP 客户端实现
- 定义通用的基础请求参数和响应参数规范，所有工具的执行请求和结果都派生于此

