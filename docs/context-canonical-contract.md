# Context Canonical Contract

这份文档定义 provider 无关的内部上下文契约。它只描述产品层可以依赖的结构，不描述任何真实 API 请求体，也不把 OpenAI、Claude、Gemini 等 provider 的专有字段提升为主字段。

## 目标

主聊天、上下文工作区、revision/restore 和后续 provider adapter 都应该围绕同一套 canonical contract 工作：

- `PromptBlock`: 顶层提示词、记忆、摘要等非真实对话历史。
- `TranscriptRecord`: 产品层真实 transcript，只允许 `user` 和 `assistant` 两种角色。
- `CanonicalItem`: assistant/user record 内部可编辑、可编译的 provider 无关 item。
- `ToolEventRecord`: 产品层可展示、可记录的工具事件。
- `ProviderRaw`: 只用于调试或追溯的原始 provider 附加信息。
- `AssistantRoundState`: 单轮 assistant 生成期间的临时聚合状态。

## 核心原则

1. transcript 是产品语义，不是 provider 协议语义。
2. 每次用户回合最终只应该落库一条 `user` record 和一条 `assistant` record。
3. provider 专有字段只能进入 `provider_raw`，不能成为主逻辑依赖。
4. `PromptBlock` 不属于 transcript。system/developer/memory/summary 都应该和真实聊天历史分层。
5. 工具调用和工具结果不允许长成新的 transcript 角色。它们只能作为 assistant record 内部的 `canonical_items`、`tool_events` 或 `blocks`。

## Naming

后端 canonical JSON 使用 snake_case。TypeScript 类型也追加了同名字段，避免前后端 contract 分叉。

现有前端历史字段仍保留：

- `toolEvents`
- `providerItems`
- `providerRaw` 如后续 UI 层需要可作为桥接字段

新的 canonical 主字段是：

- `tool_events`
- `canonical_items`
- `provider_raw`

## PromptBlock

`PromptBlock` 表示注入到模型上下文的非 transcript 内容。

| Field | Type | Notes |
| --- | --- | --- |
| `kind` | `system | developer | memory | summary` | 产品层语义，不等于 provider 请求字段。 |
| `text` | `string` | 注入内容。 |
| `editable` | `boolean` | 是否允许上下文工作区编辑。 |
| `source` | `string` | 来源标记，比如 app、workspace、restore。 |
| `id` | `string` | 可选稳定 ID。 |
| `metadata` | object | 产品层附加信息。 |

`PromptBlock.kind` 不能扩展成 provider 专有概念。如果某个 provider 的放置位置不同，由 adapter 编译时处理。

## TranscriptRecord

`TranscriptRecord` 是可持久化 transcript 的基本单元。

| Field | Type | Notes |
| --- | --- | --- |
| `role` | `user | assistant` | transcript 只允许这两个角色。 |
| `text` | `string` | 用户可见主文本。 |
| `attachments` | `AttachmentRecord[]` | 用户上传或产品层附件。 |
| `blocks` | `TranscriptBlock[]` | 展示顺序结构，通常是 text/tool。 |
| `tool_events` | `ToolEventRecord[]` | 这一条 record 内聚合的工具事件。 |
| `canonical_items` | `CanonicalItem[]` | provider 无关的可编辑 item。 |
| `provider_raw` | `ProviderRaw` | 可选调试信息，不能参与主逻辑判断。 |
| `metadata` | object | 产品层附加信息。 |

约束：

- `role` 不能是 `system`、`developer`、`tool`、`function`、`model`。
- `user` record 只记录真实用户输入和真实用户附件。
- 工具结果回传、provider 协议消息、应用注入摘要都不能变成 `user` record。
- `assistant` record 聚合本轮 agent 的最终回答、工具调用、工具结果摘要和 canonical items。

## CanonicalItem

`CanonicalItem` 是 provider 无关的内部 item。它可以被 adapter 编译成不同 provider 的请求结构，也可以被上下文工作区读取和编辑。

