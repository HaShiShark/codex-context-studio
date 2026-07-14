# 上下文审查建议

## 目标

Suggestions 页面不再是本地红色节点/token 概览，而是自动上下文维护提案的审查界面。

主 Codex 对话空闲时，上下文模型可以创建一份待处理的压缩 transcript 提案。提案是临时的；只有用户明确应用后，它才会改变规范的代理 transcript。

## 产品流程

```text
主 Codex 轮次结束
-> 空闲时间窗口内没有新的主轮次
-> Codex 进程仍在运行
-> 后端让上下文模型基于当前代理 transcript 运行
-> 后端将 pending_context_review 存入代理 session
-> 如果 ctx 窗口处于隐藏状态，Electron 显示 Windows 通知
-> Suggestions 页面显示待处理审查
-> 用户可以在上下文地图中预览提议的 transcript
-> 用户可以关闭预览并返回实时 transcript
-> 用户可以应用提案，替换规范 transcript
-> 如果用户先继续主对话，则丢弃待处理审查
```

## 状态模型

`transcript` 仍是唯一的规范业务状态。待处理审查是一份临时审查产物：

```json
{
  "id": "审查 id",
  "session_id": "Codex session id",
  "status": "pending",
  "source": "auto_idle or manual",
  "base_transcript_version": 12,
  "created_at": "UTC 时间戳",
  "summary": "面向模型的简短审查摘要",
  "model": "上下文模型 id",
  "before": { "node_count": 18, "token_count": 42000 },
  "after": { "node_count": 7, "token_count": 13000 },
  "proposed_transcript": []
}
```

Session 列表只暴露审查 metadata。完整 session payload 才暴露 `proposed_transcript`，供前端预览。这样可避免通过每次实时 session-list 事件推送很大的 draft transcript。

## 预览规则

预览只发生在前端：

- 上下文地图从实时 `transcript` 切换到 `pending_context_review.proposed_transcript`。
- 在上下文地图上方显示预览横幅。
- 预览期间禁用节点锁定。
- 关闭预览后恢复实时 transcript。
- 预览期间不更新代理 transcript。

## 应用规则

应用审查是唯一的写入路径：

1. 代理检查 `review.id`。
2. 代理检查 `base_transcript_version == current transcript_version`。
3. 检查通过后，代理替换规范 transcript。
4. 代理清除 `pending_context_review`。
5. 前端关闭预览并渲染更新后的实时 transcript。

如果 transcript version 已变化，审查就已过期，必须丢弃或重新生成。首个实现直接返回冲突，不尝试合并。

## 丢弃规则

如果用户在应用待处理审查前继续主 Codex 对话，则自动丢弃该审查。

清理发生在两个位置：

- Prompt-submit hook 通过 `set_main_turn_state(..., running=true)` 将主轮次标记为运行中时，立即清除待处理审查。
- 当 hook 未运行时，`begin_request(...)` 也会作为兜底清除它。

这是有意设计的。此方案没有审查历史，也没有 restore 层。

## 自动触发契约

自动审查的范围是 Codex Desktop 或 ChatGPT Desktop 的一次进程运行。一次进程运行由可执行文件名、PID 和进程创建时间共同标识，因此重启不会与上一次运行混淆。支持的 Windows 可执行文件为 `Codex.exe` 和 `ChatGPT.exe`。

调度器为当前进程运行维护一套内存目标集合。只有某个对话的 session ID 在当前进程运行期间通过代理发送了常规主请求，该对话才会进入集合。仅加载旧代理 session 或打开 ctx 窗口不会使其成为目标。

每个目标 session 都根据最近一次主代理请求独立计时：

```text
session A 的主代理请求开始
-> 清除 A 的待处理审查（如果存在）
-> 取消 A 正在进行的自动审查（如果存在）
-> 持久化 A.last_proxy_request_at
-> 将 A 加入当前桌面进程的目标集合
-> 根据该请求时间戳设置 A 的空闲截止时间
-> 到达配置的时间间隔后，验证进程和存档
-> 仅当 A 始终保持空闲时才生成并持久化审查
```

生成前，以下条件必须全部仍然成立：

