# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


USER_GLOBAL_REUSABLE_CONFIG = "user_global_reusable_config"
MACHINE_LOCAL_CONFIG = "machine_local_config"
PROJECT_WORKSPACE_DATA = "project_workspace_data"
RUNTIME_HISTORY_DIAGNOSTIC_STATE = "runtime_history_diagnostic_state"


@dataclass(frozen=True, slots=True)
class PersistenceInventoryItem:
    item_id: str
    title: str
    scope: str
    storage_medium: str
    storage_location: str
    persisted_data: list[str]
    config_sections: list[str] = field(default_factory=list)
    server_sync_candidate: bool = False
    sync_recommendation: str = "local_only"
    rationale: str = ""
    secret_handling: str | None = None
    migration_notes: str | None = None


def build_persistence_inventory(
    *,
    config_path: str,
    raw_config: dict[str, Any],
) -> list[PersistenceInventoryItem]:
    """构建当前运行时持久化清单与跨机器同步分类。

    该清单是 phase-1 的只读边界说明：它不迁移、不创建、不读取任何仓储文件，
    只基于当前配置路径与已知模块持久化契约说明哪些数据适合作为未来用户级服务端配置。
    """

    llm_registry_dir = _config_value(
        raw_config, "llm_model_registry", "registry_dir", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/llm-model-registry"
    )
    prompt_snippets_dir = _config_value(
        raw_config, "prompt_template", "snippets_dir", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/prompt-snippets"
    )
    mcp_config_dir = _config_value(
        raw_config, "mcp_client", "config_dir", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/mcp-servers"
    )
    agent_definitions_dir = _config_value(
        raw_config, "agent_definition", "definitions_dir", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/agents"
    )
    skill_settings_dir = _config_value(
        raw_config, "skill_service", "settings_dir", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/skill"
    )
    skill_default_dir = _config_value(
        raw_config, "skill_service", "skills_dir", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/skills"
    )
    file_resource_root = _config_value(
        raw_config, "file_resource_service", "local_resource_storage_root_path", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/file-resource"
    )
    conversation_data_dir = _config_value(
        raw_config, "conversation_repository", "data_dir", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/conversations"
    )
    agent_runtime_data_dir = _config_value(
        raw_config, "agent_runtime", "data_dir", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/agent-runtime"
    )
    agent_trace_dir = _config_value(
        raw_config, "agent_runtime", "trace_dir", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/agent-traces"
    )
    communication_data_dir = _config_value(
        raw_config, "communication", "data_dir", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/communication"
    )
    scene_db_path = _config_value(
        raw_config, "scene", "scenes_db_path", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/scene/scene.db"
    )
    recent_workspaces_path = _config_value(
        raw_config, "scene", "recent_workspaces_path", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/recent-workspaces.json"
    )
    codex_oauth_data_dir = _config_value(
        raw_config, "codex_oauth", "data_dir", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/codex-oauth"
    )
    trace_data_dir = _config_value(
        raw_config, "llm_http_trace", "data_dir", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/llm-http-traces"
    )
    context_compression_dir = _config_value(
        raw_config, "context_compression", "archive_data_dir", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/context-compression"
    )
    lacp_data_dir = _config_value(
        raw_config, "lacp", "data_dir", default="${SPREAD_AI_DATA_ROOT:-~/.spread-ai}/lacp"
    )

    return [
        PersistenceInventoryItem(
            item_id="runtime_config_yaml",
            title="运行时主配置 YAML",
            scope=MACHINE_LOCAL_CONFIG,
            storage_medium="YAML file",
            storage_location=config_path,
            config_sections=["agent_runtime_service", "file_resource_service", "agent_runtime", "scene"],
            persisted_data=[
                "服务监听地址、端口、环境标识",
                "各业务模块的数据目录、缓存目录、Trace 目录",
                "OAuth、LACP、上下文压缩等功能默认运行参数",
            ],
            server_sync_candidate=False,
            sync_recommendation="local_only",
            rationale="主配置混合了本机端口、数据根目录、开发/正式环境路径和外部服务默认值，属于机器/部署绑定配置，不应整体跨机器同步。",
            migration_notes="未来只应抽取其中可复用的用户偏好字段，不能把整份 YAML 当作用户全局配置上传。",
        ),
        PersistenceInventoryItem(
            item_id="llm_model_registry",
            title="LLM 模型、服务商与执行策略注册表",
            scope=USER_GLOBAL_REUSABLE_CONFIG,
            storage_medium="SQLite database",
            storage_location=_join_location(llm_registry_dir, "llm_model_registry.sqlite3"),
            config_sections=["llm_model_registry"],
            persisted_data=[
                "模型元数据",
                "服务商定义、API Base URL、自定义请求头",
                "服务商模型接入项、模型别名、API 协议格式",
                "执行策略组与候选模型顺序",
                "服务商级与模型级 API Key",
            ],
            server_sync_candidate=True,
            sync_recommendation="server_sync_candidate",
            rationale="这些数据决定用户可复用的模型和供应商能力，安装到不同机器后仍应复用；当前本地 SQLite 只是运行期权威存储，适合后续迁移为用户级服务端配置。",
            secret_handling="包含 API Key 和可能敏感的自定义请求头；服务端同步必须做字段级加密、掩码显示和账号归属控制。",
            migration_notes="legacy YAML 只是一轮迁移输入，不应再作为同步源；迁移时需保持 API Key 与候选项 position 顺序。",
        ),
        PersistenceInventoryItem(
            item_id="image_generation_llm_registry",
            title="图像生成专用 LLM 注册表",
            scope=USER_GLOBAL_REUSABLE_CONFIG,
            storage_medium="SQLite database",
            storage_location=_join_location(llm_registry_dir, "image_generation_llm_registry.sqlite3"),
            config_sections=["llm_model_registry"],
            persisted_data=[
                "图像生成模型元数据",
                "图像生成服务商与模型接入项",
                "图像生成执行策略候选项",
                "可能覆盖图像生成模型调用的 API Key 与请求参数",
            ],
            server_sync_candidate=True,
            sync_recommendation="server_sync_candidate",
            rationale="图像生成能力与普通 LLM 一样属于用户可复用的模型接入资源，跨设备安装后应跟随用户账号复用。",
            secret_handling="与 LLM 注册表相同，需要对 API Key、Header 和请求参数做字段级敏感数据处理。",
            migration_notes="当前与主 LLM 注册表共享 registry_dir，未来迁移时需要协调同目录下两个 SQLite 数据库。",
        ),
        PersistenceInventoryItem(
            item_id="agent_definitions",
            title="Agent 静态定义",
            scope=USER_GLOBAL_REUSABLE_CONFIG,
            storage_medium="YAML files",
            storage_location=_join_location(agent_definitions_dir, "*.yaml"),
            config_sections=["agent_definition"],
            persisted_data=[
                "Agent ID、名称、描述、分组和启停状态",
                "系统提示词文本和 prompt template 引用",
                "本地工具、MCP 工具、Skill 和子 Agent 绑定",
            ],
            server_sync_candidate=True,
            sync_recommendation="server_sync_candidate",
            rationale="Agent 定义是用户构建的可复用系统能力，不绑定单个机器或项目；应作为用户全局配置在不同设备自动复用。",
            migration_notes="迁移后运行态实例仍应只保存 agent_definition_id 等逻辑引用，避免把 Agent 定义快照复制进运行态。",
        ),
        PersistenceInventoryItem(
            item_id="prompt_snippets",
            title="Prompt 可复用片段",
            scope=USER_GLOBAL_REUSABLE_CONFIG,
            storage_medium="YAML files",
            storage_location=_join_location(prompt_snippets_dir, "*.yaml"),
            config_sections=["prompt_template"],
            persisted_data=[
                "可复用提示词片段 ID、描述名、正文和更新时间",
                "Agent 定义通过 template_id/ref 引用的共享提示词内容",
            ],
            server_sync_candidate=True,
            sync_recommendation="server_sync_candidate",
            rationale="Prompt snippet 是用户长期维护的可复用知识资产，跨机器复用价值高且不依赖本机运行状态。",
            migration_notes="同步时需要保留描述名唯一性和 Agent 定义引用关系。",
        ),
        PersistenceInventoryItem(
            item_id="mcp_service_definitions",
            title="MCP 服务定义",
            scope=USER_GLOBAL_REUSABLE_CONFIG,
            storage_medium="YAML files",
            storage_location=_join_location(mcp_config_dir, "*.yaml"),
            config_sections=["mcp_client"],
            persisted_data=[
                "MCP service_id、启停状态和传输类型",
                "stdio 命令、参数、环境变量或 streamable HTTP URL",
                "服务能力、工具和资源的配置入口",
            ],
            server_sync_candidate=True,
            sync_recommendation="field_level_review",
            rationale="MCP 服务定义通常是可复用外部能力配置，但其中可能混有本机命令路径、环境变量和密钥，需要字段级判断哪些可同步。",
            secret_handling="stdio env、HTTP Header 或 URL 参数可能含密钥；服务端同步必须支持密钥字段加密和机器本地覆盖。",
            migration_notes="可优先同步 service 元数据、传输类型和稳定 URL；本机绝对路径、命令解释器路径等应保留机器本地覆盖。",
        ),
        PersistenceInventoryItem(
            item_id="object_storage_settings",
            title="对象存储用户设置",
            scope=USER_GLOBAL_REUSABLE_CONFIG,
            storage_medium="SQLite database",
            storage_location=_join_location(file_resource_root, "object_storage_settings.db"),
            config_sections=["file_resource_service"],
            persisted_data=[
                "对象存储服务商、Endpoint、Region、Bucket、公共访问 URL 和 Key 前缀",
                "对象存储 AccessKey ID 与 Secret",
                "连接超时时间",
            ],
            server_sync_candidate=True,
            sync_recommendation="field_level_review",
            rationale="对象存储连接信息可让多设备复用同一资源后端，但它混合了可共享元数据和高敏凭证，不能无差别同步。",
            secret_handling="AccessKey ID/Secret 必须加密保存；公共 URL、Bucket 等元数据可单独同步或由产品默认值提供。",
            migration_notes="当前 UI 只允许用户改密钥，其他字段多为内置默认；后续迁移应区分产品默认、用户覆盖和密钥字段。",
        ),
        PersistenceInventoryItem(
            item_id="image_generation_settings",
            title="图像生成运行偏好",
            scope=USER_GLOBAL_REUSABLE_CONFIG,
            storage_medium="SQLite database",
            storage_location=_join_location(llm_registry_dir, "image_generation_settings.db"),
            config_sections=["llm_model_registry"],
            persisted_data=[
                "图像生成默认 LLM 执行策略组引用",
                "默认请求参数 JSON",
            ],
            server_sync_candidate=True,
            sync_recommendation="server_sync_candidate",
            rationale="该设置本质是用户对图像生成能力默认策略的偏好，应跟随用户账号复用；它应引用服务端同步后的策略 ID，而不是复制策略快照。",
            migration_notes="迁移时必须先保证被引用的 LLM policy_group_id 已在用户全局注册表中存在。",
        ),
        PersistenceInventoryItem(
            item_id="web_tools_settings",
            title="Web 工具模型候选设置",
            scope=USER_GLOBAL_REUSABLE_CONFIG,
            storage_medium="SQLite database",
            storage_location=_join_location(llm_registry_dir, "web_tools_settings.db"),
            config_sections=["llm_model_registry"],
            persisted_data=[
                "联网搜索/URL 读取等 Web 工具使用的 LLM 候选项",
                "候选项启停状态和顺序",
            ],
            server_sync_candidate=True,
            sync_recommendation="server_sync_candidate",
            rationale="这是用户对工具调用模型策略的全局偏好，跨设备复用可避免重复配置。",
            migration_notes="同步时应只保存 provider_id/model_binding_id 等逻辑引用，并依赖已同步的 LLM 注册表解析。",
        ),
        PersistenceInventoryItem(
            item_id="context_compression_settings",
            title="上下文压缩偏好与 Worker Prompt",
            scope=USER_GLOBAL_REUSABLE_CONFIG,
            storage_medium="SQLite database",
            storage_location=_join_location(context_compression_dir, "context_compression_worker_prompts.db"),
            config_sections=["context_compression"],
            persisted_data=[
                "上下文压缩默认 LLM 执行策略组引用",
                "输入模式偏好",
                "用户自定义 Worker Prompt 覆盖文本",
                "上下文压缩 job/archive 历史记录",
            ],
            server_sync_candidate=True,
            sync_recommendation="field_level_review",
            rationale="策略引用、输入模式和自定义 Worker Prompt 是用户全局偏好；但同目录还保存压缩任务历史和归档，属于运行历史，不应一起同步。",
            migration_notes="迁移时需拆分同一 SQLite/目录中的设置表、Prompt 表和 archive/job 数据，只同步偏好与自定义 Prompt。",
        ),
        PersistenceInventoryItem(
            item_id="skill_settings_and_repository",
            title="Skill 根目录设置与本地 Skill 仓库",
            scope=MACHINE_LOCAL_CONFIG,
            storage_medium="SQLite database + local filesystem",
            storage_location=f"settings={_join_location(skill_settings_dir, 'skill_settings.db')}; repository={skill_default_dir}",
            config_sections=["skill_service"],
            persisted_data=[
                "用户选择的 Skill 根目录路径",
                "Skill 仓库内的 SKILL.md 与实现文件",
            ],
            server_sync_candidate=False,
            sync_recommendation="field_level_review",
            rationale="Skill 内容可能是可复用资源，但当前持久化的核心字段是本机文件系统路径；不同机器路径和仓库同步方式不同，不能直接作为用户全局设置同步。",
            migration_notes="未来可单独设计 Skill 内容仓库/市场同步；当前目录路径应作为机器本地覆盖。",
        ),
        PersistenceInventoryItem(
            item_id="scene_project_data",
            title="项目、Scene 与最近工作区",
            scope=PROJECT_WORKSPACE_DATA,
            storage_medium="SQLite database + JSON files",
            storage_location=f"scenes={scene_db_path}; recent={recent_workspaces_path}; project=<workspace>/.spread-ai/project.json",
            config_sections=["scene"],
            persisted_data=[
                "project_id 与 workspace 归属关系",
                "Scene 记录、presence 相关状态和最近工作区索引",
                "每个项目内的 .spread-ai/project.json manifest",
            ],
            server_sync_candidate=False,
            sync_recommendation="project_bound_local_state",
            rationale="这些数据描述本机打开过哪些工作区以及 workspace 内 scene 归属，和本地路径、项目目录强绑定，不属于跨机器复用配置。",
            migration_notes="可以考虑未来做项目级协作/备份，但不能并入用户全局系统配置同步。",
        ),
        PersistenceInventoryItem(
            item_id="conversation_history",
            title="会话消息与聊天历史",
            scope=RUNTIME_HISTORY_DIAGNOSTIC_STATE,
            storage_medium="SQLite database + JSONL files",
            storage_location=conversation_data_dir,
            config_sections=["conversation_repository"],
            persisted_data=[
                "conversation/thread/task/agent 维度索引与元数据",
                "用户消息、assistant 消息、工具结果等 JSONL 正文",
            ],
            server_sync_candidate=False,
            sync_recommendation="local_runtime_history",
            rationale="会话是项目和任务运行历史，包含用户输入、工具输出和可能敏感的项目上下文；它不应作为用户全局配置自动同步。",
            migration_notes="若未来需要云端会话历史，应作为独立历史/审计产品能力设计，并取得用户授权。",
        ),
        PersistenceInventoryItem(
            item_id="agent_runtime_state",
            title="Agent 任务、实例运行态与 Trace",
            scope=RUNTIME_HISTORY_DIAGNOSTIC_STATE,
            storage_medium="SQLite database + Markdown trace files",
            storage_location=f"db={_join_location(agent_runtime_data_dir, 'agent_runtime.db')}; traces={agent_trace_dir}",
            config_sections=["agent_runtime"],
            persisted_data=[
                "Agent task、instance、父子关系和状态流转",
                "每个 Agent 实例的 Markdown Trace 文件",
            ],
            server_sync_candidate=False,
            sync_recommendation="local_runtime_history",
            rationale="运行态和 Trace 服务于本机正在执行或已执行任务的恢复、诊断和追踪，不是可复用系统配置。",
            migration_notes="运行态应继续引用 agent_definition_id、llm_policy_group_id 等配置 ID，而不是持有配置快照。",
        ),
        PersistenceInventoryItem(
            item_id="communication_and_terminal_state",
            title="通信流、Chat Bubble 与终端历史",
            scope=RUNTIME_HISTORY_DIAGNOSTIC_STATE,
            storage_medium="SQLite databases",
            storage_location=f"communication={communication_data_dir}; terminal=${{SPREAD_AI_DATA_ROOT:-~/.spread-ai}}/terminal; chat-bubble=${{SPREAD_AI_DATA_ROOT:-~/.spread-ai}}/chat-bubble",
            config_sections=["communication"],
            persisted_data=[
                "SSE/通信消息日志与逻辑 stream 序号",
                "聊天气泡展示状态",
                "终端 session、命令、输出 chunk 与快照",
            ],
            server_sync_candidate=False,
            sync_recommendation="local_runtime_history",
            rationale="这些数据属于本机运行过程中的 UI/通信/终端历史，包含高频日志和本机进程状态，不适合跨设备自动复用。",
        ),
        PersistenceInventoryItem(
            item_id="file_resource_archive",
            title="文件资源缓存与归档索引",
            scope=RUNTIME_HISTORY_DIAGNOSTIC_STATE,
            storage_medium="Local filesystem + SQLite database",
            storage_location=file_resource_root,
            config_sections=["file_resource_service"],
            persisted_data=[
                "上传/生成的图片、PDF、附件等本地缓存文件",
                "文件资源归档索引、对象存储 object key 和访问地址",
            ],
            server_sync_candidate=False,
            sync_recommendation="local_runtime_artifacts",
            rationale="文件资源是任务运行产物和本地缓存，和具体会话、项目及对象存储生命周期相关，不属于用户全局配置。",
            migration_notes="对象存储连接设置可字段级同步；文件资源内容本身应按资源/会话产品能力单独治理。",
        ),
        PersistenceInventoryItem(
            item_id="auth_and_oauth_state",
            title="Runtime Auth 与 Codex OAuth 凭据",
            scope=RUNTIME_HISTORY_DIAGNOSTIC_STATE,
            storage_medium="SQLite databases",
            storage_location=f"runtime_auth=${{SPREAD_AI_DATA_ROOT:-~/.spread-ai}}/auth/runtime_auth.sqlite3; codex_oauth={_join_location(codex_oauth_data_dir, 'codex_oauth.sqlite3')}",
            config_sections=["codex_oauth"],
            persisted_data=[
                "当前 Runtime 的本地账号、密码哈希与设备会话 token 哈希",
                "Codex OAuth pending login、access token、refresh token、账号信息",
            ],
            server_sync_candidate=False,
            sync_recommendation="security_local_or_dedicated_identity",
            rationale="Runtime Auth 是访问当前本机后端的安全边界；OAuth token 是高敏认证材料，不能混入普通用户全局配置同步。",
            secret_handling="如需跨设备身份体系，应设计独立云账号、授权和 token 托管方案，而不是同步本机 auth SQLite。",
        ),
        PersistenceInventoryItem(
            item_id="diagnostic_archives",
            title="LLM HTTP Trace、上下文压缩归档和诊断日志",
            scope=RUNTIME_HISTORY_DIAGNOSTIC_STATE,
            storage_medium="SQLite databases + local files",
            storage_location=f"llm_http_trace={trace_data_dir}; context_compression={context_compression_dir}; electron_logs=%APPDATA%/spread-ai-web/logs",
            config_sections=["llm_http_trace", "context_compression"],
            persisted_data=[
                "LLM HTTP 请求/响应 Trace",
                "上下文压缩 job、archive 和快照",
                "Electron 主进程与后端 stdout/stderr 日志",
            ],
            server_sync_candidate=False,
            sync_recommendation="diagnostic_local_only",
            rationale="诊断数据体积大、时效短且可能包含敏感请求内容，只应按诊断/支持场景显式导出，不应自动同步为用户配置。",
        ),
        PersistenceInventoryItem(
            item_id="lacp_settings",
            title="远程 Agent 协作 LACP 设置",
            scope=USER_GLOBAL_REUSABLE_CONFIG,
            storage_medium="SQLite database",
            storage_location=_join_location(lacp_data_dir, "lacp_settings.db"),
            config_sections=["lacp"],
            persisted_data=[
                "LACP 启停状态",
                "Registry URL",
                "LACP API Key",
            ],
            server_sync_candidate=True,
            sync_recommendation="field_level_review",
            rationale="LACP registry 地址和启用偏好可能是用户跨设备复用的协作配置，但 API Key 是敏感凭据，且不同机器可能需要不同授权。",
            secret_handling="API Key 必须作为密钥字段加密，且应允许机器本地覆盖或重新授权。",
        ),
        PersistenceInventoryItem(
            item_id="browser_runtime_storage",
            title="前端浏览器本地运行态",
            scope=MACHINE_LOCAL_CONFIG,
            storage_medium="Browser localStorage/sessionStorage",
            storage_location="renderer localStorage/sessionStorage",
            persisted_data=[
                "Runtime base URL、访问 token、device_id",
                "当前 workspace/session 选择",
                "面板可见性、Scene claim 等 UI/多标签状态",
            ],
            server_sync_candidate=False,
            sync_recommendation="browser_local_only",
            rationale="浏览器存储保存的是当前设备如何连接本机 Runtime 以及 UI 会话状态，和设备、标签页、窗口生命周期强绑定。",
            secret_handling="Runtime token 只能用于访问当前后端，不应被跨设备同步或写入普通服务端配置。",
        ),
    ]


def _config_value(raw_config: dict[str, Any], section: str, key: str, *, default: str) -> str:
    section_value = raw_config.get(section)
    if not isinstance(section_value, dict):
        return default
    value = section_value.get(key)
    if value is None or value == "":
        return default
    return str(value)


def _join_location(root: str, child: str) -> str:
    separator = "\\" if "\\" in root and "/" not in root else "/"
    normalized_root = root.rstrip("/\\")
    normalized_child = child.lstrip("/\\")
    return f"{normalized_root}{separator}{normalized_child}"
