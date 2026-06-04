# Agent Runtime — 通用 Agent 运行时环境服务

为 AI Agent 提供完整的运行基础设施，包括 LLM 调用、工具执行、消息管理、文件资源管理等核心能力。

---

## 开发环境安装与拉取代码后的依赖刷新

`spread-ai` 命令由 `AgentRuntime/pyproject.toml` 的 `[project.scripts]` 声明生成，Windows 下通常表现为 Python 环境 `Scripts` 目录中的 `spread-ai.exe`。它只是 CLI 启动器，负责调用 `agent_runtime.cli:main`，业务代码仍从当前安装的 `agent-runtime` 包中导入。

本项目开发态建议在 `AgentRuntime` 目录执行 editable 安装：

```powershell
python -m pip install -e .
```

普通 `.py` 源码变更不需要重新安装；editable 安装会让 `spread-ai.exe` 继续导入工作区中的最新源码。但拉取代码、切换分支或合并代码后，如果出现以下变化，需要重新执行 `python -m pip install -e .` 刷新安装元数据和依赖：

- `pyproject.toml` 中新增、删除或修改了 `dependencies` / `optional-dependencies`。
- `pyproject.toml` 中新增、删除或修改了 `[project.scripts]` 命令入口，例如 `spread-ai = "agent_runtime.cli:main"`。
- 包结构、构建配置、包名、入口模块路径发生变化。
- 当前 Python 环境发生变化，例如从全局 Python 切换到虚拟环境，或更换了解释器。

如果只拉取到普通业务源码变更，且 `pyproject.toml` 未变化，通常无需重新生成 `spread-ai.exe`。如果运行时出现 `ModuleNotFoundError`，优先检查当前 `spread-ai` 所在 Python 环境是否已重新安装依赖。

---

## dev 与正式版启动/打包边界

`spread-ai serve` 只作为开发、调试、CI 和后端诊断入口，不作为正式 Windows 桌面产品的用户启动入口。正式版用户只启动前端仓库打包出来的 `Spread AI.exe`；该 Electron exe 会在 packaged 模式下读取随包复制到用户数据目录的 `agent_runtime.yaml`，用其中 `agent_runtime_service.service_host/service_port` 作为 release 启动元数据权威，自动拉起随包携带的后端 sidecar `spread-ai-runtime.exe`，等待 `/api/health` 成功后再打开窗口，并在应用退出时关闭后端进程。

dev 后端在本仓库独立启动，推荐使用项目内配置目录：

```powershell
spread-ai serve --config-file config/agent_runtime.yaml --env-file config/.env.dev
```

dev 端口由 `config/.env.dev` 中的 `SPREAD_AI_SERVICE_PORT` 提供，推荐固定为 `18000`。`config/.env.dev` 只能保存 dev 启动元数据覆盖，且必须通过 `--env-file config/.env.dev` 显式传入；`config/.env` 与 `config/.env.prod` 不再作为 OSS、云端 PostgreSQL 密码或其他用户业务密钥来源。

正式后端入口是 `agent_runtime.desktop_server:main`，由 PyInstaller 打包成 sidecar exe，供 Electron packaged 主进程内部调用。不要要求用户手动执行 `spread-ai serve` 启动正式后端。正式 release 后端监听地址和端口由 `agent_runtime.yaml` 的 `agent_runtime_service.service_host/service_port` 决定，Electron packaged 主进程正常只传 `--config-file`，不再用 `--host` / `--port` 覆盖 release 配置；如果 YAML 配置端口被占用，应由 Electron 启动前检查并清晰失败，不得静默切换随机端口。

