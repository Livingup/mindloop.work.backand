# 统一会话设计：消息模型、多协议互转与缓存断点

本文定义 AgentRuntime 新统一会话的目标设计：同一段对话可以反复切换模型/协议，Conversation 必须保存完整的对话事实，并独立生成各目标协议的合法请求。Thinking 原样回放和缓存复用是这一设计中的两项不同职责，不能彼此替代。

**文档状态：统一会话目标设计与当前实现说明。**本文区分源码可核对的 Runtime 行为、尚待实现或验证的目标约束，以及外部协议指南的能力与示例。源码与请求结构一致，不等于真实 API 已接受或缓存已命中；外部示例也不是 Runtime 的默认请求。

当前 OpenAI Responses 路径：Agent 创建/冻结 P/T（提示词/工具）配置事件并记录到 `Conversation.timeline`；Conversation 保存事件、当前配置和 `instructions_snapshot`，仓储负责持久化/恢复；请求层构造 `RequestConfigurationView`。对于工具定义，严格区分客户端函数工具（`type == "function"`）与模型内置托管工具（Hosted Tools，如 `web_search`、`file_search`、`computer` 等）：客户端函数工具由 adapter 按锚点投影为 input 中的 `additional_tools`，享受缓存断点；模型内置托管工具必须在最顶层 `tools: [...]` 中声明，不作为 `additional_tools` 塞入 input。公共基座快照放顶层 `instructions`，业务差异配置放 input；默认不发送 `prompt_cache_options`，不注入 `prompt_cache_breakpoint`。其他协议及独立 `codex_responses` 的既有设计边界不因本文更正而扩大。

已核对实现包括：稳定 `message_id` 及 JSONL 兼容迁移、会话基座快照、配置时间线的生产/继承/持久化、Responses 配置视图和 input 投影、顶层托管工具（web_search 等）提取下发与客户端自定义函数工具严格走 input 投影、显式缓存参数处理、客户端允许顶层 `instructions`。Note 更新与子 Agent 路径的会话/缓存边界见第七、八章。

尚不能由此次源码核对证明：`call_ref`/`source_context`/通用 `metadata` 的目标闭环、所有调用方及跨协议回归、真实模型/compatible endpoint 能力与缓存效果。本次文档修订未运行程序测试或真实 API 请求；第三章沿用既有协议资料与用户确认示例，不重新认证其中的模型、TTL、价格或服务商支持范围。

阅读路径：

- 零、一章：系统职责边界与转换不变量。
- 二至六章：各协议的响应归一化、历史回放、推理状态和缓存规则；OpenAI 自定义断点用途与切回场景见第三章。
- 七章：统一会话/消息模型、上游配置输入契约、请求转换算法、持久化与压缩。
- 八、九章：实现差距、后续验收场景和证据来源。

当前 adapter 注册表包含以下 `api_format`：

- `anthropic_messages`
- `openai_responses`
- `openai_chat_completions`
- `gemini_generate_content`
- `zhipu_chat_completions`
- `codex_responses`（独立能力边界，不能自动继承 OpenAI Responses 的新增字段）

## 零、职责边界与总体闭环

### 1. 谁管理什么

| 对象/行为 | 权威责任方 | Conversation 的职责 |
| --- | --- | --- |
| 提示词/tools 定义、业务实例切换、同实例热更新 | 上游 Agent 系统及定义/工具模块 | 接收已经解析好的配置输入，不自行选择定义或推断切换 |
| P/T 各自的生效事件、位置锚点、历史冻结快照 | Agent 决定创建与冻结；Conversation 及仓储保存与恢复 | 用 `timeline` 承载事件；请求视图和 adapter 校验、消费，不从消息猜测切换或重新读取历史定义 |
| 公共基座快照与当前业务配置 | Agent 渲染，Conversation 及仓储保存 | 保存 `instructions_snapshot`、`system_prompt`、`tools`；请求期将快照与差异配置分开表达 |
| user/assistant/tool 历史与 assistant 原始响应 | Conversation | 保存公共语义和同源 raw，独立投影目标协议 |
| 目标协议请求字段、历史结构合法性 | Conversation adapter 与 LLM 请求组装层 | adapter 负责消息/配置投影；组装层负责最终参数合并与完整请求校验 |
| 模型、服务商、端点、凭据、重试及传输 | 上游模型解析与 LLM client | adapter 消费已解析能力，不选择模型、不发送网络请求 |
| 本次允许调用的工具与执行授权 | 上游当前工具视图及执行端 | 请求表达与有效集合一致；历史工具事实不授予权限 |
| 缓存布局、协议私有缓存句柄/usage | 目标协议策略与运行态 | 请求期加标、按目标作用域维护观测；不把缓存写成对话事实 |

**配置生命周期的业务决策由 Agent 管理，配置时间线的数据载体是 `Conversation.timeline`。**仓储把时间线、当前配置与基座快照写入 `conversation_configuration`；请求期的 `RequestConfigurationView` 不是持久字段。不要把“adapter 不决定切换”误写成“Conversation 不保存时间线”。

### 2. 一次调用的闭环

以下为目标闭环；源位置映射、完整兼容性诊断等不因列入流程就代表已经全部实现。当前 Responses 默认跳过显式缓存加标，实际接线范围见第八章。

```text
上游选择本次模型/协议，解析当前提示词和 tools
        │ 提供只读配置视图（历史快照 + 各自当前活动内容 + 锚点）
        ▼
Conversation 历史 + 配置视图 + 目标能力 + 资源解析器
        ▼
校验消息/工具关联 → 配置投影 → 历史渲染 → 请求期缓存标记
        ▼
协议专属渲染结果 + 源位置映射 + 兼容性诊断
        ▼
LLM client 组装最终请求并校验 → 发送
        ▼
目标协议响应 → 公共语义 + 该响应来源的 raw → 追加同一个 Conversation
        └─ 记录真实请求作用域/usage；不反推未被证明的缓存写入或命中
```

协议切换只是本次 `target` 改变，不创建另一套权威历史、不覆盖旧 raw，也不自动产生提示词/tools 切换事件。OpenAI → Claude → Gemini → OpenAI → 智谱的每一次渲染，均从同一份规范历史独立出发。

## 一、统一转换总原则

### 1. assistant 公共语义不能丢

所有接入模型都支持普通文本回复和函数调用，因此任意来源协议的 assistant 历史消息转换到目标协议时，以下公共语义必须尽最大能力保留：

- **文本**：assistant 可见文本必须转换为目标协议的 assistant/model 文本内容。
- **工具调用**：assistant tool use / function call 必须转换为目标协议的工具调用结构，并保持工具调用 ID 或可映射引用，保证后续 tool result 能关联到正确调用。
- **顺序**：同一 assistant 消息内 text 与 tool use 的相对顺序应尽量保留；目标协议无法表达混排时，应按该协议官方消息结构做最小损失重建。

### 2. thinking/推理内容按目标协议规则处理

thinking 不属于统一公共语义的稳定全集。不同平台返回的 thinking 形态、能否回传、是否必须回传、是否只对同协议有效完全不同。

统一规则：

- **同源且目标兼容**：如果目标接口要求或支持回传该来源的原始 thinking 状态，应从冻结 raw 的副本原样回灌，不重建、翻译或伪造受保护块；必须遵守目标关于历史轮次和当前 thinking 模式的规则。
- **相同 api_format 不等于私有状态通用**：更换服务商、模型家族或账号作用域时，签名/加密项未必可接受；以已确认的目标兼容性为准。缺少兼容依据时采用允许的公共语义降级或明确报错，不把别家返回的加密项盲目透传。
- **跨协议转换**：不能把一个平台的私有 thinking 凭证伪造成另一个平台的 thinking 凭证，例如不能把 Claude `signature` 伪造成 Gemini `thoughtSignature`，不能把 OpenAI `encrypted_content` 伪造成 Claude `thinking`。
- **明文 thinking**：只有目标协议允许把历史明文推理作为普通文本或明文 reasoning 字段回传时，才可转换；否则必须舍去。
- **加密/签名 thinking**：签名、加密推理、thought signature 等只对原平台有效，跨协议必须舍去，只可保存在源 assistant 的 `provider_extras.raw` 供同协议回灌。

### 3. 缓存按目标协议隔离

缓存不是统一消息语义，不能放进公共 content 层。缓存是目标协议请求构造策略：

- **自动缓存平台**：客户端只需要保持稳定前缀，读取 usage 中的 cached tokens 指标。
- **显式缓存平台**：客户端按目标协议要求创建/引用缓存，或使用目标端点支持的断点字段，例如 Claude 的 `cache_control`、支持自定义断点的 Responses 端点的 `prompt_cache_breakpoint`；各协议字段不可混用。
- **状态锚点**：显式缓存需要记录目标协议自己的缓存状态，不能用另一个协议的缓存状态，也不能依赖不稳定的渲染后 messages 下标。

## 二、Anthropic Messages / Claude

### 1. assistant 返回形态

Claude Messages API 的 assistant response `content[]` 可以包含：

- `text` block：可见文本。
- `tool_use` block：工具调用，包含 `id`、`name`、`input`。
- `thinking` block：扩展思考内容，包含 `thinking` 明文和 `signature`。
- `redacted_thinking` block：不可见/脱敏思考内容，包含 `data`。

本地资料 `doc/claude_api.md` 中可见 `ThinkingBlockParam = object { signature, thinking, type }`、`RedactedThinkingBlockParam = object { data, type }`、`ToolUseBlockParam`、`ToolResultBlockParam` 和多处 `cache_control: optional CacheControlEphemeral`。

### 2. 多轮 thinking 透传规则

Claude extended thinking 的关键不是“把所有历史 thinking 文本当普通文本塞回去”，而是同协议消息中对官方返回的 thinking block 做原样保留。

规则：

- **同协议 Claude assistant**：必须保留完整原始 `content[]`，包括 `thinking`、`redacted_thinking`、`tool_use`、`text` 等 block。
- **带工具调用场景**：当 assistant 产生 `tool_use` 并返回 thinking block，后续提交 tool_result 时，应把该 assistant 消息中和工具调用相关的 thinking block 原样放回请求历史，维持 reasoning continuity。
- **不要修改 thinking block**：`thinking` 明文、`signature`、`redacted_thinking.data` 都是协议状态，不能截断、合并、翻译、重排或重建。
- **跨协议到 Claude**：其它平台 assistant 的普通文本和工具调用可转换为 Claude `text` / `tool_use`；其它平台 thinking 不可伪造成 Claude `thinking`，默认舍去。只有在显式策略允许时，明文 thinking 可降级为普通 `text`，但不能标记为 `thinking`。

### 3. 工具结果规则

Claude 的工具结果不是独立 `tool` role，而是放在 `role="user"` 消息的 `content[]` 中，block 类型为 `tool_result`，通过 `tool_use_id` 关联前序 assistant `tool_use.id`。

客户端应保持消息边界，不为了猜测服务端内部处理而主动合并连续 user 消息。合法客户端序列可以包含：

```text
assistant -> user(tool_result) -> user(普通用户输入) -> user(普通用户输入)
```

### 4. 缓存规则

Claude prompt caching 是显式缓存断点机制，客户端需要参与。

规则：

