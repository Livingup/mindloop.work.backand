# 上下文压缩多 Worker 设计草案

## 1. 设计目标

本文档描述 Agent Runtime 在上下文窗口接近压缩阈值时，如何使用多方向并发 Worker 对历史会话进行高质量压缩，并将压缩结果汇编为一份可恢复任务状态的 Markdown 文档。

核心目标：

- 将较早历史会话压缩为结构化任务恢复文档。
- 保留用户需求、硬约束、任务进度、关键代码事实、重要发现、风险与待办。
- 避免单次 LLM 输出过长导致截断、遗漏、质量下降。
- 避免压缩 Worker 误执行历史任务。
- 压缩完成后由 Runtime 将最终 Markdown 作为运行时生成的 user message 放回原 conversation 开头，并保留最近 raw tail。

## 2. 总体架构

上下文压缩不是一个普通 Agent loop，而是 Runtime 内部的受限压缩流水线。

```text
Agent Execution Loop
  -> ContextCompressionGate
  -> CompressionInputPlanner
  -> NativeMultimodalHistoryBuilder
  -> TranscriptSnapshotBuilder fallback
  -> FactExtractor
  -> Section Workers 并发压缩
  -> FinalAssembler
  -> Optional Verifier
  -> Replace Conversation.messages
```

默认 Worker 划分：

```text
1. RequirementConstraintWorker
2. ProgressPlanWorker
3. CodeArchitectureWorker
4. ObservationEvidenceWorker
5. DecisionRationaleWorker
6. TodoRiskWorker
7. FinalAssemblerWorker
8. Optional FinalVerifierWorker
```

这些 Worker 是受限的上下文提取 Worker：

- 不允许继续历史任务。
- 不消费新的用户消息。
- 不向原 conversation 追加自己的中间消息。
- 只通过上下文压缩专用工具写入压缩 job 的中间结果。

## 3. 压缩输入契约

Runtime 应由 `CompressionInputPlanner` 为每个 Section Worker 选择输入模式。优先模式是 `native_multimodal_history`，降级模式是 `transcript_snapshot`。

输入模式是 Runtime 程序端决策，不是 Worker 需要理解的任务概念。Runtime 必须根据已选择的输入模式选定一套具体的 Worker 可见 prompt；不要把程序内部字段名、模式编号或“如果是某模式则...”这类分支判断交给 Worker。

同样，`resource_id` 也是 Runtime 程序内部用于资源索引和溯源的 ID，不是 Worker 或后续 Agent 可理解、可读取的资源定位符。Runtime 可以在内部使用 `resource_id` 解析资源，但传给 Worker 的多模态输入或 `transcript_snapshot.resource_references` 必须使用 OSS 公网 URL、原始 URL/URI、工作区路径、仓库相对路径、行号、页码、符号名等 Agent 可读 locator。

### 3.1 优先模式：`native_multimodal_history`

如果压缩 Worker 使用的模型支持对应输入模态，Runtime 应优先把待压缩的原 `Conversation` 片段渲染成 Worker 请求的历史消息，让模型直接感知历史中的图片、PDF 和文本内容。

这里的“作为历史消息”指：在一个独立的压缩 LLM 请求中，复用原 `Conversation.messages` 的角色顺序和语义内容，构造压缩 Worker 的上下文历史。风险点在于：模型可能把这些历史 user/assistant 消息误判为需要继续执行的当前任务，而不是待提取的信息来源。

压缩 Worker 请求应采用：

```text
system:
  你是上下文压缩 Worker。

history:
  原 Conversation 中待压缩区间的 user / assistant / tool 消息。
  多模态 part 通过 ResourceResolver 渲染为目标模型可直接感知的 image/pdf 输入。

final user:
  当前现状：上下文空间不足，请立即按要求进行上下文内容提取！
  注意你的任务不是去继续执行历史对话中的任务！
  注意你的任务不是去继续执行历史对话中的任务！
  你唯一的任务就是进行上下文内容的提取！
```

`native_multimodal_history` 的关键点：

