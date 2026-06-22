# Skill 模块

Skill 发现、元数据管理与内置 Skill 安装。扫描用户配置的 Skill 仓库目录，解析各子目录 `SKILL.md` YAML frontmatter，提供 REST 读路径和应用层分组能力。

Skill 遵循 Agent Skills Open Standard；每个 Skill 是文件系统上的一个目录，最小结构为 `skill-name/SKILL.md`。

---

## 关键边界

- 本模块不再提供 OS/system CLI；不要恢复 `spread-ai skill ...` 或 `skill_cli_adapter.py`。
- Runtime 随 release 携带内置 `spread-ai-config-management` Skill，后端通过 `BuiltinSkillInstaller` 幂等安装/升级到用户配置的 Skill 仓库目录。
- 内置 Skill 目录若不存在则安装；若同名目录由 `manifest.json` 标记 `managed_by=spread-ai-runtime` 且 `package_id=spread-ai-config-management`，可按 source hash/version 升级；同名非托管目录不得覆盖，只返回诊断。
- 安装触发点：SkillFactory 创建时、Skill settings 保存后、Skill list/get 兜底读路径。安装导致内容变化时必须刷新仓库缓存。
- `POST /api/skills/refresh` 只保留内部/诊断兜底；公共配置管理脚本不依赖它。
- Skill 元数据对公共管理脚本只读：允许 list/get，不提供 refresh/settings/group/content 写入入口。

---

## REST API

| 能力 | REST API | 说明 |
|---|---|---|
| Skill 摘要列表 | `GET /api/skills?summary=true` | 返回 `skill_id/name/description/group/metadata`，供脚本低噪声查询 |
| Skill 兼容完整列表 | `GET /api/skills` | 返回完整元数据、目录路径和分组，供现有前端使用 |
| Skill 详情 | `GET /api/skills/{skill_id}` | 返回指定 Skill 元数据详情 |
| 内置 Skill 安装诊断 | `GET /api/skills/builtin-installation` | 幂等检查/安装内置 Skill 并返回状态 |
| Skill 设置 | `GET/PUT /api/skills/settings` | 保存用户 Skill 仓库目录；保存后自动安装内置 Skill 并刷新 |
| 内部刷新 | `POST /api/skills/refresh` | 内部/诊断兜底，非公共脚本工作流 |

---

## 内置配置管理 Skill

内置源目录：`builtin-skills/spread-ai-config-management/`。release 打包时由 `scripts/build_desktop_runtime.py` 作为 PyInstaller data 放入 sidecar，运行时解析顺序为：

1. `SPREAD_AI_BUILTIN_SKILLS_DIR` 显式覆盖。
2. PyInstaller `_MEIPASS/builtin-skills`。
3. 源码仓库根 `builtin-skills`。
4. 包内 `agent_runtime/builtin_skills` 兜底。

内置 Skill 脚本只作为本机 Runtime REST client，不直接写 PG/YAML/SQLite，不绕过 Runtime auth/cache/reconcile/领域校验。

---

## DDD 分层结构

```
src/agent_runtime/skill/
├── __init__.py
├── builtin_skill_installer.py              # 内置 Skill 幂等安装/升级
├── skill_config.py                         # 模块配置 dataclass
├── skill_definition.py                     # 领域实体（聚合根）
├── skill_repository.py                     # 仓储接口（Protocol）
├── skill_service.py                        # 领域服务
├── infrastructure/
│   ├── __init__.py
│   ├── filesystem_skill_repository.py      # 仓储实现：文件系统扫描 + 自动刷新 + SKILL.md 解析
│   └── skill_group_store.py                # 应用层分组存储
├── north_gateway/
│   ├── __init__.py
│   ├── skill_contracts.py                  # 北向网关契约（SkillGateway Protocol + DTO）
│   ├── skill_gateway_impl.py               # 网关实现
│   └── skill_rest_adapter.py               # REST 适配器：/api/skills
└── skill_factory.py                        # 工厂：组装依赖并安装内置 Skill
```

### SKILL.md frontmatter 字段

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `name` | ✅ | string | 稳定 Skill ID，来自 frontmatter；字符规则与文件目录名一致，物理目录名只是承载路径 |
| `description` | ✅ | string | 做什么、何时使用 |
| `license` | | string | 许可证 |
| `compatibility` | | string | 环境/依赖要求 |
| `metadata` | | map | 字符串到字符串映射；友好展示名可放 `metadata.display-name` |
| `allowed-tools` | | string | 空格分隔的工具列表（实验性） |

重复 `name` 在同一仓库内按扫描顺序保留首个有效 Skill 并跳过后续重复项。