- 缓存通过 `cache_control: {"type":"ephemeral"}` 标记在可缓存 block 上。
- 支持可选 TTL，例如默认短 TTL 和更长 TTL；具体 TTL 取值按官方当前文档。
- 一次请求最多只能有有限数量断点，当前 Claude 官方规则是最多 4 个 `cache_control` 断点。
- Claude 会围绕断点做前缀匹配；官方文档说明在增长对话中，如果每轮新增 block 少于约 20 个，末尾断点通常可通过 lookback 找到前一次缓存。
- `system` 和 `tools` 是稳定静态前缀，可在合法位置设置静态断点；`messages` 通常在最新可缓存内容附近设置动态断点。
- **system、tools、messages 合计**共用目标服务的断点预算，不能各自都按 4 个规划。
- 约 20 个 content blocks 的查找规则属于 Claude 的协议约束，不套用到 OpenAI；超过目标查找范围时，可在实际已提交的稳定前缀边界补充断点，仍须满足总预算。

实现含义：

- `cache_control` 是请求时动态字段，不写入 `provider_extras.raw`。
- 不能只保存渲染后 `messages[]` 下标作为长期缓存状态；多协议穿插、首条占位、消息边界策略变化都会让下标失效。
- 若需要保存长期锚点，记录规范消息/内容块的稳定位置，在本次渲染时映射到 Claude 请求内容块。
- 最近 Claude assistant 的来源位置只能帮助定位历史，不能证明其已作为请求输入提交或被缓存；缓存观测应关联实际发送的请求边界与 usage。

## 三、OpenAI Responses

### 1. assistant 返回形态

OpenAI Responses API 的 response `output[]` 可以包含：

- `reasoning` item：推理模型的 reasoning 项，可能包含 `summary`、`encrypted_content` 等。
- `message` item：assistant 可见文本输出。
- `function_call` item：工具调用。
- `function_call_output` item：工具结果在后续请求中的输入项。

本地资料 `doc/openai_resposne_api.md` 明确：`reasoning.encrypted_content` 包含 reasoning tokens 的加密版本，使 stateless Responses API 多轮对话可使用 reasoning items；`Reasoning` item 有 `type: "reasoning"`、`encrypted_content`；请求参数包含 `previous_response_id`、`prompt_cache_key`、`reasoning`、`store`。

### 2. 多轮 reasoning 透传规则

Responses API 有两种上下文模式：

- **stateful**：使用 `store: true` 和 `previous_response_id`，服务端保存上一轮状态。
- **stateless**：使用 `store: false` 或零数据保留场景，客户端必须在后续请求中保留并回传必要 output items。

AgentRuntime 当前设计采用 stateless 路径，因此规则是：

- 请求必须设置 `store=false`。
- 请求应 include `reasoning.encrypted_content`，使响应 reasoning item 带上可回传的加密推理内容。
- 后续同协议请求必须把历史 response `output[]` 中的 reasoning item、message item、function_call item 等 raw items 原样回灌到 input。
- 工具结果使用顶层 `function_call_output` item，通过 `call_id` 关联历史 `function_call.call_id`。
- OpenAI 的 `encrypted_content` 是 OpenAI Responses 协议私有状态，跨协议不得解密、不得转写成其它平台 thinking。

跨协议转换规则：

- 其它协议到 OpenAI Responses：普通文本转 assistant message item；工具调用转 `function_call` item；其它平台 thinking 默认舍去，除非显式策略允许把明文 thinking 作为普通 `output_text`。
- OpenAI Responses 到其它协议：`message.output_text` 和 `function_call` 必须保留；`reasoning.encrypted_content` 只能供 OpenAI Responses 同协议回灌，跨协议舍去；`summary` 若被产品策略允许，可作为普通文本降级，但不能伪造成目标平台 thinking。

### 3. 缓存规则与配置输入契约（Runtime 实现与协议能力分开）

**当前 Runtime 的请求构造策略，不是所有 Responses 端点的通用强制要求：**

- **固定基座与差异配置分离**：会话首次渲染时生成 `instructions_snapshot`，冻结公共模板及当时的变量值；后续请求把其 content 放顶层 `instructions`。业务差异提示词排除已收录基座，投影为 input 中的 `developer`。工具严格按类别分别处理：
  - **客户端自定义函数工具（Client Function Tools，`type == "function"`）**：投影为 input 中的 `additional_tools`，参与会话时间线配置演化与 Prompt Caching 断点；禁止直接绕过视图发送顶层自定义函数 tools。
  - **模型内置/托管工具（Hosted Tools，如 `web_search`、`file_search`、`computer` 等）**：必须在请求体的最顶层 `tools: [...]` 中声明，禁止投影为 input 中的 `additional_tools`。OpenAI Responses API 规范中，服务端对 `tool_choice`（如 `{"type": "web_search"}`）的合法性校验以及模型自身的原生联网能力检测，均严格依赖顶层 `tools` 参数；若错误放置在 `input.additional_tools` 中，会导致服务端直接拒绝请求（报错 `Tool choice 'web_search_preview' not found in 'tools' parameter`）或模型判定为“缺乏原生联网搜索工具”从而跳过搜索执行。
- **覆盖入口**：请求层允许显式 `instructions` 覆盖快照在本次请求中的值，但不改写快照；最终 instructions 未提供、为 None 或空白时，客户端使用 `你是一个善于学习的AI。`。`request_params.tools` 覆盖若包含客户端函数工具则规范化为 input 配置，若包含模型内置托管工具（如 `web_search`）则由请求客户端提取保留在顶层 `tools` 中发送；显式 `configuration_view` 不能与 instructions/tools 覆盖同时使用。
- **时间线不以切换为存在前提**：初始会话即可有 start 工具/提示词事件；存在时间线时从历史快照与本次 active 值构造视图，无时间线但有当前业务配置时构造初始视图。两条路径都会投影业务 prompt/tools，而不是只有发生切换才投影。
- **默认不声明缓存扩展参数**：`prompt_cache_options` 与 `prompt_cache_breakpoint` 默认均为 None，因此不发 options、不注入 marker，也不由 Runtime 默认指定 mode 或 TTL。省略字段后服务端实际采用的策略由目标端点决定，不能将其等同于客户端发送了 `mode="implicit"`。
- **显式可选能力**：提供字典类型 options 时按请求参数合并/透传；提供 marker 字典时，只在 start 配置最后一个合法 developer `input_text` 内容块上重建标记。如果该文本后还有 `additional_tools`，或找不到合法文本块，就不放 marker；不会自动给全部历史、新阶段或分支尾部打点。

以下请求级控制、断点语义及第 3.1～5 节示例，沿用已确认的协议资料，**仅适用于实际支持相应能力的模型/服务商端点，不是 Runtime 默认布局或全平台支持声明**。不能从某次端点拒绝推导所有服务商都不支持，也不能凭模型名称推导一定支持。OpenAI Chat Completions 须独立核实，不能继承 Responses 的 marker、`additional_tools` 或其他 input item。显式断点是请求内容块标记，不是可单独引用的缓存 ID 或可拼装的缓存切片。

请求级控制（下述默认值指所引用协议资料中的服务端规则，不是 Runtime 下发值）：

- `prompt_cache_options.mode = "implicit"`（所引用指南的默认模式）：保留服务端自动选择的 implicit breakpoint，同时可以使用开发者声明的显式断点。该指南描述的 GPT-5.6+ 自动写入边界位于最新符合条件的 user/tool 消息末尾，不等于为所有内部前缀都建立独立缓存。
- `prompt_cache_options.mode = "explicit"`：关闭自动断点，只使用请求中声明的显式断点；没有显式断点时不使用 prompt caching，也不产生缓存写入。自定义断点不要求启用此模式：只有想关闭自动断点时才需要设为 `explicit`。
- `prompt_cache_options.ttl = "30m"`：所引用 GPT-5.6+ 指南描述的默认且唯一支持值；写入或命中复用后刷新有效期。具体目标仍须核验，Runtime 不默认下发 TTL；不要与早期模型的 `prompt_cache_retention` 的 `in_memory`/`24h` 混用。
- 每个请求最多 4 个缓存写入槽位；`explicit` 模式可使用最多 4 个显式断点，`implicit` 模式的自动断点占 1 个槽位，最多剩 3 个显式写入槽位。
- 写入上限与读取窗口不同：读取寻找最长匹配的可用前缀。官方指南写最近 50 个断点，Responses API Reference 写最近 80 个；这是资料表述差异，不表示每请求可以写入 50 或 80 个断点，本文也不依赖其精确临界值。
- GPT-5.6+ 的可缓存前缀需达到 1,024 个可见输入 tokens；短 JSON 示例仅展示结构，不能据此保证缓存写入或命中。

内容块级断点：

- 在请求 `input` 的支持内容块上添加 `"prompt_cache_breakpoint": {"mode": "explicit"}`。用户确认指南列出 `input_text`、`input_image`、`input_file`，并给出 assistant 历史 `output_text` 内容块上的断点示例；已读取的官方工具多轮示例还展示了 `function_call_output.output[]` 内 `input_text` 的标记方式。不要把内容块上的 marker 提升到外层 `function_call`、`function_call_output` 或工具定义对象。
- 顶层 `instructions` 不能直接承载显式断点；不要把 OpenAI 字段写成 Claude 的 `cache_control`。
- 断点表示从请求起点累计到该内容块结束的完整可复用前缀。断点之后的内容可以变化，但断点之前的模型、工具定义及其顺序、系统/开发者前缀、输出格式、reasoning 设置、verbosity 和历史 raw items 等相关渲染结果必须保持一致，才有机会命中。
- Responses 使用 `usage.input_tokens_details.cached_tokens` 观察读取，使用支持模型返回的 `usage.input_tokens_details.cache_write_tokens` 观察写入；不要误用 Chat Completions 的 `usage.prompt_tokens_details.cached_tokens` 路径。
- GPT-5.6+ 指南中的读取与写入费率分别约为普通输入的 0.1 倍与 1.25 倍，写入费率替代普通输入费率而非额外叠加；具体以目标模型现行价格为准。`explicit` 模式最后断点后的新增内容按普通未缓存输入处理，不产生该部分的缓存写入费。
- `cached_tokens > 0` 只证明有部分前缀命中，不证明整个指定断点都命中；`cached_tokens = 0` 也可能由首次请求、前缀太短、TTL 或缓存可用性等原因造成，不能直接断定历史内容被改动。

路由键与 AgentRuntime 策略：

- 当前 `prompt_cache_key` 优先级为：render options 显式非空值 > 会话 OpenAI provider_state 已记录值 > `conversation.id`。它只是路由/分组提示，不是缓存句柄、回滚句柄、固定节点或 100% 命中保证。
- Runtime 每轮发送完整 input，强制 `store=false`、include `reasoning.encrypted_content`，去除 `previous_response_id`；不是依靠 response ID 续接。仅显式选择 marker 时在请求副本重建合法标记，默认不加。**读取旧自动缓存并不要求把旧自动边界重标为显式边界。**
- 统一 token usage 当前读取 `input_tokens_details.cached_tokens`（缺失为 0），没有在该分支统计 `cache_write_tokens`。协议示例的读/写 usage 不能视为 Runtime 已完成这两项观测。
- 缓存字段属于目标协议请求构造策略，不写入统一消息公共 content，也不应污染持久化的 provider raw。

#### 3.1 自定义断点到底有什么用

