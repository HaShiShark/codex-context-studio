# Assistant 活动与 Compact 决策

本文只保留当前有效决定。功能行为见[Assistant 活动与 Compact](assistant-activity.md)。

## 规范 Input 优先于视觉连续性

决定：上下文地图只呈现当前规范 provider input。Compact 已移除的 assistant/tool item 不通过 `display_items`、`display_continuation` 或其他旁路继续保存在 transcript 中。

原因：transcript 是 input 的无损结构化投影。保留已经被 replacement history 删除的 item 会造成展示与真实模型上下文不一致，并可能被再次发送给模型。

## Compact 模拟是 Cursor 锚点

决定：模拟只包含能够从 compact 请求和响应可靠推导出的规范 item：协议前缀、保留的 user messages 和一条 user 角色摘要。

原因：模拟用于消除 compact 完成到下一次请求之间的前端空窗，并提高 cursor 前缀匹配率；它不是对下一轮完整 input 的猜测。

## 当前 User 必须完整保留

决定：当前最后一条真实 user message 始终进入 replacement history 模拟，不受历史 user token 预算限制。预算只选择更早的 user messages。

原因：当前 Codex mid-turn compact 会保留当前 user。将它删除或截断会破坏任务边界、fingerprint 和后续 cursor 对齐。

## 压缩前 Assistant/Tool 不进入 Replacement History

决定：压缩前 assistant、reasoning 和工具 item 参与 summarization，但 compact 成功后不保留在新的 transcript/cursor 中。只有后续真实模型响应才能再次产生 assistant 节点。

原因：Codex replacement history 使用保留的 user messages 和摘要重建，不重放压缩前的 assistant/tool item。

## 不猜测重新注入的上下文

决定：模拟不提前构造下一轮可能出现的 developer、context 或 world-state item。下一份真实 effective input 是这些内容和位置的权威来源。

原因：Codex 可能在不同位置重新注入动态上下文。最长公共前缀和保守 pop 已能安全修正模拟尾部。

## Reasoning 不是活动组边界

决定：在两个可见 assistant 文本之间，reasoning、thinking 和工具调用属于同一活动组。只有可见 assistant 文本开始新的区段。

原因：Reasoning 表示同一操作序列中的进展。把它当作边界会产生大量“调用一个工具/思考完成”的碎片记录。

## 展开后保持原始顺序

决定：折叠标题可以聚合整个活动区段，展开内容必须按 provider item 顺序展示 reasoning 和工具。

原因：为了紧凑而重新排列工具会歪曲真实执行时间线。

## Exec 数量必须先经过静态投影

决定：活动组计数先应用安全的 exec 静态解析。无法证明的动态调用不进入精确计数。

原因：外层 exec 可能包含多个嵌套命令，但源码中出现调用表达式不等于运行时一定执行。具体边界见[Exec 展示决策](exec-display-decisions.md)。
