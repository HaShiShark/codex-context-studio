# Assistant 活动与 Compact 模拟

## 目标

1. 即使 reasoning item 与工具调用交替出现，也让同一段 assistant 活动在视觉上保持紧凑。
2. 在后续请求到达前的间隙中，模拟 Codex 自动压缩后的 replacement history。
3. 保持无损 provider input 契约。Transcript 中的每个 item 都必须是规范的 provider item。

## Assistant 活动展示

只有可见的 assistant 文本才会切分 assistant 记录。在由文本划分的每个区段内，reasoning、thinking 和工具区块共同组成一个活动组。

- 包含工具的区段渲染为一条折叠的活动记录。
- Reasoning 不会结束或拆分工具组。
- 展开记录后，保持原始的 reasoning/工具顺序。
- 计算分组数量前，先投影静态嵌套 exec 调用。
- 完全由命令或 exec 活动组成的分组使用本地化标签 `已运行 N 条命令`。真正混合了原生工具的分组使用通用工具标签。
- 包含 reasoning 但不含工具的区段沿用现有 reasoning 展示方式。

可见的 assistant 文本位于活动组之外，并保持原有顺序。文本是开始新活动组的唯一边界。

## 自动 Compact 模拟

Codex 可能在一个仍活跃的用户轮次中间执行 compact。本地 compact 请求包含压缩前的完整 input，包括当前用户和当前 assistant/工具活动。摘要完成后，Codex 的 replacement history 会保留选定的用户消息（包括当前用户），随后追加一条 user 角色的摘要。压缩前的 assistant/工具 item 不再属于 provider input。

自动 compact 成功后：

1. 保留符合条件的用户消息，包括当前最后一条用户消息。
2. 将生成的 compact 摘要作为 user 角色的 provider message 追加。
3. 仅用这些规范 item 同时重建 transcript 和 cursor。
4. 从模拟 transcript 中删除压缩前的 assistant/工具 item。

用户消息的保留沿用本地 compact 现有的 token 预算选择规则，不会特别排除当前用户。

Codex 的下一次后续 input 可能重新插入 developer/context/world-state item。Cursor diff 会把这份真实 input 与模拟 cursor 比较。如果模拟的用户消息/摘要序列已经是前缀，则不弹出任何模拟 item。如果 Codex 在该序列中插入上下文，则弹出不匹配的后缀，并根据真实 input 重建。常规响应路径随后会把新生成的 assistant item 同时追加到 transcript 和 cursor。

## 范围之外

- 所有 compact 路径都不得创建仅用于展示的 transcript item。
- 当 Codex replacement history 删除压缩前的 assistant/工具 item 时，这些 item 也会被删除。
- 不在间隙中猜测 Codex 内部重建的初始上下文；由后续请求提供并确定其位置。

## 验证

- 前端契约测试覆盖被 reasoning 分隔的工具聚合和文本边界。
- Compact controller 测试覆盖当前用户保留和规范模拟。
- Proxy core 测试覆盖嵌套 compact metadata 和后续 cursor diff。
- Transcript codec 测试继续保证 provider item 往返无损。
- 使用包含活跃 assistant 节点的中文真实上下文地图页面进行检查。

## 2026-07-13 验证结果

- 与截图等价的 assistant 节点在六个可见文本区段中渲染出五个活动组，不再产生几十条被 reasoning 拆开的工具记录。其中一个区段将 25 条命令折叠为 `已运行 25 条命令`。
- 工具活动内已完成的 reasoning 只出现在父级折叠区内；仅包含 reasoning 的区段仍独立可见。
- 未发现活动组溢出或页面控制台错误。
- `npm test` 已通过，其中包括 14 项 transcript codec 测试、7 项 compact controller 测试、17 项 proxy core 测试、全部扩展后端测试，以及前端 activity 和 exec-display 契约测试。
- 自动 mid-turn compact 模拟保留了当前用户、删除了压缩前的 assistant，并在接收完全相同的后续 input 时保持 transcript 不变且未触发 tail conflict。
- 后端、前端和测试代码中均不再存在已弃用的 display-continuity 字段。
- Vite 生产构建成功完成。