- 设置中已启用自动上下文建议。
- 同一次 Codex/ChatGPT 桌面进程运行仍然存活。
- 本次进程运行期间，该 session 曾通过代理发送主请求。
- 没有后续主代理请求重置该 session 的截止时间。
- 该 session 没有运行主轮次或其他上下文模型请求。
- 该 session 没有待处理审查。
- Codex 的 `sessions` 存储中仍存在该 session ID。
- 对于当前请求时间戳，当前 transcript version 尚未生成或跳过自动审查。

用户可配置的时间间隔默认为 10 分钟。调度器可以轮询，但资格应根据持久化的 `last_proxy_request_at` 计算，而不是根据调度器首次发现 session 空闲的时间计算。

如果自动审查仍在生成时，同一 session 又发送主请求，则必须从代理请求路径立即发出取消信号。模型的部分输出和 draft 的部分编辑都应丢弃。其他 session 的请求不会取消该审查。

## 进程与重启规则

按进程范围跟踪的状态刻意保持为临时状态，但待处理审查不是：

- 已完成的审查持久化在对应代理 session 上。
- 关闭 Codex/ChatGPT 后保留已完成的待处理审查。
- 重启 ctx 或桌面应用后保留已完成的待处理审查。
- 即使某条持久化审查所属的 session 不是新桌面进程运行的目标，它仍保持可见。
- 打开旧对话但不发送代理请求，不会删除审查。
- 同一 session 的下一次主代理请求会在转发前删除审查，并开始新的空闲计时。
- 应用审查会原子地替换 transcript 并删除审查。
- 明确丢弃审查只删除审查，不改变 transcript。
- 关闭自动建议会取消正在运行的自动生成，但不删除已完成的待处理审查。

## 自动模型契约

自动审查与手动 Context Workbench 共用 provider 配置、transcript 规范化、draft 编辑、持久化、预览、应用、丢弃和版本验证，但不共用手动聊天 prompt 或手动聊天历史。

自动模型接收完整 transcript 和自动审查专用指令。它必须执行保守、选择性的压缩：

- 不能仅仅因为可以压缩就进行压缩。
- 对干净、连贯的对话保持不变。
- 保留当前主题、近期工作集、需求、约束、决策、路径、未解决工作，以及未来工作所需的证据。
- 优先压缩已经完成或已被取代的阶段、重复输出、已纠正的错误、已放弃的方法和过时探索。
- 只有对话已经明确转向后端工作，且旧细节不太可能影响当前任务时，才压缩较早的前端阶段；当前后端节点保持不变。
- 信心不足时保留节点。
- 默认情况下，绝不把完整 transcript 折叠成一条摘要。
- 摘要节点的长度必须随有用源材料的数量而变化。较长的源区段可能需要较长的摘要；简短不是目标。

自动工具契约应尽可能在一次 `write_nodes` 调用中提交所有选定的节点操作。它的提案专用 schema 还携带简洁、面向用户的 `review_rationale`。手动页面保留当前的 `write_nodes` schema，绝不暴露该字段。

`review_rationale` 解释提案为何有用，以及是否可能影响当前任务。它描述的是待处理提案，而不是已经完成的操作：

- 使用“建议整理”或“将合并”等提案语气，不能使用“已压缩”或“已删除”。
- 说明建议整合哪个已完成主题或过时探索，以及它为何已不属于当前工作集。
- 说明当前任务是什么，以及提案为什么不应影响当前任务。
- 说明提案保留了哪些重要决策、约束和未解决工作。
- 存在实质性不确定性或风险时要明确提及。
- 不得暴露节点编号、token 数量、工具名称、transcript 内部结构或实现机制。UI 已经展示汇总统计。

## 设置

Context Model 设置区包含：

- `Automatic context suggestions`：持久化开关，默认启用，以保持当前功能默认行为。
- `Suggestion trigger interval`：仅在开关开启时显示；以分钟为单位编辑，默认值为 10 分钟。

关闭开关会阻止新的自动生成，并取消所有正在运行的自动审查。手动 `Analyze now` 仍然可用。重新启用后，从后续代理请求开始跟踪，不会追溯分析已经空闲的旧 session。

环境控制仍可用于开发和部署覆盖：

