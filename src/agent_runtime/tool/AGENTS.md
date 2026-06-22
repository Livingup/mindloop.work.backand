# 统一工具模块（tool）

负责本地工具的定义、自动发现、注册、执行、统一执行协议，以及各 LLM 平台工具 JSON Schema 的生成与缓存管理。

## 对外服务能力

本模块对外提供四个服务：

1. **列出所有工具** — 默认只返回 `public=True` 的工具，支持是否按 `group` 分组后返回结果（默认不分组）。返回工具名称、描述、分组、wiki、category、always_enabled、allowed_loop_decisions 等元数据。
2. **按名称查询工具** — 不受 `public` 限制，返回工具的完整信息（名称、描述、wiki、分组、统一协议元数据、各平台 JSON Schema）。
3. **执行工具** — 传入 tool_name + params，调用工具函数，返回统一 `BaseToolResult` / `ToolExecuteResult` 执行结果。
4. **获取工具 wiki 列表** — 默认只返回 `public=True` 的工具，必须按 `group` 分组。每个工具返回工具名称、wiki、group、统一协议元数据和各平台 JSON Schema。

## 工具定义方式

各业务模块使用 `@tool` 装饰器声明工具函数，并在组合根或工厂中显式 import 对应模块来触发注册。面向 CLI 的工具通常由 `cli.py` import；运行时可供 Agent 授权使用的工具需要由对应工厂触发注册，例如 `LocalToolFactory.create()` 会 import `agent_runtime.agent_lifecycle.agent_lifecycle_tool`。注册只表示进入本地工具库，是否默认对 Agent 可见由 `category` 决定。

### @tool 装饰器字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `name` | str | 是 | — | 工具唯一名称 |
| `description` | str | 是 | — | LLM 看到的工具描述（简洁、功能导向） |
| `group` | str | 否 | `"default"` | 展示与筛选分组（如 "文件"、"网络"、"数据库"），不承载默认可用或授权语义 |
| `public` | bool | 否 | `True` | 是否公开（列出所有工具时是否可见） |
| `wiki` | str | 否 | `None` | 人类可读的工具介绍文档（用于生成系统工具说明文档） |
| `category` | `"business" \| "lifecycle" \| "system"` | 否 | `"business"` | 工具类别与默认可用语义来源：`lifecycle`/`system` 默认对所有 Agent 可见，`business` 必须通过 `AgentDefinition.tool_ids` 显式授权 |
| `always_enabled` | bool | 否 | `False` | 兼容展示字段，由 `category in {"lifecycle", "system"}` 派生；不要用它为 `business` 工具单独制造默认可用例外 |
| `allowed_loop_decisions` | tuple | 否 | `("continue",)` | 该工具允许返回的循环控制决策集合 |

### 简单参数示例

参数直接使用 Python 类型注解，适合参数少、无复杂约束的工具：

```python
from agent_runtime.tool import tool

@tool(
    name="search_files",
    description="在项目中搜索文件",
    group="文件",
    wiki="递归搜索项目目录中的文件，支持关键词匹配和文件类型过滤。返回匹配的文件路径列表。",
)
def search_files(query: str, max_results: int = 10) -> str:
    ...
```

### 复杂参数示例（Pydantic 模型）

参数需要表达字段描述、取值范围、枚举、嵌套结构等约束时，使用 Pydantic BaseModel + Field() 定义：

```python
from enum import Enum
from pydantic import BaseModel, Field
from agent_runtime.tool import tool

class FileType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    PDF = "pdf"

class SearchParams(BaseModel):
    query: str = Field(..., description="搜索关键词")
    file_type: FileType = Field(FileType.TEXT, description="文件类型过滤")
    max_results: int = Field(10, ge=1, le=100, description="最大返回条数")

@tool(
    name="advanced_search",
    description="高级文件搜索，支持文件类型过滤和结果数限制",
    group="文件",
    wiki="高级搜索接口，支持按文件类型过滤，限制返回条数在1-100之间。",
)
def advanced_search(params: SearchParams) -> str:
    ...
```

### 工具实现规范