断点把“从请求起点到某个内容块结束”的累计前缀声明为缓存复用边界。它的核心用途是主动指定一个值得保留、可以单独缓存并在后续请求复用的稳定累计前缀，不是单纯告诉服务端“上次聊到这里”。它既不是普通可见文本，也不是客户端拿到的 cache ID，更不是能从任意长缓存中独立拼接的片段。

- **自动断点**：为当前最新合格输入边界建立缓存，适合历史不断向后追加。
- **自定义断点**：主动保留完整输入中的其他稳定累计前缀，适合配置复用、分支公共历史，以及后缀经常更换的场景。
- **纯 explicit 模式**：只选择值得写入的前缀，不为断点之后的一次性后缀支付缓存写入成本；本节用于对比的可选 `implicit + 配置尾显式 B` 则保留历史自动增长写入，二者均不是 Runtime 默认下发配置。

假设配置为 8,000 tokens，而每次用户问题不同：

```text
只有 implicit：
R1 = 配置 + 问题 A【自动】  → 可写入“配置 + A”
R2 = 配置 + 问题 B【自动】  → 整段不再相同；不能从“配置 + A”的缓存
                              推断内部“配置”已有单独可复用的缓存边界

implicit + 配置尾显式 B：
R1 = 配置【B】+ 问题 A【自动】 → 可分别写入“配置”与“配置 + A”的累计边界
R2 = 配置【B】+ 问题 B【自动】 → 问题变化时仍可复用“配置”前缀
```

不要把上例中的“替换后缀”与“保留完整旧输入后追加”混为一谈：前者需要内部稳定边界，后者可直接利用先前完整输入的自动边界。

#### 3.2 读取旧自动边界，不要求补显式 marker

官方 Prompt Caching 的 `Request parameters and response usage` 给出成对请求与 usage：

| 请求 | 输入 | 标记 | 文档示例 usage |
| --- | --- | --- | --- |
| R1 | tools 2,000 + developer 8,000 + user 2,000 tokens | implicit，无显式 marker | cached_tokens=0；cache_write_tokens=12,000 |
| R2 | 原完整 12,000 + 新 user 3,000 tokens | implicit；旧 user 末尾未补 marker | cached_tokens=12,000；cache_write_tokens=3,000 |

这个例子直接表明：**新的自动写入边界后移，不要求客户端把旧自动边界显式重标，才可以读取旧的完整前缀缓存。**这里的数字是官方示例，不是本项目实测。

读取仍是寻找可用的最长匹配缓存前缀，而不是“字面相同就一定命中所有 token”。TTL、目标作用域、最低长度、读取窗口、路由与服务端可用性仍成立。读取窗口与每请求写入槽位是不同限制，不据此推导无限回溯。

#### 3.3 两轮 OpenAI → 三轮 Claude → 切回 OpenAI

协议示例前提：相同且已确认支持这些字段的 GPT-5.6 OpenAI Responses 路径；期间提示词/tools 不变；调用方显式选择了配置尾 B + `mode="implicit"`。这不是 Runtime 默认请求。P/T 有变更时见第七章，不为缓存恢复过期配置。

```text
OpenAI R1 输入：配置【B】+ U1                 → 输出 O1
OpenAI R2 输入：配置【B】+ U1 + O1 + U2       → 输出 O2
                └──── 上次完整输入 A ────┘

切到 Claude：在同一规范历史追加 U3/C3、U4/C4、U5/C5
             Claude 请求使用自己的 system/tools/messages，不带 OpenAI 标记

切回 OpenAI R6 输入：
配置【B】+ U1 + O1(raw) + U2 + O2(raw) + U3 + C3(公共语义)
         + U4 + C4(公共语义) + U5 + C5(公共语义) + U6
```

在上述显式选择前提下，切回步骤如下；Runtime 未显式配置时仍不发送 options/marker，不因协议切回自动启用它们：

1. 从规范历史重建 OpenAI input；O1/O2 回放原先保存且目标可接受的 OpenAI raw，C3/C4/C5 从公共文本/工具语义转换。不能把旧 OpenAI 历史先转 Claude 再转回来。
2. 保持配置、工具定义顺序、相关模型/请求设置及原配置尾 B 的确定性渲染；继续使用原缓存路由 key 和 `mode="implicit"`。
3. **不强制在 U2 补显式断点，也不强制寻找最近 OpenAI assistant 打点。**完整的旧输入 A 保持不变，新内容只追加在后，服务端可尝试复用旧自动前缀。
4. 本例此前提交给 OpenAI 的最长完整输入到 U2；O2 是输出，直到 R6 才首次成为历史 input，不能因为已生成就声称旧缓存覆盖 O2。
5. 中间 Claude 请求不会刷新 OpenAI 缓存 TTL，也不会把 Claude 的缓存带回 OpenAI。O2、Claude 历史及 U6 作为新输入处理，再由本次自动机制建立更长前缀的缓存。
6. 看 R6 的 `usage.input_tokens_details.cached_tokens` / `cache_write_tokens` 观察结果；本地可验证前缀一致性，但不能把它当成命中保证。

额外历史显式断点仅用于主动选择其他值得保留的稳定边界，按槽位预算设置；它不是协议切回的必需动作，也不天然提高本次命中率。

### 4. 自定义断点使用案例：多轮分支探索与回退

以下案例只用于说明支持这些能力的 OpenAI Responses 端点的自定义断点匹配语义，不代表 AgentRuntime 已实现或必须实现分支状态机、回退功能或任意历史内容块加标。当前 Runtime 的显式 marker 入口仅处理 start 配置中的合法提示词块。假设 Agent A 先用 Prompt 1 与 Tools 1 对话 10 轮；随后以 Prompt 2 与 Tools 2 探索方案一，再对话 5 轮；发现不可行后回到第 10 轮，恢复 Prompt 1 与 Tools 1 并执行方案二直到完成。

第 10 轮回复收到后，在下一次请求的 `input` 中重放这段历史，并在末尾 assistant 的 `output_text` 内容块上添加断点 B。以下是历史消息的内容结构片段，不是完整请求，也不是要求模型在第 10 轮输出中生成 marker；真实回传需保留原始 message item 的其他必要字段及 reasoning/function_call items，只在请求副本上补充标记：

```json
{
  "role": "assistant",
  "content": [
    {
      "type": "output_text",
      "text": "前 10 轮分析完成，总结状态如下……",
      "prompt_cache_breakpoint": { "mode": "explicit" }
    }
  ]
}
```

记 `P10` 为 `Tools 1 + Prompt 1 + 第 1～10 轮完整历史`，其中已经包含 B 所在的最后一个内容块。B 标记的是整个 `P10` 的结束边界，不是额外追加一份第 10 轮内容，也不是只缓存第 10 轮。首次把带 B 的历史发给 API 时，若没有匹配的已有缓存，需要先计算并写入；仅收到第 10 轮回复或在本地打标，不等于服务端已经写好了该断点。后续请求重建相同前缀并重新携带 B，才能尝试复用。

```text
公共请求前缀：Tools 1 + Prompt 1 + 第 1～10 轮完整历史【B】
探索方案一：  公共前缀【B】 + additional_tools(Tools 2) + developer(Prompt 2) + 方案一输入/后续历史
探索到第15轮：公共前缀【B】 + Tools 2/Prompt 2 + 方案一5轮历史【可选分支尾断点】
回退走方案二：公共前缀【B】 + developer(方案二阶段指令) + 方案二输入/后续历史
```

各请求保持同一个模型及相关生成设置、稳定的 `prompt_cache_key`，例如 `branch-session-001`，并使用 `prompt_cache_options: {"mode":"explicit","ttl":"30m"}`。

分支一：在 B 之后追加差异：

- 不替换 B 之前的 Prompt 1 或顶层 Tools 1。
- 在 B 之后追加阶段性 developer 指令，表达“进入方案一、遵循 Prompt 2 规则”。如果只是阶段规则变化，应以后置 developer 输入表达，而不是修改前面的系统/开发者前缀。
- 如果需要阶段专用工具，使用会话流中的 `additional_tools` 在 B 之后追加 Tools 2；这样前 10 轮公共前缀仍保持可匹配。
- 分支一后续请求继续重放 `P10 + B + 分支一历史`；需要缓存分支自身增长前缀时，可在当前分支末尾再设置一个显式断点，但不要为每一轮无限累积断点。

分支失败后的回退：

- 丢弃 B 之后的 `additional_tools`、阶段指令和方案一的全部历史，不要在方案一历史后追加“现在改走方案二”。
- 重新构造 `P10 + B`，再在 B 之后追加方案二 developer 指令和用户输入。因为 B 之前的累计前缀恢复一致，方案二才有机会复用第 10 轮缓存。
- 如果真正修改请求顶层 system prompt、顶层 tools、工具 schema/顺序或其他位于 B 之前的相关设置，则形成新的缓存谱系，不能用 B 强行跨越配置差异。

### 5. `additional_tools` 的含义与使用

`additional_tools` 是 Responses 会话历史 `input` 列表中的结构化工具注入条目，不是顶层 `tools` 的别名，也不是缓存断点本身。它用于在对话进行到某个位置后，按需追加阶段性工具定义。

与顶层 `tools` 的区别：

- 顶层 `tools` 属于请求的基础工具前缀；若在已有断点之前替换、增删或改变 schema/顺序，会改变该断点对应的累计前缀。
- `additional_tools` 跟随 `input` 时间线出现；把它放在已有断点之后，可把新工具作为后置增量引入，而不改变断点之前的公共前缀。
- 可以配合后续 developer 消息说明阶段任务和工具用途；这是使用建议，不是 `additional_tools` 的必填协议字段。
- “追加 Tools 2”不等于删除或替换 Tools 1；不能由此推断同名工具覆盖、工具撤权或安全隔离能力。阶段指令也不等于修改了原始 Prompt 1，更不能覆盖仍然生效的更高优先级指令。案例中的“切换”是阶段规则和增量工具变化，而非字面改写公共前缀。
- 本案例回退到 B 时，从下一次请求中同时移除 B 之后注入的 `additional_tools` 及其分支历史；它不会替你执行服务端回滚，也不会撤销已经执行的工具副作用。

下面是依据确认指南整理的完整请求结构示例。为便于阅读，公共前缀使用一段短 developer 文本；在上述案例中，它对应完整 `P10`，并将 B 放在其最后一个合法内容块上。此短示例不足以达到缓存 token 阈值，亦未进行真实 API 调用验证。

```json
{
  "model": "gpt-5.6",
  "store": false,
  "include": ["reasoning.encrypted_content"],
  "prompt_cache_key": "branch-session-001",
  "prompt_cache_options": {
    "mode": "explicit",
    "ttl": "30m"
  },
  "input": [
    {
      "role": "developer",
      "content": [
        {
          "type": "input_text",
          "text": "你是服务排错助手。这里放各阶段共享的稳定指令和资料。",
          "prompt_cache_breakpoint": { "mode": "explicit" }
        }
      ]
    },
    {
      "type": "additional_tools",
      "role": "developer",
      "tools": [
        {
          "type": "function",
          "name": "lookup_service",
          "description": "只读查询指定服务的状态",
          "strict": true,
          "parameters": {
            "type": "object",
            "properties": {
              "service_name": { "type": "string" }
            },
            "required": ["service_name"],
            "additionalProperties": false
          }
        }
      ]
    },
    {
      "role": "developer",
      "content": "进入状态核查阶段，使用新增的只读查询工具核实服务状态。"
    },
    {
      "role": "user",
      "content": "请查询 api 服务当前的状态。"
    }
  ]
}
```

