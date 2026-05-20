# 上下文压缩多 Worker 设计草案

## 1. 设计目标

本文档描述 Agent Runtime 在上下文窗口接近压缩阈值时，如何使用多方向并发 Worker 对历史会话进行高质量压缩，并将压缩结果汇编为一份可恢复任务状态的 Markdown 文档。

核心目标：

- 将较早历史会话压缩为结构化任务恢复文档。
- 保留用户需求、硬约束、任务进度、关键代码事实、重要发现、风险与待办。
- 避免单次 LLM 输出过长导致截断、遗漏、质量下降。
- 避免压缩 Worker 误执行历史任务。
- 压缩完成后由 Runtime 新建一个压缩后的 Conversation，将最终 Markdown 作为运行时生成的 user message 放入新 Conversation 开头，并保留最近 raw tail。

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
  -> Create Compressed Conversation
  -> Rebind AgentInstance to New Conversation
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
- 如果内容太多，必须分多次调用 append 工具写入，而不是一次性输出完整章节。
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
  "notes": "最终压缩文档可用于创建压缩后的新 Conversation。"
}
```

## 14. Runtime 新建压缩 Conversation 策略

压缩完成后，Runtime 不覆盖旧 Conversation，而是构造一个新的压缩后 Conversation：

```text
new Conversation.messages = [
  synthetic user message: final Runtime Compressed Task Context Markdown,
  ...retained raw tail messages
]
```

新建规则：

- 旧 `source_conversation_id` 保持不变，旧 Conversation 继续作为压缩前完整历史持久化记录存在。
- 新建 `target_conversation_id`，新 Conversation 绑定同一个 `task_id`、`thread_id`、`agent_id` 和 `agent_instance_id`。
- 当前 AgentInstance 的 `conversation_id` 更新为新的 `target_conversation_id`。
- 新 Conversation 的 `provider_state` 为空，因为 message index 和 provider cache 状态不能跨压缩后的新消息窗口继承。
- 新 Conversation 的 `conversation.state` 根据新 messages 重算。
- 压缩摘要的 user message 必须带运行时生成标记。
- 最近 raw tail 中的信息优先于压缩摘要。
- 压缩摘要必须保留可再读取的资源引用，确保后续 Agent 能重新打开历史中用过的文件、URL、URI、附件或代码符号。
- 压缩归档必须记录 `source_conversation_id`、`target_conversation_id`、`root_conversation_id` 和 `compression_index`，用于多次压缩后的有序查询与恢复。

## 15. 当前系统接入设计

本节描述上下文压缩如何接入当前 `agentruntime` 代码结构，目标是让后续实现可以直接按模块落地。

### 15.1 当前代码中的权威接入点

当前 Agent 主循环位于：

```text
src/agent_runtime/agent/agent_execution_engine.py
```

关键入口：

- `_run_agent_instance`
  - 每轮循环先调用 `_drain_pending_user_messages_to_conversation`，把待处理用户消息追加到 `Conversation.messages`。
  - 随后调用 `_resolve_instance_llm_candidates` 获取当前 Agent 实例的 LLM 候选模型。
  - 再调用 `_send_prompt_with_policy_failover` 发送主 Agent LLM 请求。
  - LLM 响应后调用 `_record_last_context_usage` 把本次请求 token usage 写入 `conversation.state.last_context_usage`。
- `_send_prompt_with_policy_failover`
  - 遍历候选模型。
  - 渲染 Agent system prompt。
  - 构建业务工具 schema。
  - 调用 `LlmRequestService.send_prompt`。
- `Conversation`
  - `messages` 是 LLM 历史真相源。
  - `provider_state` 保存 provider cache / thinking / prompt cache 等协议状态。
  - `state.last_context_usage` 保存最新 LLM 请求 token usage 快照。
- `SqliteConversationRepository.create`
  - 可为压缩结果创建新的 Conversation 元数据和 JSONL 消息文件。
- `AgentRuntimeGateway.bind_agent_instance_conversation`
  - 可将当前 AgentInstance 重新绑定到压缩后的新 `target_conversation_id`。

因此，上下文压缩闸门应插入 `_run_agent_instance` 中每轮主 Agent LLM 请求之前：

```text
while True:
  drain pending user messages into conversation
  resolve LLM candidates
  load agent definition
  maybe run context compression, create new conversation, and rebind instance
  send main Agent LLM prompt
  record last_context_usage
  execute tools if any
```

压缩不能插入 LLM 流式响应中，也不能插入 assistant tool_use 与 tool_result 之间。

### 15.2 新增运行时模块

建议新增包：

```text
src/agent_runtime/context_compression/
  __init__.py
  config.py
  models.py
  gate.py
  range_planner.py
  input_planner.py
  transcript_snapshot.py
  prompt_loader.py
  job_store.py
  summary_tools.py
  worker_runner.py
  final_assembler.py
  settings.py
  settings_repository.py
  concurrency_limiter.py
  conversation_rewriter.py
  compression_service.py
  north_gateway/
    context_compression_rest_adapter.py
