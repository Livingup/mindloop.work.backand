# Agent Runtime — 通用 Agent 运行时环境服务

为 AI Agent 提供完整的运行基础设施，包括 LLM 调用、工具执行、消息管理、文件资源管理等核心能力。

---

## 已完成模块

### 统一文件资源服务（file_resource）

负责 Agent 运行过程中所有文件资源的统一管理，包括图片、PDF 等文件的上传、存储、元数据管理和访问地址生成。支持本地存储和对象存储（阿里云 OSS）。

### 全局配置注册中心（config）

全局单例 ConfigRegistry，统一加载 YAML 配置文件并分发给各业务域。支持环境变量占位符解析、.env 文件加载、watchdog 文件监听热更新。

### 统一历史对话消息领域（uni_llm/message）

建立支持多平台（OpenAI / Claude / Gemini）的统一历史消息列表模型。包括统一消息模型、协议适配器、会话状态管理。不关心 LLM 调用本身，只关注消息的统一表示和协议转换。

### 会话持久化仓储（uni_llm/conversation_repository）

会话聚合根的持久化。支持 InMemory 内存实现和 SQLite + JSONL 混合持久化实现。会话组织层级：task_id → thread_id → agent_id → agent_instance_id → conversation_id。

### 提示词模板服务（prompt_template）

负责提示词可复用片段的增删改查管理。片段以 YAML 文件存储在 `.spread-ai/prompt-snippets/` 下（文件名 UUID），每个片段自声明唯一描述名，启动时扫描建立索引。Agent 定义通过 `ref` 引用描述名拼接系统提示词。

### 统一工具模块（tool）

负责本地工具的定义、自动发现、注册、执行，以及各 LLM 平台工具 JSON Schema 的生成与缓存管理。通过 `@tool` 装饰器声明工具函数（支持 name、description、group、public、wiki 元数据），import 时自动注册到全局仓储。内置函数签名→JSON Schema 解析器（支持基本类型、Optional、List、Dict、Literal、Pydantic BaseModel），以及 OpenAI / Claude / Gemini 三个平台的 Schema 生成器实现。Schema 生成是本模块职责，LLM 统一调用客户端只取现成 schema。DDD 分层：领域实体（聚合根）+ 仓储接口/内存实现 + 领域服务（四个业务用例）+ 北向网关（Gateway 契约 + CLI adapter + REST adapter）。CLI 命令：`spread-ai tool list/get/groups`。工具执行不提供 CLI 命令（工具可返回多模态数据，不适合 CLI 场景）。

### MCP 模块（mcp）

拆分为两个子模块：

**mcp_client** — 通用 MCP 服务管理和调用能力。不关心程序如何使用 MCP 服务，面向 MCP 协议提供通用能力。DDD 分层：领域实体 McpServiceDefinition（聚合根，含嵌套传输配置值对象 StdioTransportConfig / StreamableHttpTransportConfig）+ 值对象 5 个（McpServiceCapabilities / McpToolInfo / McpToolSummary / McpResourceInfo / McpResourceContent / McpToolResult）+ 仓储接口/YAML 实现（~/.spread-ai/mcp-servers/{service_id}.yaml）+ McpSessionManager（MCP SDK 连接池，支持 stdio 和 streamable-http 传输）+ 领域服务 McpClientService（配置管理 + 协议操作）+ 北向网关（McpClientGateway Protocol + DTO + 网关实现）+ CLI 适配器 + REST 适配器 + 工厂。CLI 命令：`spread-ai mcp-config list/get/add/update/remove/enable/disable`（配置管理）+ `spread-ai mcp inspect/tools/tool-detail/resources/read-resource`（协议操作）。

**mcp_meta_tools** — 供 Agent 使用的 5 个 MCP 元工具（@tool 装饰器注册为本地工具，async 函数）：mcp_get_service_detail / mcp_list_resources / mcp_read_resource / mcp_get_tool_detail / mcp_call_tool。依赖 mcp_client 北向网关契约。