为复用公共断点前缀，新增工具条目在本例放在 B 之后；这不表示所有 `additional_tools` 都必须跟在某个断点之后，也可以在会话初始位置定义基础工具。真实请求应选择支持这些字段的模型和服务端，并使用其接受的完整工具 schema。该示例不应直接套用到 Chat Completions 的 `messages`。

## 四、OpenAI Chat Completions

### 1. assistant 返回形态

OpenAI Chat Completions assistant message 通常包含：

- `content`：可见文本。
- `tool_calls`：函数调用数组，包含 `id`、`function.name`、`function.arguments`。
- 部分兼容模型可能返回 `reasoning_content` 或类似字段，但这不是 OpenAI Chat Completions 标准推理状态协议。

### 2. 多轮 reasoning 透传规则

标准 OpenAI Chat Completions 没有与 Responses `reasoning.encrypted_content` 等价的官方 stateless reasoning item 机制。

规则：

- 同协议 assistant 的 `content` 和 `tool_calls` 必须原样保留或等价重建。
- 如果某个 OpenAI-compatible 服务商在 Chat Completions message 上返回 `reasoning_content`，它应作为该服务商扩展字段处理，不应当成 OpenAI 官方通用规则。
- 跨协议到 Chat Completions 时，普通文本转 `content`，工具调用转 `tool_calls`。
- 其它平台私有 thinking 默认舍去；显式策略允许时只能作为普通文本拼入 `content`，不能伪造成标准推理状态。

### 3. 缓存规则

Chat Completions 的缓存能力必须按目标端点和模型独立核实。OpenAI Chat Completions 不接收 Responses 的 `prompt_cache_breakpoint` 内容块 marker 或 `additional_tools`；不能仅凭 Responses 的 GPT-5.6 示例，把 Responses input item/schema 套入 Chat `messages`。某个兼容服务商若提供自己的缓存字段，必须以该端点自身契约单独描述和验证。

规则：

- 除非目标 Chat 端点明确记录并接受同名字段，否则不得在 Chat 请求中注入 `prompt_cache_options`、`prompt_cache_breakpoint` 或 Responses input item；仅支持自动缓存时只保持稳定前缀并观察其自身 usage。
- 目标端点若有自己的缓存边界字段，使用其自身 schema；它仍表示累计前缀边界，不是独立缓存切片。不得使用 Claude 的 `cache_control`，也不得发送 Responses `additional_tools`。
- 客户端应保持稳定消息前缀、工具定义和顺序；缓存命中通过该端点 usage 中的 cached token 统计或服务商扩展字段观察。
- Chat Completions 没有与 Responses `reasoning.encrypted_content` 等价的官方 stateless reasoning item 机制；任何缓存边界字段都不能替代 reasoning 状态透传。

## 五、Gemini generateContent

### 1. assistant 返回形态

Gemini generateContent 中：

- 用户/工具结果/模型回复都在 `contents[]` 中表达。
- assistant 对应 Gemini `role="model"`。
- 文本为 `parts[].text`。
- 工具调用为 `parts[].functionCall`。
- 工具结果为 user role `parts[].functionResponse`。
- thinking 配置位于 `generationConfig.thinkingConfig`。
- 响应 parts 可能包含 `thoughtSignature`，用于保持 thinking model 的推理状态。

本地资料 `doc/gemini_generating_api.md` 记录：`cachedContent` 是请求字段；`cachedContentTokenCount` 是 usage 输出；`thinkingConfig` 包含 `thinking_budget`、`thinking_level`、`include_thoughts`；Python SDK 中 `systemInstruction`、`tools`、`cachedContent` 是顶层请求字段。

### 2. 多轮 thinking 透传规则

Gemini 的核心机制是 thought signatures，而不是把完整明文链路思考作为普通历史文本回传。

官方 Vertex/Gemini thought signatures 文档说明：

- thought signature 是模型内部推理过程的加密表示，用于在多轮、多步骤，尤其是函数调用时保存 reasoning state。
- 最简单可靠的处理方式是：把之前模型返回的所有 parts 按原样放回 conversation history。
- 如果手工处理 REST 或裁剪历史，必须保留 thought signatures。
- 当 response 包含 `functionCall` parts 时，`thought_signature` 对正确处理是必需的。
- 并行 function calls 时，通常只有第一个 functionCall part 带 thought signature；下一请求必须先包含所有 functionCall，再包含所有 functionResponse，错误交错会导致 400。
- 顺序多步骤 function calling 中，每一步 functionCall part 都可能带 signature；当前 turn 内缺失必需 signature 会导致 400。
- 非 function calling 场景，最后 text part 也可能带 thought signature；回传推荐但省略通常不报错，只会影响质量。

实现规则：

- 同协议 Gemini assistant/model parts 必须原样回灌，包括 `thoughtSignature`。
- 跨协议到 Gemini 时，可重建 `text` 和 `functionCall`，但不能伪造真实 thoughtSignature。
- 如果目标是 Gemini 3 且跨协议历史中存在工具调用但没有 Gemini thoughtSignature，只有在明确接受质量下降/兼容风险时才可使用官方 fallback 标记，例如 `skip_thought_signature_validator`；默认不应伪造。
- Gemini thinking 明文如果通过 `include_thoughts` 或 SDK 返回为可见/可解析 part，也只在同协议 raw 中原样保留；跨协议默认不转为 Gemini thought state。

### 3. 缓存规则

Gemini 同时有隐式缓存和显式 context caching。

官方 Gemini context caching 文档说明：

- Gemini 2.5 及更新模型默认启用 implicit caching；客户端无需开启。
- 提高 implicit cache 命中的方式是把大型、共同内容放在 prompt 前缀，并在短时间内发送相似前缀请求。
- cache 命中 token 数在 response `usage_metadata` 中可见。
- explicit caching 允许先创建 cached content，然后后续请求通过 `cachedContent` 引用。
- explicit cache 有 TTL，默认 1 小时。
- cached content 是 prompt 前缀；模型不区分 cached tokens 和普通输入 tokens。
- token limits 包含 cached tokens。

实现含义：

- 普通多轮对话可先依赖 implicit caching，保持稳定前缀。
- 对大型稳定系统提示词、代码库上下文、长文档/视频/PDF，可使用 explicit caching，创建 cache 后在请求中设置 `cachedContent`。
- `cachedContent` 属于 Gemini provider_state 或上层上下文缓存管理，不是统一消息 content。

## 六、Zhipu / Z.AI Chat Completions

### 1. assistant 返回形态

Z.AI Chat Completion 响应 assistant message 可包含：

- `content`：可见文本。
- `reasoning_content`：明文推理内容。
- `tool_calls`：工具调用。

官方 Chat Completion 文档响应示例包含 `choices[].message.reasoning_content`、`tool_calls` 和 `usage.prompt_tokens_details.cached_tokens`。

### 2. 多轮 thinking 透传规则

Z.AI/GLM 支持 thinking 模式，并有 preserved thinking 语义。

官方 Thinking Mode 文档示例说明：

- thinking 可通过 `thinking` 参数控制。
- preserved thinking 使用 `"thinking": {"type":"enabled", "clear_thinking": false}`。
- 工具调用场景下，模型第一轮返回 reasoning_content 和 tool_calls 后，客户端要把 assistant message 以 `content`、`reasoning_content`、`tool_calls` 一起追加回 messages，再追加 tool result。
- 后续请求继续设置 `clear_thinking=false` 时，模型可基于历史 reasoning_content 和工具结果继续推理。

规则：

- `clear_thinking=false` 表示保留历史 reasoning_content，客户端应把历史 assistant raw message 中的 reasoning_content 原样放回。
- `clear_thinking=true` 表示本次请求清理历史 reasoning_content，客户端仅从**请求副本**移除该字段，保留可见文本、tool_calls、tool results 等非 reasoning 上下文；持久 assistant raw 不变，供以后兼容的 preserved-thinking 请求重放。
- thinking disabled 时也不应回灌历史 reasoning_content。
- Z.AI 的 reasoning_content 是明文字段，但它是 Z.AI Chat Completions 的协议语义；跨协议不能伪造成 Claude/Gemini/OpenAI 私有 thinking 凭证。

跨协议转换规则：

- 其它协议到 Z.AI：普通文本转 `content`，工具调用转 `tool_calls`。其它平台明文 thinking 是否转为 `reasoning_content` 要谨慎，默认不转；如果目标模型/配置明确允许 preserved thinking 且产品策略接受，可作为明文 reasoning_content 迁移，但不得迁移签名/加密 thinking。
- Z.AI 到其它协议：`content` 和 `tool_calls` 必须保留；`reasoning_content` 只有目标协议允许明文 thinking 回传时才可转换，否则舍去或降级为普通文本。

### 3. 缓存规则

Z.AI Chat Completion 文档响应 usage 中包含 `prompt_tokens_details.cached_tokens`，说明服务端存在 prompt/cache 命中统计。

当前可确认规则：

- 标准 Chat Completion 请求中没有 Claude 风格 `cache_control` 断点。
- GLM-5.1 当前接入路径下的缓存由 Z.AI 服务端自动/best-effort 管理；客户端没有显式创建缓存、引用缓存或打缓存断点的字段。
- 客户端主要通过稳定历史前缀、preserved thinking 原样回灌来提高一致性和服务端自动缓存命中。
- cached token 命中通过 `usage.prompt_tokens_details.cached_tokens` 观察。
- preserved thinking 会增加 prompt tokens，但可保持推理连续性，并可能提高长任务/工具任务的上下文一致性。

AgentRuntime 接入策略：

- Z.AI/GLM-5.1 默认采用 Preserved Thinking，即请求体显式设置 `"thinking": {"type": "enabled", "clear_thinking": false}`。
- 同协议 Z.AI assistant 历史必须原样回灌 `raw.reasoning_content`、`content` 和 `tool_calls`，不能重写或裁剪 reasoning_content。
- Z.AI 缓存不在统一消息模型中保存显式缓存状态，只在 `provider_state["zhipu_chat_completions"]["cached_tokens"]` 记录最近响应中的命中统计。

默认值与模型差异：

- `thinking` 参数仅 GLM-4.5 系列及更高模型支持；官方 Chat Completion 文档说明默认启用。
- GLM-5.1、GLM-5、GLM-5-Turbo、GLM-5V-Turbo、GLM-4.7、GLM-4.5V 在启用 thinking 时会强制思考；GLM-4.6、GLM-4.6V、GLM-4.5 等会自动判断是否思考。
- `clear_thinking` 默认值是 `true`，表示本次请求忽略/移除历史轮次中的 `reasoning_content`，只保留非 reasoning 上下文。
- `clear_thinking=false` 才启用 Preserved Thinking；此时必须完整、未修改、顺序正确地回传历史 `reasoning_content`，缺失、截断、重写或乱序都会降低效果或导致 preserved thinking 不生效。
- `clear_thinking` 只影响跨轮历史 thinking block，不改变当前轮是否生成/返回 thinking。

### 4. GLM-5.1 图片/PDF 输入与文件块降级规则

Z.AI `chat/completions` 总接口支持 text、image、audio、video、file 等多模态输入，但这是接口层能力，不代表每个模型别名都支持全部输入模态。