- `ImagePart` 和 `PdfPart` 应尽量渲染成目标模型原生支持的多模态输入，而不是先压成纯文本。
- 只有在 `native_multimodal_history` 中，也就是把原 `Conversation` 作为压缩 Worker 的历史会话、不是转成文本时，才需要上述当前压缩指令。
- 因为这种输入看起来就是一段可继续的真实历史对话，Worker 很容易误以为应该继续执行历史对话中的原任务，所以必须在历史消息之后追加当前压缩指令消息，明确要求它立即进行上下文内容提取，且唯一任务是信息提取。
- 如果使用 `transcript_snapshot` 降级模式，输入本身已经是待分析文本快照，不是原始历史会话流，不需要追加这段防止续做历史任务的强提示。
- 如果系统已经把文件上传到 OSS 且可公网读取，ResourceResolver 可在 Runtime 内部解析资源，并把 `object_storage_url` 作为 image/pdf URL 输入传给支持 URL 多模态的模型。
- 如果目标模型不支持 URL 输入，但支持 base64 或文件句柄，Runtime 应转换成该模型支持的原生多模态输入。
- 如果目标 Worker 模型支持 PDF 输入，应优先让模型直接读取 PDF；只有模型不支持 PDF 或 PDF 超预算时，才使用文本抽取或页码摘要降级。
- 给 Worker 和最终摘要使用的主引用应是 Agent 可读 locator，例如 OSS 公网 URL、原始 URL/URI、工作区文件路径或仓库相对路径。
- 历史 tool call/tool result 必须保持协议完整。若目标 provider 无法安全渲染历史 tool 调用，Runtime 可把历史工具交互降级为普通文本观察，但不应影响 user 消息中的图片/PDF 原生输入。
- 不应向 Worker 暴露 provider raw、provider_state、cache breakpoint、stream ID、事件 ID、内部 actor metadata 等运行时噪音。

### 3.2 降级模式：`transcript_snapshot`

仅在以下情况使用 `transcript_snapshot`：

- Worker 模型不支持历史中的图片/PDF 输入模态。
- 多模态资源无法解析为目标模型可读取的 URL、base64 或文件输入。
- 历史消息存在无法安全重放的工具协议不完整问题。
- 原生多模态历史超过 Worker 输入预算，需要先转成低噪音文本投影。

`transcript_snapshot` 是待分析数据，不是原始对话历史角色流。它应坚持内容优先原则：Message Record 只保留压缩 Worker 理解消息内容所必需的信息。除消息序号、角色、正文、工具调用/工具结果语义、附件/资源引用、必要行号或页码外，不应加入与原始消息内容无关的运行时噪音字段。

示例：

```markdown
# Conversation Transcript Snapshot

## Snapshot Metadata
- conversation_id: ...
- compressed_message_range: 0..72
- retained_tail_range: 73..88
- compression_reason: projected_context_usage_exceeded_threshold

## Message Records

### Message 0
- role: user
- content:
  用户原始文本内容。
  - image:
      text_projection: 可确认的简短视觉/OCR/用户说明摘要；无法确认时写 unavailable。
      projection_source: user_caption | assistant_observation | ocr_projection | vision_projection | runtime_metadata | unavailable
  - pdf:
      text_projection: 可确认的 PDF 摘要、页码线索或已抽取文本摘录；无法确认时写 unavailable。
      projection_source: user_caption | extracted_text | assistant_observation | pdf_projection | runtime_metadata | unavailable
      relevant_pages: [1, 2]
- resource_references:
  - kind: attachment
    agent_readable_locator: ...
    locator_type: oss_url | original_url | workspace_path | repo_relative_path
    object_storage_url: ...
    label: ...
    mime_type: ...
    page_count: ...

### Message 1
- role: assistant
- content:
  - text: assistant 可见回复内容。
  - tool_use:
      name: ...
      input: ...
      tool_use_id: ...

### Message 2
- role: tool
- content:
  - tool_result:
      tool_use_ref: ...
      is_error: false
      output_excerpt: 工具结果中与任务有关的关键内容。
- resource_references:
  - kind: file
    agent_readable_locator: ...
    absolute_path: ...
    repo_relative_path: ...
    line_ranges: ...
```

