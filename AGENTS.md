# Agent Runtime — 通用 Agent 运行时环境服务

为 AI Agent 提供完整的运行基础设施，包括 LLM 调用、工具执行、消息管理、文件资源管理等核心能力。

---

## 开发环境安装与配置管理入口边界

本仓库不再提供 `spread-ai` OS/system CLI，也不再通过 `pyproject.toml [project.scripts]` 暴露配置管理命令。不要新增或恢复 `agent_runtime.cli`、`*_cli_adapter.py`、release CLI exe、PATH 注入或直接读写 PG/YAML/SQLite 的传统 CLI。

配置管理的长期入口是 Runtime 后端 REST API，以及 release 随包内置并自动安装到用户 Skill 仓库的 `spread-ai-config-management` Skill/脚本；该脚本只是本机 REST client，必须复用 Runtime auth/cache/reconcile/持久化逻辑。

本项目开发态如需刷新依赖，仍可在 `AgentRuntime` 目录执行 editable 安装：

```powershell
python -m pip install -e .
```

普通 `.py` 源码变更不需要重新安装。拉取代码、切换分支或合并代码后，如果 `pyproject.toml` 的 `dependencies` / `optional-dependencies`、包结构、构建配置、包名或入口模块路径发生变化，再重新执行 `python -m pip install -e .`。

---

## dev 与正式版启动/打包边界

正式版用户只启动前端仓库打包出来的 `Spread AI.exe`；该 Electron exe 会在 packaged 模式下读取随包复制到用户数据目录的 `agent_runtime.yaml`，用其中 `agent_runtime_service.service_host/service_port` 作为 release 启动元数据权威，自动拉起随包携带的后端 sidecar `spread-ai-runtime.exe`，等待 `/api/health` 成功后再打开窗口，并在应用退出时关闭后端进程。

dev 后端在本仓库独立启动时，使用内部后端模块而不是 OS CLI，推荐使用项目内配置目录：

```powershell
python -m agent_runtime.desktop_server --config-file config/agent_runtime.yaml --env-file config/.env.dev
```

dev 端口由 `config/.env.dev` 中的 `SPREAD_AI_SERVICE_PORT` 提供，推荐固定为 `18000`；dev 数据根由 `SPREAD_AI_DATA_ROOT` 提供，默认固定为 `~/.spread-ai-dev`，用于源码自测沙箱，避免写入正式安装版默认使用的 `~/.spread-ai` 用户数据。`config/.env.dev` 只能保存 dev 启动元数据覆盖，且必须通过 `--env-file config/.env.dev` 显式传入；`config/.env` 与 `config/.env.prod` 不再作为 OSS、云端 PostgreSQL 密码或其他用户业务密钥来源。

正式后端入口是 `agent_runtime.desktop_server:main`，由 PyInstaller 打包成 sidecar exe，供 Electron packaged 主进程内部调用。不要要求用户手动执行任何 `spread-ai` OS CLI 启动正式后端。正式 release 后端监听地址和端口由 `agent_runtime.yaml` 的 `agent_runtime_service.service_host/service_port` 决定，Electron packaged 主进程正常只传 `--config-file`，不再用 `--host` / `--port` 覆盖 release 配置；如果 YAML 配置端口被占用，应由 Electron 启动前检查并清晰失败，不得静默切换随机端口。正式 sidecar 可额外接收 `--frontend-dist-dir` 挂载同一份 Vite production `dist/`：`/api/*` 仍是 Runtime API，非 `/api` 路径服务 release SPA，供 PC/手机浏览器按页面自身 HTTP origin 访问同一个 Runtime。

正式 release 后端是 PyInstaller `--noconsole` GUI sidecar；任何从 Runtime 后端启动的普通 Windows 子进程（git、rg、node、诊断命令等）默认必须复用 `agent_runtime.subprocess_utils.hidden_subprocess_kwargs()` 或等价隐藏策略，避免用户执行 Agent 任务时出现 CMD/控制台黑框闪烁。`WINPTY_SHOW_CONSOLE=1` 仅允许作为显式本机诊断开关，正式终端 PTY 环境不得无意继承该变量。