1. **参数类型注解必须完整** — 每个参数必须有类型注解，各平台 SDK 依赖类型注解生成 JSON Schema。
2. **普通工具可返回原始业务值** — 旧工具可继续返回 `str`、`dict`、`list` 等原始结果，`LocalToolService` 会自动包装为 `BaseToolResult(output=原始值, loop_control.decision="continue")`。
3. **新工具可返回 `BaseToolResult`** — 需要表达执行状态、错误信息、前端展示消息或循环控制时，工具应直接返回 `BaseToolResult` 或兼容子类。
4. **异常会被结构化封装** — 工具抛出的异常会由 `LocalToolService` 转换为 `success=False`、`execution_status="failed"` 的结构化结果；工具内部仍可按业务需要自行捕获并返回更友好的 `BaseToolResult`。
5. **循环决策必须被授权** — 普通业务工具默认只允许 `continue`，只有在 `allowed_loop_decisions` 中声明的决策才能返回。生命周期工具可声明 `pause`、`complete`、`fail`。
6. **单参数 Pydantic 请求模型优先用于复杂工具** — 如果工具函数只有一个参数且参数类型是 Pydantic `BaseModel` 子类，服务会把调用 params 构造成该请求模型实例。请求模型只承载工具业务参数语义，Runtime 上下文（task / agent_instance / trace 等）走独立的上下文注入机制，不混在 LLM 可见的请求模型里。
7. **工具需要 Runtime 上下文时显式声明 `ToolRuntimeContext` 参数** — 在函数签名里新增一个类型为 `ToolRuntimeContext` 的参数（推荐命名 `ctx`），框架会自动在执行时注入该参数，同时从 JSON Schema 中跳过该参数不暴露给 LLM。工具函数不声明 ctx 则完全不受影响。

```python
@tool(name="end_task", ...)
def end_task(request: EndTaskRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    logger.info("ending task=%s instance=%s", ctx.task_id, ctx.agent_instance_id)
    ...
```

### 统一工具执行协议

`tool_execution_models.py` 定义统一协议：

1. **`BaseToolRequest`** — 所有工具请求模型的统一基类，只承载工具业务参数，不携带 Runtime 调度元数据，避免在 JSON Schema 中暴露给 LLM 造成上下文污染。允许扩展业务字段。
2. **`BaseToolResult`** — 所有工具执行结果的统一基类，包含 `success`、`execution_status`、`error_code`、`error_message`、`output`、`loop_control`、`frontend_message`、`metadata`。
3. **`LoopControl`** — 工具对 Agent 执行循环表达下一步决策，`decision` 支持 `continue`、`pause`、`complete`、`fail`。
4. **`FrontendMessage`** — 工具需要前端展示消息时使用，显式声明 `text`、`text_format`、`channel` 和渲染元数据。
5. **`ToolResultEnvelope` / `ToolExecuteResult`** — 网关层带 `tool_name` 的执行结果信封，`result` 是 `output` 的兼容别名。
6. **`ToolRuntimeContext`** — Runtime 上下文注入载体，携带 `request_id` / `task_id` / `agent_instance_id` / `trace_id` / `communication_stream_id`。网关层（`LocalToolGatewayImpl`）从 `ToolExecuteRequest` 提取字段构造，领域服务（`LocalToolService`）按工具函数签名决定是否注入。该模型不会进入 LLM JSON Schema。

## 工具注册机制

`@tool` 装饰器在模块被导入时自动将工具注册到全局注册表。不采用 `pkgutil.walk_packages` 全量扫描（避免启动变慢），而是由组合根或工厂显式 import 工具模块来触发注册。**新增工具模块时需要选择合适的注册触发点：面向 CLI 的工具通常在 `cli.py` 中 import；运行时工具需要在对应 Factory 中 import 才能进入本地工具库。默认可见性只由 `category` 决定，`group` 与 import 位置都不能绕过 `AgentDefinition.tool_ids`。**

```
程序启动
  ↓
组合根或工厂显式 import 工具模块
  ↓
模块被 import → @tool 装饰器自动触发注册到全局注册表
  ↓
注册表就绪
```

## JSON Schema 策略

各 LLM 平台 SDK（OpenAI / Claude / Gemini）对工具 JSON Schema 的格式要求不同。本模块采用以下策略：