```

模块职责：

- `config.py`
  - 定义 `ContextCompressionConfig`。
  - 包含 enabled、默认阈值比例、最小可压缩消息数、raw tail 保留策略、Worker 模型选择策略、是否启用 verifier、失败策略等。
- `models.py`
  - 定义 `CompressionJob`、`CompressionDecision`、`CompressionRange`、`CompressionInputPlan`、`SectionItem`、`FinalSummary`、`CompressionResult`。
- `gate.py`
  - 根据当前 Conversation、候选模型和 token usage 决定是否触发压缩。
- `range_planner.py`
  - 选择待压缩消息区间和 retained raw tail。
  - 保证不拆分 tool_use/tool_result 协议配对。
- `input_planner.py`
  - 为每个 Worker 选择历史会话输入或文本快照输入。
  - Worker 不感知选择过程。
- `transcript_snapshot.py`
  - 生成 Conversation Transcript Snapshot。
  - 将图片/PDF/附件资源转换为 Agent 可再读取 locator。
- `prompt_loader.py`
  - 加载两套完整 Worker prompt 文档。
- `job_store.py`
  - 保存压缩 job 的中间 Section 条目、最终 Markdown、校验问题和运行状态。
  - 第一阶段可使用内存实现；后续可增加 SQLite/JSON 持久化。
- `summary_tools.py`
  - 提供 `context_summary.append_section_items`、`mark_section_complete`、`append_final_markdown_section`、`mark_final_complete`、`report_section_issue` 等受限工具。
  - 这些工具只写入压缩 job store，不操作业务文件、终端、MCP 或原 Conversation。
- `worker_runner.py`
  - 使用 `LlmRequestService` 驱动单个 Worker 的受限 LLM/tool 循环。
  - 只暴露 context_summary 工具，不暴露 Agent 业务工具。
- `final_assembler.py`
  - 调用 FinalAssemblerWorker 和可选 FinalVerifierWorker。
- `settings.py`
  - 定义上下文压缩设置聚合，包括 Worker 列表、Worker LLM 选择、最大并发、prompt override 和 prompt revision。
- `settings_repository.py`
  - 持久化用户在前端上下文压缩设置页中修改的 Worker LLM 服务商、模型接入、最大并发和系统提示词。
  - 只保存 `provider_id`、`model_binding_id`、prompt 文本和 revision，不保存 API key。
- `concurrency_limiter.py`
  - 为上下文压缩 Worker LLM 请求提供全局并发闸门。
  - 以 provider 或 provider+model 为 key 创建 `asyncio.Semaphore`，限制多 AgentInstance 同时触发压缩时的 Worker 请求并发。
- `conversation_rewriter.py`
  - 将最终压缩 Markdown 写成 synthetic user message，并与 retained raw tail 组成新的压缩后 Conversation。
  - 新 Conversation 使用新的 `target_conversation_id`。
  - 新 Conversation 的 `provider_state` 为空。
  - 重建新 Conversation 的 `state`。
- `compression_service.py`
  - 对外提供 `compress_if_needed(...)`。
  - 由 `AgentExecutionEngine` 在主 Agent LLM 请求前调用，成功时返回新的压缩后 Conversation。
- `north_gateway/context_compression_rest_adapter.py`
  - 提供前端上下文压缩设置页使用的 REST API。
  - 支持读取 Worker 列表、读取/保存 Worker prompt override、设置 Worker LLM 服务商与模型接入、设置最大并发和恢复默认 prompt。

### 15.3 执行引擎接入方式

`AgentExecutionEngine.__init__` 增加可选依赖：

```text
context_compression_service: ContextCompressionService | None = None
```

`AgentExecutionFactory.create` 负责装配：

```text
resource_resolver = _LazyFileResourceResolver()
llm_request_service = LlmClientFactory.create(resource_resolver, event_publisher=event_publisher)
context_compression_service = ContextCompressionService(
  llm_request_service=llm_request_service,
  llm_registry_gateway=LlmModelRegistryFactory.create(),
  conversation_repository=conversation_repository,
  runtime_gateway=AgentRuntimeFactory.create(),
  resource_resolver=resource_resolver,
  event_publisher=event_publisher,
  config_accessor=lambda: ConfigRegistry.instance().context_compression,
)
```

注意：

- `resource_resolver` 应与主 LLM 请求共用同一个懒加载解析器，避免纯文本场景提前初始化 OSS 或文件资源服务。
- `ContextCompressionService` 不应依赖业务工具调度器。
- Worker LLM 请求使用独立临时 Conversation，不复用或污染主 Agent Conversation。
- Worker LLM 请求的 tool schema 只包含 context_summary 受限工具。

主循环接入位置：

```text
await drain_pending_user_messages_to_conversation(...)
resolved_candidates = resolve_instance_llm_candidates(instance)
agent_definition = load_agent_definition(instance.agent_definition_id)

if context_compression_service is not None:
  compression_result = await context_compression_service.compress_if_needed(
    task=task,
    instance=instance,
    source_conversation=conversation,
    resolved_candidates=resolved_candidates,
    communication_context=communication_context,
    interrupt_checker=lambda: interrupt_state.requested,
  )
  if compression_result.created:
    conversation = compression_result.target_conversation
    communication_context = communication_context.with_metadata(conversation_id=conversation.id)
    trace_recorder.record_event(...)

llm_response = await send_prompt_with_policy_failover(...)
```

触发节点的完整语义：

- 每轮主 Agent LLM 请求前都可以检查一次上下文是否需要压缩。
- 检查必须发生在 `_drain_pending_user_messages_to_conversation` 之后，确保本轮用户最新输入已经进入 `Conversation.messages`。
- 检查必须发生在 `_send_prompt_with_policy_failover` 之前，避免在上下文已经超限时仍然发起主 Agent LLM 请求。
- 检查不得发生在主 Agent assistant tool_use 与 tool_result 之间，避免破坏 LLM 工具协议完整性。
- 检查不得发生在主 Agent LLM 流式响应过程中；流式响应中如果收到用户中断，应走现有中断语义，而不是边流式边压缩。
- 如果主 Agent 上一轮 LLM 请求返回的 `last_context_usage` 已超过阈值，则下一轮进入主 Agent LLM 请求前优先执行压缩。

### 15.4 触发策略

触发依据优先使用模型元数据中的 `compression_threshold`：

```text
effective_threshold =
  resolved_model.compression_threshold
  or floor(resolved_model.context_window * config.default_threshold_ratio)
```

当存在多个 failover candidates 时，闸门应使用当前可用候选中的最小 effective threshold，避免主候选失败后降级到更小上下文模型时才发现历史过长。

第一阶段触发条件：

- `conversation.state.last_context_usage` 存在。
- `last_context_usage.context_window_used_tokens >= min_effective_threshold`。
- Conversation 中存在足够长、可安全压缩的历史区间。
- 当前没有用户中断请求。

后续阶段可增强：

- 在主 LLM 请求前增加轻量级 token 估算。
- 捕获 provider 返回的 context window exceeded 错误后立即压缩并重试一次。
- 按模型、模态和资源预算分别计算压缩触发阈值。

闸门必须具备幂等性：

- 如果当前 active Conversation 已经只剩 synthetic summary + raw tail，且没有新的可压缩历史，必须跳过。
- 不得因为旧的 `last_context_usage` 数值仍然较高而反复压缩同一段消息。
- 判断是否可压缩应以 `CompressionRangePlanner` 返回的有效区间为准。

### 15.5 压缩区间规划

`CompressionRangePlanner` 输入：

- `conversation.messages`
- 最新 raw tail 保留策略
- 当前消息模态和工具协议结构
- 是否已经存在 synthetic compressed summary

输出：

```json
{
  "compressed_start_index": 0,
  "compressed_end_index": 42,
  "retained_tail_start_index": 43,
  "retained_tail_end_index": 58,
  "retained_tail_anchor": "latest_assistant_message",
  "reason": "context_usage_exceeded_threshold",
  "compression_index": 3
}
```

规则：

- 默认从消息 0 开始压缩较早历史。
- 如果消息 0 已经是旧的 Runtime Compressed Task Context，也应纳入新的压缩输入，由新 target Conversation 中的新 summary 替代旧 summary，避免多层嵌套摘要。
- retained raw tail 默认从最近一次 assistant 消息开始，保留该 assistant 消息及其之后的所有消息。
- 如果最近一次 assistant 消息之后还有 user 消息、tool 消息或后续 assistant 消息，必须全部保留，不能只保留最新 user。
- 如果最近一次 assistant 消息包含 tool_use，raw tail 必须同时保留该 assistant tool_use 消息和对应 tool_result 消息。
- 如果 Conversation 中还没有 assistant 消息，retained raw tail 至少从最近一次 user 消息开始保留。
- 不允许 compressed range 与 raw tail 拆开同一组 tool_use/tool_result 配对。
- 如果发现已有历史存在未配对 tool_use/tool_result，优先降级为 transcript snapshot；仍无法安全处理时暂停主 Agent 并报告 `context_compression_unsafe_tool_protocol`。
- 如果可压缩区间消息数低于配置阈值，应跳过压缩。
- retained raw tail 选择完成后，compressed range 才能确定；压缩区间永远是 retained raw tail 之前的较早历史。

### 15.6 Worker LLM 执行方式

每个 Section Worker 使用独立临时 Conversation：

- 历史会话输入版：
  - 临时 Conversation 包含待压缩区间的原始历史消息拷贝。
  - 末尾追加当前压缩强指令 user message。
  - system prompt 来自 `context_compression_worker_prompts_historical_conversation.md` 中对应 Worker 的完整 prompt。
- 文本快照输入版：
  - 临时 Conversation 只包含一条 user message，内容为 Conversation Transcript Snapshot。
  - system prompt 来自 `context_compression_worker_prompts_transcript_snapshot.md` 中对应 Worker 的完整 prompt。

WorkerRunner 执行循环：

```text
while not section_complete and round_count < max_rounds:
  call LlmRequestService.send_prompt(temp_conversation, worker_system_prompt, context_summary_tools)
  extract tool_use parts from assistant message
  execute only context_summary tool calls against CompressionJobStore
  append tool result message to temp_conversation