正式 release 监听地址是安全边界。任何非 loopback / LAN 暴露默认值调整前，必须保持无账号首次初始化只能由本机来源完成；未创建 Runtime 本地账号时，除健康检查和账号初始化入口外，普通 `/api/*` 不得因为“无账号”而向 LAN 客户端免认证开放。

远程访问网络通道（FRP/Caddy/VPS/DNS/证书/公网端口/proxy/token/frpc 启停等）属于外部系统网络或部署运维配置；Runtime 与前端不保存、不下载、不启动、不停止也不配置这些通道参数。外部 FRP 若把请求转发到本机完整可用的前端/Runtime 服务入口，程序只按普通 HTTP/API/SSE 请求处理，并继续负责自身认证、first-run 安全和本机监听边界。

正式版配置由 Electron 首次启动时从打包资源 `resources/runtime-config/agent_runtime.yaml` 复制到应用用户数据目录的 `runtime-config/agent_runtime.yaml`，该 YAML 是 packaged release 程序启动元数据权威，承载服务 host/port、本机数据根和 bootstrap/cloud 默认元数据等信息；它不能写入用户资源密钥、Runtime 本地登录凭据或共享云配置的长期本地权威缓存。正式 release 不扫描外部 `.env`，也不把目标机器 `SPREAD_AI_*` / `VITE_*` 环境变量作为启动或业务配置来源；dev/诊断可通过显式 `--env-file` 或 CLI 参数覆盖。Electron packaged 主进程只负责传入配置文件，不负责决定业务数据目录，也不得硬注入 `SPREAD_AI_DATA_ROOT`。`config/agent_runtime.yaml` 中所有运行数据路径都应通过 `${SPREAD_AI_DATA_ROOT:-~/.spread-ai}` 表达：正式安装版未显式配置时默认使用 `~/.spread-ai` 用户业务数据；源码 dev 自测必须通过 `config/.env.dev` 显式设置 `SPREAD_AI_DATA_ROOT=~/.spread-ai-dev`，作为可重置的沙箱数据根。除非明确做迁移或兼容性验证，不要让 dev 临时服务直接读写 release 用户数据根。

构建正式版后端 sidecar：

```powershell
python scripts/build_desktop_runtime.py --output ..\agent-frontend\build-resources\backend
```

该命令生成 `build-resources/backend/spread-ai-runtime.exe`，随后由前端仓库的 Windows 打包脚本作为 `extraResources/backend` 打入正式桌面 exe。

---

## 运维资源环境

- 云端 PostgreSQL 资源：轻量云实例 `liuliangMtI5Khg8gfjU`（`#server-19413`），公网 `103.236.88.224`；实例必须保持运行，暂停会导致 SSH 与数据库不可用。
- SSH：`ssh -i ~/.ssh/lacp_registry_deploy_ed25519 -p 44524 root@103.236.88.224`（本机已配置公钥）。
- PostgreSQL：项目访问 `db.mindloop.chat:25432`，数据库 `agent_runtime`，用户 `agent_runtime_client`；密码只从本机 `~/.spread-ai/cloud-user-config/cloud_user_config_bootstrap.db` 读取，不写入仓库。
- 服务端：Docker 容器 `agent-runtime-postgres`（PostgreSQL 16），容器监听 `5432`；排查优先看 `docker ps`、`docker logs agent-runtime-postgres` 和外部 psycopg 连通性。

---
## 已完成模块

### 统一文件资源服务（file_resource）

负责 Agent 运行过程中所有文件资源的统一管理，包括图片、PDF 等文件的上传、存储、元数据管理和访问地址生成。支持本地存储和对象存储（阿里云 OSS）。

### 全局配置注册中心（config）