正式版配置由 Electron 首次启动时从打包资源 `resources/runtime-config/agent_runtime.yaml` 复制到应用用户数据目录的 `runtime-config/agent_runtime.yaml`，该 YAML 是 packaged release 程序启动元数据权威，承载服务 host/port、本机数据根和 bootstrap/cloud 默认元数据等信息；它不能写入用户资源密钥、Runtime 本地登录凭据或共享云配置的长期本地权威缓存。正式 release 不扫描外部 `.env`，也不把目标机器 `SPREAD_AI_*` / `VITE_*` 环境变量作为启动或业务配置来源；dev/诊断可通过显式 `--env-file` 或 CLI 参数覆盖。Electron packaged 主进程只负责传入配置文件，不负责决定业务数据目录，也不得硬注入 `SPREAD_AI_DATA_ROOT`。`config/agent_runtime.yaml` 中所有运行数据路径都应通过 `${SPREAD_AI_DATA_ROOT:-~/.spread-ai}` 表达：未显式配置时，dev 与正式版默认使用同一套 `~/.spread-ai` 用户业务数据；dev/prod/release 是运行环境，不是同一台机器上的用户数据隔离边界。

构建正式版后端 sidecar：

```powershell
python scripts/build_desktop_runtime.py --output ..\agent-frontend\build-resources\backend
```

该命令生成 `build-resources/backend/spread-ai-runtime.exe`，随后由前端仓库的 Windows 打包脚本作为 `extraResources/backend` 打入正式桌面 exe。

---
## 已完成模块

### 统一文件资源服务（file_resource）

负责 Agent 运行过程中所有文件资源的统一管理，包括图片、PDF 等文件的上传、存储、元数据管理和访问地址生成。支持本地存储和对象存储（阿里云 OSS）。

### 全局配置注册中心（config）

全局单例 ConfigRegistry，统一加载运行时 YAML 配置并分发给各业务域。支持环境变量占位符 `${ENV_VAR}` / `${ENV_VAR:-default}` 解析和 watchdog 文件监听热更新；不会自动加载 YAML 同目录 `.env`，只有调用方显式传入的 `--env-file` 会作为启动元数据覆盖来源。启动时只读取原始 YAML，各业务配置段在被访问时才解析环境变量并构建强类型配置对象，避免无关模块的必填环境变量阻塞 CLI 子命令。`spread-ai config init --source <agent_runtime.yaml> [--force]` 负责把用户提供的服务配置复制到 `~/.spread-ai/agent_runtime.yaml`；普通 CLI 管理命令默认加载这一份初始化后的运行配置。`spread-ai serve` 是开发/诊断入口，默认同样加载 `~/.spread-ai/agent_runtime.yaml`，但允许通过 `--config-file` 与 `--env-file` 显式指定开发态配置源。正式 Windows 桌面版不通过 `spread-ai serve` 启动，而由 Electron packaged 主进程内部拉起 `agent_runtime.desktop_server` 打包出的 sidecar 后端。仓库根目录的 `config/agent_runtime.yaml` 作为项目提供的初始化配置来源示例/开发态服务配置存在。所有业务模块的配置（统一文件资源服务、会话持久化、Agent 运行态仓储与 Trace 目录、通用通信、Agent 执行循环、提示词模板、Skill 服务、MCP 客户端、Agent 定义、LLM 服务商与模型注册）均纳入同一配置体系，各模块只认 ConfigRegistry 内存数据，不关心配置文件的初始化与维护。CLI 入口（`cli.py`）作为组合根，各模块网关通过工厂函数延迟创建（命令执行时 ConfigRegistry 已就绪），工厂函数以 `Callable` 形式传给 CLI adapter，adapter 在命令回调中调用。运行时 REST 服务装配在 `agent_runtime.server` 中，供 `spread-ai serve` 与正式版内部后端入口复用。

#### 用户全局配置云端 PostgreSQL 边界

`cloud_user_config_database` 的 YAML 段只保存云端 PostgreSQL 默认连接元数据；真正可用的 PostgreSQL 启动凭据（包括 password，以及必要时同一组 host/port/database/username）属于每台机器本地用户数据，由 runtime bootstrap 本地存储覆盖，不能写入同一个云端 PostgreSQL 库以免形成自举悖论。缺少本机 bootstrap 凭据时，REST 服务只能进入初始化引导有限模式，不得把已批准的共享配置悄悄落回本地权威。