```

约束：

- Worker 不使用主 Agent 的业务工具 schema。
- Worker 不写主 Conversation。
- Worker 不消费 AgentInstance.pending_user_messages。
- Worker 中间 assistant/tool 消息不持久化到主 Conversation。
- Worker 被用户中断时，应取消当前 Worker LLM 请求，不创建或绑定 target Conversation，并让主 Agent 暂停在安全边界。

### 15.7 Section 并发与汇编

第一批并发 Worker：

```text
RequirementConstraintWorker
ProgressPlanWorker
CodeArchitectureWorker
ObservationEvidenceWorker
DecisionRationaleWorker
TodoRiskWorker
```

并发完成后：

1. Runtime 将所有 SectionItem 按 section、priority、status 汇总。
2. FinalAssemblerWorker 基于 SectionItem 和 provenance metadata 生成最终 Markdown。
3. 如果启用 FinalVerifierWorker，则校验最终 Markdown。
4. Verifier 发现 P0 缺失或无来源事实时，Runtime 可重跑相关 Section Worker 或 FinalAssembler。
5. 多次失败后不创建 target Conversation，并暂停主 Agent。

### 15.8 Conversation 新建、绑定与状态更新

压缩成功后必须新建业务 Conversation，不允许覆盖或复用旧 `conversation_id`。

本次压缩开始时，当前 active Conversation 记为 `source_conversation`：

```text
source_conversation_id = conversation.id
```

压缩完成后，`conversation_rewriter` 构造一个新的 `target_conversation`：

```text
target_conversation = create_conversation()
target_conversation.messages = [
  synthetic user message: "# Runtime Compressed Task Context\n...",
  ...retained raw tail messages
]
target_conversation.provider_state = {}
target_conversation.state = recompute from target_conversation.messages
```

随后 Runtime 必须：

```text
ConversationRepository.create(
  target_conversation,
  task_id=source task_id,
  thread_id=source thread_id,
  agent_id=source agent_id,
  agent_instance_id=source agent_instance_id,
)
AgentRuntimeGateway.bind_agent_instance_conversation(
  agent_instance_id,
  target_conversation.id,
)
conversation = target_conversation
communication_context = communication_context.with_metadata(conversation_id=target_conversation.id)
```

旧 `source_conversation` 保持不变：

- 不重写旧 JSONL 消息文件。
- 不清空旧 `provider_state`。
- 不删除旧 Conversation 元数据。
- 旧 Conversation 作为压缩前窗口和审计数据继续保留。

新 `target_conversation` 的规则：

- 使用新的 `target_conversation_id`。
- 绑定同一个 `task_id`、`thread_id`、`agent_id` 和 `agent_instance_id`。
- `provider_state` 为空，因为 provider cache、thinking 续接、message index 和 cache breakpoint 都不能跨压缩后的新消息窗口继承。
- `conversation.state` 根据新 messages 重算。
- `conversation.state.last_context_usage` 初始可以为空；下一次主 Agent LLM 请求成功后由 `_record_last_context_usage` 写入新快照。

synthetic user message 的 Markdown 必须包含 Usage Contract，并明确：

- 这是 Runtime 自动生成的历史上下文压缩摘要。
- 它不是新用户任务。
- 如果它与后续 retained raw tail 冲突，以 raw tail 为准。
- 本摘要对应的 `job_id`、`root_conversation_id`、`source_conversation_id`、`target_conversation_id`、`compression_index`、压缩消息范围和 retained raw tail 范围。

`Conversation` 领域对象本身不承载 `task_id`、`agent_instance_id` 或 `compression_index`。当前代码中 task / thread / agent / agent_instance 绑定存在 `SqliteConversationRepository` 的 `conversations` 表中；压缩链路、压缩次数和压缩历史应由独立的 `ContextCompressionArchiveRepository` 维护。

`ContextCompressionArchiveRepository` 是 Phase 1 必须能力。每次压缩在新建 target Conversation 前后必须归档：

- job_id
- task_id
- thread_id
- agent_id
- agent_instance_id
- root_conversation_id
- source_conversation_id
- target_conversation_id
- compression_index
- created_at
- completed_at
- status
- compressed message index range in source conversation
- retained tail range in source conversation
- retained_tail_anchor
- final summary markdown
- verifier result
- source conversation messages snapshot path
- target conversation messages snapshot path
- synthetic summary message metadata
- worker runtime config snapshot
- worker prompt revision snapshot
- worker model selection snapshot
- provider concurrency limit snapshot

`compression_index` 规则：

- 对同一个压缩链路按 `(task_id, agent_instance_id, root_conversation_id)` 从 1 开始递增。
- `root_conversation_id` 是该 AgentInstance 初始未压缩 Conversation 的 ID。
- 第一次压缩时，如果 source Conversation 没有既有压缩链路，则 `root_conversation_id = source_conversation_id`，`compression_index = 1`。
- 第 N 次压缩时，`source_conversation_id` 通常是上一次压缩生成的 `target_conversation_id`，`root_conversation_id` 沿用链路根 ID，`compression_index = previous_successful_index + 1`。
- 每次成功创建并绑定 target Conversation 时消耗一个新的 successful `compression_index`。
- 失败但已经创建 job 的压缩可记录为 failed/cancelled，但不得占用 successful compression 的有序恢复语义；查询时可同时返回所有 job 状态。
- synthetic summary message 中写入的 `compression_index` 必须与 archive 中一致。

Archive 用于 Runtime 内部审计、恢复和未来 UI 查询，不作为后续 Agent 的主上下文来源。后续 Agent 继续只读取当前 active `target_conversation.messages`。

### 15.8.1 多次压缩后的历史查询模型

Agent 多轮对话过程中可能触发多次上下文压缩。持久化数据必须能回答以下查询：

- 按 `task_id` 查看该任务下所有 Agent 实例的 Conversation 链路和压缩节点。
- 按 `agent_instance_id` 查看单个 Agent 实例当前 active Conversation 以及从 root Conversation 到当前 Conversation 的压缩链路。
- 按 `root_conversation_id` 查看该 Agent 实例从初始 Conversation 开始发生过的所有压缩 job。
- 按 `source_conversation_id` 或 `target_conversation_id` 定位某次压缩 job。
- 按 `root_conversation_id + compression_index` 查看某次压缩前的 source 消息快照、压缩区间、保留尾部、最终 summary 和 target 消息快照。

建议 archive 表按以下键组织：

```text
context_compression_jobs
  job_id primary key
  task_id
  thread_id
  agent_id
  agent_instance_id
  root_conversation_id
  source_conversation_id
  target_conversation_id
  compression_index
  status
  compressed_start_index
  compressed_end_index
  retained_tail_start_index
  retained_tail_end_index
  retained_tail_anchor
  input_mode
  worker_config_json
  worker_prompt_revision_json
  worker_model_selection_json
  provider_concurrency_limit_json
  final_summary_path or final_summary_json
  before_messages_snapshot_path
  after_messages_snapshot_path
  created_at
  completed_at
