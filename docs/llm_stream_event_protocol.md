# LLM 流式事件协议

## 范围

本文只记录 provider 原始流式事件的事件名称与节点含义。

场景：一次 assistant response 同时包含：

1. thinking / reasoning
2. 普通 content / message
3. 多个 tool / function_call

不记录：

- 具体文本内容
- tool arguments 内容
- 具体 item id / call id 值
- 具体 token 数值
- runtime 内部包装事件

下文 `[]` 中内容不是事件名的一部分，而是区分同名事件节点的真实判别字段。

---

# 后端统一语义事件

前端不直接消费 provider 原始事件。后端必须把原始事件转换成以下统一语义事件：

```text
llm.response.started
llm.thinking.started
llm.thinking.completed
llm.content.started
llm.content.delta
llm.content.completed
llm.tool_call.started
llm.tool_call.completed
llm.response.completed
```

`llm.thinking.delta` 不推给前端；前端只关心 thinking 是否开始和结束。

`llm.response.completed` 最终由 LLM 请求服务在 provider 响应完成、assistant message 写回 conversation 后发布。parser 可识别 provider 完成事件，但该语义事件不直接透传给前端，避免和最终落库完成事件重复。

## GPT-5.5 判定规则

| 前端语义节点 | 原始事件判定条件 | 说明 |
| --- | --- | --- |
| `llm.response.started` | 收到 SSE stream started / HTTP stream headers | LLM 开始有流式响应，不等第一个 token |
| `llm.thinking.started` | `response.output_item.added` + `item.type=reasoning` | reasoning item 开始 |
| `llm.thinking.completed` | `response.output_item.done` + `item.type=reasoning`；或已 thinking 后首次进入 `output_text` / `function_call` 阶段时兜底完成 | 前端不展示 reasoning 内容 |
| `llm.content.started` | 首个 `response.output_text.delta` | 普通文本开始流式输出 |
| `llm.content.delta` | 每个 `response.output_text.delta` | 前端实时追加普通文本 |
| `llm.content.completed` | `response.output_text.done` / `response.content_part.done`；或已 content 后首次进入 `function_call` 阶段时兜底完成 | 普通文本流结束 |
| `llm.tool_call.started` | `response.output_item.added` + `item.type=function_call` | 每个 function_call item 都单独开始；用 `output_index` / `item.id` / `call_id` 关联 |
| `llm.tool_call.completed` | `response.output_item.done` + `item.type=function_call` | 单个工具调用生成结束；参数可由之前的 `response.function_call_arguments.*` 聚合 |
| `llm.response.completed` | provider `response.completed` 后，runtime 完成 assistant message ingest | 整个 assistant response 完成；usage 在最终 response 上 |

GPT-5.5 的工具调用是多个独立 `function_call` output item，因此可以准确判断每个工具的开始和结束。

## DeepSeek v4 判定规则

| 前端语义节点 | 原始事件判定条件 | 说明 |
| --- | --- | --- |
| `llm.response.started` | 收到 SSE stream started / HTTP stream headers | LLM 开始有流式响应，不等第一个 token |
| `llm.thinking.started` | 首个 `message[choices[0].delta.reasoning_content]` | thinking 开始 |
| `llm.thinking.completed` | 已 thinking 后首次出现 `delta.content` / `delta.tool_calls` / `finish_reason` | Chat Completions 没有单独 thinking done 事件，只能根据阶段切换判断 |
| `llm.content.started` | 首个 `message[choices[0].delta.content]` | 普通文本开始流式输出 |
| `llm.content.delta` | 每个 `message[choices[0].delta.content]` | 前端实时追加普通文本 |
| `llm.content.completed` | 已 content 后首次出现 `delta.tool_calls` / `finish_reason` | Chat Completions 没有单独 content done 事件，只能根据阶段切换判断 |
| `llm.tool_call.started` | 某个 `tool_calls[].index` 首次出现 | 每个 index 对应一个工具调用；用 `index` / `id` 关联 |
| `llm.tool_call.completed` | `finish_reason=tool_calls` 时，对已出现的所有 `tool_calls[].index` 逐个补完成 | DeepSeek 兼容协议没有单个 tool_call done 事件，不能在协议层精确知道某个工具何时结束，只能在工具调用流整体结束时按 index 完成 |
| `llm.response.completed` | provider `finish_reason` 非空后，runtime 完成 assistant message ingest；usage chunk 只补 token 统计 | assistant response 完成 |