本机到云端的数据迁移只能通过一次性运维入口执行，例如 `spread-ai config migrate-user-global-to-cloud` 或等价手工脚本；不得在 runtime bootstrap、服务启动或普通业务请求中自动导入本机数据。完成迁移后再切换共享域 CRUD 权威。启用 PostgreSQL 后，已批准共享域必须以同一个 PostgreSQL 权威库为准；Runtime 后端可以维护只读进程内缓存并轮询数据库侧 revision 以降低读取成本，但该缓存不能成为失败后的本地权威或 silent fallback，也不能由前端保存、比较或轮询 revision。

当前已批准共享/云端权威范围包括 prompt snippets、Agent definitions、主 LLM Registry、图像生成 LLM Registry、图像生成运行时设置、Web Tools 运行时设置、context-compression 运行时设置与 worker prompts、`streamable-http` MCP 服务定义、LACP registry-service 设置，以及通过 bootstrap 配置的 OSS 对象存储设置。`stdio` MCP 服务定义、LACP instance registrations/runtime state、Skill、conversation/history、terminal/runtime state、scene/project/workspace、诊断数据、机器本地路径等仍属于本机或混合数据，不得进入云端用户共享配置，除非后续重新做范围设计并获确认。

### 会话消息模块（conversation_message）

统一历史对话消息管理，建立支持多平台（OpenAI / Claude / Gemini）的统一历史消息列表模型。包括统一消息模型、协议适配器、会话状态管理。不关心 LLM 调用本身，只关注消息的统一表示和协议转换。会话聚合根的持久化支持 InMemory 内存实现和 SQLite + JSONL 混合持久化实现，仓储接口定义在领域层（conversation_repository.py），实现类位于基础设施层（infrastructure/）。会话组织层级：task_id → thread_id → agent_id → agent_instance_id → conversation_id。

### 通用通信模块（communication）

提供业务无关的逻辑通信流、通信消息、消息日志持久化和后端主动推送接入能力。核心模型包括 `CommunicationStream`（逻辑 stream_id，不绑定 SSE/WebSocket 物理连接）和 `CommunicationMessage`（统一消息信封，字段含 stream_id、seq、type、topic、payload、metadata）。通信消息通过 SQLite 仓储持久化，开启 WAL 并按 stream 内递增 seq 支持断线后按 `after_seq` 恢复读取；进程内实时分发由 `InMemoryCommunicationMessageBus` 完成。北向网关提供创建/关闭流、发布消息、读取历史和订阅消息能力；REST adapter 暴露 `/api/communication/streams`、`/messages` 和 `/events` SSE。运行链路通过 `RuntimeCommunicationContext` 透传逻辑流 ID，并通过 `RuntimeEventPublisher` 发布 Agent、LLM、Tool 关键节点事件，底层运行对象不感知具体前端连接。

### 提示词模板服务（prompt_template）

负责提示词可复用片段的增删改查管理。片段以 YAML 文件存储在 `.spread-ai/prompt-snippets/` 下（文件名 UUID），每个片段自声明唯一描述名，启动时扫描建立索引。Agent 定义通过 `ref` 引用描述名拼接系统提示词。

### 统一工具模块（tool）

负责本地工具的定义、自动发现、注册、执行、统一执行协议，以及各 LLM 平台工具 JSON Schema 的生成与缓存管理。通过 `@tool` 装饰器声明工具函数（支持 name、description、group、public、wiki、category、always_enabled、allowed_loop_decisions 元数据），import 时自动注册到全局仓储。所有工具执行结果统一归一为 `BaseToolResult` / `ToolExecuteResult`：旧工具可继续返回原始业务值，运行时会自动包装为 `output` 且默认 `loop_control.decision=continue`；新工具可直接返回 `BaseToolResult` 表达 `success`、`execution_status`、`error_message`、`frontend_message` 和 `loop_control`。工具异常由 `LocalToolService` 转换为结构化失败结果，普通业务工具默认只允许 `continue`，生命周期工具可通过 `allowed_loop_decisions` 声明 `pause` / `complete` / `terminate` / `fail` 等决策。内置函数签名→JSON Schema 解析器支持基本类型、Optional、List、Dict、Literal、Pydantic BaseModel，以及 OpenAI / Claude / Gemini 三个平台的 Schema 生成器实现，其中 Gemini 生成器会内联 Pydantic `$defs/$ref` 以支持嵌套结构化参数。Schema 生成是本模块职责，LLM 统一调用客户端只取现成 schema。DDD 分层：领域实体（聚合根）+ 仓储接口/内存实现 + 领域服务（四个业务用例）+ 北向网关（Gateway 契约 + CLI adapter + REST adapter）。CLI 命令：`spread-ai tool list/get/groups`。工具执行不提供 CLI 命令（工具可返回多模态数据，不适合 CLI 场景）。