```

推荐索引：

```text
(task_id, agent_instance_id, root_conversation_id, compression_index)
(root_conversation_id, compression_index)
(source_conversation_id)
(target_conversation_id)
(job_id)
```

这样未来通过 task / agent_instance 查看任务过程时，可以将 Conversation 链路与压缩归档按 `compression_index` 有序分组：

```text
Task
  AgentInstance
    Conversation C0 root
    Compression #1
      source_conversation_id: C0
      target_conversation_id: C1
      before/source snapshot
      final summary
      retained tail
    Conversation C1
    Compression #2
      source_conversation_id: C1
      target_conversation_id: C2
      before/source snapshot
      final summary
      retained tail
    Conversation C2 active
```

AgentInstance 始终只绑定一个当前 active `conversation_id`，用于后续 LLM 执行；Archive 则保存 Conversation 链路、每次压缩的 source/target 边界和压缩前后快照。

Archive 必须保存本次压缩开始时实际生效的 Worker 设置快照，而不是只保存当前最新设置引用。原因是用户后续可能修改 Worker prompt、LLM 服务商或最大并发，如果历史 job 只引用当前设置，将无法解释历史压缩结果。建议保存：

- `worker_config_json`
  - `enabled`
  - `max_global_worker_concurrency`
  - `provider_concurrency_limits`
  - `worker_model_policy`
  - `default_worker_llm`
  - 每个 Worker 的启用状态和模型覆盖配置。
- `worker_prompt_revision_json`
  - 每个 Worker 在每种 input mode 下使用的 prompt revision。
  - 是否使用默认 prompt 或用户 override。
- `worker_model_selection_json`
  - 每个 Worker 实际使用的 `provider_id`、`model_binding_id`、resolved model id、api format 和 input mode。
  - 不保存 API key 或 provider raw secret。
- `provider_concurrency_limit_json`
  - 本次 job 获取并发许可时使用的 provider / provider+model 限流 key 和 limit。

### 15.9 事件、Trace 与用户中断

上下文压缩事件通过现有 `communication` 通信流发布。SSE 帧层仍使用统一事件名：

```text
event: communication.message
data: CommunicationMessage
```

前端不应监听新的原生 SSE event name，而应读取 `CommunicationMessage.type` 中的业务事件类型。也就是说，新增的是通信消息的 `type = context_compression.*`，不是新增一套独立 SSE endpoint。

Runtime 应发布独立压缩事件，不伪装成普通 Agent assistant message，也不写入 Conversation 消息流：

```text
context_compression.started
context_compression.range_planned
context_compression.worker.queued
context_compression.worker.started
context_compression.worker.progress
context_compression.worker.completed
context_compression.worker.failed
context_compression.section.started
context_compression.section.completed
context_compression.assembly.started
context_compression.assembly.completed
context_compression.verification.started
context_compression.verification.completed
context_compression.conversation.rebind.completed
context_compression.completed
context_compression.failed
context_compression.cancelled
context_compression.skipped
```

事件语义：

- `context_compression.started`
  - 压缩 job 已创建，Runtime 即将规划压缩范围。
  - 前端创建或更新当前执行过程气泡中的“上下文压缩”阶段。
- `context_compression.range_planned`
  - 已确定 compressed range 和 retained raw tail。
  - 前端展示压缩范围、保留尾部锚点和预计 compression index。
- `context_compression.worker.queued`
  - 某个 Worker 正在等待全局或 provider 并发许可。
  - 前端将该 Worker 显示为“等待并发许可”。
- `context_compression.worker.started`
  - 某个 Worker 已获得并发许可并开始 LLM 请求。
  - Section Worker、FinalAssemblerWorker、FinalVerifierWorker 都使用这个事件。
- `context_compression.worker.progress`
  - Worker 在本轮或多轮中已追加 section items 或 final markdown chunk。
  - 为避免泄露大量压缩内容，payload 默认只包含计数、轮次和状态，不包含完整条目内容。
- `context_compression.worker.completed`
  - 某个 Worker 完成。
  - 前端显示耗时、追加条目数、使用的 provider/model 和 input mode。
- `context_compression.worker.failed`
  - 某个 Worker 失败或重试耗尽。
  - 前端显示错误摘要；是否继续由 Runtime failure policy 决定。
- `context_compression.section.started`
  - Section 阶段开始，可选事件。
  - 如果已发布 `worker.started`，该事件可用于兼容旧 UI 或统计，不要求前端重复展示。
- `context_compression.section.completed`
  - Section 阶段完成，可选事件。
  - 如果已发布 `worker.completed`，前端应去重展示。
- `context_compression.assembly.started`
  - FinalAssemblerWorker 即将汇编最终 Markdown。
- `context_compression.assembly.completed`
  - 最终 Markdown 已汇编完成，但 target Conversation 可能尚未创建或绑定。
- `context_compression.verification.started`
  - FinalVerifierWorker 即将校验最终 Markdown。
  - `verifier_enabled=false` 时不发布。
- `context_compression.verification.completed`
  - 校验完成。
  - payload 只包含 `verification_status`、issue count 和简短 issue 摘要，不包含完整 summary。
- `context_compression.conversation.rebind.completed`
  - target Conversation 已创建，AgentInstance 已重新绑定到新的 `target_conversation_id`。
  - 这是“压缩结果已成为后续主 Agent 上下文”的关键确认事件。
- `context_compression.completed`
  - 本次压缩闭环完成，下一步可以继续主 Agent LLM 请求。
- `context_compression.failed`
  - 压缩失败。
  - 如果 failure policy 是 `pause_agent`，前端应将执行过程标记为暂停/失败并等待用户下一步输入。
- `context_compression.cancelled`
  - 用户中断或 Runtime 取消本次压缩。
  - 不创建或不绑定 target Conversation，除非此前已发布 `context_compression.conversation.rebind.completed`。
- `context_compression.skipped`
  - Runtime 判断无需或不能执行压缩。
  - 普通“未达到阈值”的检查不应发布 `skipped`，避免每轮主 LLM 请求前制造噪音；只有达到阈值但因为配置关闭、压缩区间不足、工具协议不安全等业务原因跳过时才发布。

所有压缩事件的公共 payload 应包含：

- task_id
- agent_instance_id
- root_conversation_id
- source_conversation_id
- target_conversation_id
- compression_index
- job_id
- compressed_range
- retained_tail_range
- reason
- status

公共 payload 建议结构：

```json
{
  "job_id": "ccjob-...",
  "task_id": "task-...",
  "agent_instance_id": "agentinst-...",
  "root_conversation_id": "conv-root",
  "source_conversation_id": "conv-source",
  "target_conversation_id": null,
  "compression_index": 3,
  "phase": "workers",
  "status": "running",
  "reason": "context_usage_exceeded_threshold",
  "compressed_range": {
    "start_index": 0,
    "end_index": 42
  },
  "retained_tail_range": {
    "start_index": 43,
    "end_index": 58,
    "anchor": "latest_assistant_message"
  },
  "worker": null,
  "metrics": {
    "elapsed_ms": 1200,
    "worker_total": 6,
    "worker_completed": 2,
    "worker_failed": 0
  }
}
```

Worker 事件 payload 追加：

```json
{
  "worker": {
    "name": "RequirementConstraintWorker",
    "role": "section_worker",
    "output_section": "requirements_constraints",
    "input_mode": "transcript_snapshot",
    "round_index": 2,
    "max_rounds": 8,
    "provider_id": "glm",
    "model_binding_id": "glm-5.1",
    "concurrency": {
      "global_limit": 6,
      "provider_key": "glm",
      "provider_limit": 4,
      "waited_ms": 320
    },
    "prompt_revision": {
      "use_default": false,
      "revision": 3
    },
    "progress": {
      "section_items_appended": 8,
      "total_section_items": 21,
      "final_markdown_chunks_appended": 0
    }
  }
}
```

完成事件 payload 追加：

```json
{
  "target_conversation_id": "conv-target",
  "source_message_count": 59,
  "target_message_count": 12,
  "compressed_message_count": 43,
  "retained_tail_message_count": 16,
  "summary_message_id": "msg-summary",
  "archive_job_id": "ccjob-...",
  "token_usage_before": {
    "used_tokens": 180000,
    "context_window": 200000
  },
  "estimated_token_usage_after": {
    "estimated_tokens": 42000,
    "context_window": 200000
  }
}
```

不应在事件中暴露 API key、provider raw、完整压缩输入或大段中间摘要。

也不应默认在 SSE 事件中推送完整最终 summary Markdown。完整 summary 已写入 target Conversation 的 synthetic user message，并由 Archive 持久化；前端如需查看详情，应通过后续压缩 job 查询接口或 Conversation 查询接口读取，而不是依赖实时事件承载大段内容。

#### 15.9.1 事件 envelope 与推荐顺序

上下文压缩事件应沿用现有 `RuntimeEventPublisher.publish(...)` 机制：

- `CommunicationMessage.type` 使用上文的 `context_compression.*`。
- `CommunicationMessage.topic` 沿用当前 Agent 执行通信流 topic，例如 `agent.task` 或 `agent.scene`。
- `source` 建议使用：
  - `context_compression_service`
  - `context_compression_worker_runner`
  - `context_compression_final_assembler`
  - `context_compression_archive`
- `actor_id` 使用当前 `agent_instance_id`。
- `correlation_id` 使用 `job_id`，便于前端和日志按一次压缩聚合。
- `metadata` 至少包含 `run_id`、`task_id`、`agent_instance_id`、`trace_id`、`conversation_id`。
- `schema_version` 沿用 `CommunicationMessage` 的版本；payload 内可额外带 `payload_version: "1.0"`。

推荐正常完成顺序：

```text
context_compression.started
context_compression.range_planned