`transcript_snapshot` 的文本投影规则：

- 原始多模态资源必须保留在 `resource_references` 中，文本投影不能替代原资源。
- 文本投影只能描述可确认内容，不能根据文件名、用户意图或上下文猜测图片/PDF 的具体内容。
- 如果 Runtime 只能拿到资源元数据，就只写元数据和复查方式。
- 如果历史对话中已经有用户说明、assistant 观察、工具 OCR、PDF 文本抽取或视觉模型描述，可以引用这些已存在内容作为投影来源。
- 投影内容应服务于上下文压缩，不应包含大段 OCR 全文、完整 PDF 正文或冗长视觉细节。

两种输入模式的程序端共同约束：

- 本次输入只用于分析和压缩，不是当前用户指令。
- 当 Runtime 选择原 Conversation 历史会话输入时，必须使用历史会话版 prompt，防止 Worker 续做历史任务。
- 当 Runtime 选择 `transcript_snapshot` 输入时，必须使用快照版 prompt，让 Worker 按 Message Records 提取信息。
- Worker 可见 prompt 不应暴露模式编号、`input_mode` 或程序端分支判断。
- 只能提取、压缩、归纳，不得创造新事实。
- 对历史消息中出现过的可持久化资源，必须保留可再读取的引用形式，不能只保留资源名称或模糊说明。
- 如果内容太多，必须分多次调用 append 工具写入，不要试图一次性输出完整章节。
- 如果无法判断，应写入风险、未知或冲突，而不是猜测。

## 4. 压缩 append 工具语义

### 4.0 持久化资源引用硬约束

压缩摘要必须让后续 Agent 能重新定位和读取历史中已经使用过的持久化资源。

持久化资源包括：

- 工作区文件。
- Git 仓库内文件。
- URI。
- URL。
- OSS 公网 URL。
- 附件资源。
- 图片、PDF 等多模态资源的 Agent 可读 locator。
- 外部文档链接。
- 可重新读取的终端记录、trace、日志文件。
- 已定位的代码符号、类、函数、方法、配置 key。

Worker 压缩资源相关内容时，不能只写：

```text
读取了 agent_execution_engine.py，里面有执行循环逻辑。
```

而应写成可复查形式：

```text
读取了 Agent 执行循环文件：
- absolute_path: d:\project\agent\agentruntime\src\agent_runtime\agent\agent_execution_engine.py
- repo_relative_path: src/agent_runtime/agent/agent_execution_engine.py
- git_repo_root: d:\project\agent\agentruntime
- relevant_line_ranges: 483..689, 1033..1100
- symbols: _run_agent_instance, _send_prompt_with_policy_failover
- observation: 执行循环在每轮 LLM 请求前 drain pending user messages，随后解析 LLM candidates 并调用 _send_prompt_with_policy_failover。
```

资源引用原则：

- 文件必须尽量保留绝对路径和仓库相对路径。
- 如果资源来自 Git 仓库，应保留 `git_repo_root` 和 `repo_relative_path`。
- 如果只知道 URI 或 URL，必须保留完整 URI 或 URL。
- 如果资源是附件，必须优先保留 Agent 可读 locator，例如 `object_storage_url`、原始 URL、工作区路径或仓库相对路径，并尽量保留文件名、mime、页数、图片尺寸等元数据。
- Runtime 内部资源 ID 不能进入 Worker 可见资源引用，也不能作为后续 Agent 的主引用。
- 如果历史消息读取了文件的局部内容，应保留相关行号范围。
- 如果历史消息定位了代码符号，应保留符号名、所在文件和行号范围。
- 如果资源已被确认无法再次读取，应明确写入风险或未知，不能假装可读取。

### 4.1 `context_summary.append_section_items`

用途：向指定压缩章节追加结构化条目。

该工具是所有 Section Worker 的主要输出方式。Worker 不需要一次性输出完整章节；当内容较多时，应多次调用该工具逐步追加。

参数语义：

