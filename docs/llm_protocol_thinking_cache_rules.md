# 多协议 LLM 多轮对话 Thinking 透传与缓存规则

本文档用于约束 AgentRuntime 统一消息模型与各协议 adapter 的实现。核心问题不是“统一 assistant 长什么样”，而是：当历史对话里混合了多个协议模型的 assistant 回复时，目标协议平台在多轮对话中要求如何回传 assistant 文本、工具调用、thinking/推理状态，以及目标协议的缓存是否需要客户端参与管理。

当前项目支持的 `api_format` 范围：

- `anthropic_messages`
- `openai_responses`
- `openai_chat_completions`
- `gemini_generate_content`
- `zhipu_chat_completions`

## 一、统一转换总原则

### 1. assistant 公共语义不能丢

所有接入模型都支持普通文本回复和函数调用，因此任意来源协议的 assistant 历史消息转换到目标协议时，以下公共语义必须尽最大能力保留：

- **文本**：assistant 可见文本必须转换为目标协议的 assistant/model 文本内容。
- **工具调用**：assistant tool use / function call 必须转换为目标协议的工具调用结构，并保持工具调用 ID 或可映射引用，保证后续 tool result 能关联到正确调用。
- **顺序**：同一 assistant 消息内 text 与 tool use 的相对顺序应尽量保留；目标协议无法表达混排时，应按该协议官方消息结构做最小损失重建。

### 2. thinking/推理内容按目标协议规则处理

thinking 不属于统一公共语义的稳定全集。不同平台返回的 thinking 形态、能否回传、是否必须回传、是否只对同协议有效完全不同。

统一规则：

- **同协议续聊**：如果目标协议要求或支持回传该协议的原始 thinking 状态，应原样回灌 `provider_extras.raw`，不得重建、删改、移动、翻译或伪造。
- **跨协议转换**：不能把一个平台的私有 thinking 凭证伪造成另一个平台的 thinking 凭证，例如不能把 Claude `signature` 伪造成 Gemini `thoughtSignature`，不能把 OpenAI `encrypted_content` 伪造成 Claude `thinking`。
- **明文 thinking**：只有目标协议允许把历史明文推理作为普通文本或明文 reasoning 字段回传时，才可转换；否则必须舍去。
- **加密/签名 thinking**：签名、加密推理、thought signature 等只对原平台有效，跨协议必须舍去，只可保存在源 assistant 的 `provider_extras.raw` 供同协议回灌。

### 3. 缓存按目标协议隔离

缓存不是统一消息语义，不能放进公共 content 层。缓存是目标协议请求构造策略：

- **自动缓存平台**：客户端只需要保持稳定前缀，读取 usage 中的 cached tokens 指标。
- **显式缓存平台**：客户端必须按目标协议要求创建/引用缓存或打 `cache_control` 断点。
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
- `system` 和 `tools` 是稳定静态前缀，应该单独打缓存断点。
- `messages` 是动态增长部分，通常在最新消息末尾附近打一个动态断点。
- 如果距离上一次缓存锚点新增 content block 超过约 20 个，应在旧锚点和新末尾之间规划额外断点，但总断点数不能超过 4。

实现含义：

- `cache_control` 是请求时动态字段，不写入 `provider_extras.raw`。
- 不能只保存渲染后 `messages[]` 下标作为长期缓存状态；多协议穿插、首条占位、消息边界策略变化都会让下标失效。
- 应记录最近 Claude assistant 在统一 `Conversation.messages` 中的语义位置，并在渲染时映射到 Claude 请求内容块。

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

### 3. 缓存规则

OpenAI prompt caching 主要是自动/服务端 best-effort 前缀缓存，客户端不打显式 content block 断点。

规则：

- 客户端应保持尽量稳定的请求前缀。
- `prompt_cache_key` 用于帮助 OpenAI 把相似请求路由到更可能命中缓存的位置，但不是强一致 sticky session，也不保证必定命中。
- 缓存命中情况通过 usage 中的 cached tokens 指标观察。
- Responses API 相对 Chat Completions 对多轮推理和工具上下文有更好的缓存利用潜力，尤其在保留 raw reasoning/function_call items 时。

实现含义：

- AgentRuntime stateless Responses 路径应使用 `conversation.id` 作为稳定 `prompt_cache_key`。
- 不需要也不能像 Claude 那样在 content block 上注入 `cache_control`。

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