context_compression.worker.queued        RequirementConstraintWorker
context_compression.worker.queued        ProgressPlanWorker
context_compression.worker.queued        CodeArchitectureWorker
...

context_compression.worker.started       RequirementConstraintWorker
context_compression.worker.progress      RequirementConstraintWorker
context_compression.worker.completed     RequirementConstraintWorker

context_compression.worker.started       ProgressPlanWorker
context_compression.worker.progress      ProgressPlanWorker
context_compression.worker.completed     ProgressPlanWorker

...

context_compression.assembly.started
context_compression.worker.started       FinalAssemblerWorker
context_compression.worker.progress      FinalAssemblerWorker
context_compression.worker.completed     FinalAssemblerWorker
context_compression.assembly.completed

context_compression.verification.started
context_compression.worker.started       FinalVerifierWorker
context_compression.worker.completed     FinalVerifierWorker
context_compression.verification.completed

context_compression.conversation.rebind.completed
context_compression.completed
```

并发场景下，多个 Section Worker 的 `started/progress/completed` 可以交错到达。前端不得假设 Worker 严格按固定顺序完成，只能按 `job_id`、`worker.name` 和 SSE `seq` 合并。

失败顺序：

```text
context_compression.started
context_compression.range_planned
context_compression.worker.started
context_compression.worker.failed
context_compression.failed
```

取消顺序：

```text
context_compression.started
context_compression.worker.queued | context_compression.worker.started
context_compression.cancelled
```

跳过顺序：

```text
context_compression.skipped
```

如果 Runtime 尚未创建 job，只能发布 `skipped`，并使用一个临时 `job_id` 或空 `job_id`。如果已经创建 job，则 `skipped` 必须携带真实 `job_id` 并写入 Archive 状态。

#### 15.9.2 前端展示设计

前端应把上下文压缩渲染为 Agent 执行过程的一部分，而不是渲染为普通 user/assistant 聊天气泡。

展示原则：

- 上下文压缩开始后，在当前 Agent process bubble 中增加一个“上下文压缩”阶段块。
- 不为每个 Worker 创建独立聊天气泡。
- 不把 Worker 中间 LLM assistant 内容直接显示成 Agent 回复。
- 不展示完整压缩输入、完整 prompt、API key 或 provider raw。
- 如果用户打开详情，可以展示 Worker 名称、状态、耗时、模型、input mode、并发等待原因、追加条目数量和简短错误摘要。

推荐 process bubble 结构：

```text
Agent process bubble
  上下文压缩 #3 · 运行中
    触发原因：上下文窗口超过阈值
    压缩范围：0..42
    保留尾部：43..58 · latest_assistant_message
    Worker 进度：2/6 完成
    并发限制：global 6 · glm 4

    RequirementConstraintWorker  完成  1.8s  glm-5.1  transcript_snapshot  8 items
    ProgressPlanWorker           运行中 2.1s  glm-5.1  transcript_snapshot
    CodeArchitectureWorker       等待并发许可 320ms  provider=glm limit=4
    ObservationEvidenceWorker    完成  2.3s  glm-5.1  transcript_snapshot  11 items
    DecisionRationaleWorker      运行中
    TodoRiskWorker               运行中

  汇编最终摘要
  校验最终摘要
  已创建压缩 Conversation
  继续主 Agent LLM 请求