```json
{
  "job_id": "上下文压缩任务 ID",
  "section": "章节标识",
  "items": [
    {
      "priority": "P0 | P1 | P2",
      "status": "active | superseded | resolved | unknown",
      "content": "压缩后的事实、约束、进度、发现或待办内容",
      "source_message_ranges": [[0, 3]],
      "source_fact_ids": ["fact_001"],
      "entities": ["相关文件路径、函数名、类名、工具名、错误码等"],
      "resource_references": [
        {
          "kind": "file | uri | url | attachment | symbol | trace | terminal_record | other",
          "label": "资源的人类可读名称",
          "agent_readable_locator": "后续 Agent 可直接理解并尝试读取的主定位符，例如 OSS URL、原始 URL/URI、绝对路径、仓库相对路径；未知时为 null",
          "locator_type": "oss_url | original_url | uri | workspace_path | repo_relative_path | terminal_id | trace_path | other | unknown",
          "absolute_path": "本地绝对路径；未知时为 null",
          "repo_relative_path": "Git 仓库相对路径；未知时为 null",
          "git_repo_root": "Git 仓库根目录绝对路径；未知时为 null",
          "uri": "完整 URI；未知时为 null",
          "url": "完整 URL；未知时为 null",
          "object_storage_url": "OSS 公网读取 URL；未知或不适合暴露时为 null",
          "line_ranges": [[1, 20]],
          "symbol_names": ["相关类名、函数名、方法名、配置 key"],
          "access_method": "后续 Agent 应如何重新读取该资源",
          "observation": "该资源中与任务相关的已知结论"
        }
      ],
      "notes": "可选补充说明"
    }
  ]
}
```

字段说明：

- `job_id`：当前压缩任务 ID。
- `section`：目标章节，例如 `requirements_constraints`、`progress_plan`。
- `items`：追加的条目列表。
- `priority`：重要性。
  - `P0`：必须保留，影响任务继续执行或用户硬约束。
  - `P1`：应尽量保留，包含重要背景、设计原因、关键发现。
  - `P2`：可选保留，包含辅助过程、低价值背景。
- `status`：该条目的状态。
  - `active`：当前仍有效。
  - `superseded`：已被后续信息替代或否定。
  - `resolved`：问题已解决，但结论有保留价值。
  - `unknown`：不确定，需要后续确认。
- `content`：应是可直接汇编进最终任务 Markdown 的内容。
- `source_message_ranges`：该条目的原始依据消息范围。
- `source_fact_ids`：如果 FactExtractor 已生成事实 ID，则引用对应 ID。
- `entities`：文件路径、函数名、类名、命令、错误信息、URL、URI、资源 locator 等关键词。
- `resource_references`：该条目涉及的可持久化资源引用。凡是后续 Agent 可能需要复查的文件、URL、URI、附件、代码符号、trace 或终端记录，都必须在这里保留 Agent 可理解、可重新读取的定位信息。字段约束必须在工具 description 中直接明文说明，不能只依赖 JSON Schema。
- `notes`：仅用于汇编或校验的辅助说明。

行为要求：

- Worker 应按内容自然分组追加。
- 当一个章节内容较多时，Worker 应主动多次 append。
- 不设置固定条数或固定 token 上限，由 Worker 根据输出稳定性自行分批。
- 每个追加条目都应尽量带来源，便于后续校验。
- 涉及持久化资源的条目必须带 `resource_references`，并优先填写 `agent_readable_locator`，以便压缩后的 Agent 能重新读取原始文件、链接或附件。
- 不确定的内容必须标记为 `unknown`，不能伪装成确定事实。

### 4.2 `context_summary.mark_section_complete`

用途：声明某个 Worker 负责的章节已经完成。

参数语义：

```json
{
  "job_id": "上下文压缩任务 ID",
  "section": "章节标识",
  "completion_summary": "本章节覆盖范围说明",
  "known_gaps": ["仍可能遗漏或无法确认的信息"],
  "quality_notes": ["对后续汇编器或校验器的提醒"]
}
```

行为要求：

- Worker 只有在完成本方向分析后才能调用。
- 如果内容太多，应先多次 append，再 mark complete。
- 如果发现输入不足、来源冲突或预算明显不够，应在 `known_gaps` 中说明。

### 4.3 `context_summary.report_section_issue`