当前可确认规则：

- 官方 GLM-5.1 文档把 `glm-5.1` 作为 LLM/text 模型介绍，Quick Start 示例均为纯文本 `content`。
- 官方 Vision Understanding / VLM 示例使用 `glm-4.5v`、`glm-4.6v` 等视觉模型，并通过 `image_url`、`video_url`、`file_url` 传递图片、视频和 PDF/文档。
- 因此不能把 `glm-5.1` 当作支持图片/PDF的多模态模型；项目中 `glm-5.1` 模型元数据应只声明 `supported_input_modalities=["text"]`。

渲染规则：

- 当目标 Z.AI 模型元数据未声明支持 `image` 或 `pdf` 时，user 消息中的 `ImagePart` / `PdfPart` 不能渲染为 `image_url` / `file_url` block。
- 这类资源必须降级为普通文本，优先写入统一文件资源服务解析出的本地绝对路径，例如 `[image file: G:\workspace\image.jpg]`、`[pdf file: G:\workspace\doc.pdf]`。
- tool result 消息中携带的图片/PDF资源也按同一规则拼接到 tool 消息 `content` 文本中。
- 只有当模型元数据显式声明支持对应模态，例如 VLM 模型声明 `supported_input_modalities=["text","image","pdf"]` 时，Z.AI adapter 才能把 user 图片/PDF渲染为 `image_url` / `file_url` block。
- 不允许仅根据 `api_format=zhipu_chat_completions` 推断可发送多模态 block；必须以 resolved model 的 `supported_input_modalities` 为准。

需要二次复核：

- Z.AI 是否还有服务端显式缓存创建/引用 API，当前项目接入的 `chat/completions` 路径未体现。

## 七、新统一会话模型与转换算法

本章保留统一会话的目标逻辑模型，并标明与本次核对源码的关系。`message_id`、时间线、配置视图和基座快照已存在；`call_ref`、`source_context`、通用 `metadata` 等仍按目标设计讨论。示意只列相关字段，不代表完整 Python 类型，也不要求因文档修订修改仓储或 API。

### 1. Conversation：一份规范历史，不绑定单个协议

```text
Conversation
  ├─ id
  ├─ messages: UniMessage[]          // 规范对话历史；压缩/裁剪由上游显式处理
  ├─ state: ConversationState        // 从消息派生的统计信息
  ├─ provider_state                 // 按协议记录运行态，不证明跨模型/账号兼容
  ├─ timeline                       // Agent 创建/冻结的配置事件
  ├─ system_prompt                  // 本次业务差异提示词
  ├─ tools                          // 本次工具定义
  └─ instructions_snapshot          // 会话级公共基座模板名及渲染内容

RequestConfigurationView            // 请求期只读视图，不作为 Conversation 持久字段
  ├─ prompt_events[]                // 提示词自己的历史/活动事件
  ├─ tool_events[]                  // 工具自己的历史/活动事件
  ├─ current_prompt                 // 本次 active 提示词内容
  ├─ current_tools                  // 本次 active 工具名称差集的有序定义
  └─ available_tool_names           // 当前完整可用工具名，供补充提示词渲染
```

- `Conversation` 不新增一个必须固定的 `provider`，本次目标协议来自调用参数。
- `state.providers` 是历史出现过的协议统计，不表示当前模型或多个同时活动模型。
- `messages` 是对话事实；`timeline` 承载历史配置快照与位置，Agent 提供当前配置，二者共同构造本次视图。不能由 messages 反推业务配置或补造切换。
- `provider_state` 不是服务端缓存目录，也不是已缓存 token 范围的保证；不能把业务配置时间线塞入此字段。

### 2. UniMessage 与内容块

| 字段/结构 | 目标语义 | 与现状的关系 |
| --- | --- | --- |
| `UniMessage.message_id` | 持久、稳定的规范消息身份；当前配置锚点依赖它，目标来源映射/规范调用身份可复用 | 已存在并随 JSONL 持久化，旧行有稳定 ID 迁移；不等于 call_ref 等目标闭环已实现 |
| `role` | 仅 user / assistant / tool；developer、system、additional_tools 均不是新增公共消息角色 | 沿用 |
| `content[]` | 保序的公共语义内容块 | 沿用 |
| `source_provider` | 实际产生 assistant 响应的 api_format；不因渲染到另一协议而改写 | 沿用 |
| `source_vendor` | 来源服务商元数据；不单凭品牌名称分发 adapter | 沿用 |
| `provider_extras[api_format].raw` | assistant 的原始可重放协议结构，入库后冻结 | 沿用并加强完整性校验 |
| `source_context` | 私有 raw 兼容性所需的模型/服务作用域标识，不含凭据 | 拟新增元数据；无依据时不得假定跨服务商签名兼容 |
| `metadata` | 非 LLM 协议的消息来源/压缩归档元数据 | 拟新增，避免与 provider raw 混放 |

公共块：

| Part | 关键内容 | 规则 |
| --- | --- | --- |
| `TextPart` | text | 可见文本，不把被省略的私有推理伪装为答案 |
| `ImagePart` / `PdfPart` | resource_id | 保存资源身份；URL/base64/本地路径由目标渲染与资源服务决定 |
| `ThinkingPart` | 可选明文/摘要 | 仅供展示或明确允许的降级，不保存签名/加密凭证 |
| `ToolUsePart` | name、input、tool_use_id；拟新增 call_ref | 工具调用事实与原始协议 ID，不代表当前执行授权 |
| `ToolResultPart` | tool_use_ref、is_error、output | 关联具体调用，保留结构化/文本/多模态结果及错误状态 |

assistant 采用“公共语义 + 原协议 raw”双层保存。user/tool 不保存 LLM 响应 raw；本地压缩归档等非协议元数据进入 `metadata`，不伪装成某个平台的响应。现有压缩路径把归档信息写在 user.provider_extras 的做法须在实现时做兼容迁移，而不是声称现状已符合新模型。

不要为每个目标协议提前保存一份伪造 raw。OpenAI 的消息被转换为 Claude 请求，不会因此获得 Claude 来源身份；只有 Claude 真正返回的新回复才生成一条带 Claude raw 的新 assistant 消息。

### 3. 工具关联：规范调用身份与协议 ID 分离

为避免不同来源、不同轮次重复使用相同工具 ID，目标模型区分：

- `ToolUsePart.call_ref`：拟新增的规范调用身份，在归一化追加时确定并持久化，例如由 `message_id + 调用序号` 派生；渲染时不重新随机生成。
- `ToolUsePart.tool_use_id`：保留来源协议提供的原始 ID，可为空；不能为了跨协议映射去修改冻结 raw 的签名关联字段。
- `ToolResultPart.tool_use_ref`：目标语义引用规范 `call_ref`。现有数据引用的是原协议 ID，实现迁移时必须解析为唯一原调用；无法唯一解析则报关联错误，不按函数名随便猜。
- 请求期建立 `call_ref → target_wire_id` 与 `call_ref → tool_name` 映射。同源 raw 回放优先使用原 wire ID；跨协议重建优先沿用合法且无冲突的原 ID，否则使用稳定合法映射。调用和结果必须使用同一个映射。

协议字段对应：

| 协议 | 调用 ID | 结果引用 |
| --- | --- | --- |
| OpenAI Responses / 经验证兼容的 Codex | function_call.call_id | function_call_output.call_id |
| Claude | tool_use.id | tool_result.tool_use_id |
| Gemini | functionCall.id（来源提供时必须保留） | functionResponse.id，另带正确 name |
| OpenAI Chat / Z.AI | tool_calls[].id | tool 消息 tool_call_id |

执行/渲染不变量：

1. 结果必须能唯一定位到前序调用，不允许悬空结果、把多个同名调用合并或错配。来源未提供 ID 时，在规范层建立稳定调用身份；目标是否发送合成 ID 服从该接口规则。
2. 同轮并行调用先完整表达调用集合，再提交对应结果；尤其 Gemini 不得把成组 functionCall/functionResponse 错误交错。
3. 同源 raw 中的原 ID 若在目标请求中发生无法合法处理的冲突，不可修改受签名保护的块硬凑；降级需要明确策略，否则报兼容错误。
4. 切协议不是执行工具的动作。存在未完成工具调用时，先由上游推进真实工具结果或明确处理未完成状态；adapter 不伪造结果、不重执行历史调用。
5. 历史调用/结果不会因当前工具撤权而删除；未来调用仍由当前 allowlist 和工具执行端硬校验。
6. 无法解析的参数应保留原始值并暴露诊断；不能静默替换为 `{}` 后假装语义一致。

### 4. 配置输入契约：Agent 决定 P/T 生命周期，Conversation 承载事件

术语：

- **热更新**：同一个提示词/工具实例内容改变，例如 P1-v1 → P1-v2。
- **切换**：当前活动业务实例改变，例如 P1 → P2 或 T1 → T2。
- **协议切换**：本次 target 改变，独立于以上两者；不能因此自动增加配置事件。

Agent 将提示词/工具事件记录到同一 `Conversation.timeline`，请求视图按组件拆成两个序列，每类最多一个 active。当前相关字段如下（省略类型细节）：

```text
ConfigurationComponentEvent
  event_id
  definition_id
  component: prompt | tool
  anchor: start | before_message_id | after_message_id
  anchor_message_id                // 非 start 锚点引用持久 message_id
  sequence                         // 同一位置的确定顺序
  status: historical | active
  snapshot                         // historical 必需，由 Agent 切换/继承路径冻结
  revision                         // 可选版本信息，不能代替快照
  supplementary_prompts            // 补充模板，工具名在请求期替换

RequestConfigurationView
  conversation_id                  // 与本次历史作用域一致
  view_revision                    // 本次请求期视图标识
  prompt_events: ConfigurationComponentEvent[]
  tool_events: ConfigurationComponentEvent[]
  current_prompt: {event_id, definition_id, resolved_content, revision}
  current_tools:  {event_id, definition_id, ordered_definitions, revision}
  available_tool_names             // 当前完整工具名；不是执行授权机制
```

`RequestConfigurationView` 是请求期契约，不是 Conversation 的持久字段；它可以由请求层从 `Conversation.timeline` 与当次 Agent 配置构造，也可显式传入。OpenAI Responses 消费其中的事件和锚点；其他协议不重放这些历史配置事件，直接使用当次 Agent 提供的最新系统提示词和 tools。

输入契约规则：

- **历史事件主值**使用冻结 snapshot，不重新读取定义；但事件的 `supplementary_prompts` 仍按本次 `available_tool_names` 替换占位符。因此冻结 snapshot 不等于整个历史 developer 块字节恒定。
- **活动提示词/活动 tools**使用 Agent 当次解析的实时值，绑定原 active 事件与原锚点；热更新不追加切换事件。工具值再按全部历史工具名称做差集，不按 schema hash 比较；历史中已有同名工具时，新 schema 不进入这段增量。adapter 不自行访问定义仓库。
- Agent 的切换/继承路径负责提供被冻结的配置主值并追加新 active 事件，仓储不代替 Agent 选择定义。Note 进入阶段冻结业务配置后在边界后追加；子会话继承时保留复制范围内的父锚点并冻结，不能一律移动到 start。
- 同一次请求使用一份一致视图，渲染到一半不能再热读取定义；同一逻辑请求重试默认复用该视图。若上游显式重新解析，则是新的请求输入版本。
- 提示词快照保存最终有序片段；工具快照保存工具模块提供的完整有序定义及必要的历史语义。schema 生成仍属工具模块，adapter 仅按目标协议包装。
- 空提示词/空工具集与未提供视图不是同一概念；没有 timeline 但存在当前 prompt/tools 时，请求层可以构造 start 初始视图，这不是猜测过去发生过的业务切换。
- `anchor` 依赖持久 message_id，不能依赖渲染下标。初始事件用 start；同锚点按 `(sequence, 工具优先, event_id)` 确定顺序，渲染整体为 start 配置、消息前配置、消息、消息后配置。
- Agent 决定分支/压缩的会话内容与配置；adapter 校验作用域和锚点。仓储保存时会删除非 start 且 message_id 已不存在的孤儿事件，不把孤儿事件移到首部。