1. **生成**：由各平台实现 `ToolSchemaGenerator` 接口，使用各自 SDK 从函数签名（类型注解 + Pydantic 模型）解析生成该平台所需格式的 JSON Schema。
2. **净化**：`ToolSchemaCache` 在缓存前统一净化所有 Provider 的 LLM 可见工具 Schema，递归移除 Pydantic / SDK 自动生成的 `title`，避免 LLM 把 schema 标题误当作真实参数名；参数名只能以 `properties` 的 key 和 `required` 为准。
3. **缓存**：生成并净化后的 Schema 按 `(tool_name, platform)` 索引缓存，同一工具同一平台只生成一次。
4. **查询**：对外查询工具时返回各平台的净化后 Schema，调用 LLM 时直接取缓存，无需重复生成。
5. **Gemini 嵌套模型兼容**：Gemini 生成器会内联 Pydantic JSON Schema 中的 `$defs/$ref`，以支持 `agent_lifecycle` 这类包含嵌套结构化参数的工具。

### ToolSchemaGenerator 接口

```python
class ToolSchemaGenerator(Protocol):
    """各 LLM 平台的工具 Schema 生成器接口。"""

    @property
    def platform(self) -> str:
        """SDK 平台标识（如 "openai"、"anthropic"、"gemini"、"zhipu"）。"""
        ...

    def generate(self, tool_def: LocalToolDefinition) -> dict:
        """根据工具定义生成该平台格式的 JSON Schema。"""
        ...
```

各平台 adapter 在自己的模块中实现此接口（如 `OpenAISchemaGenerator`），注册到模块内部的 Schema 缓存管理器。

## 架构分层

### 领域层

- **`LocalToolDefinition`**：领域实体（同时也是聚合根，每个工具独立，无跨工具的一致性约束）。持有工具名称、描述、分组、公开标志、wiki、category、always_enabled、allowed_loop_decisions 和函数引用，自身携带字段校验业务规则。
- **`LocalToolRepository`**：仓储接口（Protocol），定义在领域层。集合语义，方法用业务语言命名。
- **`LocalToolService`**：领域服务（无状态），协调仓储和 Schema 缓存，提供四个业务用例：
  1. 列出所有工具（支持 public 过滤、group 分组）
  2. 按名称查询工具
  3. 执行工具
  4. 获取工具 wiki 列表（按 group 分组）

### 北向网关

- **`LocalToolGateway`**：网关契约（Protocol），定义对外服务接口和数据模型。外部消费方（Agent 定义模块、执行引擎、REST）通过此契约访问本模块能力。
- 本模块不再提供 OS/system CLI，也不作为公共配置管理 Skill 的覆盖范围。
- **REST adapter**：挂在 `/api/tools/` 路由下，供前端/内部诊断读取本地工具元数据。

### 基础设施层

- **`InMemoryToolRepository`**：`LocalToolRepository` 的内存实现。工具从代码扫描加载，无持久化需求。
- **`ToolSchemaGenerator`**：Schema 生成器接口（Protocol），各 LLM 平台在各自模块中实现。
- **`ToolSchemaCache`**：Schema 缓存管理，按 `(tool_name, platform)` 维度缓存。

### 工具声明

- **`@tool` 装饰器**：声明工具函数，附加元数据，import 时自动注册到全局注册表。
- **统一执行协议模型**：`BaseToolRequest`、`BaseToolResult`、`LoopControl`、`FrontendMessage`、`ToolResultEnvelope` 是工具执行与 Agent 循环控制的公共协议。

## 目录结构

```
tool/
├── __init__.py                    # 模块公开 API
├── tool_decorator.py              # @tool 装饰器（import 时自动注册）
├── local_tool_definition.py       # 领域实体（聚合根）
├── local_tool_repository.py       # 仓储接口（Protocol）
├── local_tool_service.py          # 领域服务
├── tool_execution_models.py       # 统一工具请求/结果/循环控制协议
├── tool_schema_generator.py       # Schema 生成器接口（Protocol）
├── tool_schema_cache.py           # Schema 缓存管理
├── infrastructure/
│   ├── __init__.py
│   └── in_memory_tool_repository.py   # 仓储内存实现
├── north_gateway/
│   ├── __init__.py
│   ├── local_tool_gateway.py          # 北向网关契约（Protocol）
│   ├── local_tool_contracts.py        # 对外数据模型（DTO）
│   └── local_tool_rest_adapter.py     # REST 接入
└── AGENTS.md
```