用途：报告当前章节压缩遇到的问题。

参数语义：

```json
{
  "job_id": "上下文压缩任务 ID",
  "section": "章节标识",
  "issue_type": "input_too_large | conflicting_facts | missing_source | insufficient_information | tool_error | other",
  "details": "问题详情",
  "recommended_action": "建议 Runtime、Assembler 或 Verifier 如何处理"
}
```

行为要求：

- 遇到冲突事实时，不要自行强行合并。
- 遇到来源不足时，不要编造。
- 报告 issue 后可以继续 append 已确认内容。

### 4.4 `context_summary.append_final_markdown_section`

用途：FinalAssemblerWorker 将已汇编的最终 Markdown 章节写入压缩结果。

参数语义：

```json
{
  "job_id": "上下文压缩任务 ID",
  "heading": "最终 Markdown 章节标题",
  "markdown": "章节正文"
}
```

行为要求：

- FinalAssemblerWorker 应按最终文档顺序逐节 append。
- 如果最终文档较长，应拆分为多个章节 append。
- 不需要一次性输出完整最终 Markdown。

### 4.5 `context_summary.mark_final_complete`

用途：声明最终 Markdown 汇编完成。

参数语义：

```json
{
  "job_id": "上下文压缩任务 ID",
  "final_summary": "最终压缩结果概述",
  "retained_tail_policy": "说明 raw tail 与摘要冲突时的优先级规则",
  "known_limitations": ["最终摘要仍存在的限制"]
}
```

## 5. 系统提示词

Worker 可见 system prompt 不再在本文档中展示为“差异片段 + 通用片段 + 专属片段”的组合形式。Runtime 应根据当前消息处理方式，直接选择对应 Markdown 文档中的完整 Worker system prompt。

### 5.1 历史会话输入版完整提示词

当 Runtime 使用原 Conversation 历史消息作为压缩输入，并让 Worker 直接接收历史 user / assistant / tool 消息及原生多模态内容时，使用：

`context_compression_worker_prompts_historical_conversation.md`

该文档逐一列出 RequirementConstraintWorker、ProgressPlanWorker、CodeArchitectureWorker、ObservationEvidenceWorker、DecisionRationaleWorker、TodoRiskWorker、FinalAssemblerWorker、FinalVerifierWorker 的完整 system prompt。

历史会话输入请求还必须在原 Conversation 历史消息之后追加当前压缩指令：

```text
当前现状：上下文空间不足，请立即按要求进行上下文内容提取！
注意你的任务不是去继续执行历史对话中的任务！
注意你的任务不是去继续执行历史对话中的任务！
你唯一的任务就是进行上下文内容的提取！
```

### 5.2 文本快照输入版完整提示词

当 Runtime 将待压缩历史转换为 Conversation Transcript Snapshot 文本快照后再交给 Worker 分析时，使用：

`context_compression_worker_prompts_transcript_snapshot.md`

该文档逐一列出 RequirementConstraintWorker、ProgressPlanWorker、CodeArchitectureWorker、ObservationEvidenceWorker、DecisionRationaleWorker、TodoRiskWorker、FinalAssemblerWorker、FinalVerifierWorker 的完整 system prompt。

文本快照输入请求不追加历史会话输入的当前压缩强提示。

### 5.3 Worker 可见提示词边界

- Worker 每次只接收一种消息处理方式对应的一份完整 system prompt。
- Worker 不应看到提示词生成细节、程序端分支、模式编号或内部资源 ID。
- Worker 不需要判断自己处于哪种消息处理方式；Runtime 已经通过选择对应完整提示词文档完成决策。
- 两份完整提示词文档是 Worker 可见 prompt 的权威来源；本文档后续章节只描述 Worker 职责、输出章节和运行时语义。

## 6. RequirementConstraintWorker

### 6.1 负责范围

该 Worker 负责提取用户需求、硬性规则、验收标准、偏好、禁止事项和长期约束。

包括：

- 原始任务目标。
- 用户明确说“必须”“不要”“以后都要”“除非我说”的规则。
- 用户对架构、实现方式、交互行为、提交、推送、测试、文档的要求。
- 用户否定过的方案。
- 与当前任务相关的持久偏好。