### Agent 生命周期控制模块（agent_lifecycle）

提供 always-enabled 的 `agent_lifecycle` 内置工具，用统一工具协议驱动 Agent 生命周期和用户沟通。该工具通过 `AgentLifecycleRequest` 接收 `notify`、`ask_user`、`pause`、`complete`、`terminate`、`fail` 动作，并返回 `BaseToolResult`：`notify` 映射 `continue`，`ask_user` / `pause` 映射 `pause`，`complete` 映射 `complete`，`terminate` 映射 `terminate`，`fail` 映射 `fail`。`ask_user.options` 必须是结构化对象数组，每个选项包含稳定 `id`、展示 `label` 和可选 `description`。前端展示内容通过 `frontend_message` 传递，并显式声明 `text_format`（plain_text / markdown / json）和可选 channel。该模块不让 Runtime 根据工具名硬编码生命周期逻辑，Runtime 只消费工具结果中的 `loop_control.decision`。

### MCP 模块（mcp）

拆分为两个子模块：

**mcp_client** — 通用 MCP 服务管理和调用能力。不关心程序如何使用 MCP 服务，面向 MCP 协议提供通用能力。DDD 分层：领域实体 McpServiceDefinition（聚合根，含嵌套传输配置值对象 StdioTransportConfig / StreamableHttpTransportConfig）+ 值对象 5 个（McpServiceCapabilities / McpToolInfo / McpToolSummary / McpResourceInfo / McpResourceContent / McpToolResult）+ 仓储接口/YAML 实现（~/.spread-ai/mcp-servers/{service_id}.yaml）+ McpSessionManager（MCP SDK 连接池，支持 stdio 和 streamable-http 传输）+ 领域服务 McpClientService（配置管理 + 协议操作）+ 北向网关（McpClientGateway Protocol + DTO + 网关实现）+ CLI 适配器 + REST 适配器 + 工厂。CLI 命令：`spread-ai mcp-config list/get/add/update/remove/enable/disable`（配置管理）+ `spread-ai mcp inspect/tools/tool-detail/resources/read-resource`（协议操作）。

**mcp_meta_tools** — 供 Agent 使用的 5 个 MCP 元工具（@tool 装饰器注册为本地工具，async 函数）：mcp_get_service_detail / mcp_list_resources / mcp_read_resource / mcp_get_tool_detail / mcp_call_tool。依赖 mcp_client 北向网关契约。

**工具注册机制**：各模块的 `@tool` 装饰工具函数需在组合根或工厂中显式 import 对应模块来触发注册（不采用全量扫描，避免启动变慢）。面向 CLI 的工具模块通常在 `cli.py` 中 import；运行时必须默认可用的工具（如 `agent_lifecycle`）需要在 `LocalToolFactory.create()` 中 import，确保 Agent 执行器创建本地工具网关时已注册。

**CLI 设计原则**：`spread-ai` 是给用户、脚本、CI 和外部 Agent 调用的统一脚本命令入口。命令按业务域组织，输入输出保持结构化；复杂输入使用文件；错误输出 JSON 且使用非 0 exit code；查询命令不产生业务副作用；子命令不能被无关模块配置阻塞。

### Skill 模块（skill）

