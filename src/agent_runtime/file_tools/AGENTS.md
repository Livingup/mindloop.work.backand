# 文件工具模块（file_tools）

负责面向 Agent 的本地文件系统上下文获取能力，包括文件读取、目录探索、文件名搜索、文本搜索和项目上下文文档发现。

## 模块边界

本模块是具体业务工具模块，不负责工具框架本身。

- `tool` 模块负责统一工具协议、注册表、Schema 生成、执行服务和静态查询入口。
- `file_tools` 模块负责文件系统相关工具的业务实现，并通过 `@tool` 注册到统一工具体系。
- 文件工具必须保持 `group="file"` 供展示筛选，并保持 `category="business"`；它们不属于默认可见系统工具，Agent 只有在 `AgentDefinition.tool_ids` 显式授权后才能看到或调用。
- 本地工具执行依赖完整 Runtime / Agent 执行环境，不通过 CLI 提供通用执行命令。

## 当前工具

### 只读上下文工具

- `file_read`：批量按文件与行范围读取文本文件内容。
- `context_document_discover`：发现项目内标准 `AGENTS.md` 上下文文档。
- `directory_explore`：递进式探索目录结构。
- `file_name_search`：按文件名、正则、glob、扩展名定位候选文件或目录。
- `text_search`：在 `search_root` 指定的目录或单个普通文件内执行文本/正则搜索；目录模式递归并应用 extensions/path_globs/exclude_names/include_hidden/respect_gitignore，文件模式只扫描该文件且这些目录候选过滤器全部 no-op。优先 `ripgrep`，无 `rg` 时降级 Python；成功输出含 `search_target_type`（`file`|`directory`）。
- `image_read`：批量读取远程 `http(s)://` 图片 URL 或本地图片文件绝对路径，直接将轻量引用（`url` 或 `path`）写入 `ToolResultPart.output`，在协议渲染阶段按 `<multimodal_file type="image" url/path="...">` + `input_image` + `</multimodal_file>` 提供给模型；不经过 `FileResourceService` 建档或上传。

### 受控写入工具

- `file_modify`：在项目工作区内通过连续行精确匹配修改文本文件内容，默认只返回最小成功状态，失败时返回可修复原因。
- `file_create`：在项目工作区内创建文本文件，默认禁止覆盖已有文件。
- `file_delete`：在项目工作区内删除单个文件，不支持目录或递归删除。
- `file_move`：在项目工作区内移动或重命名单个文件，`rename` 作为同目录移动处理。
- `image_save`：在项目工作区内批量将远程 `http(s)://` 图片 URL 原子保存到目标路径（最大 4 并发），默认支持覆盖已有普通文件；优先从本地 `FileResourceService` 查询 URL 是否已存在本地缓存文件并直接读取复制，未命中时再走 HTTP 下载（不向 `FileResourceService` 新建建档或上传）。

写入类工具必须显式声明 `project_root`，并在工具内部执行工作区沙箱、`.git` 保护、符号链接拒绝和原子写入等安全策略（文本写入类工具额外执行内容 SHA256 并发校验）。

### Workspace REST 边界

- `/api/workspaces/memory/list`、`/read`、`/save` 只能暴露当前 workspace 下 `.spread-ai/memory/<agent-id>/memory.md`；`agent_id` 必须是单层目录名，目标文件名固定为 `memory.md`。
- Memory REST 读写必须拒绝 `..`、绝对路径、路径分隔符注入、符号链接逃逸、非 `memory.md` 目标和任意文件读写；保存必须使用 `expected_sha256` 做乐观并发控制，旧 SHA 返回冲突而不是覆盖。