| Field | Type | Notes |
| --- | --- | --- |
| `type` | `message | tool_call | tool_result` | 必须至少支持这三类。 |
| `role` | `user | assistant` | 只用于 `message`，不允许 tool/system/developer。 |
| `content` | JSON value | message 内容。可以是字符串或产品层 block。 |
| `name` | `string` | 工具名，provider 无关。 |
| `call_id` | `string` | 工具调用和结果的关联 ID。 |
| `arguments` | JSON value | 工具参数，优先使用对象。 |
| `output` | JSON value | 工具输出或摘要。 |
| `status` | `pending | running | completed | error | skipped` | 工具相关状态。 |
| `provider_raw` | `ProviderRaw` | 可选调试信息。 |
| `metadata` | object | 产品层附加信息。 |

约束：

- `tool_call` 和 `tool_result` 必须通过 `call_id` 关联。
- provider 专有字段如 `tool_calls`、`tool_use`、`functionCall`、`function_call_output` 只能放进 `provider_raw.payload`。
- 主逻辑不能依赖 `provider_raw`。

## ToolEventRecord

`ToolEventRecord` 是产品层展示和审计用的工具事件，不等于 provider 的 tool-call 协议结构。

| Field | Type | Notes |
| --- | --- | --- |
| `name` | `string` | 工具名。 |
| `arguments` | JSON value | 工具参数。 |
| `output_preview` | `string` | 面向 UI 的短摘要。 |
| `raw_output` | `string` | 原始工具输出文本。 |
| `display_title` | `string` | UI 标题。 |
| `display_detail` | `string` | UI 详情。 |
| `display_result` | `string` | UI 结果。 |
| `status` | `pending | running | completed | error | skipped` | 产品层状态。 |
| `call_id` | `string` | 可选关联 ID。 |
| `error` | `string` | 可选错误摘要。 |
| `provider_raw` | `ProviderRaw` | 可选调试信息。 |

## ProviderRaw

`ProviderRaw` 是隔离 provider 专有信息的唯一出口。

| Field | Type | Notes |
| --- | --- | --- |
| `provider_id` | `string` | provider 或 adapter 标识。 |
| `model` | `string` | 可选模型标识。 |
| `request_id` | `string` | 可选请求追踪 ID。 |
| `event_type` | `string` | 可选原始事件类型。 |
| `payload` | JSON value | 原始 provider 数据。 |
| `notes` | `string[]` | 调试备注。 |

主流程、workbench、revision/restore 不应该读取 `ProviderRaw.payload` 来决定产品语义。它只用于调试、迁移和审计。

## AssistantRoundState

`AssistantRoundState` 是单轮 assistant 生成期间的临时聚合状态，不直接等同于 transcript record。

| Field | Type | Notes |
| --- | --- | --- |
| `round_id` | `string` | 可选本轮 ID。 |
| `answer_text` | `string` | 当前累计的用户可见文本。 |
| `canonical_items` | `CanonicalItem[]` | 本轮累计 canonical items。 |
| `tool_events` | `ToolEventRecord[]` | 本轮累计工具事件。 |
| `provider_raw` | `ProviderRaw` | 可选调试信息。 |
| `is_final` | `boolean` | 是否已完成。 |
| `error` | `string` | 可选错误摘要。 |
| `metadata` | object | 产品层附加信息。 |

最终落库时，`AssistantRoundState` 应被编译成一条 `assistant` `TranscriptRecord`，而不是把中间 provider 协议消息逐条写入 transcript。

## Compatibility Notes

- 当前前端已有 `TranscriptRecord.providerItems`。它是历史兼容字段，不是新 contract 的主字段。
- 新代码应该优先写入和读取 `canonical_items`。
- 现有 `toolEvents` 可以继续服务旧 UI。canonical 层新增 `tool_events`，后续可由集成层做双写或桥接。
- 这份 contract 不要求任何真实 provider adapter 现在接入，也不要求改变现有 `append_turn`、`normalize_transcript` 或 `SimpleAgent.run_turn` 行为。