不包括：

- 代码实现细节。
- 命令输出细节。
- 具体执行进度。

### 6.2 完整系统提示词位置

- 历史会话输入版：`context_compression_worker_prompts_historical_conversation.md#requirementconstraintworker`
- 文本快照输入版：`context_compression_worker_prompts_transcript_snapshot.md#requirementconstraintworker`

### 6.3 输出章节

`requirements_constraints`

典型条目：

```json
{
  "priority": "P0",
  "status": "active",
  "content": "用户要求上下文压缩不能设计成单次 LLM 请求一次性输出完整压缩文档，因为输出内容较多时可能超出模型 API 限制并降低质量；应支持多次 append 或多方向并发分析。",
  "source_message_ranges": [[120, 123]],
  "entities": ["context compression", "append", "multi-worker"]
}
```

## 7. ProgressPlanWorker

### 7.1 负责范围

该 Worker 负责提取任务计划、执行进度、当前停顿点和下一步动作。

包括：

- 已完成的阶段。
- 正在进行的阶段。
- 还未完成的待办。
- 任务计划的变化。
- 是否已测试、已提交、已推送。
- 当前 Agent 应从哪里继续。

不包括：

- 深入代码架构事实。
- 用户长期规则。
- 详细命令输出。

### 7.2 完整系统提示词位置

- 历史会话输入版：`context_compression_worker_prompts_historical_conversation.md#progressplanworker`
- 文本快照输入版：`context_compression_worker_prompts_transcript_snapshot.md#progressplanworker`

### 7.3 输出章节

`progress_plan`

典型条目：

```json
{
  "priority": "P0",
  "status": "active",
  "content": "当前任务处于上下文压缩功能设计阶段，用户正在评审多 Worker 并发压缩方案，尚未进入代码实现。",
  "source_message_ranges": [[110, 124]],
  "entities": ["context compression", "multi-worker", "design review"]
}
```

## 8. CodeArchitectureWorker

### 8.1 负责范围

该 Worker 负责提取代码库结构、模块职责、数据流、调用链、不变量和文件级事实。

包括：

- 关键文件路径。
- 类名、函数名、模块名。
- 当前功能应接入的位置。
- Conversation、Agent loop、LLM request、repository 等核心数据流。
- 需要保持的协议完整性和领域不变量。
- 已修改文件和修改目的。

不包括：

- 用户语气偏好。
- 普通计划列表。
- 未经确认的代码推测。

### 8.2 完整系统提示词位置

- 历史会话输入版：`context_compression_worker_prompts_historical_conversation.md#codearchitectureworker`
- 文本快照输入版：`context_compression_worker_prompts_transcript_snapshot.md#codearchitectureworker`

### 8.3 输出章节

`code_architecture`

典型条目：

```json
{
  "priority": "P0",
  "status": "active",
  "content": "Agent 执行循环在 agent_execution_engine.py 的 _run_agent_instance 中按轮次 drain pending user messages、解析 LLM candidates、调用 _send_prompt_with_policy_failover、记录 LLM response、执行工具并保存 tool result；上下文压缩闸门应位于下一次 LLM 请求前。",
  "source_message_ranges": [[80, 95]],
  "entities": ["agent_execution_engine.py", "_run_agent_instance", "_send_prompt_with_policy_failover"]
}
```

## 9. ObservationEvidenceWorker

### 9.1 负责范围

该 Worker 负责提取工具观察、命令结果、错误、外部 API 事实和证据性信息。

包括：

- 搜索结果结论。
- 读取文件后的关键发现。
- 命令执行结果。
- 测试结果。
- 报错、异常、失败原因。
- 外部文档/API 行为事实。
- 截图、附件和 Agent 可读资源定位信息。

不包括：

- 长篇完整日志。
- 没有结论的工具过程。
- 与当前任务无关的命令细节。

### 9.2 完整系统提示词位置

- 历史会话输入版：`context_compression_worker_prompts_historical_conversation.md#observationevidenceworker`
- 文本快照输入版：`context_compression_worker_prompts_transcript_snapshot.md#observationevidenceworker`