Skill 发现与元数据管理。扫描用户配置的 Skill 仓库目录（通用公共目录，不绑定本项目），解析各子目录中 `SKILL.md` 的 YAML frontmatter，提供 Skill 列表查询和刷新能力。遵循 Agent Skills Open Standard 规范。Skill 仓库路径在 `config/agent_runtime.yaml` 的 `skill_service.skills_dir` 中配置。用户直接在仓库目录下创建/编辑/删除 Skill 子目录，无需通过 API。启动时自动扫描 + 接口触发重新扫描。只解析 frontmatter 元数据（name、description、license、compatibility、metadata、allowed-tools），不读取 Markdown 正文（Agent 拿到目录路径后自行按规范读取）。DDD 分层：领域实体 SkillDefinition（聚合根，含 name 格式校验）+ 仓储接口/文件系统实现（扫描目录 + 解析 SKILL.md）+ 领域服务 + 北向网关（SkillGateway Protocol + DTO + 网关实现）+ CLI 适配器 + REST 适配器 + 工厂。CLI 命令：`spread-ai skill list`（列出所有 Skill）/ `spread-ai skill refresh`（重新扫描）。REST：`GET /api/skills` / `POST /api/skills/refresh`。

### Agent 定义模块（agent_definition）

负责 Agent 静态模板数据管理。每个 Agent 定义一个 YAML 文件，文件名即 `agent_id`，默认存储在 `~/.spread-ai/agents/` 下，配置项为 `agent_definition.definitions_dir`。定义字段包括 `agent_id`、可选 `name` / `description`、`group`、系统提示词混合数组、本地工具 ID、MCP 服务工具绑定、Skill ID、可用子 Agent ID、是否系统内置后台 Agent、是否禁用。系统提示词数组支持 `text` 与 `template_id` 交叉混合并保持顺序，例如 `[{text: ...}, {template_id: ...}, {text: ...}]`。DDD 分层：领域实体 AgentDefinition（聚合根，携带文件名安全和引用列表去重校验）+ 值对象 SystemPromptSegment / McpToolBinding + 仓储接口/YAML 实现（`~/.spread-ai/agents/{agent_id}.yaml`）+ 领域服务 + 北向网关（Protocol + 请求模型 + 网关实现）+ CLI 适配器 + REST 适配器 + 工厂。CLI 命令：`spread-ai agent list/get/create/update/delete/enable/disable`。REST：`/api/agents` CRUD + enable/disable。

### Agent 运行时实例与任务管理模块（agent）

负责程序内部动态运行态管理和真正 Agent 执行循环。本模块不提供 REST/CLI 运行态管理接口，只作为程序内部调用入口。一个项目运行时支持无上限多个 task 并发；每个 task 支持无上限多个 Agent 实例并发；每个 task 必须且只能有一个根 Agent 实例入口，并在 task 内维护 Agent 实例父子关系树。核心领域模型包括 `AgentTask`（任务聚合根，字段含 `task_id`、`root_agent_instance_id`、`agent_instance_ids`、`parent_child_relations`、任务状态和时间戳）、`AgentInstance`（运行时实例，字段含 `instance_id`、`task_id`、`thread_id`、`agent_definition_id`、`llm_provider_id`、`llm_model_binding_id`、`conversation_id`、`trace_id`、实例状态和时间戳）、`AgentParentChildRelation`（同一 task 内父子实例关系边）。任务/实例状态均为 `created` / `running` / `paused` / `completed` / `failed` / `cancelled`，状态流转显式触发；`agent instance completed` 不自动代表 `task completed`，root 执行循环正常结束时由 `AgentExecutionEngine` 显式完成实例和任务。父子树规则：唯一 root，root 不能有 parent，非 root 必须有且只有一个 parent，父子实例必须属于同一 task，禁止环，所有实例必须从 root 可达。运行态仓储接口为 `AgentRuntimeRepository`，基础设施实现包括线程安全内存仓储 `InMemoryAgentRuntimeRepository` 和 SQLite 共享仓储 `SqliteAgentRuntimeRepository`；`agent_runtime.repository_type=sqlite` 时 task、Agent 实例和父子关系写入 `agent_runtime.data_dir/agent_runtime.db`，开启 WAL，支持本机 FastAPI 多 worker / 多 Python 进程共享运行态。`AgentRuntimeFactory` 根据 ConfigRegistry 装配 memory/sqlite 仓储，并在单 Python 进程内复用同一网关。