- `CODEX_CONTEXT_STUDIO_REVIEW_AUTO=0`：强制禁用自动生成。
- `CODEX_CONTEXT_STUDIO_REVIEW_IDLE_SECONDS`：为测试或运维覆盖保存的时间间隔。
- `CODEX_CONTEXT_STUDIO_REVIEW_POLL_SECONDS`：覆盖调度器轮询间隔。
- `CODEX_CONTEXT_STUDIO_REVIEW_TOAST=0`：禁用 Electron 通知 watcher。
- ctx 隐藏或最小化时，Electron watcher 会发送通知；点击通知会打开对应 session。

## 实现说明

- 将待处理审查存储在代理 session 上，不放在 React 状态或仅供 Web 使用的 AppState 中。代理 session 是 transcript 状态的事实来源。
- 复用现有 context-workbench 模型循环创建提议的 draft，但不调用现有 commit 路径。
- 只有上下文模型确实改变了 draft 时才存储提案。
- 不得重新引入旧的 `override`、`restore` 或多 transcript 状态。

## 当前构建日志

- 在编辑代码前创建了本文档。
- 第一个实现刻意限制为每个 session 只有一条待处理审查。
- 实时事件暴露审查 metadata；如果尚未加载 proposed transcript，前端会在预览前获取完整 session。
- 添加了 `pending_context_review` 的代理 session 存储，以及设置、应用和丢弃审查的 API endpoint。
- 为 Suggestions 添加了获取待处理审查、手动生成、应用和丢弃的 Web API。
- 将审查生成提取为共享后端函数，让手动触发和空闲触发使用相同的模型路径。
- 围绕桌面进程运行和本次运行期间发送主代理请求的所有 session，重建了 `backend/context_review_scheduler.py`。每个 session 根据持久化的请求时间戳和配置的时间间隔独立运行。
- 添加了活动 `.codex/sessions` 验证、`Codex.exe`/`ChatGPT.exe` 进程检测、异步的单 session 审查 worker，以及模型流式响应期间针对同一 session 的取消检查。
- 在代理 session 上添加了持久化的 `last_proxy_request_at` 和取消 revision 字段。即使较晚返回的模型结果与新主请求发生竞态，也会在存储层被拒绝。
- 将 Suggestions 提案与手动 Context Workbench 聊天 prompt 分离。空闲自动化和 `Analyze now` 都接收完整 transcript，不携带手动聊天历史，并使用包含 `review_rationale` 的单次调用 `write_nodes` schema。
- 为自动建议和条件式触发间隔添加了持久化 Context Model 设置，默认启用，时间间隔为 10 分钟。
- 围绕审查产物重建了 Suggestions 页面，从该页面移除了旧的红色节点概览。
- 添加了上下文地图预览模式。预览会将渲染的地图数据切换到提议的 transcript、禁用节点锁定，并显示关闭预览横幅。
- 为隐藏/最小化的 ctx 窗口添加了 Electron 通知。这是“用户无需先打开 ctx 也能看到建议”的路径。
- 更新通知 watcher 以支持多 session 调度：每条新创建的审查可以通知一次；Electron 启动时恢复的审查会标记为已查看，避免突发过期通知。
- 应用审查仍经过代理版本检查。过期提案会被拒绝并清除，而不是合并。
- 若干前端文件中的现有中文字符串在终端中显示为乱码。本次没有进行大范围文本清理，只添加了此功能所需的新字符串。
- 添加了调度器、设置、持久化、取消、存档和自动工具契约测试。Proxy/core 测试套件现在包括 3 项调度器测试、7 项设置测试、9 项工作台提交测试、7 项 session ID 测试和 24 项 proxy store 集成测试。
- 已通过 `node --check electron/context-window.cjs`、`npm run typecheck`、`python -m compileall backend simple_agent scripts`、`npm run test:proxy-core` 和 `npm run build:react` 验证。构建仍会报告现有的大 chunk 警告。
- 已在运行中的桌面布局中目视验证 Context Model 设置。启用状态显示 10 分钟间隔；禁用后隐藏间隔；重新启用后恢复间隔且没有布局重叠。
- 移除了预览/应用/丢弃后的临时成功文案。因主对话继续而使审查消失时，Suggestions 会直接返回常规空状态，不显示主轮次警告。
- 将自动审查文案重新定义为待处理、面向用户的理由，而不是实现报告。自动工具 schema prompt 禁止内部节点编号、token 数量和过去时的完成声明。
- Provider 下拉选项的悬停背景受菜单圆角裁剪，因此悬停表面会贴合可见的圆角菜单。
