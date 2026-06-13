# MCP 模块（mcp）

包含两个子模块：

```
src/agent_runtime/mcp/
├── mcp_client/         ← MCP 客户端子模块（通用 MCP 服务管理和调用能力）
└── mcp_meta_tools/     ← MCP 元工具子模块（供 Agent 使用的本地工具）
```

**依赖关系**：`mcp_meta_tools` → `mcp_client`

---

## 子模块一：MCP 客户端（mcp_client）

通用的 MCP 服务管理和调用能力模块。**不关心程序如何接入使用 MCP 服务**，只提供面向 MCP 协议的通用能力。

### 核心职责

1. **MCP 服务配置管理** — 以 YAML 文件定义外部 MCP 服务的连接方式（stdio / streamable-http）
2. **MCP 客户端连接管理** — 按需建立、复用、优雅关闭与 MCP 服务的连接
3. **MCP 服务能力与工具定义管理** — 通过“本地持久化快照 + 进程全局内存注册表”维护服务能力、instructions、工具定义和资源列表元数据；Agent 执行热路径、MCP 元工具和管理端元数据查询必须读内存快照，禁止同步等待远端 MCP 元数据网络调用
4. **MCP 元数据后台刷新** — 专用刷新管理器负责轮询远端服务，刷新服务能力、服务说明、工具列表、工具 schema 和资源列表；刷新结果变化时先更新内存，再异步落盘，刷新失败保留旧快照
5. **MCP 工具执行** — 将工具调用请求转发到对应 MCP 服务，真实工具执行仍是实时协议调用，不使用元数据缓存替代
6. **MCP 资源访问** — 资源列表属于元数据缓存；资源内容读取仍按需实时访问 MCP 服务

## 子模块二：MCP 元工具（mcp_meta_tools）

将 MCP 客户端能力封装为 5 个本地工具（通过 `@tool` 装饰器注册，async 函数），供 Agent 在对话中自主调用 MCP 服务。

依赖 `mcp_client` 子模块的北向网关契约。

---

## MCP 协议概要

基于 [MCP 规范 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25) 和 [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)。

### 参与者

| 角色 | 说明 | 对应本项目 |
|---|---|---|
| **Host** | AI 应用，协调管理多个 MCP 客户端 | AgentRuntime（我们） |
| **Client** | 维护与一个 MCP 服务的连接 | 本模块的 `McpClientManager` 内部管理的每个 `ClientSession` |
| **Server** | 对外提供上下文和工具的程序 | 外部 MCP 服务（如 filesystem、search 等） |

每个 Client 与 Server 是 **1:1** 关系。Host 可以同时管理多个 Client。

### 传输方式

| 传输方式 | 适用场景 | 连接参数 |
|---|---|---|
| **stdio** | 本地进程，最常用 | `command`, `args`, `env` |
| **streamable-http** | 远程服务，推荐生产用 | `url`, `headers` |

### 协议生命周期

```
1. 初始化阶段
   Client → Server:  initialize 请求
     ├─ 协议版本（2025-11-25）
     ├─ 客户端能力声明（roots, sampling, elicitation）
     └─ 客户端信息（name, version）

   Server → Client:  initialize 响应
     ├─ 协议版本（协商结果）
     ├─ 服务端能力声明（tools, resources, prompts, logging）
     └─ 服务端信息（name, version, instructions）

   Client → Server:  notifications/initialized 通知
     └─ 客户端准备好，进入操作阶段

2. 操作阶段
   工具相关：
     Client → Server:  tools/list      → 发现可用工具
     Client → Server:  tools/call      → 执行工具
     Server → Client:  notifications/tools/list_changed → 工具列表变更通知

   资源相关（可选，后续支持）：
     Client → Server:  resources/list  → 列出资源
     Client → Server:  resources/read  → 读取资源

   提示词相关（可选，后续支持）：
     Client → Server:  prompts/list    → 列出提示词模板
     Client → Server:  prompts/get     → 获取提示词详情

   通用：
     双向:             ping/pong       → 心跳检测
     Server → Client:  logging         → 日志消息

3. 关闭阶段
   stdio:  关闭 stdin → 等待退出 → SIGTERM → SIGKILL
   HTTP:   关闭 HTTP 连接
```