全局单例 ConfigRegistry，统一加载运行时 YAML 配置并分发给各业务域。支持环境变量占位符 `${ENV_VAR}` / `${ENV_VAR:-default}` 解析和 watchdog 文件监听热更新；不会自动加载 YAML 同目录 `.env`，只有调用方显式传入的 `--env-file` 会作为启动元数据覆盖来源。启动时只读取原始 YAML，各业务配置段在被访问时才解析环境变量并构建强类型配置对象。正式 Windows 桌面版由 Electron packaged 主进程内部拉起 `agent_runtime.desktop_server` 打包出的 sidecar 后端。仓库根目录的 `config/agent_runtime.yaml` 作为项目提供的初始化配置来源示例/开发态服务配置存在。所有业务模块的配置均纳入同一配置体系，各模块只认 ConfigRegistry 内存数据，不关心配置文件的初始化与维护；配置管理对外走 Runtime REST 与内置 Skill 脚本，不再走 OS CLI。

#### 用户全局配置云端 PostgreSQL 边界

`cloud_user_config_database` 的 YAML 段只保存云端 PostgreSQL 默认连接元数据；真正可用的 PostgreSQL 启动凭据（包括 password，以及必要时同一组 host/port/database/username）属于每台机器本地用户数据，由 runtime bootstrap 本地存储覆盖，不能写入同一个云端 PostgreSQL 库以免形成自举悖论。缺少本机 bootstrap 凭据时，REST 服务只能进入初始化引导有限模式，不得把已批准的共享配置悄悄落回本地权威。

本机到云端的数据迁移只能通过一次性受控运维入口或等价手工脚本执行；不得在 runtime bootstrap、服务启动或普通业务请求中自动导入本机数据。完成迁移后再切换共享域 CRUD 权威。启用 PostgreSQL 后，已批准共享域必须以同一个 PostgreSQL 权威库为准；Runtime 后端可以维护只读进程内缓存并轮询数据库侧 revision 以降低读取成本，但该缓存不能成为失败后的本地权威或 silent fallback，也不能由前端保存、比较或轮询 revision。

当前已批准共享/云端权威范围包括 prompt snippets、Agent definitions、主 LLM Registry、图像生成 LLM Registry、图像生成运行时设置、Web Tools 运行时设置、context-compression 运行时设置与 worker prompts、`streamable-http` MCP 服务定义、LACP registry-service 设置，以及通过 bootstrap 配置的 OSS 对象存储设置。`stdio` MCP 服务定义、LACP instance registrations/runtime state、Skill、conversation/history、terminal/runtime state、scene/project/workspace、诊断数据、机器本地路径等仍属于本机或混合数据，不得进入云端用户共享配置，除非后续重新做范围设计并获确认。

### 会话消息模块（conversation_message）

统一历史对话消息管理，建立支持多平台（OpenAI / Claude / Gemini / 智谱）的统一历史消息列表模型。包括统一消息模型、协议适配器、会话状态管理。不关心 LLM 调用本身，只关注消息的统一表示和协议转换。会话聚合根的持久化支持 InMemory 内存实现和 SQLite + JSONL 混合持久化实现，仓储接口定义在领域层（conversation_repository.py），实现类位于基础设施层（infrastructure/）。会话组织层级：task_id → thread_id → agent_id → agent_instance_id → conversation_id。

