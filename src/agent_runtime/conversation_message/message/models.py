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
from typing import Any, Protocol, TypeAlias, runtime_checkable

# ── 基础类型 ─────────────────────────────────────────────────────

# 协议标识（开放字符串，可注册第三方）。
# 内置三家: "claude" | "gemini" | "openai"
# 第三方示例: "deepseek" | "qwen" | "glm" | ...
Provider: TypeAlias = str

# 服务商标识（元数据，本层不依据它做任何逻辑）。
Vendor: TypeAlias = str

# 用户输入模态。当前仅支持 text / image / pdf。
Modality: TypeAlias = str

# 消息角色。
Role: TypeAlias = str

# 内置 Provider 常量，方便调用方引用。
PROVIDER_CLAUDE: str = "claude"
PROVIDER_GEMINI: str = "gemini"
PROVIDER_OPENAI: str = "openai"

# 角色常量。
ROLE_USER: str = "user"
ROLE_ASSISTANT: str = "assistant"
ROLE_TOOL: str = "tool"

# 模态常量。
MODALITY_TEXT: str = "text"
MODALITY_IMAGE: str = "image"
MODALITY_PDF: str = "pdf"


# ── UniPart：消息内容块 ──────────────────────────────────────────

@dataclass(slots=True)
class TextPart:
    """文本内容块。"""

    # 文本内容。
    text: str

    # 内容块类型标识，固定为 "text"。
    kind: str = field(default="text", init=False)


@dataclass(slots=True)
class ImagePart:
    """图片内容块。

    仅用于 user / tool 消息。
    通过 resourceId 引用统一文件资源服务中的图片资源，
    渲染时由 ResourceResolver 按需转为 base64 或 URL。
    """

    # 统一文件资源标识，指向 FileResourceArchive.resource_id。
    resource_id: str

    # 内容块类型标识，固定为 "image"。
    kind: str = field(default="image", init=False)


@dataclass(slots=True)
class PdfPart:
    """PDF 内容块。

    仅用于 user / tool 消息。
    通过 resourceId 引用统一文件资源服务中的 PDF 资源。
    """

    # 统一文件资源标识，指向 FileResourceArchive.resource_id。
    resource_id: str

    # 内容块类型标识，固定为 "pdf"。
    kind: str = field(default="pdf", init=False)


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

    # 内容块类型标识，固定为 "tool_use"。
    kind: str = field(default="tool_use", init=False)


@dataclass(slots=True)
class ToolResultPart:
    """工具执行结果内容块（预留，当前不实现）。

    tool 消息是纯通用 POJO，不存 raw。
    """

    # 关联 assistant 消息里 tool_use 的标识。
    tool_use_ref: str

    # 工具执行结果（字符串或结构化对象）。
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
    # key = source_provider 值（如 "claude" / "deepseek"）。
    # value = {"raw": [...]} 原始响应内容数组。
    # 同平台渲染时：直接回灌 raw（无损）。
    # 跨平台渲染时：忽略 raw，仅用 content[] 公共层重建（降级）。
    provider_extras: dict[str, dict[str, Any]] | None = None


# ── PartMeta ─────────────────────────────────────────────────────


@dataclass(slots=True)
class TextPartMeta:
    kind: str = "text"


@dataclass(slots=True)
class ImagePartMeta:
    kind: str = "image"


@dataclass(slots=True)
class PdfPartMeta:
    page_count: int
    kind: str = "pdf"


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
    if kind == "text":
        return TextPartMeta()
    if kind == "image":
        return ImagePartMeta()
    if kind == "pdf":
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


@dataclass(slots=True)
class Conversation:
    """统一 LLM 会话模型。

    该模型是消息领域的聚合根，维护一个 append-only 的历史消息列表、
    各平台的会话运行态以及由消息列表派生的统计状态。

    - messages：历史消息列表，是会话的唯一真相源。
    - provider_state：各平台的会话运行态，render 时读取，API 成功后写回。
      key = provider 标识（如 "claude" / "openai" / "deepseek"），
      value 由各 ProviderAdapter 自定义。
    - state：派生状态，由 messages 驱动。
    """

    # 会话唯一标识。
    id: str

    # 历史消息列表（append-only，入库后 providerExtras.*.raw 冻结）。
    messages: list[UniMessage] = field(default_factory=list)

    # 派生状态。messages 是真相源，state 由状态维护模块同步。
    state: ConversationState = field(default_factory=ConversationState)

    # 各平台的会话运行态，render 时读取，API 成功后写回。
    # key = provider 标识（如 "claude" / "openai" / "deepseek"），
    # value 由各 ProviderAdapter 自定义。
    #
    # 内置示例：
    #   claude:   {"cache_breakpoint_indexes": [int, ...]}
    #   openai:   {"last_response_id": str, "last_message_index": int, "prompt_cache_key": str}
    #   gemini:   {"cached_content": str}
    provider_state: dict[str, Any] = field(default_factory=dict)

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
        """协议标识，如 "claude"、"deepseek"。"""
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

        例：OpenAI 记录 last_response_id，Claude 记录 cache_breakpoint_indexes。
        默认不维护任何会话态。
        """
        ...

    def preflight(self, conv: Conversation) -> None:
        """可选：预检校验（渲染前调用）。

        例：Claude 校验图片 ≤100 / PDF 页数 ≤100。
        默认跳过预检。
        """
        ...