### MCP 服务端三大原语

| 原语 | 谁控制 | 说明 | 第一阶段是否支持 |
|---|---|---|---|
| **Tools** | LLM 模型 | 可调用的函数，有副作用 | ✅ 核心需求 |
| **Resources** | 应用程序 | 只读数据源（文件、DB、API） | ⚠️ 后续可选 |
| **Prompts** | 用户 | 预定义提示词模板 | ⚠️ 后续可选 |

### MCP 工具定义格式（协议标准）

MCP 工具的 `inputSchema` 就是标准 JSON Schema（2020-12）：

```json
{
  "name": "get_weather",
  "description": "Get current weather for a location",
  "inputSchema": {
    "type": "object",
    "properties": {
      "location": {
        "type": "string",
        "description": "City name or zip code"
      }
    },
    "required": ["location"]
  }
}
```

**与 LLM 平台的关系**：LLM 不知道 MCP 的存在，它只看 tool 的 name + description + JSON Schema。MCP 工具的 `inputSchema` 天然兼容各平台：

| 平台 | Schema 格式 | MCP → 平台转换 |
|---|---|---|
| **Claude** | `input_schema` 为标准 JSON Schema | 天然一致，直接用 |
| **OpenAI** | `parameters` 为 JSON Schema | 外包 `{"type":"function","function":{...}}` |
| **Gemini** | `types.Schema` 对象 | `types.Schema.model_validate(inputSchema)` |

Schema 格式转换由 `tool` 模块负责，本模块只提供原始 JSON Schema。

---

## MCP 客户端运行机制

### Python SDK 使用模式

全异步，基于 `asyncio`：

```python
# === stdio 传输 ===
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(command="npx", args=["-y", "server-name"])
async with stdio_client(params) as (read_stream, write_stream):
    async with ClientSession(read_stream, write_stream) as session:
        await session.initialize()                    # 协议握手 + 能力协商
        tools = await session.list_tools()            # 发现工具
        result = await session.call_tool("name", {})  # 执行工具
        # 退出 async with 时自动关闭连接

# === streamable-http 传输 ===
from mcp.client.streamable_http import streamable_http_client

async with streamable_http_client("https://example.com/mcp") as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        # ... 同上
```

### 客户端需要处理的协议事件

| 事件方向 | 事件 | 处理方式 |
|---|---|---|
| Client → Server | `initialize` | SDK 自动处理，调用 `session.initialize()` |
| Client → Server | `tools/list` | 主动调用 `session.list_tools()` |
| Client → Server | `tools/call` | 主动调用 `session.call_tool(name, args)` |
| Server → Client | `notifications/tools/list_changed` | 监听，收到后重新 `list_tools()` 刷新缓存 |
| Client → Server | `ping` | SDK 内部处理 |
| Server → Client | `logging` | 可选监听，转发到本地日志 |

### 工具调用结果解析

`CallToolResult` 包含多种内容类型：

```python
result = await session.call_tool("tool_name", arguments={...})

# 文本内容
for content in result.content:
    if isinstance(content, types.TextContent):
        text = content.text

# 结构化内容
if result.structuredContent:
    data = result.structuredContent

# 错误判断
if result.isError:
    # 工具执行失败
```

### 关键特征

- **全异步** — MCP SDK 基于 `asyncio`，所有调用都是 `async/await`
- **1:1 会话** — 每个 `ClientSession` 实例对应一个 MCP Server
- **有状态** — Session 维护协商后的能力状态
- **上下文管理器** — 连接生命周期由 `async with` 管理
- **SDK 封装** — JSON-RPC 序列化、协议握手、能力协商均由 SDK 处理

---

## Agent 整合方案：MCP 元工具

### 设计决策

MCP 协议定义了 Tools、Resources、Prompts 三大原语。经分析：