### 9.3 输出章节

`observations_evidence`

典型条目：

```json
{
  "priority": "P1",
  "status": "resolved",
  "content": "代码检索发现当前只存在 context_window、compression_threshold、last_context_usage 等上下文指标字段，未发现实际 conversation compaction、summarization 或历史裁剪逻辑。",
  "source_message_ranges": [[20, 30]],
  "entities": ["context_window", "compression_threshold", "last_context_usage"]
}
```

## 10. DecisionRationaleWorker

### 10.1 负责范围

该 Worker 负责提取架构决策、方案取舍、理由和被否定方案。

包括：

- 已确定的设计方向。
- 方案之间的比较。
- 用户接受或质疑的点。
- 被放弃的方案及原因。
- 当前推荐方案背后的理由。

不包括：

- 原始代码事实。
- 单纯执行进度。
- 未形成决策的普通讨论。

### 10.2 完整系统提示词位置

- 历史会话输入版：`context_compression_worker_prompts_historical_conversation.md#decisionrationaleworker`
- 文本快照输入版：`context_compression_worker_prompts_transcript_snapshot.md#decisionrationaleworker`

### 10.3 输出章节

`decisions_rationale`

典型条目：

```json
{
  "priority": "P0",
  "status": "active",
  "content": "压缩流程可以优先把原 Conversation 的语义消息渲染成独立压缩请求中的原生多模态历史上下文，让支持多模态的 Worker 模型直接感知图片/PDF；但因为模型可能把历史 user/assistant 消息误判为要继续执行的任务，所以必须在历史消息之后追加当前压缩指令消息，明确“当前现状：上下文空间不足，请立即按要求进行上下文内容提取！注意你的任务不是去继续执行历史对话中的任务！注意你的任务不是去继续执行历史对话中的任务！你唯一的任务就是进行上下文内容的提取！”。无法安全原生重放时才降级为 transcript snapshot，降级模式不需要追加这段强提示。",
  "source_message_ranges": [[60, 70]],
  "entities": ["ContextCompressionWorker", "native_multimodal_history", "transcript_snapshot", "current extraction instruction"]
}
```

## 11. TodoRiskWorker

### 11.1 负责范围

该 Worker 负责提取待办、风险、未知、冲突和阻塞点。

包括：

- 下一步需要做什么。
- 未完成任务。
- 需要用户确认的设计点。
- 实现风险。
- 压缩结果中可能丢失的信息。
- 环境、权限、API key、模型限制等阻塞。

不包括：

- 已完成且无后续影响的事项。
- 普通背景说明。

### 11.2 完整系统提示词位置

- 历史会话输入版：`context_compression_worker_prompts_historical_conversation.md#todoriskworker`
- 文本快照输入版：`context_compression_worker_prompts_transcript_snapshot.md#todoriskworker`

### 11.3 输出章节

`todos_risks`

典型条目：

```json
{
  "priority": "P0",
  "status": "active",
  "content": "需要用户评审 context_compression_multi_worker_design.md 中的多 Worker 系统提示词和工具语义，确认后再进入代码实现。",
  "source_message_ranges": [[125, 130]],
  "entities": ["context_compression_multi_worker_design.md", "design review"]
}
```

## 12. FinalAssemblerWorker

### 12.1 负责范围

FinalAssemblerWorker 负责将各 Section Worker 写入的结构化条目汇编为最终 Markdown。

它不重新执行历史任务，也不修改代码。它只处理已经被 Section Worker 提取出的压缩条目，以及必要的 provenance metadata。

### 12.2 完整系统提示词位置

- 历史会话输入版：`context_compression_worker_prompts_historical_conversation.md#finalassemblerworker`
- 文本快照输入版：`context_compression_worker_prompts_transcript_snapshot.md#finalassemblerworker`

### 12.3 最终 Markdown 顺序

FinalAssemblerWorker 应按以下顺序输出：

