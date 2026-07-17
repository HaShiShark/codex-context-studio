# 上下文智能建议

## 目标

Suggestions 页面用于审查上下文模型生成的维护提案。主 Codex 对话空闲后，系统可以基于当前 transcript 生成一份待处理建议；只有用户明确应用后，建议才会改变规范 transcript。

## 产品流程

```text
主 Codex 轮次结束
→ 空闲窗口内没有新主请求
→ 当前 Codex/ChatGPT 进程仍在运行
→ 上下文模型分析当前 transcript
→ 代理 session 保存一条待处理建议
→ ctx 隐藏或最小化时发送 Windows 通知
→ 用户预览、应用或丢弃建议
```

如果用户在应用前继续主对话，待处理建议立即失效并被删除。

## 状态模型

`transcript` 始终是唯一规范业务状态。建议只是存放在代理 session 上的临时审查产物：

```json
{
  "id": "review id",
  "session_id": "Codex session id",
  "status": "pending",
  "source": "auto_idle or manual",
  "base_transcript_version": 12,
  "created_at": "UTC timestamp",
  "summary": "面向用户的提案理由",
  "model": "context model id",
  "before": { "node_count": 18, "token_count": 42000 },
  "after": { "node_count": 7, "token_count": 13000 },
  "proposed_transcript": []
}
```

- 每个 session 同时最多有一条待处理建议。
- Session 列表只暴露 metadata，避免通过实时事件反复推送完整提案。
- 完整 session payload 才包含 `proposed_transcript`，供前端预览。
- 已完成建议持久化在对应 session 上；调度器的进程跟踪状态只保存在内存中。

## 预览、应用和丢弃

### 预览

- 预览只发生在前端。
- 上下文地图临时从实时 transcript 切换为 `proposed_transcript`。
- 预览期间显示明确横幅并禁用节点锁定。
- 关闭预览后恢复实时 transcript。
- 预览本身不写入代理状态。

### 应用

代理按顺序检查：

1. 建议 ID 是否匹配。
2. `base_transcript_version` 是否等于当前 transcript version。
3. 检查通过后，原子替换规范 transcript 并删除建议。

版本已经变化时，建议过期。系统返回冲突并删除旧建议，不尝试自动合并。

### 丢弃

明确丢弃只删除建议，不修改 transcript。新主请求也会在转发前删除同一 session 的待处理建议：Prompt-submit hook 是首选路径，代理 `begin_request` 是兜底。

本功能没有建议历史、restore 层或第二份持久 transcript。

## 自动触发契约

自动建议以一次 Codex Desktop 或 ChatGPT Desktop 进程运行为范围。进程身份由可执行文件名、PID 和创建时间共同确定，支持 `Codex.exe` 和 `ChatGPT.exe`。

只有本次进程运行期间确实通过代理发送过常规主请求的 session 才进入调度目标集合。加载旧 session 或打开 ctx 窗口不会触发自动建议。

每个目标 session 根据持久化的 `last_proxy_request_at` 独立计时。生成前必须同时满足：

- 自动建议已启用。
- 同一次桌面进程运行仍然存活。
- 本次运行期间，该 session 发送过主请求。
- 没有后续主请求重置空闲截止时间。
- 主轮次和其他上下文模型请求均未运行。
- 当前没有待处理建议。
- 该 session 仍存在于活动 `.codex/sessions` 存档中。
- 当前请求时间戳和 transcript version 尚未生成或跳过自动建议。

同一 session 在建议生成期间发送新主请求时，代理立即增加取消 revision。Web 模型循环在流式响应和工具轮次之间检查该 revision，丢弃部分输出和 draft。其他 session 的请求不会取消当前生成。

## 进程与重启

- 关闭 Codex/ChatGPT 不删除已经生成的建议。
- 重启 ctx、代理或桌面应用后，已完成建议仍然可见。
- 打开旧对话但不发送请求，不删除建议。
- 新桌面进程不会继承上一进程的调度目标集合。
- 关闭自动建议会取消正在运行的自动生成，但不删除已完成建议。
- 重新启用后只跟踪后续主请求，不追溯已经空闲的旧 session。

## 上下文模型契约

自动建议和手动 Context Workbench 共用 provider 配置、transcript 规范化、draft 编辑和底层写入能力，但不共用手动聊天 prompt 或聊天历史。

模型必须执行保守、选择性的维护：

- 不为了缩短而压缩干净、连贯的对话。
- 保留当前任务、近期工作集、需求、约束、决策、路径、未完成工作和未来需要的证据。
- 优先整理已完成或被取代的阶段、重复输出、已纠正错误、放弃的方法和过期探索。
- 信心不足时保留节点。
- 默认不把完整 transcript 折叠成一条摘要。
- 摘要长度随有用源材料变化，简短本身不是目标。

自动建议使用独立的 `write_nodes` schema，尽可能一次提交全部节点操作，并提供 `review_rationale`。手动 Workbench 的 schema 不暴露该字段。

`review_rationale` 必须：

- 使用“建议整理”“将合并”等提案语气。
- 说明建议处理的旧主题及其与当前任务的关系。
- 说明保留了哪些决策、约束和未完成工作。
- 对实质风险或不确定性作出提示。
- 不暴露节点编号、token 数量、工具名称或内部实现机制。

只有 draft 确实改变时才保存建议。

## 设置和通知

Context Model 设置包含：

- `Automatic context suggestions`：持久化开关，默认启用。
- `Suggestion trigger interval`：启用时显示，默认 10 分钟。

开发和部署可使用以下覆盖项：

- `CODEX_CONTEXT_STUDIO_REVIEW_AUTO=0`
- `CODEX_CONTEXT_STUDIO_REVIEW_IDLE_SECONDS`
- `CODEX_CONTEXT_STUDIO_REVIEW_POLL_SECONDS`
- `CODEX_CONTEXT_STUDIO_REVIEW_TOAST=0`

Electron watcher 检查所有 session 的新建议。启动前已经存在的建议先标记为已查看，避免重启后集中弹出旧通知；之后每条新建议只通知一次，点击通知打开对应 session。

## 验证要求

- 调度资格、独立计时、活动 session 检查和进程切换。
- 同 session 即时取消及跨 session 隔离。
- 建议持久化、重启恢复、应用、丢弃和版本冲突。
- 设置开关、时间间隔和通知去重。
- 自动工具契约、提案语气和内部信息隐藏。
- 预览期间不修改规范 transcript，且锁定操作被禁用。

具体实现取舍见[决策记录](decisions.md)。