- **Tools** — 业界广泛使用，核心需求
- **Resources** — 协议设计理想化，缺少实用消费模式（无搜索/过滤，大数据量下列表不可用）。但可通过让 Agent 自主 list + read 解决
- **Prompts** — 本质是提示词模板，已被 `prompt_template` 模块覆盖，且 MCP initialize 响应的 `instructions` 字段已能传达服务级使用指南

**采用元工具方案**：将 MCP 操作封装为 5 个本地工具（通过 `@tool` 装饰器注册），Agent 通过系统提示词获知可用 MCP 服务和工具名列表，按需调用元工具完成 MCP 交互。

### 系统提示词注入

程序根据 Agent 定义中配置的 MCP 服务，启动时连接并 `tools/list`，自动在 Agent 的系统提示词中注入轻量信息：

```
你可以使用以下 MCP 服务：

## project-mgr（项目管理服务）
可用工具：list_tasks, create_task, assign_task

## weather（天气服务）
可用工具：get_forecast, get_history

使用 mcp_get_service_detail 获取服务详情和使用指南。
使用 mcp_get_tool_detail 获取工具参数定义后再调用。
```

### 5 个 MCP 元工具

注册为本地 tool（`@tool` 装饰器），Agent 定义中配置了 MCP 服务时自动可用。

| 元工具 | 入参 | 返回 | 说明 |
|---|---|---|---|
| `mcp_get_service_detail` | `service_id` | capabilities（仅 tools/resources 是否支持）+ instructions | 了解服务能做什么、获取跨工具的使用策略 |
| `mcp_list_resources` | `service_id` | 资源列表（uri, name, mimeType） | 发现可用上下文数据 |
| `mcp_read_resource` | `service_id`, `uri` | 资源内容 | 读取具体资源，Agent 可据此与用户沟通或制定后续策略 |
| `mcp_get_tool_detail` | `service_id`, `tool_names: list[str]` | 工具的 inputSchema 列表 | 批量获取工具参数定义（JSON Schema） |
| `mcp_call_tool` | `service_id`, `tool_name`, `arguments: dict` | 执行结果 | 执行 MCP 服务上的工具 |

### Agent 使用流程示例

```
系统提示词中已注入：
  project-mgr 服务，工具：[list_tasks, create_task, assign_task]

用户："帮我创建3个任务"

Agent 思考："需要用 project-mgr 的 create_task，先了解使用策略"
  → tool_call: mcp_get_service_detail("project-mgr")
  → 返回: { tools: true, resources: false,
           instructions: "创建前先 list_tasks 检查是否存在..." }

Agent："看看 create_task 和 list_tasks 的参数"
  → tool_call: mcp_get_tool_detail("project-mgr", ["create_task", "list_tasks"])
  → 返回: [{ name: "create_task", inputSchema: {...} }, ...]

Agent："按 instructions 建议，先检查"
  → tool_call: mcp_call_tool("project-mgr", "list_tasks", {"project_id": "xxx"})
  → 返回: [...]

Agent："没有重复，开始创建"
  → tool_call: mcp_call_tool("project-mgr", "create_task", {"project_id": "xxx", "title": "任务1"})
  → ...
```

### instructions 来源

MCP 协议的 `initialize` 响应包含可选的 `instructions` 字段（服务级使用指南）。来源优先级：

1. **MCP 服务自带** — `initialize` 响应中的 `instructions` 字段
2. **本地配置补充** — YAML 配置文件中的 `usage_guide` 字段

两者合并后通过 `mcp_get_service_detail` 返回给 Agent。

---

## mcp_client 子模块详细设计

### 北向网关

#### 程序内部接口（McpClientGateway Protocol）

`mcp_meta_tools` 和其他模块通过此契约使用 MCP 客户端能力：