P/T 独立切换示意（不是对所有业务切换入口均已实现的声明；工具列须遵守上述名称差集规则）：

| 时刻 | 上游提示词事件 | 上游工具事件 | Conversation 本次消费结果 |
| --- | --- | --- | --- |
| 初始 | P1 活动 | T1 活动 | P1 最新内容 + T1 最新定义 |
| 仅切提示词 | P1 冻结，P2 活动 | T1 仍活动 | P1 历史快照 + P2 当前；T1 仍可热更新 |
| T1 热更新 | P2 仍活动 | 同一 T1 内容更新 | 不新增工具实例切换事件；原 T1 活动位置呈现新内容 |
| 仅切工具 | P2 仍活动 | T1 冻结，T2 活动 | P2 仍可热更新；T1 历史快照 + T2 中历史未出现名称的定义 |
| 只换到 Claude | 以上事件不变 | 以上事件不变 | 只改变协议投影，不创建 P/T 事件 |

这也意味着：active 位于 start 时，热更新可能改变 input 首部；位于后置锚点时，最多保持该变化之前的共同前缀，不能把更新挪到尾部以伪保缓存。若历史事件含动态工具名补充模板，当前工具变化还可能影响更早的 developer 块；同名 schema 被名称差集排除时，则不能声称它已经重发。

### 5. 配置时间线如何投影到目标协议

**配置时间线的价值取决于目标协议是否能在配置变化后保持稳定缓存前缀。**它不是所有协议都要原样投影的通用请求历史。上游仍可维护统一的 P/T 事件事实，但各协议的请求构造策略不同：

1. **OpenAI Responses 是当前消费配置时间线的特殊路径。**在支持这些 item 的端点上，历史 developer 与 `additional_tools` 按原位置重放，新阶段配置按边界追加，使未改变的累计前缀有机会复用；默认没有显式 B。active 热更新、动态补充文本或 instructions 覆盖仍可能改变前缀，不能只凭时间线存在就保证命中。

2. **其他协议不支持同等的 developer + additional_tools 增量表达。**Claude、Gemini、OpenAI Chat Completions、Z.AI 以及独立的 `codex_responses`，不能把统一时间线中的多段系统提示词/工具事件按 Responses 的方式追加，同时保证旧缓存前缀不变。把历史上所有 P/T 事件全部拼进它们当前的 system/tools，既不能复现 Responses 的位置语义，也会改变缓存前缀，因此没有实际价值。

3. **其他协议每次直接使用 Agent 提供的最新实时配置。**adapter 使用本次请求的最新有效 system prompt 与 tools 覆盖设置；统一会话仍负责保存对话消息、assistant raw 和工具事实，但不把 P/T 历史时间线重放成这些协议的请求配置。它们的缓存只能在本次最新配置和历史消息形成的新前缀上按各自协议策略尝试命中。

4. 因此，时间线不是“所有协议都必须按事件投影”的要求，而是由目标协议能力决定的优化输入：Responses 为保持缓存前缀必须读取并按锚点重放；其他协议若没有等价能力，则跳过历史 P/T 重放，直接采用当前实时配置。协议切换本身仍不创建新的 P/T 事件。

#### 5.1 OpenAI Responses：当前默认布局与显式配置示例

Runtime 把 `instructions_snapshot.content` 放顶层 `instructions`，业务差异提示词以 `role="developer"` 内容项、工具以 `type="additional_tools", role="developer"` 事件投影到 input。初始化顺序通常为工具 sequence=0、提示词 sequence=1；后续按原锚点投影。默认结构示意如下，省略模型/输出参数和消息细节：

```text
instructions = 会话固定基座快照（显式覆盖除外）
input = 初始 additional_tools + 初始 developer + 历史消息
        + 边界后的配置事件 + 后续消息
prompt_cache_key = 显式值 > provider_state 已记录值 > conversation.id
store = false；include 包含 reasoning.encrypted_content
不发送 previous_response_id；默认不发送 prompt_cache_options，不注入 marker
```

以下保留 **调用方显式选择** `implicit + 配置尾 B` 的 JSON 示例，并补示顶层基座；它不是默认请求采样。只有确认目标端点支持且提供相应 options/marker 时才适用；短示例不足以达到缓存 token 阈值，也不代表对任意 compatible 端点已验证。

```json
{
  "model": "gpt-5.6",
  "store": false,
  "include": ["reasoning.encrypted_content"],
  "prompt_cache_key": "conversation-stable-key",
  "instructions": "你是一个善于学习的AI。",
  "prompt_cache_options": {"mode": "implicit", "ttl": "30m"},
  "input": [
    {
      "type": "additional_tools",
      "role": "developer",
      "tools": [
        {
          "type": "function",
          "name": "lookup_status",
          "description": "查询服务状态",
          "parameters": {
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"],
            "additionalProperties": false
          },
          "strict": true
        }
      ]
    },
    {
      "role": "developer",
      "content": [
        {
          "type": "input_text",
          "text": "你是服务状态核查助手。",
          "prompt_cache_breakpoint": {"mode": "explicit"}
        }
      ]
    },
    {"role": "user", "content": [{"type": "input_text", "text": "查询 api 服务。"}]}
  ]
}
```

- 仅显式提供 marker 时才在请求副本重建 B：范围是 start 配置最后合法 developer `input_text`，不是任意历史消息或每个阶段；未提供时不加标。历史提示词内容块中的已有 marker 会在投影时移除。
- 若候选内容块后面还有 `additional_tools`，或找不到合法文本块，则不附加 B。不能为得到联合断点擅自重排既有事件、强造业务提示词，或宣称工具-only 配置已被 marker 覆盖。
- 公共基座与 input 业务提示词不是同一份内容的重复发送；后者应排除 snapshot.template_names 已收录模板。顶层 instructions 显式覆盖会改变前缀，但不会自动改写 input 历史或快照。
- 请求层在渲染前处理 `request_params.instructions/tools` 覆盖：instructions 留在顶层，自定义函数 tools 转为配置视图，模型内置托管 tools（如 `web_search`）提取并保留在顶层 tools 发送；显式 view 与这两类覆盖冲突时会报错。客户端不接受渲染后绕过视图直接塞入顶层自定义函数 tools 或 input。
- `prompt_cache_options` 仅显式字典才合并/发送；值为 None 时删除该字段。源码没有默认 TTL 或多断点调度，不能把第三章的协议槽位预算描述为当前 Runtime 已实现的选择算法。
- 端点是否支持这些字段需独立确认。当前源码没有据此自动折叠时间线为其他表达的能力门控，不能将目标兼容性策略写成已实现的 fallback。

#### 5.2 OpenAI Responses：追加不等于严格替换

本节只适用于 OpenAI Responses 的 developer/`additional_tools` 时间线。它们是保持旧缓存前缀的增量表达，不是通用的系统提示词/工具替换语义。

- `additional_tools(T2)` 仅表示在对应历史位置追加工具声明，不自动删除 T1、撤权、覆盖同名函数或更换同名 schema。当前 Runtime 只追加历史名称集合之外的工具，同名 schema 更新不会通过这段差集重发。
- 历史 developer(P1) 后追加 developer(P2)，不能笼统解释为“P1 已从上下文消失”；其作用是尽量保留既有配置/公共历史，而非保证默认存在 B 或整段历史不变。
- 当前调用能力必须与 Agent 本次有效工具授权一致；工具追加不能重新授予已撤销权限，执行端仍按当前 allowlist 校验。
- 目标正确性原则仍是：旧/新配置语义冲突或端点不支持时，不为缓存伪造替换语义；若要改用最新配置覆盖，必须接受前缀变化并明确新策略。当前 Runtime 未实现自动语义冲突检测、同名 schema 替换或按能力自动折叠/报错，不应把这些目标策略当成现有分支。

#### 5.3 其他目标协议：只使用最新实时配置

对于 Claude、Gemini、OpenAI Chat Completions、Z.AI 和独立的 `codex_responses`：

1. 不读取统一会话中的历史 P/T 事件来重放多段 system prompt 或 tools；历史配置事件由上游保留，用于业务审计、状态理解和 OpenAI Responses 路径的缓存前缀重建。

2. 每次请求直接取 Agent 本次解析出的最新 `current_prompt` 与 `current_tools`，分别放入该协议支持的 system/systemInstruction 与 tools 位置；这属于覆盖设置，不是把时间线折叠成协议历史。

3. 不把 OpenAI developer、`additional_tools`、配置锚点或 `prompt_cache_breakpoint` 透传到这些协议。由于它们无法用等价的增量配置表达来保持旧缓存前缀，强行拼接历史配置只会改变前缀并增加语义歧义。

4. 这些协议的缓存命中只对“最新实时配置 + 本次重建的对话历史”按各自协议规则负责；不能以统一会话保存了历史时间线为由，声称它们保留了旧配置缓存前缀。

5. 如果某个未来协议明确提供等价的增量 system/tools 与稳定前缀能力，再单独增加该协议的时间线投影规则；不能仅因它有 system/tools 字段就默认支持。

| 目标 | 当前配置表达 | 历史 assistant 回放/重建 | 工具结果表达 |
| --- | --- | --- | --- |
| OpenAI Responses | 顶层基座 instructions + input 内 developer/`additional_tools`；按锚点投影历史和 active，默认无 options/marker，显式配置时仅处理合法初始 B | 同源 raw output items；异源 text/function_call，私有状态跨服务商兼容仍需核实 | 顶层 function_call_output |
| Claude Messages | 顶层 system / tools | 同源兼容 content[] 含 thinking/signature；异源 text/tool_use | user.content[] 中 tool_result |
| Gemini generateContent | systemInstruction / tools | 同源兼容 model.parts 含 thoughtSignature；异源 text/functionCall | user.parts 中 functionResponse |
| OpenAI Chat Completions | 目标支持的 system/developer messages + 顶层 tools；不沿用 Responses 时间线 item | 同源兼容 content/tool_calls；异源按消息字段重建 | role=tool + tool_call_id |
| Z.AI Chat Completions | system message + 顶层 tools | 同源按 thinking/clear_thinking 规则回放；异源 content/tool_calls | role=tool + tool_call_id |
| Codex Responses | 由独立 Codex 请求契约决定，不自动注入 OpenAI 新字段 | 同源 Codex raw；异源公共语义，仅复用已验证的 Responses 结构 | 按 Codex 支持的 function_call_output |

上述矩阵配合第二至六章的细则，覆盖任意来源到任意目标：不维护 N×N 两两转换链，而是“任一来源归一化 → 同一公共模型 → 目标 adapter”。