```

完成态展示：

```text
上下文压缩 #3 完成 · 6/6 workers · 12.4s
已从 source Conversation 创建 target Conversation
压缩 43 条历史消息，保留最近 16 条 raw tail
预计上下文从 180k 降至 42k tokens
```

失败态展示：

```text
上下文压缩失败 · CodeArchitectureWorker
原因：worker_model_unavailable
Agent 已暂停，等待用户输入或调整上下文压缩设置
```

取消态展示：

```text
上下文压缩已取消
未创建新的 target Conversation
当前 Agent 仍绑定原 source Conversation
```

跳过态展示：

- 如果只是普通未达到阈值，不显示任何 UI。
- 如果达到阈值但因为配置关闭或协议不安全跳过，应在 process bubble 中显示一行轻量 warning。
- 如果跳过会导致主 LLM 请求继续存在超限风险，应显示为 warning，并给出建议：调整设置、减少上下文或手动重试。

前端状态聚合规则：

- 以 `job_id` 作为一次上下文压缩 UI 块的稳定 key。
- 以 `worker.name` 作为 Worker 行 key。
- 事件可能因为 SSE 重连而重放，前端必须按 `seq` 去重，并按 `job_id + worker.name` 合并状态。
- `worker.progress` 事件只更新计数，不追加聊天文本。
- `context_compression.conversation.rebind.completed` 到达后，前端可更新当前 AgentInstance 的 active conversation 标识，但不应清空当前聊天 UI；历史查询应通过 Conversation lineage 和 Archive 进行。
- `context_compression.completed` 到达后，该阶段折叠为摘要行；用户可展开查看 Worker 明细。
- `context_compression.failed` 或 `cancelled` 到达后，该阶段保持展开，便于用户看到失败 Worker 和原因。

#### 15.9.3 与 Chat Bubble 持久化的关系

上下文压缩 SSE 事件属于运行过程事件，不是 Conversation 消息，也不是普通 Agent 回复。

前端可以将其纳入现有 process bubble 的前端持久化结果，但后端不应把这些事件自动转成 Chat Bubble 作为恢复真相来源。可恢复的业务真相应来自：

- Conversation lineage。
- `ContextCompressionArchiveRepository`。
- target Conversation synthetic summary message。
- 通信流事件日志。

如果前端断线导致 process bubble 中的压缩展示丢失，恢复时可通过通信流历史消息和压缩 Archive 重建展示。

用户中断语义：

- 如果中断发生在 Worker LLM 请求中，取消 Worker 请求，不保存部分 Worker assistant。
- 如果中断发生在 Section 已写入 job store 但 target Conversation 尚未创建或尚未绑定前，丢弃本次未完成 job，AgentInstance 继续绑定原 source Conversation。
- 如果中断发生在 target Conversation 已创建并绑定完成后，新 Conversation 已处于完整状态，主 Agent 在下一安全点暂停。
- 不允许在 source 或 target Conversation 中留下 assistant tool_use 没有 tool_result 的不完整协议状态。

### 15.10 配置、设置 UI 与 Worker LLM 选择设计

上下文压缩应有独立设置入口，而不是隐藏在通用系统设置页中。前端顶部菜单栏应新增一个上下文压缩设置图标，建议放在 `Tools` 与 `Settings` 之间或 `LLM` 与 `Tools` 之间。点击后进入独立页面：

```text
/context-compression
```

页面目标：

- 展示所有上下文压缩 Worker 列表。
- 展示每个 Worker 的职责、输入模式支持和当前启用状态。
- 支持点击 Worker 查看和编辑对应 system prompt。
- 支持分别编辑 `native_multimodal_history` 和 `transcript_snapshot` 两套 prompt。
- 支持恢复单个 Worker 的默认 prompt。
- 支持设置上下文压缩 Worker 使用的 LLM 服务商和模型接入。
- 支持设置全局最大 Worker 并发数。
- 支持设置按服务商或服务商模型接入维度的最大并发数，避免超过第三方 LLM 服务商并发上限。

#### 15.10.1 配置文件结构

建议在 `AgentRuntimeConfig` 增加：

```yaml
context_compression:
  enabled: true
  default_threshold_ratio: 0.75
  min_compressible_messages: 12
  retained_tail_min_messages: 8
  retained_tail_max_estimated_tokens: 12000
  section_worker_max_rounds: 8
  assembler_max_rounds: 8
  verifier_enabled: true
  max_retries_per_section: 1
  failure_policy: pause_agent

  # Worker 模型选择策略。
  # reuse_agent_policy: 默认复用当前 AgentInstance 的 LLM candidates。
  # dedicated_worker_model: 使用 default_worker_llm 或 workers.<worker>.llm 中指定的模型接入。
  worker_model_policy: dedicated_worker_model

  # 所有上下文压缩 Worker 请求的全局并发上限。
  # 该限制跨 task、跨 agent_instance、跨 compression job 生效。
  max_global_worker_concurrency: 6

  # 当某个 Worker 没有单独覆盖时使用的默认 Worker LLM。
  # 这里只保存 LLM registry 中的 provider_id 和 model_binding_id，不保存 API key。
  default_worker_llm:
    provider_id: "glm"
    model_binding_id: "glm-5.1"

  # 按服务商或服务商模型接入控制并发，防止多 Agent 同时压缩时超过服务商限流。
  provider_concurrency_limits:
    - provider_id: "glm"
      model_binding_id: null
      max_concurrency: 4
    - provider_id: "openai"
      model_binding_id: "gpt-5.5"
      max_concurrency: 2

  # 每个 Worker 的开关、模型覆盖和 prompt override。
  workers:
    RequirementConstraintWorker:
      enabled: true
      llm:
        provider_id: null
        model_binding_id: null
      prompts:
        native_multimodal_history:
          use_default: true
          revision: 1
          override_text: null
        transcript_snapshot:
          use_default: true
          revision: 1
          override_text: null
    ProgressPlanWorker:
      enabled: true
      llm:
        provider_id: null
        model_binding_id: null
      prompts:
        native_multimodal_history:
          use_default: true
          revision: 1
          override_text: null
        transcript_snapshot:
          use_default: true
          revision: 1
          override_text: null
    CodeArchitectureWorker:
      enabled: true
      llm:
        provider_id: null
        model_binding_id: null
      prompts:
        native_multimodal_history:
          use_default: true
          revision: 1
          override_text: null
        transcript_snapshot:
          use_default: true
          revision: 1
          override_text: null
    ObservationEvidenceWorker:
      enabled: true
      llm:
        provider_id: null
        model_binding_id: null
      prompts:
        native_multimodal_history:
          use_default: true
          revision: 1
          override_text: null
        transcript_snapshot:
          use_default: true
          revision: 1
          override_text: null
    DecisionRationaleWorker:
      enabled: true
      llm:
        provider_id: null
        model_binding_id: null
      prompts:
        native_multimodal_history:
          use_default: true
          revision: 1
          override_text: null
        transcript_snapshot:
          use_default: true
          revision: 1
          override_text: null
    TodoRiskWorker:
      enabled: true
      llm:
        provider_id: null
        model_binding_id: null
      prompts:
        native_multimodal_history:
          use_default: true
          revision: 1
          override_text: null
        transcript_snapshot:
          use_default: true
          revision: 1
          override_text: null
    FinalAssemblerWorker:
      enabled: true
      llm:
        provider_id: null
        model_binding_id: null
      prompts:
        native_multimodal_history:
          use_default: true
          revision: 1
          override_text: null
        transcript_snapshot:
          use_default: true
          revision: 1
          override_text: null
    FinalVerifierWorker:
      enabled: true
      llm:
        provider_id: null
        model_binding_id: null
      prompts:
        native_multimodal_history:
          use_default: true
          revision: 1
          override_text: null
        transcript_snapshot:
          use_default: true
          revision: 1
          override_text: null