```python
class McpClientGateway(Protocol):
    """​MCP 客户端模块北向网关契约。"""

    # ── 配置管理 ──

    def list_services(self) -> list[McpServiceSummary]:
        """列出所有已配置的 MCP 服务。"""
        ...

    def get_service(self, service_id: str) -> McpServiceInfo:
        """获取服务配置详情。"""
        ...

    def add_service(self, config: McpServiceAddRequest) -> None:
        """添加 MCP 服务配置。"""
        ...

    def update_service(self, service_id: str, update: McpServiceUpdateRequest) -> None:
        """更新 MCP 服务配置（部分更新）。"""
        ...

    async def remove_service(self, service_id: str) -> None:
        """删除 MCP 服务配置并断开连接。"""
        ...

    def enable_service(self, service_id: str) -> None:
        """启用 MCP 服务。"""
        ...

    def disable_service(self, service_id: str) -> None:
        """禁用 MCP 服务。"""
        ...

    # ── 协议操作 ──

    async def ensure_connected(self, service_id: str) -> None:
        """确保与指定 MCP 服务的连接已建立。"""
        ...

    async def get_service_detail(self, service_id: str) -> McpServiceDetailResult:
        """获取服务能力和 instructions。"""
        ...

    async def list_tools(self, service_id: str) -> list[McpToolSummaryDTO]:
        """列出指定服务的所有工具（名称 + 描述）。"""
        ...

    async def get_tool_detail(
        self, service_id: str, tool_names: list[str]
    ) -> list[McpToolInfoDTO]:
        """批量获取工具的 inputSchema。"""
        ...

    async def call_tool(
        self, service_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> McpToolResultDTO:
        """执行 MCP 工具。"""
        ...

    async def list_resources(self, service_id: str) -> list[McpResourceInfoDTO]:
        """列出指定服务的资源。"""
        ...

    async def read_resource(self, service_id: str, uri: str) -> McpResourceContentDTO:
        """读取指定资源内容。"""
        ...

    async def shutdown(self) -> None:
        """断开所有 MCP 连接。"""
        ...
```

#### CLI / REST 接口

**配置管理**（`spread-ai mcp-config`，纯本地 CRUD）：

| 能力 | CLI 命令 | REST API | 说明 |
|---|---|---|---|
| 列出已配置的 MCP 服务 | `spread-ai mcp-config list` | `GET /api/mcp/services` | 返回所有服务摘要 |
| 查看服务配置详情 | `spread-ai mcp-config get <id>` | `GET /api/mcp/services/{id}` | 配置 + 连接状态 |
| 添加 MCP 服务配置 | `spread-ai mcp-config add --file <yaml>` | `POST /api/mcp/services` | 写入 YAML 配置文件 |
| 更新 MCP 服务配置 | `spread-ai mcp-config update <id> --file <yaml>` | `PUT /api/mcp/services/{id}` | 部分更新 |
| 删除 MCP 服务配置 | `spread-ai mcp-config remove <id>` | `DELETE /api/mcp/services/{id}` | 删除 YAML + 断开连接 |
| 启用服务 | `spread-ai mcp-config enable <id>` | — | 设置 enabled=true |
| 禁用服务 | `spread-ai mcp-config disable <id>` | — | 设置 enabled=false |

**协议操作**（`spread-ai mcp`，需连接远程服务）：

| 能力 | CLI 命令 | REST API | 说明 |
|---|---|---|---|
| 查看服务能力 | `spread-ai mcp inspect <id>` | `GET /api/mcp/services/{id}/detail` | capabilities + instructions |
| 列出服务工具 | `spread-ai mcp tools <id>` | `GET /api/mcp/services/{id}/tools` | 工具名称 + 描述 |
| 查看工具参数 | `spread-ai mcp tool-detail <id> <tool> [tool2...]` | `GET /api/mcp/services/{id}/tools/{tool}` | 工具 inputSchema |
| 列出资源 | `spread-ai mcp resources <id>` | `GET /api/mcp/services/{id}/resources` | 资源列表 |
| 读取资源 | `spread-ai mcp read-resource <id> <uri>` | `POST /api/mcp/services/{id}/resources/read` | 资源内容 |

### 数据流

