# 本地工具模块（local_tool）

负责本地工具的定义、自动发现、注册、执行，以及各 LLM 平台工具 JSON Schema 的生成与缓存管理。

## 对外服务能力

本模块对外提供四个服务：

1. **列出所有工具** — 默认只返回 `public=True` 的工具，支持是否按 `group` 分组后返回结果（默认不分组）。返回工具名称、描述、分组和各平台 JSON Schema。
2. **按名称查询工具** — 不受 `public` 限制，返回工具的完整信息（名称、描述、wiki、分组、各平台 JSON Schema）。
3. **执行工具** — 传入 tool_name + params，调用工具函数，返回执行结果。
4. **获取工具 wiki 列表** — 默认只返回 `public=True` 的工具，必须按 `group` 分组。每个工具返回工具名称、wiki、group 和各平台 JSON Schema。

## 工具定义方式

各业务模块使用 `@tool` 装饰器声明工具函数，并在 `cli.py` 中显式 import 对应模块来触发注册。

### @tool 装饰器字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `name` | str | 是 | — | 工具唯一名称 |
| `description` | str | 是 | — | LLM 看到的工具描述（简洁、功能导向） |
| `group` | str | 否 | `"default"` | 工具分组（如 "文件"、"网络"、"数据库"） |
| `public` | bool | 否 | `True` | 是否公开（列出所有工具时是否可见） |
| `wiki` | str | 否 | `None` | 人类可读的工具介绍文档（用于生成系统工具说明文档） |

### 简单参数示例

参数直接使用 Python 类型注解，适合参数少、无复杂约束的工具：

```python
from agent_runtime.local_tool import tool

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
from agent_runtime.local_tool import tool

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
2. **返回值类型为 `str`** — 工具返回结果统一为字符串，由工具实现方负责将结果序列化为可读文本。
3. **异常处理** — 工具内部异常应自行捕获并返回错误描述字符串，不应向外抛出未处理异常。

## 工具注册机制

`@tool` 装饰器在模块被导入时自动将工具注册到全局注册表。不采用 `pkgutil.walk_packages` 全量扫描（避免启动变慢），而是在 `cli.py` 中显式 import 工具模块来触发注册。**新增工具模块时需在 `cli.py` 中添加一行 import。**

```
程序启动
  ↓
cli.py 显式 import 工具模块
  ↓
模块被 import → @tool 装饰器自动触发注册到全局注册表
  ↓
注册表就绪
```

## JSON Schema 策略

各 LLM 平台 SDK（OpenAI / Claude / Gemini）对工具 JSON Schema 的格式要求不同。本模块采用以下策略：

1. **生成**：由各平台实现 `ToolSchemaGenerator` 接口，使用各自 SDK 从函数签名（类型注解 + Pydantic 模型）解析生成该平台所需格式的 JSON Schema。
2. **缓存**：生成后的 Schema 按 `(tool_name, platform)` 索引缓存，同一工具同一平台只生成一次。
3. **查询**：对外查询工具时返回各平台的 Schema，调用 LLM 时直接取缓存，无需重复生成。

### ToolSchemaGenerator 接口

```python
class ToolSchemaGenerator(Protocol):
    """各 LLM 平台的工具 Schema 生成器接口。"""

    @property
    def platform(self) -> str:
        """平台标识（如 "openai"、"claude"、"gemini"）。"""
        ...

    def generate(self, tool_def: LocalToolDefinition) -> dict:
        """根据工具定义生成该平台格式的 JSON Schema。"""
        ...
```

各平台 adapter 在自己的模块中实现此接口（如 `OpenAISchemaGenerator`），注册到模块内部的 Schema 缓存管理器。

## 架构分层

### 领域层

- **`LocalToolDefinition`**：领域实体（同时也是聚合根，每个工具独立，无跨工具的一致性约束）。持有工具名称、描述、分组、公开标志、wiki 和函数引用，自身携带字段校验业务规则。
- **`LocalToolRepository`**：仓储接口（Protocol），定义在领域层。集合语义，方法用业务语言命名。
- **`LocalToolService`**：领域服务（无状态），协调仓储和 Schema 缓存，提供四个业务用例：
  1. 列出所有工具（支持 public 过滤、group 分组）
  2. 按名称查询工具
  3. 执行工具
  4. 获取工具 wiki 列表（按 group 分组）

### 北向网关

- **`LocalToolGateway`**：网关契约（Protocol），定义对外服务接口和数据模型。外部消费方（Agent 定义模块、执行引擎、CLI、REST）通过此契约访问本模块能力。
- **CLI adapter**：挂在 `spread-ai tool ...` 子命令下，供 Agent / 外部 AI 发现和查询系统工具。
  - `spread-ai tool list` — 列出所有公开工具（支持 `--group` 筛选、`--group-by` 分组）
  - `spread-ai tool get <names>` — 查询指定工具详情，支持逗号分隔多个工具名（如 `add,greet`）
  - `spread-ai tool groups` — 列出所有工具分组名称
- **REST adapter**：挂在 `/api/tools/` 路由下。

### 基础设施层

- **`InMemoryToolRepository`**：`LocalToolRepository` 的内存实现。工具从代码扫描加载，无持久化需求。
- **`ToolSchemaGenerator`**：Schema 生成器接口（Protocol），各 LLM 平台在各自模块中实现。
- **`ToolSchemaCache`**：Schema 缓存管理，按 `(tool_name, platform)` 维度缓存。

### 工具声明

- **`@tool` 装饰器**：声明工具函数，附加元数据，import 时自动注册到全局注册表。

## 目录结构

```
local_tool/
├── __init__.py                    # 模块公开 API
├── tool_decorator.py              # @tool 装饰器（import 时自动注册）
├── local_tool_definition.py       # 领域实体（聚合根）
├── local_tool_repository.py       # 仓储接口（Protocol）
├── local_tool_service.py          # 领域服务
├── tool_schema_generator.py       # Schema 生成器接口（Protocol）
├── tool_schema_cache.py           # Schema 缓存管理
├── infrastructure/
│   ├── __init__.py
│   └── in_memory_tool_repository.py   # 仓储内存实现
├── north_gateway/
│   ├── __init__.py
│   ├── local_tool_gateway.py          # 北向网关契约（Protocol）
│   ├── local_tool_contracts.py        # 对外数据模型（DTO）
│   ├── local_tool_cli_adapter.py      # CLI 接入
│   └── local_tool_rest_adapter.py     # REST 接入
└── README.md
```