```

配置语义：

- `max_global_worker_concurrency` 控制所有上下文压缩 Worker LLM 请求的总并发。
- `provider_concurrency_limits` 控制某个服务商或某个服务商模型接入的并发。
- `workers.<worker>.llm` 为空时使用 `default_worker_llm`。
- `default_worker_llm` 为空且 `worker_model_policy = reuse_agent_policy` 时，复用当前 AgentInstance 的 LLM candidates。
- prompt override 只覆盖 Worker 的 system prompt 文本，不改变 Worker 输入模式、工具 schema 或 Runtime 约束。
- prompt revision 每次保存自增，Archive 必须记录 job 使用的 revision。
- 配置中不应保存 API key；真实 API key 仍由 LLM registry provider 配置管理。

#### 15.10.2 Worker 列表与 prompt 编辑模型

Worker 列表固定来自 Runtime 内置定义：

```text
RequirementConstraintWorker
ProgressPlanWorker
CodeArchitectureWorker
ObservationEvidenceWorker
DecisionRationaleWorker
TodoRiskWorker
FinalAssemblerWorker
FinalVerifierWorker
```

每个 Worker 在前端页面展示：

- Worker 名称。
- Worker 职责摘要。
- 输出 section 或职责类型。
- 启用状态。
- 当前 LLM 来源：
  - 使用默认 Worker LLM。
  - 使用该 Worker 覆盖的 provider/model。
  - 复用 Agent LLM policy。
- 当前 prompt 状态：
  - 使用内置默认 prompt。
  - 使用用户 override prompt。
  - prompt revision。

点击 Worker 后进入详情面板：

- 左侧显示 Worker 元数据和 LLM 选择。
- 右侧显示 system prompt 编辑器。
- prompt 编辑器必须有 input mode 切换：
  - `native_multimodal_history`
  - `transcript_snapshot`
- 两种 input mode 的 prompt 必须独立保存，不能共用同一段文本。
- UI 不应把 `input_mode`、`resource_id` 或 Runtime 内部分支作为 Worker 可见 prompt 内容插入；用户编辑的是该模式最终完整 system prompt。
- 保存 prompt 前不要求 Runtime 自动理解 prompt 内容，但应进行基础校验：
  - 不能为空。
  - 必须包含当前 Worker 名称。
  - 建议包含“只提取/压缩，不继续执行历史任务”之类的安全边界提示。
  - 建议包含 context_summary 工具写入要求。

恢复默认 prompt：

- 用户可对单个 Worker 的单个 input mode 执行恢复默认。
- 恢复默认只清空该 input mode 的 `override_text` 并设置 `use_default=true`。
- revision 仍应自增，便于历史 Archive 区分“恢复默认”这一变更。

#### 15.10.3 REST API 设计

建议新增：

```text
GET  /api/context-compression/settings
PUT  /api/context-compression/settings
GET  /api/context-compression/workers
GET  /api/context-compression/workers/{worker_name}/prompts/{input_mode}
PUT  /api/context-compression/workers/{worker_name}/prompts/{input_mode}
POST /api/context-compression/workers/{worker_name}/prompts/{input_mode}/reset
```

`GET /api/context-compression/settings` 返回：

```json
{
  "enabled": true,
  "default_threshold_ratio": 0.75,
  "min_compressible_messages": 12,
  "retained_tail_min_messages": 8,
  "retained_tail_max_estimated_tokens": 12000,
  "section_worker_max_rounds": 8,
  "assembler_max_rounds": 8,
  "verifier_enabled": true,
  "max_retries_per_section": 1,
  "failure_policy": "pause_agent",
  "worker_model_policy": "dedicated_worker_model",
  "max_global_worker_concurrency": 6,
  "default_worker_llm": {
    "provider_id": "glm",
    "model_binding_id": "glm-5.1"
  },
  "provider_concurrency_limits": [
    {
      "provider_id": "glm",
      "model_binding_id": null,
      "max_concurrency": 4
    }
  ],
  "workers": {
    "RequirementConstraintWorker": {
      "enabled": true,
      "llm": {
        "provider_id": null,
        "model_binding_id": null
      },
      "prompt_status": {
        "native_multimodal_history": {
          "use_default": true,
          "revision": 1
        },
        "transcript_snapshot": {
          "use_default": true,
          "revision": 1
        }
      }
    }
  }
}
```

`GET /api/context-compression/workers` 返回 Worker 元数据和当前设置摘要：

```json
[
  {
    "name": "RequirementConstraintWorker",
    "title": "用户需求与硬约束提取",
    "role": "section_worker",
    "output_section": "requirements_constraints",
    "enabled": true,
    "supports_input_modes": ["native_multimodal_history", "transcript_snapshot"],
    "llm": {
      "effective_provider_id": "glm",
      "effective_model_binding_id": "glm-5.1",
      "source": "default_worker_llm"
    },
    "prompt_status": {
      "native_multimodal_history": {
        "use_default": true,
        "revision": 1
      },
      "transcript_snapshot": {
        "use_default": false,
        "revision": 3
      }
    }
  }
]
```

`GET /api/context-compression/workers/{worker_name}/prompts/{input_mode}` 返回：

```json
{
  "worker_name": "RequirementConstraintWorker",
  "input_mode": "transcript_snapshot",
  "use_default": false,
  "revision": 3,
  "default_text": "...",
  "override_text": "...",
  "effective_text": "..."
}
```

`PUT /api/context-compression/workers/{worker_name}/prompts/{input_mode}` 请求：

```json
{
  "override_text": "...完整 system prompt...",
  "use_default": false
}
```

REST 校验规则：

- `worker_name` 必须是 Runtime 支持的固定 Worker 名称。
- `input_mode` 只能是 `native_multimodal_history` 或 `transcript_snapshot`。
- `provider_id` 和 `model_binding_id` 必须存在于 LLM registry。
- 如果指定 Worker 使用原生多模态历史输入，模型接入必须支持对应输入模态；否则 Runtime 执行时仍可降级 transcript snapshot。
- `max_global_worker_concurrency` 必须大于等于 1。
- 每个 `provider_concurrency_limits.max_concurrency` 必须大于等于 1。
- API 响应不得返回 API key、环境变量解析后的 secret 或 provider raw secret。

#### 15.10.4 Worker LLM 选择优先级

每次压缩开始时，Runtime 为每个 Worker 决定实际 LLM candidate：

```text
if worker.llm.provider_id and worker.llm.model_binding_id:
  use worker override
else if default_worker_llm is configured:
  use default_worker_llm
else if worker_model_policy == reuse_agent_policy:
  use current AgentInstance resolved candidates
else:
  fail configuration validation
```

选择后仍必须检查：

- 目标模型是否支持工具调用，因为 Worker 需要调用 `context_summary` 工具。
- 目标模型是否支持当前输入模式需要的输入模态。
- 如果不支持 `native_multimodal_history`，则降级到 `transcript_snapshot`。
- 如果模型无法支持工具调用，应跳过该模型并按候选策略选择下一个；没有可用模型时暂停 Agent 并报告 `context_compression_worker_model_unavailable`。

#### 15.10.5 最大并发与服务商限流

上下文压缩会在一个 job 内并发运行多个 Section Worker；同时系统可能有多个 AgentInstance 同时触发压缩。因此最大并发必须是后端 Runtime 级别约束，而不是只靠前端 UI 控制。

后端必须实现两层并发控制：

```text
global_worker_semaphore = Semaphore(max_global_worker_concurrency)
provider_worker_semaphore[provider_key] = Semaphore(provider_limit)
```

Worker LLM 请求执行前必须同时获取：

```text
async with global_worker_limiter.acquire():
  async with provider_limiter.acquire(provider_id, model_binding_id):
    await LlmRequestService.send_prompt(...)
