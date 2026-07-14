# Assistant 活动与 Compact 连续性边界日志

## 2026-07-13：展示连续性决定已被取代

本日志早先关于 `display_items` / `display_continuation` 的决定已被取代，不得实施。上下文地图呈现当前 Codex provider input，而不是重放 Desktop 对话事件流。将已被 compact 移除的 assistant 内容保留在 transcript 的任何位置，都会违背“transcript 是 input 的结构化投影”这一项目不变量。

自动 compact 现在模拟选定的用户消息（包括当前用户），再加上一条 user 角色的摘要。Transcript 和 cursor 接收相同的规范 item。压缩前的 assistant/工具 item 会在 Codex replacement history 移除它们时同步消失。只有收到真实模型响应后，压缩后的 assistant 才会出现。

## 2026-07-13：模拟是 Cursor 锚点，而不是猜测出的完整 Input

间隙模拟包含所有能够根据 compact 请求和响应可靠推导出的 replacement-history item。保留当前用户后，常见后续路径会更接近 cursor 前缀，也避免仅因遗漏当前用户而弹出并重新加入摘要。

Codex 仍可能重建 developer、context 或 world-state item，并将它们插入最终用户消息或摘要之前。代理不猜测这些 item。下一份原始 input 仍是权威来源，现有的最长公共前缀 diff 只需弹出并重建模拟结果中不匹配的后缀。

## 2026-07-13：Reasoning 不是工具组边界

决定：reasoning 和 thinking 区块与前后工具保留在同一个 assistant 活动区段中。可见的 assistant 文本仍是边界。

原因：当前渲染器会在每个非工具区块处结束工具组，从而反复产生“调用了 1 个工具 / 思考完成”的模式。Reasoning 表示一次操作序列内部的进展，不是新的用户可见任务区段。

## 2026-07-13：折叠区内保留时间线

决定：折叠记录聚合整个区段，展开面板则按 provider 顺序保留 reasoning 和工具 item。

原因：把所有工具都移到开头或结尾虽然能让紧凑视图整洁，却会在用户展开后歪曲执行时间线。

## 2026-07-13：规范 Input 与展示连续性相互独立

决定：将 compact 移除的活跃 assistant item 作为 `display_items` 存放在连续性节点上。只有规范的 `items` 会序列化为 provider input。

原因：最新的 Codex 本地 compact 请求快照会用 compact 摘要替换 mid-turn 工具产物。把这些产物留在规范 item 中会悄然将其再次发给模型并破坏 cursor 一致性。但如果将其从 UI 中删除，正在运行的任务中途又会失去可见的 assistant 节点。

## 2026-07-13：必须是 Mid-Turn 阶段

决定：只有轮次 metadata 明确报告自动 compact 且 `phase = mid_turn` 时，才创建 assistant 连续性。

原因：pre-turn compact 前最后一个 assistant 是已经完成的历史轮次。仅根据节点位置推断活动状态会保留错误节点。

## 2026-07-13：当前用户属于规范内容

决定：自动 mid-turn compact 保留最后一条真实用户消息。Compact prompt 本身仍被排除。

原因：之前的测试刻意移除了最后一条用户消息，但当前 Codex 的本地和远程 mid-turn 请求快照都会在压缩后的后续请求中保留该用户消息。移除它还会破坏上下文地图的轮次边界。

## 2026-07-13：节点 ID 在连续性重建后保持不变

决定：保留的当前用户节点和 assistant 连续性节点复用原 ID。

原因：展开、选择和锁定状态都以节点 ID 为键。即使保留内容，只要重新生成 ID，视觉上仍会像节点被替换。

## 2026-07-13：当前用户不受历史预算限制

决定：先完整保留当前用户 item，再只对更早的用户消息应用保留预算。

原因：第一个实现虽然保留了当前用户，却仍让它经过 20k 历史用户消息限制。足够大的当前请求可能因此被截断，并获得新的 fingerprint/节点身份。

## 2026-07-13：运行时验证

真实中文上下文地图确认 reasoning 不再拆分命令活动。与截图等价的 assistant 节点生成了 `已运行 10 条命令`、`已运行 25 条命令` 等紧凑分组，没有横向溢出或控制台错误。后端连续性通过了完整请求、compact 响应、后续请求和最终 assistant 响应的状态机验证，且没有在活动任务期间重启实时代理。

## 2026-07-13：Codex Input 不保留压缩前的 Assistant Item

已对照开源 Codex `9e552e9d15` 和当前 Desktop 发布版本验证。Compact 请求包含当前 assistant/工具产物，但本地 compact 会用保留的用户消息加摘要重建 replacement history。下一次后续请求包含这份 replacement history，不含压缩前的 assistant message。四条真实的 `compacted` rollout 记录中，replacement history 内 assistant 和工具 item 的数量均为零。

因此，Codex Desktop 显示的稳定 assistant 属于展示/轮次连续性，而不是 provider-input 保留。将 `display_items` 放在规范 `items` 之外符合这种分离关系。

## 2026-07-13：Cursor Diff 可在后续请求前重新注入上下文

决定：当请求 delta 追加规范的 developer/context/user item 时，暂时移除空的 assistant 连续性节点，完成规范追加后再将其恢复到 transcript 末尾。Assistant 响应追加仍直接指向该连续性节点。

原因：Codex 的 mid-turn 后续请求会在最后一条真实用户消息和摘要之前重新注入初始上下文，因此 cursor diff 可能替换整个 compact cursor。如果不先移除空的连续性节点，新追加的规范节点会落在它之后，最终 assistant 响应也会因此创建新节点。