DeepSeek v4 的工具调用增量都挂在 `message[choices[0].delta.tool_calls]` 下。协议没有 GPT-5.5 那种 `function_call_arguments.done` / `output_item.done[item.type=function_call]`，所以“一个工具生成结束”只能由后端状态机在整体 `finish_reason=tool_calls` 到达时补齐，不能从 provider 原始事件中逐个精确读取。

## GLM 5.1 官方判定规则

请求条件：

```text
model=glm-5.1
stream=true
thinking={"type":"enabled","clear_thinking":false}
tool_stream=true               # 使用工具流式输出时需要
```

`clear_thinking=false` 是 Preserved Thinking，用于保留历史 turns 的 `reasoning_content`；它不改变当前 turn 是否产生 thinking。GLM 5.1 当前 turn 在实测中会流式返回 `delta.reasoning_content`。

| 前端语义节点 | 原始事件判定条件 | 说明 |
| --- | --- | --- |
| `llm.response.started` | 收到 SSE stream started / HTTP stream headers | LLM 开始有流式响应，不等第一个 token |
| `llm.thinking.started` | 首个 `message[choices[0].delta.reasoning_content]` | thinking 开始 |
| `llm.thinking.completed` | 已 thinking 后首次出现 `delta.content` / `delta.tool_calls` / `finish_reason` | GLM 5.1 Chat Completions 没有单独 thinking done 事件，只能根据阶段切换判断 |
| `llm.content.started` | 首个 `message[choices[0].delta.content]` | 普通文本开始流式输出 |
| `llm.content.delta` | 每个 `message[choices[0].delta.content]` | 前端实时追加普通文本 |
| `llm.content.completed` | 已 content 后首次出现 `delta.tool_calls` / `finish_reason` | 没有单独 content done 事件，只能根据阶段切换判断 |
| `llm.tool_call.started` | 某个 `tool_calls[].index` 首次出现 | 每个 index 对应一个工具调用；首个 chunk 通常携带 `id`、`type`、`function.name` 和参数片段 |
| `llm.tool_call.completed` | `finish_reason=tool_calls` 时，对已出现的所有 `tool_calls[].index` 逐个补完成 | GLM 5.1 工具流式没有单个 tool_call done 事件，只能在工具调用流整体结束时按 index 完成 |
| `llm.response.completed` | provider `finish_reason` 非空后，runtime 完成 assistant message ingest；usage 在同一个 finish chunk 上 | assistant response 完成 |

GLM 5.1 官方实测流式顺序是 `reasoning_content -> content -> tool_calls[index=0] -> tool_calls[index=1] -> finish_reason=tool_calls + usage -> [DONE]`。`choices[0].delta.role` 可能在多数字段 chunk 中重复出现，不能作为阶段边界。

---

# GPT-5.5

协议：OpenAI Responses API

## 真实流式顺序

```text
response.created
response.in_progress
response.output_item.added[output_index=0,item.type=reasoning]          # reasoning item 开始
response.output_item.done[output_index=0,item.type=reasoning]           # reasoning item 完成
response.output_item.added[output_index=1,item.type=message]            # message item 开始
response.content_part.added[output_index=1,content_index=0,part.type=output_text]  # message 文本 part 开始
response.output_text.delta[output_index=1,content_index=0]              # 普通文本增量，可重复
response.output_text.done[output_index=1,content_index=0]               # 普通文本完成
response.content_part.done[output_index=1,content_index=0,part.type=output_text]   # message 文本 part 完成
response.output_item.done[output_index=1,item.type=message]             # message item 完成
response.output_item.added[output_index=2,item.type=function_call]      # 第 1 个 function_call item 开始
response.function_call_arguments.delta[output_index=2,item_id=<function_call item id>]  # 第 1 个 function_call 参数增量，可重复
response.function_call_arguments.done[output_index=2,item_id=<function_call item id>]   # 第 1 个 function_call 参数完成
response.output_item.done[output_index=2,item.type=function_call]       # 第 1 个 function_call item 完成
response.output_item.added[output_index=3,item.type=function_call]      # 第 2 个 function_call item 开始
response.function_call_arguments.delta[output_index=3,item_id=<function_call item id>]  # 第 2 个 function_call 参数增量，可重复
response.function_call_arguments.done[output_index=3,item_id=<function_call item id>]   # 第 2 个 function_call 参数完成
response.output_item.done[output_index=3,item.type=function_call]       # 第 2 个 function_call item 完成
response.completed[response.usage]                                     # assistant response 完成；携带最终 response 与 token usage
[DONE]                                                                 # SSE 传输结束标记，如 provider 发送
```