#### 协议与时间线核心约束
1. 统一会话模型的消息列表（`Conversation.messages`）是通用规范消息列表（只包含 `user`、`assistant`、`tool` 等通用角色），绝对不得直接追加任何 OpenAI Responses 协议的 `developer` 消息或 `additional_tools` 进行污染。
2. 系统提示词与工具组件的变更记录（时间线）统一由 Agent 维护并记录在 `Conversation.timeline`；会话模型只负责记录，底层根据变更记录和当次请求上下文动态构建适合目标协议的请求结构。
3. 针对 OpenAI Responses 协议：系统提示词和 tools 投影到 `input`（作为 `developer` 消息与 `additional_tools`）并在初始提示词末尾打上显式缓存断点（自定义断点+自动隐式断点模式），是该协议本身的强制要求，与会话是否存在时间线彻底解耦。未发生角色切换的纯净会话（无时间线）无条件自动投影为首部 input 配置；时间线仅在中途发生定义切换时记录历史快照并按锚点追加新事件以维持前缀缓存。
4. 针对非 OpenAI Responses 协议（Claude、Gemini、Chat Completions 等）：底层自动忽略配置时间线重放，直接按本次请求传入的最新的系统提示词和工具定义处理。
5. 异构模型接力对话时，各协议适配器遵循“同协议无损原样回灌 raw、跨协议公共层安全降级”原则；跨协议时必须剥离平台私有签名或加密思维链（如 Claude signature、OpenAI encrypted_content），绝不得伪造成目标协议的思维链凭证。
6. 子 Agent 继承父 Agent 会话前缀时，必须构建并继承配置时间线：将父会话配置冻结为 start 处的 historical 快照；子 Agent 的新配置作为 active 事件锚定在边界消息（boundary message）之后。子 Agent 系统提示词需在最开头挂载补充提示词（明确任务边界与可用工具范围），支持两阶段占位符替换（创建事件时替换父实例 ID，协议渲染时替换最新可用工具名列表）。子 Agent 工具若为父 Agent 工具子集，不追加新的 `additional_tools`，由提示词声明可用权限；若不包含，仅增量追加缺失的差集工具（$\Delta Tools = Tools_{child} \setminus Tools_{parent}$），确保最大化维持前缀缓存。

### 通用通信模块（communication）

提供业务无关的逻辑通信流、通信消息、消息日志持久化和后端主动推送接入能力。核心模型包括 `CommunicationStream`（逻辑 stream_id，不绑定 SSE/WebSocket 物理连接）和 `CommunicationMessage`（统一消息信封，字段含 stream_id、seq、type、topic、payload、metadata）。通信消息通过 SQLite 仓储持久化，开启 WAL 并按 stream 内递增 seq 支持断线后按 `after_seq` 恢复读取；进程内实时分发由 `InMemoryCommunicationMessageBus` 完成。北向网关提供创建/关闭流、发布消息、读取历史和订阅消息能力；REST adapter 暴露 `/api/communication/streams`、`/messages` 和 `/events` SSE。运行链路通过 `RuntimeCommunicationContext` 透传逻辑流 ID，并通过 `RuntimeEventPublisher` 发布 Agent、LLM、Tool 关键节点事件，底层运行对象不感知具体前端连接。

### 提示词模板服务（prompt_template）

负责提示词可复用片段的增删改查管理。片段以 YAML 文件存储在 `.spread-ai/prompt-snippets/` 下（文件名 UUID），每个片段自声明唯一描述名，启动时扫描建立索引。Agent 定义通过 `ref` 引用描述名拼接系统提示词。

### 统一工具模块（tool）

负责本地工具的定义、自动发现、注册、执行、统一执行协议，以及各 LLM 平台工具 JSON Schema 的生成与缓存管理。通过 `@tool` 装饰器声明工具函数，import 时自动注册到全局仓储。`category` 承载默认可见语义，只有 `lifecycle` 与 `system` 工具默认对所有 Agent 可见，`business` 工具必须通过 `AgentDefinition.tool_ids` 显式授权。工具异常由 `LocalToolService` 转换为结构化失败结果，生命周期工具可通过 `allowed_loop_decisions` 声明生命周期决策。Schema 生成是本模块职责，LLM 统一调用客户端只取现成 schema。本模块不再提供 OS CLI 发现入口。

### Agent 生命周期控制模块（agent_lifecycle）