```

`provider_key` 规则：

- 如果配置了 `provider_id + model_binding_id` 的 limit，优先使用该精确 key。
- 否则使用 `provider_id` 级别 limit。
- 如果都没有配置，默认只受 `max_global_worker_concurrency` 约束。

并发控制必须跨压缩 job 共享：

- 同一个后端进程内，`ContextCompressionConcurrencyLimiter` 应作为单例或由 `ContextCompressionService` 长生命周期持有。
- 多个 AgentInstance 同时压缩时，共用同一组 semaphore。
- 同一个 job 内多个 Section Worker 也必须走同一组 semaphore。
- FinalAssemblerWorker 和 FinalVerifierWorker 也属于上下文压缩 Worker LLM 请求，必须计入并发限制。

配置热更新语义：

- 每次压缩 job 开始时读取一次设置快照。
- job 运行期间使用该快照保持一致，不因用户中途保存设置而改变正在运行的 job。
- 新 job 使用最新设置。
- limiter 可在获取许可时读取最新 limit，但 Archive 仍记录 job 开始时的 limit snapshot。

用户中断语义：

- Worker 等待 semaphore 时收到中断，应取消等待并让主 Agent 暂停在安全边界。
- Worker 已获得 semaphore 并发起 LLM 请求后收到中断，应取消当前 LLM 请求并释放 semaphore。
- 不允许因为取消或异常泄漏 semaphore permit。

#### 15.10.6 前端页面信息架构

页面建议分三栏或主从布局：

```text
Context Compression Settings
  ├─ Global Settings
  │   ├─ enabled
  │   ├─ threshold ratio
  │   ├─ max global worker concurrency
  │   ├─ default worker provider/model
  │   └─ provider concurrency limits
  ├─ Worker List
  │   ├─ RequirementConstraintWorker
  │   ├─ ProgressPlanWorker
  │   ├─ CodeArchitectureWorker
  │   ├─ ObservationEvidenceWorker
  │   ├─ DecisionRationaleWorker
  │   ├─ TodoRiskWorker
  │   ├─ FinalAssemblerWorker
  │   └─ FinalVerifierWorker
  └─ Worker Detail
      ├─ enabled
      ├─ provider/model override
      ├─ input mode tabs
      ├─ system prompt editor
      ├─ save prompt
      └─ reset to default
```

交互规则：

- LLM 服务商和模型接入下拉框必须从现有 LLM Registry API 获取。
- `provider_id` 选择后，`model_binding_id` 下拉框只展示该服务商下的模型接入。
- 最大并发设置应清晰说明它是后端真实并发闸门，不是 UI 并发显示。
- prompt 编辑器保存成功后显示 revision 变化。
- 默认 prompt 和 override prompt 可以对比查看，但保存时只保存 override。
- 页面不直接编辑 LLM provider API key；用户需要到 LLM 页面维护服务商凭据。

### 15.11 实现阶段拆分

#### Phase 1：最小可用上下文压缩闭环

目标：在主 Agent LLM 请求前，当 `last_context_usage` 超过阈值时，完成一次可用压缩，创建新的 target Conversation，并把当前 AgentInstance 绑定到新 Conversation。

实现内容：

1. 新增 `context_compression` 包和内存 job store。
2. 新增 ContextCompressionConfig，并接入 ConfigRegistry。
3. 新增 ContextCompressionSettingsRepository，支持保存 Worker LLM 选择、最大并发、provider 并发限制和 prompt override。
4. 新增 ContextCompression REST API，供前端读取 Worker 列表、保存 prompt、恢复默认 prompt、保存 LLM 与并发配置。
5. 新增前端顶部上下文压缩设置入口和 `/context-compression` 设置页。
6. 新增 ContextCompressionConcurrencyLimiter，并让所有 Worker LLM 请求经过全局 semaphore 和 provider semaphore。
7. 新增 Gate、RangePlanner、ConversationRewriter。
8. 新增 ContextCompressionArchiveRepository，记录 task / agent_instance / root_conversation / source_conversation / target_conversation / compression_index、压缩前后快照、Worker 配置快照、prompt revision 快照和模型选择快照。
9. 实现 transcript snapshot 输入路径。
10. 实现 Section WorkerRunner 和 context_summary 受限工具。
11. 实现 FinalAssemblerWorker。
12. 接入 AgentExecutionEngine 主循环。
13. 实现基础 `context_compression.*` 通信流事件，并在前端 process bubble 中展示压缩开始、Worker 进度、汇编、校验、Conversation 重新绑定、完成/失败/取消状态。
14. 添加单元测试覆盖触发、区间、raw tail 从最近 assistant 开始保留、target Conversation 新建、AgentInstance 重新绑定、target provider_state 为空、多次 compression_index 递增、Worker LLM 选择优先级、全局/服务商并发限制、prompt override revision 记录、压缩事件顺序和 payload 安全性。

Phase 1 可以先不启用原生多模态历史输入，只实现 transcript snapshot；但 prompt 文档和接口必须保留两种输入能力，避免后续接口重构。

#### Phase 2：原生多模态历史输入

目标：Worker 模型支持图片/PDF 时，优先使用原 Conversation 历史消息作为输入。

实现内容：

1. NativeMultimodalHistoryBuilder。
2. Worker 模型模态能力选择。
3. 资源 URL/base64/PDF 输入渲染验证。
4. 工具协议安全渲染检测。
5. 原生输入失败时自动降级 transcript snapshot。

#### Phase 3：质量、恢复与可观测性增强

目标：提升压缩稳定性和可排障能力。

实现内容：

1. FinalVerifierWorker。
2. context window exceeded 失败后的压缩重试。
3. 更准确的 token 估算。
4. 压缩 job REST 查询接口。
5. 前端展示历史压缩链路、source/target Conversation、compression_index、Worker 配置快照和 prompt revision。
6. 前端支持从压缩 job REST 查询接口重建历史压缩详情、查看完整 summary、查看 Worker 明细和失败原因。

## 16. 失败处理

如果 Section Worker 失败：

- Runtime 可重试该章节。
- 如果该章节不是 P0 关键章节，可降级为在风险中记录缺失。
- 如果 P0 章节失败，应暂停 Agent，不继续请求 LLM。

如果 FinalAssembler 失败：

- 不创建 target Conversation。
- 保持 source Conversation 完整状态，AgentInstance 继续绑定 source Conversation。
- 暂停 Agent 并报告 `context_compression_failed`。

如果 Verifier 失败：

- 可局部重跑相关 Section Worker。
- 可要求 FinalAssembler 重新汇编。
- 多次失败后暂停 Agent。

## 17. 设计原则总结

- 压缩 Worker 不是普通业务 Agent。
- 原历史是待分析数据，不是当前指令。
- 多方向 Worker 并发提取，降低单次输出过长风险。
- 所有 Worker 通过 append 工具增量写入，不要求一次性完整输出。
- 不写死每次 append 的条数或 token 限制，由提示词要求模型在内容较多时自行分批。
- 持久化资源必须以可再读取形式保留，不能只保留文件名、资源名或模糊摘要。
- Runtime 统一汇编并创建压缩后的 target Conversation，Worker 不直接修改原会话。
- 最终产物是一份完整任务恢复 Markdown 加最近 raw tail，而不是普通聊天摘要。