## 节点含义

| 事件名称 | 判别字段 | 节点含义 |
| --- | --- | --- |
| `response.created` | - | response 对象创建 |
| `response.in_progress` | - | response 进入生成中 |
| `response.output_item.added` | `output_index` + `item.type=reasoning` | reasoning item 开始 |
| `response.output_item.done` | `output_index` + `item.type=reasoning` | reasoning item 完成 |
| `response.output_item.added` | `output_index` + `item.type=message` | message item 开始 |
| `response.content_part.added` | `output_index` + `content_index` + `part.type=output_text` | message 文本 part 开始 |
| `response.output_text.delta` | `output_index` + `content_index` | 普通文本增量，可重复 |
| `response.output_text.done` | `output_index` + `content_index` | 普通文本完成 |
| `response.content_part.done` | `output_index` + `content_index` + `part.type=output_text` | message 文本 part 完成 |
| `response.output_item.done` | `output_index` + `item.type=message` | message item 完成 |
| `response.output_item.added` | `output_index` + `item.type=function_call` | 一个 function_call item 开始；多个工具调用对应多个不同 `output_index` |
| `response.function_call_arguments.delta` | `output_index` + `item_id` | function_call 参数增量，可重复 |
| `response.function_call_arguments.done` | `output_index` + `item_id` | function_call 参数完成 |
| `response.output_item.done` | `output_index` + `item.type=function_call` | 一个 function_call item 完成 |
| `response.completed` | `response.usage` | 整个 assistant response 完成；token usage 在最终 response 上承载 |
| `[DONE]` | - | SSE 传输结束标记；不是业务 output item |

## 多工具调用结构

多个工具调用不是同一个事件内的数组节点，而是多个独立的 `function_call` output item：

```text
response.output_item.added[output_index=2,item.type=function_call]
response.function_call_arguments.delta[output_index=2,item_id=<function_call item id>]
response.function_call_arguments.done[output_index=2,item_id=<function_call item id>]
response.output_item.done[output_index=2,item.type=function_call]

response.output_item.added[output_index=3,item.type=function_call]
response.function_call_arguments.delta[output_index=3,item_id=<function_call item id>]
response.function_call_arguments.done[output_index=3,item_id=<function_call item id>]
response.output_item.done[output_index=3,item.type=function_call]
```

---

# DeepSeek v4

协议：OpenAI Chat Completions 兼容流式协议

## 真实流式顺序

DeepSeek v4 的 SSE 事件名基本都是 `message`，节点语义由 `choices[0].delta` 字段决定。

```text
message[choices[0].delta.role]                         # assistant role / 初始 delta
message[choices[0].delta.reasoning_content]            # thinking 增量，可重复
message[choices[0].delta.content]                      # 普通 content 增量，可重复
message[choices[0].delta.tool_calls[index=0]]          # 第 1 个 tool_call 增量，可重复
message[choices[0].delta.tool_calls[index=1]]          # 第 2 个 tool_call 增量，可重复
message[choices[0].finish_reason=tool_calls]           # assistant response 完成，结束原因是工具调用
message[usage]                                         # token usage 统计 chunk，通常在结束阶段出现
[DONE]                                                 # SSE 传输结束标记，如 provider 发送
```

## 节点含义