提供 always-enabled 的 4 个生命周期内置工具 `message_user` / `pause_task` / `end_task` / `fail_task`，统一通过 `BaseToolResult.loop_control.decision` 驱动 Runtime 生命周期，而不是让 Runtime 根据工具名硬编码行为。`message_user` 负责唯一的面向用户沟通通道，`notify` 映射 `continue`、`request_response` 映射 `pause`，并要求 `frontend_message.text_format=html` 与结构化 `options`；`pause_task` 只表达暂停意图，`timeout_seconds=-1` 表示无限期等待外部事件，`timeout_seconds>0` 表示外部事件或时间截止任一先到都可恢复下一轮 LLM 循环，结构化策略通过 `pause_timeout_seconds` / `pause_wake_mode` metadata 传递给 Runtime；`end_task` 映射 `complete`，`fail_task` 映射 `fail`。

### MCP 模块（mcp）

拆分为两个子模块：

**mcp_client** — 通用 MCP 服务管理和调用能力。不关心程序如何使用 MCP 服务，面向 MCP 协议提供通用能力。配置管理通过 Runtime REST `/api/mcp-config` 暴露给前端和内置配置管理 Skill；协议操作 REST `/api/mcp` 供运行时受控使用，不作为 OS CLI 公共面。DDD 分层保留领域实体、仓储、会话管理、领域服务、北向网关、REST 适配器与工厂。

**mcp_meta_tools** — 供 Agent 使用的 5 个 MCP 元工具（@tool 装饰器注册为本地工具，async 函数）：mcp_get_service_detail / mcp_list_resources / mcp_read_resource / mcp_get_tool_detail / mcp_call_tool。它们保持 `group="mcp"`、`category="system"`，默认对 Agent 可见；实际可用的远端 MCP 服务和工具仍必须由 `AgentDefinition.mcp_tools` 白名单约束。依赖 mcp_client 北向网关契约。

**工具注册机制**：各模块的 `@tool` 装饰工具函数需在组合根或工厂中显式 import 对应模块来触发注册（不采用全量扫描，避免启动变慢）。运行时必须默认可用的工具（如 `agent_lifecycle`）需要在 `LocalToolFactory.create()` 中 import，确保 Agent 执行器创建本地工具网关时已注册。

**配置管理脚本原则**：release 内置 `spread-ai-config-management` Skill 脚本是本机 Runtime REST client；它不直接写 PG/YAML/SQLite，不承载业务规则，不绕过 Runtime auth。

### Skill 模块（skill）

Skill 发现与元数据管理。扫描用户配置的 Skill 仓库目录，解析各子目录中 `SKILL.md` 的 YAML frontmatter。后端会在启动、Skill settings 保存以及 list/get 兜底读路径幂等安装/升级 release 内置 `spread-ai-config-management` Skill；同名非托管目录不得覆盖。公共查询走 REST：`GET /api/skills?summary=true`、`GET /api/skills/{skill_id}`，`POST /api/skills/refresh` 只保留内部诊断兜底。

### Agent 定义模块（agent_definition）

负责 Agent 静态模板数据管理。每个 Agent 定义一个 YAML 文件，文件名即 `agent_id`，默认存储在 `~/.spread-ai/agents/` 下，配置项为 `agent_definition.definitions_dir`。定义字段包括系统提示词混合数组、本地工具 ID、MCP 服务工具绑定、Skill ID、可用子 Agent ID、默认/允许 LLM 执行策略等。对外管理入口是 REST `/api/agents` CRUD + enable/disable；list 支持 `summary=true` 供内置配置管理 Skill 低噪声查询。

### Agent 运行时实例与任务管理模块（agent）