### 6. 请求渲染与响应归一化的完整算法

以下保留目标算法：完整能力门控、规范 call_ref、源位置映射及诊断并非都已实现。当前 Responses 的基座、配置投影和缓存默认行为在对应步骤明确标注，具体证据见第八章。

#### 6.1 请求前输入

- 一份不可原地改写的 Conversation 历史快照。
- 目标为 OpenAI Responses 时，请求层从 Conversation 的 timeline、基座快照和当次 Agent 配置派生视图，或消费显式 RequestConfigurationView；无 timeline 时可构造初始视图。其他协议直接使用本次最新提示词/tools，不消费历史 P/T 投影。
- 已解析的 target/api_format、模型能力、私有状态兼容作用域及协议策略。
- ResourceResolver 和目标缓存策略；模型网络凭据不进入消息模型或配置历史。

#### 6.2 渲染步骤

1. **校验规范输入**：角色/part 合法；message_id 唯一；Responses 时间线的事件锚点（若目标为 Responses）存在、P/T 当前活动项明确；工具调用/结果引用完整；资源可解析。
2. **确定配置投影**：当前 OpenAI Responses 请求层将基座快照或显式覆盖放顶层 instructions；adapter 从配置视图按锚点投影 developer/`additional_tools`，历史主值取 snapshot，active 取实时值和工具名称差集，补充模板按当前工具名渲染。其他协议跳过历史 P/T 重放，直接用最新配置；缓存不能改变执行授权。
3. **逐条渲染公共消息**：user/tool 由公共块转换；assistant 若有同源且目标兼容的 raw，复制后回放；否则按公共 text/tool-use 保序重建。
4. **处理推理状态**：同源私有状态按目标规则保留；异源不伪造、不解密。明文 thinking 的展示/降级必须由明确策略允许。Z.AI 关闭 preserved thinking 时只改请求副本，不删持久 raw。
5. **落实目标约束**：必要的角色分组、工具调用顺序、参数/结果格式、多模态能力校验。目标允许的消息边界不擅自合并；必要合并/拆分只影响请求副本，保留源位置映射。
6. **插入配置、构造映射**：Responses 当前按 P/T 锚点插入 developer/`additional_tools`；完整源位置映射仍是目标，不应从有锚点推导已返回所有映射。其他协议不把历史 P/T 事件映射进请求；raw 展开、消息合并和占位的可追溯性仍需验证。
7. **添加协议缓存元数据**：当前 Responses 默认既不发送 options 也不加 marker；显式 marker 仅尝试附加到 start 配置末尾合法文本，后有 tools 或无合法文本时跳过。协议切回不强制给旧 user 补点；协议资料中的多断点/槽位预算不是 Runtime 已实现调度。其他协议按各自机制处理。
8. **完整请求校验**：client 合并模型/输出格式/reasoning 等设置后，检查无重复配置、无外来协议字段、无冲突覆盖；重用策略不能绕过当前授权。
9. **返回或报错**：输出协议专属渲染结果、源位置映射、兼容性诊断和本次选用的缓存边界；错误应包含可定位 message/event/call 引用，不携带凭据或伪造结果。

当前请求链已有 configuration view 的消费路径；统一 target capabilities 与完整诊断/映射仍属上述目标，不因已有视图就视为完成。各 adapter 不能私下重新读取业务定义，`ProviderAdapter` 不负责 HTTP 传输。

#### 6.3 响应归一化与回写

1. 完整聚合目标响应，校验可识别的输出结构；一次成功响应只归一化并追加一次，重试不能重复插入 assistant。
2. 为新 assistant 分配稳定 message_id；提取有序 text/thinking/tool-use，保留源调用 ID并建立 call_ref。
3. 保存该来源协议需要重放的完整原始结构及来源兼容性元数据；不把请求 marker、临时映射或其他协议的伪造 raw 混入。
4. 不能把未支持的拒绝、服务端工具等 output item 静默过滤后仍宣称完整转换。已支持项正常映射，其他项须保留必要原始证据并给出明确兼容性处理；工具结果不能被凭空合成。
5. 追加到原 Conversation，更新派生 state；只更新本次目标作用域的运行态与真实 usage，不重写其他来源历史。
6. 上游执行新工具调用、校验当前授权并追加真实 ToolResultPart，再进行下一次请求；Conversation 不因保存历史而自动执行任何工具。

#### 6.4 降级与错误不是同一件事

| 情形 | 目标行为 |
| --- | --- |
| 异源私有签名/加密推理无法迁移 | 默认不传，保留源 raw；记录推理连续性不保证 |
| 目标要求缺失的 signature，例如特定 Gemini 工具续接 | 仅在目标官方允许且上游明确接受时用兼容策略；否则报错 |
| 文本/工具调用无法合法表达、结果关联不明确 | 不静默丢弃，报转换不兼容 |
| 模型不支持某图片/PDF | 仅按明确策略转资源引用/文本；路径不等于模型看到了文件，诊断说明模态损失；否则报错 |
| 源协议未知字段或新型 output item | 不把“JSON 能序列化”当完整支持，走显式兼容判断 |
| OpenAI Responses 无法合法表达所需的 P/T 时间线 item | 不静默改写或伪造缓存前缀；按能力报错，或由上游明确选择直接使用最新配置的非时间线策略 |

### 7. 缓存运行态、确定性与作用域

| 数据 | 所属位置 | 不代表什么 |
| --- | --- | --- |
| assistant raw / tool 事实 | Conversation.messages | 不是当前配置授权 |
| 提示词/tools 时间线与当前配置 | Agent 决策，Conversation.timeline/system_prompt/tools 承载，仓储保存 | 不是协议缓存目录，也不是执行权限来源 |
| instructions_snapshot | Conversation 及 conversation_configuration | 是基座内容快照，不是服务端缓存 ID 或永远不变的所有请求参数 |
| prompt_cache_breakpoint / cache_control | 本次协议请求副本 | 不是普通历史消息，不是 OpenAI cache ID |
| Gemini cachedContent | 目标缓存管理/协议运行态 | 不是其他模型或服务商可通用的引用 |
| prompt_cache_key、实际请求边界、usage | 目标作用域运行态/受控请求观测 | 不是缓存存在、完整命中或固定路由的保证 |

- `provider_state[api_format]` 下如保存可复用句柄/请求观测，必须进一步区分模型与服务端/账号或区域等兼容作用域；不能因 api_format 相同就交叉使用。
- 可以记录最近实际提交的规范边界、配置视图版本、非敏感请求指纹和 usage，用于诊断。不要本地维护一个自称准确的“服务端已缓存到第 N 条”真相源。
- 稳定性依赖工具/内容块保序、同源 raw 副本回放以及资源表示一致。当前 HTTP 使用 `json.dumps(body, ensure_ascii=False)`，没有对整份请求做 sort_keys 规范化；不能保证任意等价 JSON 或重新解析的资源 URL 都得到相同编码。请求指纹即使作为诊断信息，也不是服务端缓存键。
- 源历史和 current 配置相同才尝试稳定重放；热更新/切换、压缩、资源版本变化或相关参数变化可能改变前缀。正确性优先，不恢复旧规则来换命中。
- 缓存 marker 总是加到深拷贝的请求数据；多目标并行渲染不能互相写入 marker 或修改共享 raw。

### 8. 持久化、旧数据、压缩与恢复

持久化责任：

- Conversation 仓储用 JSONL 保存消息、message_id、content 与 provider_extras，用 SQLite 保存 state/provider_state。旧消息有稳定 message_id 兼容迁移；call_ref/通用 metadata 的目标迁移不能据此宣称已完成。
- 定义仓库仍由定义/工具模块负责；会话仓储的 `conversation_configuration` 表已保存 timeline、system_prompt、tools、instructions_snapshot，并优先从该表恢复。Agent 负责事件创建与冻结，不等于物理持久化不属于 Conversation 仓储。
- 保存时裁掉引用已删除 message_id 的非 start 孤儿事件；时间线、当前配置和基座快照均为空时删除配置记录。adapter 不移动孤儿锚点、不扫描消息猜测切换。
- 无 timeline 的旧会话可从本次 Agent 配置构造 start 初始视图，但不能据此声称恢复了未知历史配置；恢复出的 historical 事件仍用 snapshot，不以当前定义伪补。旧工具规范引用迁移是另行验证的目标。
- user.provider_extras 中既有 context_compression 归档元数据应兼容迁移到通用 metadata；不要把它误认作 LLM provider raw 或删除归档关联。

压缩/分支恢复：

1. 普通协议切换不触发压缩、复制 Conversation 或清空其它协议运行态。
2. 压缩产生新历史/窗口，摘要是新消息，不伪装成原协议 assistant。既有 `conversation_rewriter` 可保留尾部消息；当前 Agent Note 更新完成路径则新建 Conversation、新 ID、基座快照与 start 业务配置，并注入 Note 摘要/唤醒消息，不能描述为原长历史全量延续。
3. 若保留历史配置，锚点须与保留消息匹配，historical 仍用快照；被删除锚点不能悬空或由 adapter 移到首部。新建会话的初始配置是新会话配置，不是给旧历史重新赋义；其他协议仍只取本次最新配置。
4. 不能把未闭合的工具调用/结果对截成非法历史；需要保留关联单元或由上游明确重建可接受上下文。
5. 清除或重新验证与旧窗口绑定的协议缓存句柄/位置观测，重新计算请求期 marker；旧 input 下标不可继承。当前压缩实现新建 Conversation 并清空 provider_state，属于现状，不等于物理删除服务端缓存。
6. 压缩改变了前缀时不能保证旧缓存复用；若仍有稳定配置前缀，可按真实请求尝试复用该段。
7. 分支/回退是上游操作：选择正确历史及对应配置事件视图，不能由 Conversation 为命中缓存自行丢弃分支消息，也不能撤销已执行工具的副作用。
8. 子 Agent 新建 Conversation 时继承父 `instructions_snapshot`、深拷贝指定消息前缀，继承并冻结复制范围内的父事件且保留原锚点；只有无父 timeline 时用 start 快照兜底。它不默认复制父 provider_state，所以相同内容不等于继承父 prompt_cache_key，也不保证跨会话缓存共享。

## 八、当前实现核对与后续验证条件

### 1. 当前源码事实与证据边界

以下路径以仓库根目录为基准。本次按源码更正 OpenAI Responses 前缀、配置生命周期及持久化描述；其他协议行保留既有审阅范围，不代表此次重新完成全协议审计。本节不是测试报告、运行版本核验或上游缓存实测结论。