```
Agent 执行引擎
  │
  │  系统提示词已注入: project-mgr 服务, 工具: [list_tasks, create_task]
  │
  │  LLM 返回 tool_call: mcp_call_tool("project-mgr", "create_task", {...})
  │
  ▼
┌──── tool 模块 ─────────────────────────┐
│                                         │
│  注册表查找 → mcp_call_tool（本地工具） │
│  执行本地函数 mcp_call_tool(...)        │
│    └─ 内部调用 mcp_gateway.call_tool()  │
│                                         │
└───────────────┬─────────────────────────┘
                │ 南向网关调用
                ▼
┌──── MCP 模块 ──────────────────────────┐
│                                         │
│  McpClientManager 查找 "project-mgr"    │
│    → 已有连接？复用                     │
│    → 没有连接？按 YAML 配置建立         │
│                                         │
│  session.call_tool(                     │
│    "create_task",                       │
│    {"project_id": "xxx", ...})          │
│                                         │
│  解析 CallToolResult → McpToolResult    │
└─────────────────────────────────────────┘
```

---

## MCP 服务配置格式

存储路径：`~/.spread-ai/mcp-servers/{service_id}.yaml`

### stdio 示例

```yaml
# filesystem.yaml
name: "文件系统服务"
description: "提供文件读写、搜索等能力"
transport:
  type: stdio
  command: "npx"
  args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/docs"]
  env:
    NODE_ENV: "production"
enabled: true
usage_guide: |
  读取文件前应先 list_directory 确认文件存在。
  写入文件时注意检查目标路径的目录是否已创建。
```

### streamable-http 示例

```yaml
# remote-search.yaml
name: "远程搜索服务"
description: "提供网络搜索和知识库检索能力"
transport:
  type: streamable-http
  url: "https://mcp.example.com/search/mcp"
  headers:
    Authorization: "Bearer ${MCP_SEARCH_API_KEY}"
enabled: true
usage_guide: |
  优先使用 semantic_search 获取相关结果，仅在需要精确匹配时使用 keyword_search。
```

---

### DDD 分层结构

```
src/agent_runtime/mcp/mcp_client/
├── __init__.py
├── mcp_service_definition.py           # 领域实体（聚合根）：MCP 服务配置定义
├── mcp_transport_config.py            # 传输配置值对象（StdioTransportConfig / StreamableHttpTransportConfig）
├── mcp_service_repository.py           # 仓储接口（Protocol）
├── mcp_client_service.py               # 领域服务
├── infrastructure/
│   ├── __init__.py
│   ├── yaml_mcp_service_repository.py  # 仓储实现：YAML 文件存储
│   └── mcp_session_manager.py          # 基础设施：MCP 会话连接池管理
├── north_gateway/
│   ├── __init__.py
│   ├── mcp_client_contracts.py         # 北向网关契约（McpClientGateway Protocol + DTO）
│   ├── mcp_client_gateway_impl.py      # 网关实现
│   ├── mcp_client_cli_adapter.py       # CLI 适配器：spread-ai mcp-config / mcp
│   └── mcp_client_rest_adapter.py      # REST 适配器：/api/mcp/...
└── mcp_client_factory.py               # 工厂：组装依赖
```

### 领域实体

```python
@dataclass
class McpServiceDefinition:
    """MCP 服务配置定义。描述一个外部 MCP 服务的连接方式和元数据。"""

    service_id: str                       # 服务唯一标识（文件名）
    name: str                             # 人类可读名称
    transport: TransportConfig            # 传输配置（值对象）
    description: str | None = None        # 服务描述
    enabled: bool = True                  # 是否启用
    usage_guide: str | None = None        # 本地使用指南

# 传输配置值对象（二选一）：
@dataclass(frozen=True)
class StdioTransportConfig:
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

@dataclass(frozen=True)
class StreamableHttpTransportConfig:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
```

---

## mcp_meta_tools 子模块详细设计

### DDD 分层结构

```
src/agent_runtime/mcp/mcp_meta_tools/
├── __init__.py
├── mcp_meta_tools.py                   # 5 个 MCP 元工具的定义和实现（@tool 装饰器注册）
└── mcp_meta_tools_factory.py           # 工厂：注入 McpClientGateway 依赖
```

---

## SDK 依赖

```toml
"mcp>=1.25.0,<2.0.0"
```

MCP Python SDK 核心依赖：`anyio`、`httpx`、`pydantic`。