负责程序内部动态运行态管理和真正 Agent 执行循环。本模块不提供 REST/CLI 运行态管理接口，只作为程序内部调用入口。一个项目运行时支持无上限多个 task 并发；每个 task 支持无上限多个 Agent 实例并发；每个 task 必须且只能有一个根 Agent 实例入口，并在 task 内维护 Agent 实例父子关系树。核心领域模型包括 `AgentTask`（任务聚合根，字段含 `task_id`、`root_agent_instance_id`、`agent_instance_ids`、`parent_child_relations`、任务状态和时间戳）、`AgentInstance`（运行时实例，字段含 `instance_id`、`task_id`、`thread_id`、`agent_definition_id`、`llm_provider_id`、`llm_model_binding_id`、`conversation_id`、`trace_id`、实例状态和时间戳）、`AgentParentChildRelation`（同一 task 内父子实例关系边）。当前任务生命周期只保留 `unfinished` / `completed` / `failed` 三态，执行循环是否正在运行由 `AgentInstance` 的 `ready` / `running` 两态表达；pause 不会新增 Task 终态，而是让 Task 保持 unfinished、AgentInstance 回到 ready。父子树规则：唯一 root，root 不能有 parent，非 root 必须有且只有一个 parent，父子实例必须属于同一 task，禁止环，所有实例必须从 root 可达。运行态仓储接口为 `AgentRuntimeRepository`，基础设施实现包括线程安全内存仓储 `InMemoryAgentRuntimeRepository` 和 SQLite 共享仓储 `SqliteAgentRuntimeRepository`；`agent_runtime.repository_type=sqlite` 时 task、Agent 实例和父子关系写入 `agent_runtime.data_dir/agent_runtime.db`，开启 WAL，支持本机 FastAPI 多 worker / 多 Python 进程共享运行态。`AgentRuntimeFactory` 根据 ConfigRegistry 装配 memory/sqlite 仓储，并在单 Python 进程内复用同一网关。

`AgentExecutionEngine` 是内部执行循环入口：创建 task + root Agent 实例，加载 `AgentDefinition`，拼接系统提示词（支持 `text` 与 `template_id`，但 Trace 不记录系统提示词），并额外注入 Runtime lifecycle tool policy，指导 LLM 使用 `message_user` / `pause_task` / `end_task` / `fail_task` 与用户沟通和控制生命周期。执行循环创建/加载 Conversation，追加用户消息，解析 LLM provider/binding，构造当前 AgentDefinition 允许的本地工具 schema，调用通用 LLM 请求客户端，收到 assistant 响应后写回 Conversation；如果响应包含 `ToolUsePart`，交给 `AgentToolDispatcher` 执行本地工具，追加 `ToolResultPart` 后读取每个工具返回的 `BaseToolResult.loop_control.decision`，按 fail > complete > pause > continue 优先级控制运行态。`pause_task` 仍走现有 `pause` loop decision，但 `pause_timeout_seconds` / `pause_wake_mode` metadata 会驱动 `AgentExecutionEngine` 以进程内 best-effort 定时器注册双通道唤醒；超时回调会校验 pause token、task/instance/conversation 身份与非运行中状态后，无需伪造用户消息即可恢复同一执行上下文。无工具调用不再自动表示完成，而是作为 plain text fallback 继续提示模型使用生命周期工具；连续达到 `_PLAIN_TEXT_FALLBACK_PAUSE_THRESHOLD` 后暂停 instance/task。工具调用轮数仍受 `agent_execution.max_tool_rounds` 限制。执行失败时显式中断当前实例并保留可恢复状态。`AgentToolDispatcher` 暴露当前 `AgentDefinition.tool_ids` 和 `always_enabled=True` 的本地工具；`AgentDefinition.mcp_tools` 只用于系统提示词中的 MCP 服务与工具摘要，不会把远端 MCP 工具展开为 LLM 请求 `tools`。Agent 使用 MCP 时必须通过 always-enabled 的本地代理工具 `mcp_get_service_detail`、`mcp_list_resources`、`mcp_read_resource`、`mcp_get_tool_detail`、`mcp_call_tool` 发现并调用 MCP 服务。每个 Agent 实例执行过程由 `AgentTraceRecorder` 单独写 Markdown 文件到 `agent_runtime.trace_dir/{task_id}/`，文件名包含 `agent_definition_id`、时间戳、`instance_id`、`trace_id` 和随机后缀；trace 文件只记录 `agent_definition_id`、task/instance/thread/trace ID、用户消息、assistant 消息、工具结果和完成/失败事件，不记录系统提示词、工具 schema、工具清单、API Key 或请求头。

### Runtime 本地认证与 Scene Presence 生命周期