| 源码位置/入口 | 当前事实 | 与本文目标设计的差距 |
| --- | --- | --- |
| `src/agent_runtime/conversation_message/message/models.py`：Conversation、UniMessage、配置视图 | 已有 message_id、timeline、system_prompt/tools、instructions_snapshot；请求视图 historical 取 snapshot、active 取实时值，工具增量按历史名称差集 | 同名 schema 变化不通过差集重发；call_ref/source_context/通用 metadata 目标闭环另行验证，不能由 message_id 或时间线已落地推导完成 |
| `src/agent_runtime/conversation_message/message/operations.py`：render_messages / ingest_response | 注册六类 adapter，渲染与响应归一化统一分发 | 需增加配置输入和目标能力的整体校验、诊断与映射契约；不能让每个 adapter 主动解析上游定义 |
| `src/agent_runtime/conversation_message/message/openai_protocol.py`：OpenAIAdapter.render / normalize_response | 按 message_id/sequence 投影配置；遍历全量消息，同源回放 reasoning/message/function_call raw；默认 options/marker 为 None，显式 marker 只处理合法 start 内容块 | supplementary_prompts 按当前工具名渲染，历史 developer 不一定字节不变；源码不证明端点支持或缓存效果 |
| `src/agent_runtime/llm_client/llm_request_service.py`：_resolve_openai_request_configuration | 优先使用显式 instructions 覆盖，否则用基座 snapshot；有 timeline 时派生视图，无 timeline 时可建初始视图；tools 覆盖先转成 input 配置 | 显式 view 与 instructions/tools 覆盖冲突会报错；统一 usage 当前只读 cached_tokens，未在该分支统计 cache_write_tokens |
| `src/agent_runtime/llm_client/llm_protocol_clients.py`：OpenAIResponsesProtocolClient.request | 允许/兜底顶层 instructions，允许顶层托管 tools（如 web_search）并下发给服务商，拒绝直接发送顶层自定义函数 tools；强制 store=false、include encrypted_content、去除 previous_response_id；仅显式 options 才发送 | 请求构造通过不等于全部调用方或目标端点兼容；不默认发送 mode/TTL，也没有自动能力折叠策略 |
| `src/agent_runtime/agent/agent_execution_engine.py`：_render_system_prompt / Note 切换 | 首次建立基座快照，后续差异 prompt 排除已收录模板；初始化 start 事件；进入 Note 阶段冻结业务配置并在边界后追加，完成后新建会话/快照/start 配置 | 同会话快照固定不等于 Note 完成后还是同一缓存前缀；active 热更新和显式 instructions 覆盖可改变请求 |
| `src/agent_runtime/subagent/subagent_runtime.py`：子会话创建 | 继承基座快照、复制消息前缀，范围内父事件冻结并保留锚点；子配置在边界后，按工具名称决定是否需新增工具事件 | 不复制父 provider_state/cache key；工具子集不追加声明也不代表拥有全部历史工具权限 |
| `src/agent_runtime/conversation_message/message/claude_protocol.py`：render / _attach_cache_breakpoints | 复制同源 raw；不合并连续同角色消息；本次 messages 尾部打动态 cache_control | 新设计需与 client 的 system/tools 断点统一预算；现有最近 assistant/index 不足以证明输入边界已缓存 |
| `src/agent_runtime/conversation_message/message/gemini_protocol.py`：normalize_response / _build_gemini_function_response | 已把 functionCall.id 带入 ToolUsePart，再回到 functionResponse.id；有 parts raw 回放和角色规范化 | 需校验跨轮缺 ID 时规范引用唯一性、签名兼容/缺失策略和 cachedContent 作用域；不能把已有 ID 透传误列为未实现 |
| `src/agent_runtime/conversation_message/message/codex_protocol.py`：CodexAdapter | 复用 OpenAI 部分转换辅助，并保留独立 source_provider 与 provider_state | 新 OpenAI 字段不得通过共享辅助函数自动泄漏进 Codex；能力需要独立判断 |
| `src/agent_runtime/conversation_message/infrastructure/sqlite_conversation_repository.py` | JSONL 写 message_id 与消息；SQLite 保存 state/provider_state 及 conversation_configuration（timeline/system_prompt/tools/instructions_snapshot），优先从配置表恢复并在保存时裁剪孤儿锚点 | 当前机制可恢复会话配置，不证明 call_ref/source_context/通用 metadata 的目标迁移完整，也不证明真实上游缓存仍在 |
| `src/agent_runtime/context_compression/conversation_rewriter.py`：rewrite_conversation_with_summary | 新建会话、加入 user 摘要、保留尾部消息、清空 provider_state；归档元数据在 user.provider_extras | 需与稳定消息身份/metadata 和上游配置锚点重映射契约衔接 |

### 2. 当前边界与待验证事项

- **默认值与显式能力分开**：默认请求没有 options/marker；显式 marker 只在合法初始文本块上加一个，不自动标记任意历史或后续阶段。工具-only、文本后仍有 tools 时不加标，不重排事件。
- **历史主值与整个请求分开**：historical snapshot 冻结，但动态补充工具名仍取当前视图；active 在原锚点热更新，工具仅按名称去重。不能把“snapshot 不重读”推导为历史 developer 全字节相等，也不能宣称同名 schema 更新已透传。
- **请求覆盖与顶层字段分开**：顶层 instructions 合法且有稳定快照/兜底路径；tools 覆盖由请求层规范化成 input 配置。显式 view 冲突会报错，但不能据此推断每个既有调用方都经过完整回归。
- **会话边界与缓存路由分开**：子会话和 Note 完成后的新会话不默认继承旧 provider_state；默认 key 随新 conversation.id 改变。固定 key 也不能证明固定路由节点或缓存存在。
- **实现与端点能力分开**：源码尚未提供上述缓存扩展的模型/端点能力门控，不能从任意 compatible 名称判断支持；未知 output 类型、私有 raw 跨服务商兼容及 call_ref/source_context/通用 metadata 目标仍须独立验证。
- 相关现有测试可用于后续验证：`tests/test_stable_prefix_prompt.py` 覆盖快照创建/复用/透传/持久化；`tests/test_openai_responses_configuration_timeline.py` 包含时间线热更新、恢复与默认不发缓存参数的 mock 请求检查。本次只核对内容，未执行测试、真实 API、usage/cache_write_tokens 观测、构建部署或跨协议回归。

### 3. 后续验证矩阵（本次未执行）

与当前 Responses 实现相关的行按默认/显式场景分别验证；call_ref、完整兼容诊断等行仍是既有目标验收用例，不表示本次已实现或授权改造。

| 场景 | 必须观察的结果 |
| --- | --- |
| OpenAI 两轮 → Claude 三轮 → OpenAI | O1/O2 保留同源 raw，Claude 历史按公共语义转换；默认不加 options/marker，显式选择 B 时才按合法位置重建；不强制 U2 显式点；配置未变时检查原累计输入 |
| OpenAI → Claude → Gemini → OpenAI → Z.AI 多次往返 | 不原地改写历史；每次有同源兼容 raw 的项仍可回放，异源从公共模型重建 |
| 六协议中任意来源/目标的文本与工具历史 | 文本不丢、调用/结果一一关联，必要分组后顺序合法；不伪造私有推理 |
| 并行同名调用、跨轮重复 wire ID、来源无 ID | canonical call_ref 唯一；目标映射稳定；结果不按函数名错配；不能合法映射时报错 |
| 只切 P、只切 T、只热更新活动实例 | 历史主值用快照，active 原锚点取当前值；工具按历史名称差集；另检查 supplementary_prompts 工具名变化对旧 developer 的影响 |
| 切回原模型但活动配置已热更新 | 用最新有效配置；不得为了缓存回退旧内容，前缀失效范围如实报告 |
| additional_tools 追加、工具撤权、同名 schema 变化 | 新名称才进入当前差集，同名新 schema 不重发；历史调用保留，实际调用按当前 allowlist；不能以未实现的自动折叠/替换作为现状断言 |
| Responses 时间线 / 非 Responses 配置切换 | Responses 顶层基座 + 按锚点投影 developer/`additional_tools`，默认无 B；无 timeline 时有配置仍建初始视图；其他协议直接使用最新 system/tools，不重放 P/T 历史 |
| 同协议但更换服务商/模型兼容域 | 不盲用签名、加密推理或缓存句柄；策略明确且可诊断 |
| 仅自动断点：原完整输入后追加 | 对照官方请求结构，旧末尾未重标仍可尝试缓存读取；不能把命中视为客户端必然保证 |
| 动态后缀替换：有/无配置尾显式 B | 区分旧完整边界与内部共享前缀；有 B 时可独立尝试复用配置段 |
| 缓存过期、参数/工具顺序变化、多模态表示变化 | 请求仍正确；usage 如实反映，不恢复旧配置或凭本地标记声称命中 |
| marker 数量、合法位置与 raw 不变性 | Runtime Responses 默认零 marker，显式入口只在合法 start 文本块加标，无合法位置则零；多断点预算属于协议能力验证；各协议 marker 仅在请求副本，原消息/raw 不变 |
| Gemini signature 缺失、Z.AI clear_thinking、模型不支持图片/PDF | 请求合法且降级策略明确，源 raw/资源身份不变；不声称完整推理/模态无损 |
| 保存/恢复、旧数据、压缩后继续切协议 | 配置表恢复 timeline/当前配置/基座快照；非 start 孤儿被裁剪；子会话保留复制范围父锚点，Note 完成后新建快照/start；新会话不默认继承旧缓存 key |

后续验证应区分三层证据：规范历史/请求结构检查、目标接口兼容性验证、真实 usage 的缓存观测。通过 JSON/单元校验不等于真实模型接受，也不等于缓存已命中；缓存实测要避免后一次试验被前一次预热所污染。

## 九、资料来源

已使用/交叉参考的资料：

- 本地忽略资料：`doc/claude_api.md`、`doc/openai_resposne_api.md`、`doc/gemini_generating_api.md`、`doc/api大小限制规则.md`。
- Anthropic 参考：[Extended Thinking](https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking)、[Prompt Caching](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching)。本稿沿用此前本地资料与官方搜索材料；具体模型、TTL 和读取范围在实现前按目标版本复核。
- OpenAI [Prompt Caching](https://developers.openai.com/api/docs/guides/prompt-caching)：沿用此前读取的页面材料。`Request parameters and response usage` 展示 implicit 下不重标旧 user 边界仍读取 12,000 tokens 的成对请求；`A shared prefix is not always a cached prefix` 解释替换动态后缀时应单独保留稳定前缀断点。两类例子不能互相替代，本次未重新联网核验其版本。
- OpenAI [Responses Create](https://developers.openai.com/api/reference/resources/responses/methods/create)：与指南共同用于字段/能力校验；指南/参考的 50/80 读取窗口表述差异单独保留，不能据此改变每请求写入预算或宣称无限读取。
- 本次更正依据第八章中的 Agent 执行器、子 Agent、models、OpenAI adapter、LLM 请求层/client 和 SQLite 仓储；其他协议与 conversation_rewriter 的既有设计说明不扩展为本次全量审计结论。
- 此前用户确认的《OpenAI Responses API 自定义缓存断点与 `additional_tools` 技术指南》（`openai_responses_custom_cache_breakpoints_guide.md`）：作为支持内容块、assistant `output_text` 示例和 `additional_tools` 语义的补充依据。分支案例仅作为协议使用说明，不等同于 AgentRuntime 功能实现；不沿用资料中“100% 命中”“固定路由到同一节点”等绝对保证。
- Google 官方文档：[Context Caching](https://ai.google.dev/gemini-api/docs/caching)、[Thought Signatures](https://ai.google.dev/gemini-api/docs/thought-signatures)。
- Z.AI 官方文档：[Chat Completion](https://docs.z.ai/api-reference/llm/chat-completion)、[Thinking Mode](https://docs.z.ai/guides/capabilities/thinking)。
- 外部协议材料的模型能力、阈值、价格、TTL 与限制可能更新；本稿不是所有兼容服务商的统一承诺。源码未验证的目标字段和真实 API 缓存命中，均不得因本文写入就宣称已实现或实测。