```markdown
# Runtime Compressed Task Context

## 0. Usage Contract
## 1. Current Objective
## 2. User Requirements And Hard Constraints
## 3. Execution Plan And Progress
## 4. Current State
## 5. Codebase And Architecture Facts
## 6. Files Read Or Modified
## 7. Decisions And Rationale
## 8. Observations, Commands, Tool Results
## 9. Persistent Resource References
## 10. Pending Work
## 11. Risks, Unknowns, And Conflicts
## 12. Provenance
```

### 12.4 汇编规则

- P0 条目必须保留。
- active 条目优先于 superseded 条目。
- superseded 条目只能作为“已否定方案/历史变更”保留。
- resolved 条目应保留结论，不保留完整过程。
- unknown 条目应进入风险或未知章节。
- 如果同一事实在多个章节重复，只保留在最合适的位置，其他章节交叉引用或删除。
- 如果 raw tail 会保留某些最近细节，最终摘要可以压缩表达这些最近细节。
- 文件路径、函数名、类名、错误信息、命令、Agent 可读资源 locator 应尽量保持精确。
- 最终 Markdown 必须包含 `Persistent Resource References` 章节，汇总后续 Agent 可能需要重新读取的文件、URL、URI、附件、OSS 公网 URL、trace、终端记录和代码符号。每个资源应包含可复查定位信息和与任务相关的 observation。

## 13. FinalVerifierWorker

### 13.1 负责范围

FinalVerifierWorker 是可选 Worker，用于检查最终 Markdown 的质量。

它不生成最终摘要，只报告问题和建议。

### 13.2 完整系统提示词位置

- 历史会话输入版：`context_compression_worker_prompts_historical_conversation.md#finalverifierworker`
- 文本快照输入版：`context_compression_worker_prompts_transcript_snapshot.md#finalverifierworker`

### 13.3 校验输出语义

Verifier 可以使用 `context_summary.report_section_issue` 报告：

- `missing_p0_requirement`
- `unsupported_fact`
- `conflicting_summary`
- `insufficient_next_steps`
- `over_compressed_code_context`
- `missing_rereadable_resource_reference`
- `too_verbose`
- `other`

如果通过校验，Runtime 可记录：

```json
{
  "verification_status": "passed",
  "notes": "最终压缩文档可用于替换历史上下文。"
}
```

## 14. Runtime 替换策略

压缩完成后，Runtime 构造新的 conversation messages：

```text
[
  synthetic user message: final Runtime Compressed Task Context Markdown,
  ...retained raw tail messages
]
```

替换规则：

- `conversation_id` 保持不变。
- `task_id` 和 `agent_instance_id` 保持不变。
- `provider_state` 清空，因为 message index 和 provider cache 状态已失效。
- `conversation.state` 根据新 messages 重算。
- 压缩摘要的 user message 必须带运行时生成标记。
- 最近 raw tail 中的信息优先于压缩摘要。
- 压缩摘要必须保留可再读取的资源引用，确保后续 Agent 能重新打开历史中用过的文件、URL、URI、附件或代码符号。

## 15. 失败处理

如果 Section Worker 失败：

- Runtime 可重试该章节。
- 如果该章节不是 P0 关键章节，可降级为在风险中记录缺失。
- 如果 P0 章节失败，应暂停 Agent，不继续请求 LLM。

如果 FinalAssembler 失败：

- 不替换 conversation。
- 保持原 conversation 完整状态。
- 暂停 Agent 并报告 `context_compression_failed`。

如果 Verifier 失败：

- 可局部重跑相关 Section Worker。
- 可要求 FinalAssembler 重新汇编。
- 多次失败后暂停 Agent。

## 16. 设计原则总结

- 压缩 Worker 不是普通业务 Agent。
- 原历史是待分析数据，不是当前指令。
- 多方向 Worker 并发提取，降低单次输出过长风险。
- 所有 Worker 通过 append 工具增量写入，不要求一次性完整输出。
- 不写死每次 append 的条数或 token 限制，由提示词要求模型在内容较多时自行分批。
- 持久化资源必须以可再读取形式保留，不能只保留文件名、资源名或模糊摘要。
- Runtime 统一汇编和替换 conversation，Worker 不直接修改原会话。
- 最终产物是一份完整任务恢复 Markdown 加最近 raw tail，而不是普通聊天摘要。