Phase-1 访问模型是单 Runtime 主机的多端接入：后端 runtime-local auth 是权限边界，普通 `/api/*` REST 只接受 Bearer `Authorization` header；仅浏览器原生无法设置 Authorization header 且已明确收敛的通道可使用 `access_token` query token：EventSource `/api/communication/streams/*/events`、文件上传进度 EventSource `/api/file-resources/upload-tasks/*/events`、文件内容 GET `/api/file-resources/*/content`，以及 sendBeacon detach `/api/agent-runtime/scenes/*/detach`。不得把 query token 扩展到普通 REST、配置读写、文件资源元数据、上传任务快照或其他写入接口。`SceneSessionRegistry` 只记录多端 presence，不拥有任务生命周期；attach、heartbeat、detach、stop 必须保持分离。关闭窗口、unload、workspace switch 和 `/detach` 只关闭当前 `client_session_id`，不得取消 task/instance；显式停止必须走 `/stop` 或 per-Agent interrupt/stop。Scene 自动释放只允许在零客户端且连续 30 分钟没有新的 task/instance 活动、且没有 running instance 时执行，判定异常必须保守跳过。

### 通用 LLM 请求客户端（llm_client）

负责真实 LLM API 请求编排，屏蔽不同 API 协议的请求细节。`LlmRequestService` 接收 `LlmPromptRequest`，使用 `conversation_message.render_messages()` 渲染目标 `api_format` 的历史消息，合并 `ResolvedLlmProviderModel` 中的 `model_alias`、`api_base_url`、`api_keys`、`custom_headers`、`retry`、`tool_choice`、`thinking`、`custom_request_params` 和本轮覆盖参数，按 API Key 列表轮换并按 `LlmRetryPolicy` 对 429/5xx 等临时错误做重试；成功后调用 `conversation_message.ingest_response()` 归一化 assistant 响应并写回 Conversation。南向协议客户端当前实现三类已具备 message adapter 的 API 格式：`openai_responses`、`anthropic_messages`、`gemini_generate_content`。全局枚举中存在但本轮未实现真实调用的 `openai_chat_completions` 与 `zhipu_chat_completions` 需要先补齐 conversation_message 协议适配器后再接入。

### LLM 服务商与模型注册模块（llm_model_registry）

负责用户自定义的 LLM 模型型号元数据、服务商定义、服务商模型接入项和执行策略组管理。运行期权威数据存储在 `llm_model_registry.registry_dir/llm_model_registry.sqlite3`，legacy YAML 只作为一次性迁移输入。服务商级 API Key、模型接入项 API Key、模型接入项列表和执行策略候选项列表的顺序均有业务含义，仓储必须保序。对外管理入口是 REST `/api/llm-registry/models`、`/providers`、`/execution-policies` 与 provider model resolved 端点；list 支持 `summary=true`，provider/detail/resolved 默认脱敏 API Key，只有受控调用显式 `include_secrets=true` 才返回明文。

### 日志工作区与日志清理边界

`/logs` 是独立日志工作区，后端必须通过 `server.create_app()` 挂载专用 `/api/logs` 路由；不要把日志管理并入 `/api/system-settings`，也不要把 `persistence_inventory` 当作日志产品面的主要数据源。日志统计总览必须从实际 SQLite / 文件系统存储实时检查当前总量和大小。日志清理仅允许用户手动选择生命周期过滤和清理类别、先预览影响、再显式执行删除；不得新增 TTL、定时任务、后台保留策略或其他自动删除。当前允许清理的类别仅限 Communication messages、Chat Bubbles、LLM HTTP Trace、Context Compression archive、Terminal history；Conversation history、Agent Trace Markdown、File resources/archive 等受保护存储不得在日志清理中删除。Communication messages 只能删消息行不能删共享 stream；Context Compression archive 清理必须同时处理 DB 行与 summaries/snapshots 引用文件；Terminal history 按终端会话/命令/输出历史成组清理。

---

## 规划模块

暂无。