`AgentExecutionEngine` 是内部执行循环入口：创建 task + root Agent 实例，加载 `AgentDefinition`，拼接系统提示词（支持 `text` 与 `template_id`，但 Trace 不记录系统提示词），并额外注入 Runtime lifecycle tool policy，指导 LLM 使用 `agent_lifecycle` 进行用户沟通、声明前端文本格式和结构化 `ask_user.options`。执行循环创建/加载 Conversation，追加用户消息，解析 LLM provider/binding，构造当前 AgentDefinition 允许的工具 schema，调用通用 LLM 请求客户端，收到 assistant 响应后写回 Conversation；如果响应包含 `ToolUsePart`，交给 `AgentToolDispatcher` 执行本地工具或 MCP 工具，追加 `ToolResultPart` 后读取每个工具返回的 `BaseToolResult.loop_control.decision`，按 fail > terminate > complete > pause > continue 优先级控制任务状态和循环。无工具调用不再自动表示完成，而是作为 plain text fallback 继续提示模型使用生命周期工具；连续达到 `_PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD` 后暂停 instance/task。工具调用轮数仍受 `agent_execution.max_tool_rounds` 限制。执行失败时显式 fail instance 和 task。`AgentToolDispatcher` 暴露当前 `AgentDefinition.tool_ids`、`AgentDefinition.mcp_tools` 允许的工具，以及 `always_enabled=True` 的本地工具；本地工具通过 `LocalToolGateway.execute_tool()` 执行；MCP 工具以 `mcp__{service_id}__{tool_name}` 暴露给 LLM，执行时还原 service/tool 并通过 `McpClientGateway.call_tool()` 调用，MCP 结果同样包装为 `BaseToolResult`。每个 Agent 实例执行过程由 `AgentTraceRecorder` 单独写 Markdown 文件到 `agent_runtime.trace_dir/{task_id}/`，文件名包含 `agent_definition_id`、时间戳、`instance_id`、`trace_id` 和随机后缀；trace 文件只记录 `agent_definition_id`、task/instance/thread/trace ID、用户消息、assistant 消息、工具结果和完成/失败事件，不记录系统提示词、工具 schema、工具清单、API Key 或请求头。

### Runtime 本地认证与 Scene Presence 生命周期

Phase-1 访问模型是单 Runtime 主机的多端接入：后端 runtime-local auth 是权限边界，普通 `/api/*` REST 只接受 Bearer `Authorization` header；仅浏览器原生无法设置 Authorization header 且已明确收敛的通道可使用 `access_token` query token：EventSource `/api/communication/streams/*/events`、文件上传进度 EventSource `/api/file-resources/upload-tasks/*/events`、文件内容 GET `/api/file-resources/*/content`，以及 sendBeacon detach `/api/agent-runtime/scenes/*/detach`。不得把 query token 扩展到普通 REST、配置读写、文件资源元数据、上传任务快照或其他写入接口。`SceneSessionRegistry` 只记录多端 presence，不拥有任务生命周期；attach、heartbeat、detach、stop 必须保持分离。关闭窗口、unload、workspace switch 和 `/detach` 只关闭当前 `client_session_id`，不得取消 task/instance；显式停止必须走 `/stop` 或 per-Agent interrupt/stop。Scene 自动释放只允许在零客户端且连续 30 分钟没有新的 task/instance 活动、且没有 running instance 时执行，判定异常必须保守跳过。

### 通用 LLM 请求客户端（llm_client）

