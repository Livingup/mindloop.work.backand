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
- item id / call id
- 具体 token 数值
- runtime 内部包装事件

---

# GPT-5.5

协议：OpenAI Responses API

## 真实流式顺序

```text
response.created
response.in_progress
response.output_item.added        # reasoning item 开始
response.output_item.done         # reasoning item 完成
response.output_item.added        # message item 开始
response.content_part.added       # message content part 开始
response.output_text.delta        # 普通文本增量，可重复
response.output_text.done         # 普通文本完成
response.content_part.done        # message content part 完成
response.output_item.done         # message item 完成
response.output_item.added        # 第 1 个 function_call item 开始
response.function_call_arguments.delta  # 第 1 个 function_call 参数增量，可重复
response.function_call_arguments.done   # 第 1 个 function_call 参数完成
response.output_item.done         # 第 1 个 function_call item 完成
response.output_item.added        # 第 2 个 function_call item 开始
response.function_call_arguments.delta  # 第 2 个 function_call 参数增量，可重复
response.function_call_arguments.done   # 第 2 个 function_call 参数完成
response.output_item.done         # 第 2 个 function_call item 完成
response.completed + response.usage  # assistant response 完成；携带最终 response 与 token usage
[DONE]                            # SSE 传输结束标记，如 provider 发送
```

## 节点含义

| 事件名称 | 节点含义 |
| --- | --- |
| `response.created` | response 对象创建 |
| `response.in_progress` | response 进入生成中 |
| `response.output_item.added` | 一个 output item 开始，可能是 `reasoning`、`message` 或 `function_call` |
| `response.output_item.done` | 一个 output item 完成 |
| `response.content_part.added` | message 的一个 content part 开始 |
| `response.output_text.delta` | 普通文本增量，可重复 |
| `response.output_text.done` | 普通文本完成 |
| `response.content_part.done` | message 的一个 content part 完成 |
| `response.function_call_arguments.delta` | function_call 参数增量，可重复 |
| `response.function_call_arguments.done` | function_call 参数完成 |
| `response.completed` | 整个 assistant response 完成；最终 `response` 对象在该事件中完成，`response.usage` 在该事件上承载 |
| `[DONE]` | SSE 传输结束标记；不是业务 output item |

## 多工具调用结构

多个工具调用不是同一个事件内的数组节点，而是多个独立的 `function_call` output item：

```text
response.output_item.added        # function_call #1
response.function_call_arguments.delta
response.function_call_arguments.done
response.output_item.done

response.output_item.added        # function_call #2
response.function_call_arguments.delta
response.function_call_arguments.done
response.output_item.done
```

---

# DeepSeek v4

协议：OpenAI Chat Completions 兼容流式协议

## 真实流式顺序

DeepSeek v4 的 SSE 事件名基本都是 `message`，节点语义由 `choices[0].delta` 字段决定。

```text
message                         # assistant role / 初始 delta
message + delta.reasoning_content  # thinking 增量，可重复
message + delta.content            # 普通 content 增量，可重复
message + delta.tool_calls         # tool_call #1 / #2 增量，可重复，按 index 区分多个工具
message + finish_reason=tool_calls # assistant response 完成，结束原因是工具调用
message + usage                    # token usage 统计 chunk，通常在结束阶段出现
[DONE]                             # SSE 传输结束标记，如 provider 发送
```

## 节点含义

| 事件名称 / 字段 | 节点含义 |
| --- | --- |
| `message` | Chat Completions 的通用 SSE 事件名 |
| `delta.role` | assistant message 初始化 |
| `delta.reasoning_content` | thinking 增量，可重复 |
| `delta.content` | 普通 content 增量，可重复 |
| `delta.tool_calls` | tool call 增量，可重复；通过 `index` 区分多个工具调用 |
| `finish_reason=tool_calls` | assistant response 完成，下一步需要执行工具 |
| `usage` | token usage 统计；可能随结束 chunk 返回，也可能作为无 `choices` 的最终 usage chunk 返回 |
| `[DONE]` | SSE 传输结束标记；不是 Chat Completions choice |

## 多工具调用结构

多个工具调用通过同一类 `message + delta.tool_calls` 事件流承载，按 `tool_calls[].index` 聚合：

```text
message + delta.tool_calls[index=0]  # 第 1 个 tool_call 开始/增量
message + delta.tool_calls[index=0]  # 第 1 个 tool_call 参数增量，可重复
message + delta.tool_calls[index=1]  # 第 2 个 tool_call 开始/增量
message + delta.tool_calls[index=1]  # 第 2 个 tool_call 参数增量，可重复
message + finish_reason=tool_calls   # 所有 tool_call 增量结束
message + usage                      # token usage 统计
[DONE]                               # SSE 传输结束标记
```