| 事件名称 | 判别字段 | 节点含义 |
| --- | --- | --- |
| `message` | `choices[0].delta.role` | assistant message 初始化 |
| `message` | `choices[0].delta.reasoning_content` | thinking 增量，可重复 |
| `message` | `choices[0].delta.content` | 普通 content 增量，可重复 |
| `message` | `choices[0].delta.tool_calls[].index` | tool call 增量，可重复；通过 `index` 区分多个工具调用 |
| `message` | `choices[0].finish_reason=tool_calls` | assistant response 完成，下一步需要执行工具 |
| `message` | `usage` | token usage 统计；可能随结束 chunk 返回，也可能作为无 `choices` 的最终 usage chunk 返回 |
| `[DONE]` | - | SSE 传输结束标记；不是 Chat Completions choice |

## 多工具调用结构

多个工具调用通过同一类 `message[choices[0].delta.tool_calls]` 事件流承载，按 `tool_calls[].index` 聚合：

```text
message[choices[0].delta.tool_calls[index=0]]  # 第 1 个 tool_call 开始/增量
message[choices[0].delta.tool_calls[index=0]]  # 第 1 个 tool_call 参数增量，可重复
message[choices[0].delta.tool_calls[index=1]]  # 第 2 个 tool_call 开始/增量
message[choices[0].delta.tool_calls[index=1]]  # 第 2 个 tool_call 参数增量，可重复
message[choices[0].finish_reason=tool_calls]   # 所有 tool_call 增量结束
message[usage]                                  # token usage 统计
[DONE]                                          # SSE 传输结束标记
```

---

# GLM 5.1 官方

协议：OpenAI Chat Completions 兼容流式协议

Endpoint：`https://open.bigmodel.cn/api/paas/v4/chat/completions`

## 真实流式顺序

实测请求参数：

```text
model=glm-5.1
stream=true
thinking={"type":"enabled","clear_thinking":false}
tool_stream=true
tool_choice=auto
parallel_tool_calls=true
```

GLM 5.1 的 SSE 事件名是 `message`，节点语义由 `choices[0].delta` 与 `choices[0].finish_reason` 判断。

```text
message[choices[0].delta.role][choices[0].delta.reasoning_content]  # thinking 增量，可重复；role 可能重复出现
message[choices[0].delta.role][choices[0].delta.content]            # 普通 content 增量，可重复；role 可能重复出现
message[choices[0].delta.tool_calls[index=0]]                       # 第 1 个 tool_call 增量，可重复
message[choices[0].delta.tool_calls[index=1]]                       # 第 2 个 tool_call 增量，可重复
message[choices[0].delta.role][choices[0].finish_reason=tool_calls][usage]  # assistant response 完成；携带 token usage
[DONE]                                                              # SSE 传输结束标记
```

## 节点含义

| 事件名称 | 判别字段 | 节点含义 |
| --- | --- | --- |
| `message` | `choices[0].delta.reasoning_content` | thinking 增量，可重复 |
| `message` | `choices[0].delta.content` | 普通 content 增量，可重复 |
| `message` | `choices[0].delta.tool_calls[].index` | tool call 增量，可重复；通过 `index` 区分多个工具调用 |
| `message` | `choices[0].finish_reason=tool_calls` | 工具调用流整体结束，下一步需要执行工具 |
| `message` | `usage` | token usage 统计；实测随 `finish_reason` chunk 返回 |
| `[DONE]` | - | SSE 传输结束标记；不是 Chat Completions choice |

## 多工具调用结构

GLM 5.1 官方工具流式输出需要 `tool_stream=true`。多个工具调用通过 `tool_calls[].index` 聚合：

```text
message[choices[0].delta.tool_calls[index=0]]  # 第 1 个 tool_call 开始/增量，首个 chunk 通常带 id/name
message[choices[0].delta.tool_calls[index=0]]  # 第 1 个 tool_call 参数增量，可重复
message[choices[0].delta.tool_calls[index=1]]  # 第 2 个 tool_call 开始/增量，首个 chunk 通常带 id/name
message[choices[0].delta.tool_calls[index=1]]  # 第 2 个 tool_call 参数增量，可重复
message[choices[0].finish_reason=tool_calls][usage]  # 所有 tool_call 增量结束；携带 token usage
[DONE]                                          # SSE 传输结束标记
```

GLM 5.1 官方协议没有单个 tool_call done 事件；后端只能在 `finish_reason=tool_calls` 时对已见 `tool_calls[].index` 批量补 `llm.tool_call.completed`。
