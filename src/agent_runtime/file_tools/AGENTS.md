# 文件工具模块（file_tools）

负责面向 Agent 的本地文件系统上下文获取能力，包括文件读取、目录探索、文件名搜索、文本搜索和项目上下文文档发现。

## 模块边界

本模块是具体业务工具模块，不负责工具框架本身。

- `tool` 模块负责统一工具协议、注册表、Schema 生成、执行服务和静态查询入口。
- `file_tools` 模块负责文件系统相关工具的业务实现，并通过 `@tool` 注册到统一工具体系。
- 本地工具执行依赖完整 Runtime / Agent 执行环境，不通过 CLI 提供通用执行命令。

## 当前工具

- `file_read`：按行范围读取文本文件内容。
- `context_document_discover`：发现项目内标准 `AGENTS.md` 上下文文档。
- `directory_explore`：递进式探索目录结构。
- `file_name_search`：按文件名、正则、glob、扩展名定位候选文件或目录。
- `text_search`：优先使用 `ripgrep` 在文件系统中执行文本或正则搜索，环境没有 `rg` 时降级为 Python 标准库扫描。
