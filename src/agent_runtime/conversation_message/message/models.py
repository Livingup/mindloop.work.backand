"""统一 LLM 消息语义模型核心领域类型。

该模块集中定义统一 LLM 调用服务中消息领域模型的核心类型，包括：
- 基础枚举与类型别名（Provider, Vendor, Modality, Role）
- 消息内容块联合类型（UniPart 及其各具体 Part）
- 统一消息模型（UniMessage）
- 会话模型与派生状态（Conversation, ConversationState）
- 协议适配器接口（ProviderAdapter）

设计原则：
- 存储层单调追加、永不修改：每条历史消息入库后其 providerExtras.*.raw 冻结。
- 双层结构：公共语义层（content[]，跨平台可读）+ 平台层（providerExtras.raw，仅原平台可读）。
- 仅 assistant 有双层；user / tool 是纯通用 POJO，不携带平台私有数据。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable
from uuid import uuid4

from agent_runtime.input_modality import (
    INPUT_MODALITY_IMAGE,
    INPUT_MODALITY_PDF,
    INPUT_MODALITY_TEXT,
)
from agent_runtime.llm_api_protocol import (
    API_FORMAT_ANTHROPIC_MESSAGES,
    API_FORMAT_GEMINI_GENERATE_CONTENT,
    API_FORMAT_OPENAI_CHAT_COMPLETIONS,
    API_FORMAT_OPENAI_RESPONSES,
    API_FORMAT_ZHIPU_CHAT_COMPLETIONS,
)

# ── 基础类型 ─────────────────────────────────────────────────────

# 协议标识（开放字符串，当前统一使用 llm_api_protocol 中定义的具体 API 协议格式）。
# 示例："anthropic_messages" | "openai_responses" | "openai_chat_completions" | "gemini_generate_content"
Provider: TypeAlias = str

# 服务商标识（元数据，本层不依据它做任何逻辑）。
Vendor: TypeAlias = str

# 用户输入模态。当前仅支持 text / image / pdf。
Modality: TypeAlias = str

# 消息角色。
Role: TypeAlias = str

# Responses 配置时间线状态与稳定消息锚点类型。
ConfigurationEventStatus: TypeAlias = Literal["historical", "active"]
ConfigurationAnchor: TypeAlias = Literal["start", "before_message_id", "after_message_id"]

# 内置 Provider 常量，方便调用方引用。Provider 取值与 api_format 保持一致。
PROVIDER_ANTHROPIC_MESSAGES: str = API_FORMAT_ANTHROPIC_MESSAGES
PROVIDER_GEMINI_GENERATE_CONTENT: str = API_FORMAT_GEMINI_GENERATE_CONTENT
PROVIDER_OPENAI_CHAT_COMPLETIONS: str = API_FORMAT_OPENAI_CHAT_COMPLETIONS
PROVIDER_OPENAI_RESPONSES: str = API_FORMAT_OPENAI_RESPONSES
PROVIDER_ZHIPU_CHAT_COMPLETIONS: str = API_FORMAT_ZHIPU_CHAT_COMPLETIONS

# 兼容旧命名：Claude/Gemini/OpenAI 旧常量仍可引用，但值已统一为具体 API 协议格式。
PROVIDER_CLAUDE: str = PROVIDER_ANTHROPIC_MESSAGES
PROVIDER_GEMINI: str = PROVIDER_GEMINI_GENERATE_CONTENT
PROVIDER_OPENAI: str = PROVIDER_OPENAI_RESPONSES

# 角色常量。
ROLE_USER: str = "user"
ROLE_ASSISTANT: str = "assistant"
ROLE_TOOL: str = "tool"

# 模态常量。保留 MODALITY_* 旧命名作为消息模块公共 API，实际值来自全局输入模态定义。
MODALITY_TEXT: str = INPUT_MODALITY_TEXT
MODALITY_IMAGE: str = INPUT_MODALITY_IMAGE
MODALITY_PDF: str = INPUT_MODALITY_PDF


# ── UniPart：消息内容块 ──────────────────────────────────────────

@dataclass(slots=True)
class TextPart:
    """文本内容块。"""

    # 文本内容。
    text: str

    # 内容块类型标识，固定为 text。
    kind: str = field(default=INPUT_MODALITY_TEXT, init=False)


@dataclass(slots=True)
class ImagePart:
    """图片内容块。

    仅用于 user / tool 消息。
    通过 resourceId 引用统一文件资源服务中的图片资源，
    渲染时由 ResourceResolver 按需转为 base64 或 URL。
    """

    # 统一文件资源标识，指向 FileResourceArchive.resource_id。
    resource_id: str

    # 内容块类型标识，固定为 image。
    kind: str = field(default=INPUT_MODALITY_IMAGE, init=False)


@dataclass(slots=True)
class PdfPart:
    """PDF 内容块。

    仅用于 user / tool 消息。
    通过 resourceId 引用统一文件资源服务中的 PDF 资源。
    """

    # 统一文件资源标识，指向 FileResourceArchive.resource_id。
    resource_id: str

    # 内容块类型标识，固定为 pdf。
    kind: str = field(default=INPUT_MODALITY_PDF, init=False)


@dataclass(slots=True)
class ThinkingPart:
    """思考/推理内容块（纯语义层，平台无关）。

    各平台的原始凭证（signature / thoughtSignature / encrypted_content）
    统一存在消息级 providerExtras[provider].raw 中，不在 part 级重复存储。
    """

    # 可选的明文摘要，仅供 UI 展示或跨平台降级时使用。
    text: str | None = None

    # 内容块类型标识，固定为 "thinking"。
    kind: str = field(default="thinking", init=False)


@dataclass(slots=True)
class ToolUsePart:
    """工具调用内容块（预留，当前不实现）。

    各平台的原始 tool_use / functionCall / function_call 数据
    统一存在消息级 providerExtras[provider].raw 中。
    """

    # 工具名称。
    name: str

    # 工具调用参数。
    input: Any

    # 平台工具调用 ID。OpenAI Responses 使用 call_id，Anthropic 使用 tool_use.id；Gemini 无 ID 时可为空。
    tool_use_id: str | None = None

    # 内容块类型标识，固定为 "tool_use"。
    kind: str = field(default="tool_use", init=False)


@dataclass(slots=True)
class ToolResultPart:
    """工具执行结果内容块。

    tool 消息是纯通用 POJO，不存 raw。各协议适配器根据本结构生成目标平台的 tool_result 消息。
    """

    # 关联 assistant 消息里 tool_use 的标识。
    tool_use_ref: str

    # 工具是否执行失败。各协议适配器自己决定如何编码：
    # - Claude: 映射到原生 is_error 字段
    # - Gemini: 编码为 response.error key
    # - OpenAI Responses/Chat、Zhipu: 在 content 文本里加错误标记
    is_error: bool

    # 工具结果内容。支持三种形态：
    # - str: 纯文本结果（所有协议都能直接使用）
    # - list[UniPart]: 多模态内容块（TextPart + ImagePart + PdfPart，
    #   ImagePart/PdfPart 内部只存 resource_id，渲染时由 ResourceResolver 按平台支持转 URL/base64/路径）
    # - dict / 任意 JSON: 结构化对象（Gemini 原生支持，其他协议序列化为 JSON 字符串）
    output: Any

    # 内容块类型标识，固定为 "tool_result"。
    kind: str = field(default="tool_result", init=False)


# 消息内容块联合类型。
UniPart: TypeAlias = TextPart | ImagePart | PdfPart | ThinkingPart | ToolUsePart | ToolResultPart


# ── UniMessage：统一消息模型 ─────────────────────────────────────

@dataclass(slots=True)
class UniMessage:
    """统一 LLM 消息模型。

    每条消息由公共语义层 content[]（跨平台可读）和可选的平台层
    providerExtras[provider].raw（仅原平台可读的原始对象）组成。

    - user / tool 消息：纯通用 POJO，只有 content[]，
      无 source_provider、无 provider_extras。
    - assistant 消息：双层结构，content[] 公共层 + provider_extras[provider]["raw"] 平台层，
      同平台渲染走 raw 回灌（无损），跨平台渲染走公共层降级。
    """

    # 消息角色：user / assistant / tool。
    role: Role

    # 公共语义层：跨平台可读的内容块列表（平台无关）。
    content: list[UniPart]

    # 产生此消息的 LLM 协议类型。仅 assistant 消息有值。
    # user / tool 是用户侧产生的纯通用消息，不属于任何 LLM 平台。
    source_provider: Provider | None = None

    # 实际服务商标识（元数据，本层不依据它做任何逻辑）。仅 assistant 消息有值。
    source_vendor: Vendor | None = None

    # LLM 平台原始响应。仅 assistant 消息才有。
    # user / tool 消息是纯通用 POJO，永远没有此字段。
    #
    # key = source_provider 值（如 "anthropic_messages" / "openai_responses"）。
    # value = {"raw": [...]} 原始响应内容数组。
    # 同平台渲染时：直接回灌 raw（无损）。
    # 跨平台渲染时：忽略 raw，仅用 content[] 公共层重建（降级）。
    provider_extras: dict[str, dict[str, Any]] | None = None

    # 持久稳定消息标识。配置时间线引用该锚点，不能引用目标协议数组下标。
    message_id: str = field(default_factory=lambda: f"msg_{uuid4().hex}")

    def __post_init__(self) -> None:
        if not isinstance(self.message_id, str) or not self.message_id.strip():
            raise ValueError("message_id 必须是非空字符串。")


# ── Responses configuration request view ─────────────────────────


@dataclass(frozen=True, slots=True)
class ConfigurationComponentEvent:
    """提示词或工具组件在上游维护的一个配置事件。

    Conversation 只消费这个快照视图来渲染 OpenAI Responses；它不创建、
    修改、冻结或持久化事件。历史事件必须提供 snapshot，活动事件的内容
    则由相应的 Current*Configuration 在同一次请求中实时解析。
    """

    event_id: str
    definition_id: str
    anchor: ConfigurationAnchor = "start"
    anchor_message_id: str | None = None
    sequence: int = 0
    status: ConfigurationEventStatus = "historical"
    snapshot: Any | None = None
    revision: str | None = None
    component: Literal["prompt", "tool"] | None = None

    def __post_init__(self) -> None:
        if self.status not in {"historical", "active"}:
            raise ValueError("配置事件 status 必须是 historical 或 active。")
        if self.anchor not in {"start", "before_message_id", "after_message_id"}:
            raise ValueError("配置事件 anchor 不合法。")
        if self.sequence < 0:
            raise ValueError("配置事件 sequence 不能为负数。")
        if not self.event_id:
            raise ValueError("配置事件 event_id 不能为空。")
        if not self.definition_id:
            raise ValueError("配置事件 definition_id 不能为空。")
        if self.anchor == "start":
            if self.anchor_message_id is not None:
                raise ValueError("start 配置事件不能指定 anchor_message_id。")
        elif not self.anchor_message_id:
            raise ValueError(f"{self.anchor} 配置事件必须指定 anchor_message_id。")
        if self.status == "historical" and self.snapshot is None:
            raise ValueError("历史配置事件必须包含冻结 snapshot。")
        if self.component is not None and self.component not in {"prompt", "tool"}:
            raise ValueError("配置事件 component 必须是 prompt 或 tool。")


@dataclass(frozen=True, slots=True)
class CurrentPromptConfiguration:
    """上游本次按活动提示词 definition_id 解析出的实时内容。"""

    event_id: str
    definition_id: str
    resolved_content: Any
    revision: str | None = None


@dataclass(frozen=True, slots=True)
class CurrentToolsConfiguration:
    """上游本次按活动工具 definition_id 解析出的实时有序工具定义。"""

    event_id: str
    definition_id: str
    ordered_definitions: tuple[dict[str, Any], ...]
    allowlist: tuple[str, ...] = field(default_factory=tuple)
    revision: str | None = None


@dataclass(frozen=True, slots=True)
class RequestConfigurationView:
    """上游提供给一次请求的不可变配置视图。

    `prompt_events` 与 `tool_events` 是独立时间线。只有 OpenAI
    Responses 会按其 anchor/sequence 消费历史事件；其他协议不消费本视图，
    继续使用上游当次通过请求 system_prompt/tools 提供的实时配置。
    """

    conversation_id: str
    view_revision: str
    prompt_events: tuple[ConfigurationComponentEvent, ...] = field(default_factory=tuple)
    tool_events: tuple[ConfigurationComponentEvent, ...] = field(default_factory=tuple)
    current_prompt: CurrentPromptConfiguration | None = None
    current_tools: CurrentToolsConfiguration | None = None

    def __post_init__(self) -> None:
        if not self.conversation_id:
            raise ValueError("配置视图 conversation_id 不能为空。")
        if not self.view_revision:
            raise ValueError("配置视图 view_revision 不能为空。")
        _validate_component_events("提示词", self.prompt_events, self.current_prompt)
        _validate_component_events("工具", self.tool_events, self.current_tools)
        all_event_ids = [event.event_id for event in (*self.prompt_events, *self.tool_events)]
        if len(all_event_ids) != len(set(all_event_ids)):
            raise ValueError("提示词和工具配置事件的 event_id 不能相互重复。")


def _validate_component_events(
    component_name: str,
    events: tuple[ConfigurationComponentEvent, ...],
    current: CurrentPromptConfiguration | CurrentToolsConfiguration | None,
) -> None:
    event_ids = [event.event_id for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError(f"{component_name}配置事件 event_id 不能重复。")
    active_events = [event for event in events if event.status == "active"]
    if len(active_events) > 1:
        raise ValueError(f"{component_name}配置事件最多只能有一个 active 事件。")
    if active_events:
        if current is None:
            raise ValueError(f"活动{component_name}配置事件必须提供 current 解析结果。")
        active = active_events[0]
        if active.event_id != current.event_id or active.definition_id != current.definition_id:
            raise ValueError(f"活动{component_name}配置事件必须与 current 解析结果匹配。")
    elif current is not None:
        raise ValueError(f"没有活动{component_name}配置事件时不能提供 current 解析结果。")


def build_current_request_configuration_view(
    *,
    conversation_id: str,
    system_prompt: str | None,
    tools: tuple[dict[str, Any], ...],
) -> RequestConfigurationView:
    """为尚未接入上游时间线的调用构造当次初始配置视图。

    该函数不写入 Conversation，也不保存事件；它只让已有调用路径能够
    立即改用 Responses input 配置表达。上游接入时间线后应传入其自己
    持久化并冻结的 RequestConfigurationView。
    """

    prompt_events: tuple[ConfigurationComponentEvent, ...] = ()
    current_prompt: CurrentPromptConfiguration | None = None
    if system_prompt is not None:
        prompt_event_id = "runtime-current-prompt"
        prompt_events = (
            ConfigurationComponentEvent(
                event_id=prompt_event_id,
                definition_id="runtime-current-prompt",
                anchor="start",
                sequence=1,
                status="active",
            ),
        )
        current_prompt = CurrentPromptConfiguration(
            event_id=prompt_event_id,
            definition_id="runtime-current-prompt",
            resolved_content=system_prompt,
        )

    tool_events: tuple[ConfigurationComponentEvent, ...] = ()
    current_tools: CurrentToolsConfiguration | None = None
    if tools:
        tool_event_id = "runtime-current-tools"
        tool_events = (
            ConfigurationComponentEvent(
                event_id=tool_event_id,
                definition_id="runtime-current-tools",
                anchor="start",
                sequence=0,
                status="active",
            ),
        )
        current_tools = CurrentToolsConfiguration(
            event_id=tool_event_id,
            definition_id="runtime-current-tools",
            ordered_definitions=tools,
        )
    return RequestConfigurationView(
        conversation_id=conversation_id,
        view_revision="runtime-current",
        prompt_events=prompt_events,
        tool_events=tool_events,
        current_prompt=current_prompt,
        current_tools=current_tools,
    )


def _infer_component_type(event: ConfigurationComponentEvent) -> Literal["prompt", "tool"]:
    """推断或返回配置事件的组件类型（prompt 或 tool）。"""
    if event.component in {"prompt", "tool"}:
        return event.component
    event_id_lower = event.event_id.lower()
    def_id_lower = event.definition_id.lower()
    if (
        "tool" in event_id_lower
        or "tool" in def_id_lower
        or event_id_lower.startswith(("t-", "t_"))
        or def_id_lower.startswith(("t-", "t_"))
    ):
        return "tool"
    if (
        "prompt" in event_id_lower
        or "prompt" in def_id_lower
        or event_id_lower.startswith(("p-", "p_"))
        or def_id_lower.startswith(("p-", "p_"))
    ):
        return "prompt"
    if event.snapshot is not None:
        if isinstance(event.snapshot, (list, tuple)) and event.snapshot and isinstance(event.snapshot[0], dict):
            return "tool"
        if isinstance(event.snapshot, str):
            return "prompt"
    return "prompt"


def build_request_configuration_view_from_timeline(
    *,
    conversation_id: str,
    timeline: tuple[ConfigurationComponentEvent, ...] | list[ConfigurationComponentEvent],
    system_prompt: str | None,
    tools: tuple[dict[str, Any], ...],
    view_revision: str | None = None,
) -> RequestConfigurationView:
    """根据会话配置时间线和当次 Agent 实时系统提示词/工具定义构建不可变配置视图。

    - 历史事件使用冻结 snapshot。
    - 活跃事件使用本次传入的实时解析结果（system_prompt 与 tools），天然支持热更新。
    - 仅在目标为 OpenAI Responses 时由适配器按锚点消费；非 Responses 协议跳过该视图。
    """
    prompt_events: list[ConfigurationComponentEvent] = []
    tool_events: list[ConfigurationComponentEvent] = []
    for ev in timeline:
        comp = _infer_component_type(ev)
        if comp == "prompt":
            prompt_events.append(ev)
        else:
            tool_events.append(ev)

    current_prompt: CurrentPromptConfiguration | None = None
    active_prompt_events = [e for e in prompt_events if e.status == "active"]
    if len(active_prompt_events) > 1:
        raise ValueError("提示词配置事件最多只能有一个 active 事件。")
    if active_prompt_events:
        active_p = active_prompt_events[0]
        current_prompt = CurrentPromptConfiguration(
            event_id=active_p.event_id,
            definition_id=active_p.definition_id,
            resolved_content=system_prompt,
            revision=active_p.revision,
        )
    elif system_prompt is not None and not prompt_events:
        p_id = "runtime-current-prompt"
        active_p = ConfigurationComponentEvent(
            event_id=p_id,
            definition_id=p_id,
            anchor="start",
            sequence=1,
            status="active",
            component="prompt",
        )
        prompt_events.append(active_p)
        current_prompt = CurrentPromptConfiguration(
            event_id=p_id,
            definition_id=p_id,
            resolved_content=system_prompt,
        )

    current_tools: CurrentToolsConfiguration | None = None
    active_tool_events = [e for e in tool_events if e.status == "active"]
    if len(active_tool_events) > 1:
        raise ValueError("工具配置事件最多只能有一个 active 事件。")
    if active_tool_events:
        active_t = active_tool_events[0]
        current_tools = CurrentToolsConfiguration(
            event_id=active_t.event_id,
            definition_id=active_t.definition_id,
            ordered_definitions=tuple(tools),
            revision=active_t.revision,
        )
    elif tools and not tool_events:
        t_id = "runtime-current-tools"
        active_t = ConfigurationComponentEvent(
            event_id=t_id,
            definition_id=t_id,
            anchor="start",
            sequence=0,
            status="active",
            component="tool",
        )
        tool_events.append(active_t)
        current_tools = CurrentToolsConfiguration(
            event_id=t_id,
            definition_id=t_id,
            ordered_definitions=tuple(tools),
        )

    return RequestConfigurationView(
        conversation_id=conversation_id,
        view_revision=view_revision or f"view_{uuid4().hex[:8]}",
        prompt_events=tuple(prompt_events),
        tool_events=tuple(tool_events),
        current_prompt=current_prompt,
        current_tools=current_tools,
    )


# ── PartMeta ─────────────────────────────────────────────────────


@dataclass(slots=True)
class TextPartMeta:
    kind: str = INPUT_MODALITY_TEXT


@dataclass(slots=True)
class ImagePartMeta:
    kind: str = INPUT_MODALITY_IMAGE


@dataclass(slots=True)
class PdfPartMeta:
    page_count: int
    kind: str = INPUT_MODALITY_PDF


@dataclass(slots=True)
class ThinkingPartMeta:
    kind: str = "thinking"


@dataclass(slots=True)
class ToolUsePartMeta:
    kind: str = "tool_use"


@dataclass(slots=True)
class ToolResultPartMeta:
    kind: str = "tool_result"


PartMeta: TypeAlias = (
    TextPartMeta
    | ImagePartMeta
    | PdfPartMeta
    | ThinkingPartMeta
    | ToolUsePartMeta
    | ToolResultPartMeta
)


def _infer_part_meta(part: Any) -> PartMeta:
    kind = getattr(part, "kind", None)
    if kind == INPUT_MODALITY_TEXT:
        return TextPartMeta()
    if kind == INPUT_MODALITY_IMAGE:
        return ImagePartMeta()
    if kind == INPUT_MODALITY_PDF:
        return PdfPartMeta(page_count=0)
    if kind == "thinking":
        return ThinkingPartMeta()
    if kind == "tool_use":
        return ToolUsePartMeta()
    if kind == "tool_result":
        return ToolResultPartMeta()
    return TextPartMeta()


# ── Conversation ─────────────────────────────────────────────────


@dataclass(slots=True)
class ConversationState:
    """会话派生状态。

    由 messages 列表派生，通过 recompute_state / append_message 维护。
    记录会话中涉及的协议、服务商、模态和资源配额累计。
    """

    # 历史涉及过的 LLM 协议（去重）。
    providers: list[str] = field(default_factory=list)

    # 历史涉及过的服务商（去重，元数据）。
    vendors: list[str] = field(default_factory=list)

    # 出现过的用户输入模态（去重）。
    modalities: list[str] = field(default_factory=list)

    # 累计图片数。
    image_count: int = 0

    # 累计 PDF 页数。
    pdf_page_count: int = 0

    last_context_usage: dict[str, Any] | None = None


@dataclass(slots=True)
class Conversation:
    """统一 LLM 会话模型。

    该模型是消息领域的聚合根，维护一个 append-only 的历史消息列表、
    各平台的会话运行态以及由消息列表派生的统计状态。

    - messages：历史消息列表，是会话的唯一真相源。
    - provider_state：各 API 协议格式的会话运行态，render 时读取，API 成功后写回。
      key = provider/api_format 标识（如 "anthropic_messages" / "openai_responses"），
      value 由各 ProviderAdapter 自定义。
    - state：派生状态，由 messages 驱动。
    """

    # 会话唯一标识。
    id: str

    # 历史消息列表（append-only，入库后 providerExtras.*.raw 冻结）。
    messages: list[UniMessage] = field(default_factory=list)

    # 派生状态。messages 是真相源，state 由状态维护模块同步。
    state: ConversationState = field(default_factory=ConversationState)

    # 各 API 协议格式的会话运行态，render 时读取，API 成功后写回。
    #
    # 内置示例：
    #   anthropic_messages:        {"cache_breakpoint_indexes": [int, ...]}
    #   openai_chat_completions:   {}
    #   openai_responses:          {"prompt_cache_key": str, "stateless": bool}
    #   zhipu_chat_completions:    {"preserved_thinking": bool, "clear_thinking": bool, "cached_tokens": int | None}
    #   gemini_generate_content:   {"cached_content": str}
    provider_state: dict[str, Any] = field(default_factory=dict)

    # 系统提示词或工具组件切换时间线（由 Agent 自身维护，Conversation 层只读消费）。
    timeline: list[ConfigurationComponentEvent] = field(default_factory=list)

    # 当前生效的系统提示词（由 Agent 实时设置，支持热更新）。
    system_prompt: str | None = None

    # 当前生效的工具定义（由 Agent 实时设置，支持热更新）。
    tools: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def record_configuration_event(self, event: ConfigurationComponentEvent) -> None:
        """记录系统提示词或工具切换事件到会话时间线。

        由 Agent 负责维护该时间线，Conversation 层保持只读快照视图。
        """
        if any(existing.event_id == event.event_id for existing in self.timeline):
            raise ValueError(f"配置时间线已存在 event_id: {event.event_id}")
        self.timeline.append(event)

    def get_request_configuration_view(
        self,
        *,
        system_prompt: str | None = None,
        tools: tuple[dict[str, Any], ...] | None = None,
    ) -> RequestConfigurationView | None:
        """根据当前会话的时间线与实时配置构造不可变请求视图。"""
        effective_system = system_prompt if system_prompt is not None else self.system_prompt
        effective_tools = tools if tools is not None else self.tools
        if self.timeline:
            return build_request_configuration_view_from_timeline(
                conversation_id=self.id,
                timeline=self.timeline,
                system_prompt=effective_system,
                tools=effective_tools,
            )
        if effective_system is not None or effective_tools:
            return build_current_request_configuration_view(
                conversation_id=self.id,
                system_prompt=effective_system,
                tools=effective_tools,
            )
        return None

    def append_message(
        self,
        msg: UniMessage,
        parts_meta: list[PartMeta] | None = None,
    ) -> None:
        """Append a message and incrementally update derived state.

        This is the ONLY entry point for adding messages to the conversation.
        It protects the invariant: messages and state are always in sync.

        user messages MUST pass parts_meta (provides PDF page_count etc).
        assistant messages may omit it (auto-inferred from content[]).
        """
        if any(existing.message_id == msg.message_id for existing in self.messages):
            raise ValueError(f"Conversation 已存在 message_id: {msg.message_id}")
        self.messages.append(msg)

        state = self.state

        if msg.source_provider and msg.source_provider not in state.providers:
            state.providers.append(msg.source_provider)
        if msg.source_vendor and msg.source_vendor not in state.vendors:
            state.vendors.append(msg.source_vendor)

        metas: list[PartMeta]
        if parts_meta is not None:
            metas = parts_meta
        else:
            metas = [_infer_part_meta(p) for p in msg.content]

        for pm in metas:
            if pm.kind == MODALITY_TEXT:
                if MODALITY_TEXT not in state.modalities:
                    state.modalities.append(MODALITY_TEXT)
            elif pm.kind == MODALITY_IMAGE:
                if MODALITY_IMAGE not in state.modalities:
                    state.modalities.append(MODALITY_IMAGE)
                state.image_count += 1
            elif pm.kind == MODALITY_PDF:
                if MODALITY_PDF not in state.modalities:
                    state.modalities.append(MODALITY_PDF)
                if isinstance(pm, PdfPartMeta):
                    state.pdf_page_count += pm.page_count

    def add_user_message(
        self,
        parts: list[UniPart],
        parts_meta: list[PartMeta],
    ) -> None:
        """Append a user message (pure POJO, no source_provider/provider_extras)."""
        msg = UniMessage(role=ROLE_USER, content=parts)
        self.append_message(msg, parts_meta)


# ── ProviderAdapter ──────────────────────────────────────────────

@runtime_checkable
class ProviderAdapter(Protocol):
    """LLM 协议适配器接口。

    每个 LLM 协议实现此接口。所有支持的平台在代码层静态定义，
    模块加载时一次性注册到 _ADAPTERS 列表。
    接入新平台 = 实现此接口 + 加入 operations._ADAPTERS。

    本接口只负责：
    - 历史消息的归一化（响应 → UniMessage）
    - 历史消息的渲染（Conversation → 目标平台请求体）
    - 可选的会话状态更新和预检校验

    不关心模型名、温度、max_tokens、思考预算、reasoning effort 等参数，
    这些由调用方在拿到 render 返回值后自行合并。
    """

    @property
    def provider(self) -> str:
        """协议标识，即具体 api_format，如 "anthropic_messages"、"openai_responses"。"""
        ...

    def normalize_response(self, raw_response: Any) -> UniMessage:
        """归一化：将本平台原始响应 → UniMessage（公共语义层 + providerExtras.raw）。

        实现方负责：
        1. 从响应中提取 content[]（text / thinking / tool_use 等公共 part）
        2. 将响应原始内容数组存入 provider_extras[self.provider]["raw"]
        3. 设置 role="assistant", source_provider=self.provider
        """
        ...

    async def render(
        self,
        conv: Conversation,
        resolver: Any,
        options: dict[str, Any] | None = None,
    ) -> Any:
        """渲染：将 Conversation.messages[] → 本平台请求体（仅消息相关字段）。

        实现方负责：
        1. user 消息：通过 resolver 将 resource_id 转为本平台所需格式（URL / base64）
        2. assistant 消息：source_provider === self.provider 时走 raw 回灌，否则走公共层降级
        3. 读取 conv.provider_state[self.provider] 获取本平台会话态
        4. 返回值不含 model/temperature 等，由调用方合并
        """
        ...

    def update_state(
        self,
        conv: Conversation,
        raw_response: Any,
        options: dict[str, Any] | None = None,
    ) -> None:
        """可选：API 成功后更新 provider_state[self.provider]。

        例：OpenAI 记录 prompt_cache_key，Claude 记录 cache_breakpoint_indexes。
        默认不维护任何会话态。
        """
        ...

    def preflight(self, conv: Conversation) -> None:
        """可选：预检校验（渲染前调用）。

        例：Claude 校验图片 ≤100 / PDF 页数 ≤100。
        默认跳过预检。
        """
        ...