OpenAI Chat Completions 的 prompt caching 是服务端自动前缀缓存，不需要客户端显式断点。

规则：

- 客户端保持稳定消息前缀即可。
- 不支持 Claude 风格 `cache_control`。
- 缓存命中通常通过 usage 里的 cached token 统计或服务商扩展字段观察。
- 若需要保留推理状态和更好的多轮 reasoning/cache 效果，应优先使用 OpenAI Responses，而不是 Chat Completions。

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
- `clear_thinking=true` 表示本次请求清理历史 reasoning_content，客户端应移除历史 assistant raw 中的 reasoning_content，只保留用户/assistant 可见文本、tool_calls、tool results 等非 reasoning 上下文。
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

## 七、统一消息模型落地要求

### 1. assistant 消息必须双层存储

每条 assistant `UniMessage` 必须保留：

- 公共语义层 `content[]`：至少包括 `TextPart`、`ToolUsePart`，可包括 `ThinkingPart` 作为 UI/调试/可选降级信息。
- 平台私有层 `provider_extras[api_format].raw`：保存同协议下轮必须原样回灌的原始响应结构。

### 2. 渲染目标协议时按目标规则决策

目标 adapter 渲染 assistant 历史时，应按以下顺序判断：

1. **同协议 raw 可用**：优先原样回灌 raw，保留目标协议 thinking/cache/tool 状态。
2. **跨协议公共语义转换**：用公共 `text` 和 `tool_use` 重建目标协议 assistant。
3. **thinking 转换策略**：只在目标协议允许、源 thinking 是明文、产品配置显式允许时转换；否则舍去。
4. **缓存策略**：由目标协议请求客户端或 adapter 在请求期添加，不污染 raw。

### 3. 工具调用映射要求

- 工具调用 ID 是跨消息关联的关键字段，必须进入 `ToolUsePart.tool_use_id` 或等价引用。
- tool result 必须引用原 assistant tool use。
- 目标协议只支持函数名关联时，需要维护 `tool_use_ref -> tool_name` 映射。
- 并行工具调用协议必须保留协议要求的顺序，例如 Gemini functionCall parts 先于 functionResponse parts。

### 4. 不允许的实现

- 不允许把其它协议的签名/加密 thinking 伪造成目标协议 thinking。
- 不允许跨协议时丢失 assistant 文本或工具调用。
- 不允许把缓存字段写入持久 raw。
- 不允许用单一“最近一次 thinking 签名”模型套所有平台。
- 不允许为了猜测服务端内部处理而在客户端隐式合并目标协议允许保留的消息边界。

## 八、当前实现差距检查清单

后续修改代码时按此清单逐项检查：

- Claude：是否同协议 raw 回灌 thinking/signature/redacted_thinking；是否不合并客户端消息边界；是否 system/tools/messages 分离缓存断点；是否记录统一消息 index 作为缓存锚点。
- OpenAI Responses：是否 stateless store=false；是否 include `reasoning.encrypted_content`；是否 raw 回灌 reasoning/function_call/message output items；是否用 `prompt_cache_key`。
- OpenAI Chat Completions：是否只承诺 content/tool_calls 公共语义；是否避免把非标准 reasoning_content 当 OpenAI 官方通用规则。
- Gemini：是否 raw 回灌 parts 和 thoughtSignature；是否正确处理 functionCall/functionResponse 顺序；是否区分 implicit caching 与 explicit cachedContent。
- Z.AI：是否根据 `thinking.clear_thinking` 决定是否回灌历史 reasoning_content；是否记录 cached_tokens；是否不把 GLM reasoning_content 硬套到其它协议。

## 九、资料来源

已使用/交叉参考的资料：

- 本地忽略资料：`doc/claude_api.md`、`doc/openai_resposne_api.md`、`doc/gemini_generating_api.md`、`doc/api大小限制规则.md`。
- Anthropic 官方文档搜索结果：Extended Thinking、Prompt Caching。网络读取官方页面时发生 TLS/重定向问题，但搜索摘要明确包含 extended thinking、prompt caching、4 breakpoints、约 20 content blocks lookback 等规则。
- OpenAI 官方文档搜索结果：Reasoning models、Responses API、Prompt Caching。平台页面读取被拒绝，但本地 `openai_resposne_api.md` 已包含 Responses API reference 关键字段。
- Google 官方文档：Gemini Context Caching；Vertex/Gemini Thought Signatures。
- Z.AI 官方文档：Chat Completion；Thinking Mode。