负责真实 LLM API 请求编排，屏蔽不同 API 协议的请求细节。`LlmRequestService` 接收 `LlmPromptRequest`，使用 `conversation_message.render_messages()` 渲染目标 `api_format` 的历史消息，合并 `ResolvedLlmProviderModel` 中的 `model_alias`、`api_base_url`、`api_keys`、`custom_headers`、`retry`、`tool_choice`、`thinking`、`custom_request_params` 和本轮覆盖参数，按 API Key 列表轮换并按 `LlmRetryPolicy` 对 429/5xx 等临时错误做重试；成功后调用 `conversation_message.ingest_response()` 归一化 assistant 响应并写回 Conversation。南向协议客户端当前实现三类已具备 message adapter 的 API 格式：`openai_responses`、`anthropic_messages`、`gemini_generate_content`。全局枚举中存在但本轮未实现真实调用的 `openai_chat_completions` 与 `zhipu_chat_completions` 需要先补齐 conversation_message 协议适配器后再接入。

### LLM 服务商与模型注册模块（llm_model_registry）

负责用户自定义的 LLM 模型型号元数据、服务商定义、服务商模型接入项和执行策略组管理。主配置只保存注册数据根目录 `llm_model_registry.registry_dir`，默认 `~/.spread-ai/llm-model-registry`；运行期权威数据存储为该目录下的 `llm_model_registry.sqlite3`，legacy YAML 只作为一次性迁移输入，不在应用启动或仓储初始化时自动导入。模型元数据字段包括 `model_id`、`context_window`、`max_output_tokens`、`compression_threshold`、`supports_tool_calling`、`supports_structured_output`、`supported_input_modalities`，其中 `supported_input_modalities` 只能从系统统一输入模态 `text` / `image` / `pdf` 中选择，统一来源为 `src/agent_runtime/input_modality.py`。服务商定义字段包括 `provider_id`、默认 `api_base_url`、服务商级 `custom_headers`、默认 `api_keys`、`retry` 重试策略，以及 `models` 接入项列表；每个接入项包含 `id`、关联 `model_id`、请求实际模型名 `model_alias`、全局枚举 `api_format`（允许值：`anthropic_messages` / `openai_responses` / `openai_chat_completions` / `gemini_generate_content` / `zhipu_chat_completions`）、自定义 JSON 字符串 `tool_choice` / `thinking` / `custom_request_params`、模型专属 `api_base_url`、模型专属 `custom_headers` 和模型专属 `api_keys`。服务商级 `api_keys`、模型接入项 `api_keys`、服务商模型接入项列表和执行策略候选项列表的顺序均有业务含义，SQLite 仓储必须通过 position 字段保序，前端保存时不得按名称、ID 或时间重新排序；API Key 数组顺序即运行时轮换优先级。领域规则：ID 需要满足跨平台文件名安全格式；保存服务商时校验接入项引用的模型元数据必须存在；删除模型元数据会阻止删除仍被服务商引用的模型；删除服务商会阻止删除仍被执行策略组引用的服务商；保存执行策略组时校验候选项引用的服务商模型接入项必须存在；接入项 ID 在同一服务商内不能重复；`api_format` 通过全局 `llm_api_protocol` 枚举校验并可推导 `sdk_platform`（`anthropic` / `openai` / `gemini` / `zhipu`）；`thinking` / `tool_choice` / `custom_request_params` 只校验 JSON 字符串合法性，不理解厂商语义；解析接入项时模型专属 base URL/API Keys 覆盖服务商默认值，headers 由服务商级与模型级合并且模型级同名覆盖。DDD 分层：领域实体 LlmModelMetadata / LlmProviderDefinition / LlmExecutionPolicy（聚合根）+ 值对象 LlmApiKey / LlmRetryPolicy / LlmProviderModelBinding / LlmExecutionPolicyCandidate + 仓储接口/SQLite 实现 + 领域服务 + 北向网关（Protocol + 请求模型 + 网关实现）+ CLI 适配器 + REST 适配器 + 工厂。CLI 命令：`spread-ai llm-registry models list/get/save/delete`、`spread-ai llm-registry providers list/get/save/delete`、`spread-ai llm-registry resolve`。REST：`/api/llm-registry/models`、`/api/llm-registry/providers`、`/api/llm-registry/execution-policies`、`/api/llm-registry/providers/{provider_id}/models/{binding_id}/resolved`。

---

## 规划模块

暂无。