**工具注册机制**：各模块的 `@tool` 装饰工具函数需在 `cli.py` 中显式 import 对应模块来触发注册（不采用全量扫描，避免启动变慢）。新增工具模块时需在 `cli.py` 中添加一行 `import`。

### Skill 模块（skill）

Skill 发现与元数据管理。扫描用户配置的 Skill 仓库目录（通用公共目录，不绑定本项目），解析各子目录中 `SKILL.md` 的 YAML frontmatter，提供 Skill 列表查询和刷新能力。遵循 Agent Skills Open Standard 规范。Skill 仓库路径在 `config/agent_runtime.yaml` 的 `skill_service.skills_dir` 中配置。用户直接在仓库目录下创建/编辑/删除 Skill 子目录，无需通过 API。启动时自动扫描 + 接口触发重新扫描。只解析 frontmatter 元数据（name、description、license、compatibility、metadata、allowed-tools），不读取 Markdown 正文（Agent 拿到目录路径后自行按规范读取）。DDD 分层：领域实体 SkillDefinition（聚合根，含 name 格式校验）+ 仓储接口/文件系统实现（扫描目录 + 解析 SKILL.md）+ 领域服务 + 北向网关（SkillGateway Protocol + DTO + 网关实现）+ CLI 适配器 + REST 适配器 + 工厂。CLI 命令：`spread-ai skill list`（列出所有 Skill）/ `spread-ai skill refresh`（重新扫描）。REST：`GET /api/skills` / `POST /api/skills/refresh`。

---

## 规划模块

### 模块一：Agent 定义

Agent 的静态模板数据，描述一个 Agent 是什么、能做什么。每个 Agent 定义为独立 YAML 文件（文件名即 agent_id），存储在 `.spread-ai/agents/` 下。

- 名称、描述（做什么、什么时候使用）
- 系统提示词（支持引用可复用提示词片段进行模板拼接）
- 本地工具列表
- MCP 工具列表（MCP 服务 ID + 可用工具）
- Skill 列表
- 子 Agent 列表

提示词片段存储在 `.spread-ai/prompt-snippets/` 下，每个片段文件自声明唯一描述名，启动时扫描建立映射。

### 模块二：LLM 服务商

LLM 服务商与模型的注册与元数据管理。

- 服务商支持哪些模型
- 模型元数据参数（最大输入 token、最大输出 token、思考设置、输入模态支持等）
- 模型使用的协议（OpenAI / Claude / Gemini）
- 自定义请求头、API Key 等凭证信息

### 模块三：Agent 执行引擎

Agent 的核心编排层，负责 Agent 实例的创建和任务执行循环。

- 根据用户选择的 Agent 定义创建 Agent 实例
- 根据 Agent 定义和用户选择的 LLM 服务商与模型创建 LLM 请求参数
- 循环调用 LLM 执行任务
- 将 LLM 返回的工具调用请求转发给工具执行引擎
- 将工具执行结果生成工具结果消息，继续调用 LLM

### 模块四：LLM 统一调用客户端

统一的 LLM 调用入口，屏蔽不同平台协议差异。

- 接收统一的输入：用户消息/工具执行结果消息、会话 ID、系统提示词、tools 定义、服务商与模型 ID
- 根据目标平台协议组装规范请求参数
- HTTP 请求和响应的原始报文记录（根据配置控制记录详细程度：是否打印 tools schema、是否打印系统提示词等）
- 调用目标平台 SDK 请求 LLM，将结果返回给 Agent 执行引擎

### 模块五：工具执行引擎

负责工具定义的实现与调用执行。

- 支持本地工具和 MCP 工具两种类型
- MCP 工具通过封装转发 MCP 客户端实现
- 定义通用的基础请求参数和响应参数规范，所有工具的执行请求和结果都派生于此

