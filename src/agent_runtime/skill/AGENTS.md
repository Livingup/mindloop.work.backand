# Skill 模块

Skill 发现与元数据管理。扫描指定目录下的 Skill 子目录，解析 `SKILL.md` YAML frontmatter，提供 Skill 列表查询和刷新能力。

Skill 遵循 [Agent Skills Open Standard](../../doc/agent-skills-open-standard-skill-spec.md) 规范，每个 Skill 是文件系统上的一个目录，最小结构为 `skill-name/SKILL.md`。

---

## 核心能力

| 能力 | CLI 命令 | REST API | 说明 |
|---|---|---|---|
| 列出所有 Skill | `spread-ai skill list` | `GET /api/skills` | 返回完整元数据 + 目录路径 |
| 刷新 Skill 索引 | `spread-ai skill refresh` | `POST /api/skills/refresh` | 重新扫描 Skill 仓库目录 |

## Skill 仓库

- Skill 仓库目录路径在 `config/agent_runtime.yaml` 的 `skill_service.skills_dir` 中配置
- 仓库是一个公共目录，不绑定本项目，遵循通用标准
- 用户直接在仓库目录下创建/编辑/删除 Skill 子目录，无需通过 API
- 启动时自动扫描，也可通过接口触发重新扫描

## Skill 目录结构（Agent Skills Open Standard）

```
skill-name/
├── SKILL.md          # 必须，YAML frontmatter + Markdown 正文
├── scripts/          # 可选，可执行脚本
├── references/       # 可选，补充文档
├── assets/           # 可选，静态资源
└── ...
```

### SKILL.md frontmatter 字段

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `name` | ✅ | string | 稳定 Skill ID，来自 frontmatter；字符规则与文件目录名一致，物理目录名只是承载路径，不要求完全一致 |
| `description` | ✅ | string | 做什么、何时使用 |
| `license` | | string | 许可证 |
| `compatibility` | | string | 环境/依赖要求 |
| `metadata` | | map | 字符串到字符串映射；友好展示名可放 `metadata.display-name` |
| `allowed-tools` | | string | 空格分隔的工具列表（实验性） |

`name` 的校验规则与文件目录名一致：允许大小写、空格和 Unicode；但不能是空白、`.` 或 `..`，不能包含路径分隔符、Windows 非法文件名字符、控制字符，不能以空格或点号结尾，也不能使用 Windows 保留设备名。重复 `name` 在同一仓库内按扫描顺序保留首个有效 Skill 并跳过后续重复项。

---

## 服务对象

1. **Agent** — 通过 Skill 列表获取目录路径，自行按规范读取 SKILL.md 正文和子目录
2. **用户** — 管理后台查看系统中可用的 Skill 列表
3. **CLI** — `spread-ai skill list` / `spread-ai skill refresh`

---

## DDD 分层结构

```
src/agent_runtime/skill/
├── __init__.py
├── skill_config.py                         # 模块配置 dataclass
├── skill_definition.py                     # 领域实体（聚合根）
├── skill_repository.py                     # 仓储接口（Protocol）
├── skill_service.py                        # 领域服务
├── infrastructure/
│   ├── __init__.py
│   └── filesystem_skill_repository.py      # 仓储实现：文件系统扫描 + SKILL.md 解析
├── north_gateway/
│   ├── __init__.py
│   ├── skill_contracts.py                  # 北向网关契约（SkillGateway Protocol + DTO）
│   ├── skill_gateway_impl.py              # 网关实现
│   ├── skill_cli_adapter.py               # CLI 适配器：spread-ai skill list/refresh
│   └── skill_rest_adapter.py              # REST 适配器：/api/skills
└── skill_factory.py                        # 工厂：组装依赖
```

### 领域实体

```python
@dataclass
class SkillDefinition:
    """Skill 定义实体（聚合根）。"""

    skill_id: str                           # 唯一标识（= frontmatter name）
    name: str                               # Skill 名称（与 skill_id 一致，不要求等于目录名）
    description: str                        # Skill 描述
    directory_path: str                     # 目录绝对路径
    license: str | None = None              # 许可证
    compatibility: str | None = None        # 环境要求
    metadata: dict[str, str] = {}           # 额外元数据
    allowed_tools: str | None = None        # 允许的工具列表
```

### 北向网关契约

```python
class SkillGateway(Protocol):
    def list_skills(self) -> list[SkillInfo]: ...
    def refresh(self) -> None: ...
```
