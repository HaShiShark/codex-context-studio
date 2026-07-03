# 上下文地图节点锁定方案

本文记录“上下文地图节点锁定/解锁”及“上下文模型运行互斥”的实现方案，避免后续改动时忘记产品语义或破坏 proxy 主链路。

## 1. 产品语义

1. 上下文地图中的每个节点都可以被锁定或解锁。
2. `developer` 节点默认锁定；其他普通节点默认不锁定。
3. 锁状态需要持久化到会话级状态。
4. 双击节点编号或锁图标切换锁定状态。
5. 单击和拖拽仍然用于选择未锁定节点。
6. 锁定节点：
   - 不可选中；
   - 不进入上下文模型 snapshot；
   - 不分配给上下文模型可见的 Node #；
   - 上下文模型工具无法读取、删除或修改；
   - 在地图中使用当前 developer 锁态风格，不显示普通 preview 气泡。
7. 解锁后的 `developer` 节点与普通节点一致：
   - 可选中；
   - 进入 snapshot；
   - 获得 Node #；
   - 可被上下文模型工具读取和修改。
8. 上下文模型运行时，任何节点锁状态都不能改变。
9. 主 Codex 运行时，上下文模型不能发起。
10. 上下文模型运行时，主 Codex 请求在 proxy 层等待，直到上下文模型结束。
11. “主 Codex 运行中”必须代表整轮 agent turn 仍在进行中，而不是某个上游 `/v1/responses` 请求仍在进行中。agent 在一轮里可能多次请求模型，也可能在两次模型请求之间本地执行工具；这整段时间都算运行中。
12. proxy 的 `status/is_running` 只描述请求/stream 状态，不能直接用于锁住上下文模型输入。
13. 主 Codex turn 运行态由 Codex 生命周期信号维护：`UserPromptSubmit` 表示普通 prompt 的 turn 开始，`notify turn-ended` 表示 turn 结束。

## 2. 当前代码事实

1. 前端 `MessageRecord` 目前没有保留 transcript node id，但 `TranscriptNode` 类型有 `id`。
2. 当前前端不可选逻辑在 `react_app/src/contextTokenWeight.ts` 的 `buildContextMapNodeMeta`，它把 `developer` 等内部节点固定为 `selectable:false`。
3. 当前展开逻辑和选择逻辑分离，所以 developer 可以展开，但不能选择。
4. 后端 snapshot 由 `backend/web_context.py` 的 `build_context_workspace_snapshot` 生成。
5. 当前 `editable_context_node_entries` 会跳过 developer，因此 developer 没有 Node #。
6. `ContextWorkbenchDraft` 会保留内部节点并在最终 commit 时带回 transcript；这个机制可以复用来保留任意 locked 节点。
7. 上下文模型工具是 draft 内多轮执行，最后统一 `committed_transcript()`，再同步回 proxy transcript。
8. web server 和 proxy 是不同进程：
   - web server: `backend.web_server` / `backend.web_handler`
   - proxy: `backend.proxy_fastapi`
9. web server 里的 `AppState.acquire_session_request` 只能保护 web 侧上下文模型，不足以阻止主 Codex 原始请求进入 proxy。
10. 本地 Codex 配置已有 `hooks.UserPromptSubmit`，hook payload 可拿到 `session_id` / `turn_id`。
11. 本地 Codex 配置已有 `notify = [..., "turn-ended"]`，需要用 wrapper 保留原 notify 行为，同时通知本项目释放 main-turn 运行态。

## 3. 数据模型

锁状态不能写进 provider item，也不能影响 cursor。它是会话级 UI/workbench 策略，应放在 session metadata。

推荐字段：

```json
{
  "node_locks": {
    "node_abc": true,
    "node_def": false
  },
  "node_lock_revision": 3
}
```

规则：

1. 使用 transcript node `id` 作为 key。
2. 如果某个 node id 没有显式锁状态：
   - role 为 `developer`：默认 locked；
   - 其他 role：默认 unlocked。
3. 每次有效改变锁状态，`node_lock_revision += 1`。
4. `node_locks` 只保存“和默认状态不同”的 override：developer locked 和普通节点 unlocked 都不写入；developer unlocked 写 `false`，普通节点 locked 写 `true`。
5. transcript 替换、compact、节点删除后，需要清理不存在的 node id 和等同默认状态的 override。
6. 新出现的 developer 节点默认 locked。
7. 新插入的 user/assistant 节点默认 unlocked。

重要注意：如果上下文模型 commit 会重建 transcript node，必须尽量保留未改节点的 id。否则锁状态会因为 id 改变而丢失。

## 4. 锁策略函数

前后端都需要统一语义：

```text
is_node_locked(node):
  if node.id in node_locks:
    return node_locks[node.id]
  return node.role == "developer"
```

后端建议集中放在 `backend/web_context.py` 或新建轻量 helper，避免 snapshot、draft、工具各写一套判断。

前端建议在 `contextTokenWeight.ts` 里由 `nodeId + role + nodeLocks` 计算 meta。

## 5. 前端改动

### 5.1 类型和数据

修改：

- `react_app/src/types.ts`
- `react_app/src/utils.ts`
- `react_app/src/api.ts`

需要做：

1. `MessageRecord` 增加 `nodeId: string`。
2. `normalizeConversation` 从 `TranscriptNode.id` 保留 `nodeId`。
3. proxy session 类型增加：
   - `node_locks?: Record<string, boolean>`
   - `node_lock_revision?: number`
4. 新增锁状态 API 调用函数。

### 5.2 地图 meta

修改：

- `react_app/src/contextTokenWeight.ts`
- `react_app/src/components/ContextMapSidebar.helpers.ts`

需要做：

1. `buildContextMapNodeMeta(messages, nodeLocks)` 接收锁状态。
2. locked 节点：
   - `selectable:false`
   - `editable:false`
   - `displayNodeNumber:null`
   - `locked:true`
3. unlocked 节点才参与 Node # 计数。
4. `MessageStat` 增加 locked 信息，供 minimap 和建议列表过滤。

### 5.3 地图交互

修改：

- `react_app/src/components/ContextMapSidebar.tsx`
- `react_app/src/components/ContextMapNodeList.tsx`
- `react_app/src/components/ContextMinimap.tsx`
- `react_app/src/components/ContextMapSidebar.polish.css`
- `react_app/src/workbench-window.css`

需要做：

1. gutter 对所有节点存在：
   - unlocked 显示编号；
   - locked 显示锁图标。
2. hover 显示轻量 tooltip：`双击锁定/解锁`。
3. `onDoubleClick` 切换锁状态。
4. locked 节点单击不进入选择逻辑。
5. unlocked 节点单击/拖拽逻辑保持现状。
6. 锁定一个节点时，从 `selectedIndexes` 中移除该节点；展开状态不需要清理，锁定节点仍然可以展开查看。
7. 上下文模型运行时禁用锁切换按钮。
8. 锁 API 失败时需要恢复 UI 或刷新 session，并给用户可见反馈，不能静默失败。

## 6. 后端 snapshot / tool 改动

### 6.1 web session 状态

修改：

- `backend/web_constants.py`
- `backend/web_state.py`
- `backend/web_handler.py`
- `backend/web_runtime.py`

需要做：

1. `SessionState` 增加：
   - `node_locks: dict[str, bool]`
   - `node_lock_revision: int`
2. `upsert_proxy_session` 从 proxy payload 同步锁状态。
3. 新增 web API，给前端切换锁用。
4. 如果 `session.active_request_mode == "context"`，锁 API 必须拒绝。
5. 主请求运行中允许改锁；锁状态按 node id 存储，不影响当前主请求，只影响后续上下文模型可见范围。

### 6.2 snapshot 和 Node #

修改：

- `backend/web_context.py`

需要做：

1. `editable_context_node_entries(transcript, node_locks)` 改为“未锁节点 entries”。
2. `selected_display_node_numbers` 使用同一套未锁 entries。
3. `build_context_workspace_snapshot` 跳过 locked 节点。
4. snapshot 文案改为：
   - locked 节点不会展示；
   - Node # 只代表当前未锁节点；
   - 解锁 developer 后可以像普通节点一样处理。

### 6.3 Draft 和工具

修改：

- `backend/web_context.py`
- `backend/web_runtime.py`

需要做：

1. `ContextWorkbenchDraft(transcript, selected_indexes, node_locks)` 接收锁状态。
2. draft 初始化时：
   - locked 节点保留在 `self.nodes`；
   - `editable=False`；
   - `source_node_number=None`；
   - `status="locked"`。
3. `_nodes_by_number` 只能找到未锁节点。
4. `committed_nodes()` 仍返回所有 active 节点，确保 locked 节点不会被 commit 丢掉。
5. `build_draft_snapshot_text` 仍只显示未锁节点。
6. 工具 schema/prompt 清理旧描述：
   - 不再说 developer 永远内部；
   - 改成 locked 节点不可见不可操作；
   - unlocked developer 是普通节点。
7. commit 前检查 `node_lock_revision` 是否仍等于 turn 开始时的 revision；不一致则拒绝 commit 并提示用户重试。

## 7. Proxy 持久化和 API

修改：

- `backend/proxy_store.py`
- `backend/proxy_session_storage.py`
- `backend/proxy_fastapi.py`
- `backend/realtime_events.py` 如 payload schema 需要同步

### 7.1 持久化

`ProxySession` 增加：

```python
node_locks: dict[str, bool]
node_lock_revision: int
```

读写 metadata：

- load: 读取 `node_locks` / `node_lock_revision`
- save: 写入 `node_locks` / `node_lock_revision`
- payload: 返回给前端和 web server

替换 transcript 后：

1. 清理不存在的 node id。
2. 保留仍存在 node id 的显式锁状态。
3. 新 developer 走默认 locked，无需立即写入 node_locks。

### 7.2 锁状态 API

proxy 新增：

```text
POST /api/proxy/sessions/{session_id}/node-locks
```

payload：

```json
{
  "node_id": "node_abc",
  "locked": true,
  "expected_revision": 3
}
```

行为：

1. session 不存在返回 404。
2. node_id 不存在返回 400。
3. expected_revision 不匹配返回 409。
4. context running 时返回 409。
5. 成功后更新 revision，并通过 websocket 发布 session update。

web server 可新增对应转发 API，例如：

```text
POST /api/proxy-session-node-lock
```

### 7.3 上下文模型运行锁

因为 web server 和 proxy 是不同进程，需要 proxy 也知道 context 是否运行。

proxy 新增：

```text
POST /api/proxy/sessions/{session_id}/context-run
```

payload：

```json
{
  "request_id": "ctx_turn_xxx",
  "running": true
}
```

行为：

1. running=true：标记该 session context running。
2. running=false：只有 request_id 匹配才释放。
3. 释放后通知等待中的主 Codex 请求继续。
4. 不使用 stale timeout 兜底；main turn 只由 Codex lifecycle 信号开始/结束。

web server 在上下文模型 turn：

```text
acquire_session_request(context)
-> notify proxy context-run running=true
-> run_context_chat_turn
-> commit
-> notify proxy context-run running=false
-> release_session_request(context)
```

## 8. 主 Codex 运行态和等待逻辑

主 Codex 是否运行，分两层：

1. `main_turn`：整轮 agent turn 是否还活着。用于决定上下文模型能不能发起。
2. proxy request `status/is_running`：当前是否有一条主 `/v1/responses` 请求正在 stream。只用于 proxy 自己的请求展示和 transcript 捕获，不能锁住上下文模型。

### 8.1 main_turn 生命周期

修改：

- `scripts/codex-context-hook.ps1`
- `scripts/codex-turn-ended-notify.ps1`
- `backend/proxy_fastapi.py`
- `backend/proxy_store.py`
- `backend/web_state.py`
- `react_app/src/WorkbenchWindow.tsx`

流程：

```text
普通 UserPromptSubmit
-> hook 读取 session_id / turn_id
-> POST /api/proxy/sessions/{session_id}/main-turn { running: true, turn_id }
-> 前端收到 is_main_turn_running=true
-> 上下文模型输入禁用

Codex notify turn-ended
-> wrapper 读取 notify payload；如果没有 session_id，就读取 hook 写下的 active-turn 文件
-> POST /api/proxy/sessions/{session_id}/main-turn { running: false, turn_id }
-> 前端收到 is_main_turn_running=false
-> 上下文模型输入恢复
```

注意：

1. `/context` / `ctx` 这种打开手动页的 hook 命令会被 hook 拦截，不会进入主 agent turn，因此不能标记 main turn running。
2. `main_turn` 是运行时事实，不持久化到 session metadata，避免服务重启后误认为仍在运行。
3. 不使用 stale timeout；如果 `turn-ended` 通知没有到达，运行态不会被时间自动改写，避免用猜测状态替代真实 lifecycle。
4. 当用户提交 `ctx` 打开上下文页时，如果能识别当前 session，hook 会清掉该 session 的旧 main-turn running；这是明确的用户输入 lifecycle 信号，不是时间兜底。
5. 前端右侧上下文模型只能看 `is_main_turn_running`，不能再看 `status/is_running`。
6. web server 的 `active_request_mode="main"` 也只能由 `is_main_turn_running` 映射，不能由 proxy request `is_running` 映射。

### 8.2 context 运行时主请求等待

修改：

- `backend/proxy_fastapi.py`
- `backend/proxy_store.py`

在 `/v1/responses` 中：

1. 识别 `is_internal_context`。
2. 识别 `capture_proxy_session`。
3. 只有普通主 Codex 请求，也就是 `capture_proxy_session=True` 时，才等待 context idle。
4. 内部 context 请求不能等待自己。
5. 等待应发生在 `STORE.begin_request` 之前，避免 context 运行期间主请求修改 transcript/cursor。

伪流程：

```text
if capture_proxy_session:
  session_id = session_id_for_request(...)
  await STORE.wait_context_idle(session_id)
  session, forwarded_body = STORE.begin_request(...)
```

## 9. 关键竞态和处理

### 9.1 用户在 context 模型运行中改锁

风险：模型基于旧 snapshot 执行工具，最后 commit 覆盖新锁规则。

处理：

- 前端禁用；
- web API 拒绝；
- proxy API 拒绝；
- commit 前检查 lock revision。

### 9.2 主 Codex 在 context 模型运行中进入 proxy

风险：主请求先 append transcript，context 模型随后用旧 draft commit 覆盖。

处理：

- proxy 等待 context idle；
- 等待放在 `STORE.begin_request` 之前。

### 9.3 context 内部请求误等待自己

风险：上下文模型通过 codex-proxy provider 调 `/v1/responses`，被 proxy 等待 context idle，造成死锁。

处理：

- `is_internal_context=True` 时不等待。

### 9.4 锁定节点被 commit 丢失

风险：locked 节点不在 snapshot，工具 commit 时如果 draft 只包含可见节点，会丢 locked。

处理：

- draft 保留 locked 节点，只是不分配 Node # 和不可编辑；
- `committed_nodes()` 包含 locked active 节点。

### 9.5 Node # 不一致

风险：前端显示 Node #3，snapshot 里 Node #3 指向另一个节点。

处理：

- 前端和后端都按同一语义：未锁节点才编号；
- 测试覆盖 developer 默认锁、解锁、锁定普通节点后的编号。

### 9.6 node id 在 commit 后变化

风险：锁状态按 id 保存，如果未改节点 id 被重建，锁状态丢失。

处理：

- normalize/commit 路径尽量保留已有 node id；
- 新建节点生成新 id；
- 删除节点清理 lock map。

## 10. 测试计划

### 10.1 前端 contract tests

覆盖：

1. developer 默认 locked，不参与 Node #。
2. developer 显式 unlocked 后获得 Node #。
3. 普通 user locked 后不参与 Node #，后续节点重新编号。
4. selected indexes 会过滤 locked 节点。

### 10.2 后端 web_context tests

覆盖：

1. snapshot 排除 locked 节点。
2. unlocked developer 出现在 snapshot。
3. `get_nodes` 找不到 locked 节点。
4. unlocked developer 可被 `get_nodes/write_nodes/write_items` 操作。
5. locked 节点在 `committed_transcript()` 中被保留。

### 10.3 proxy tests

覆盖：

1. node_locks metadata 持久化。
2. transcript replace 后清理不存在 node id。
3. context running 时 node-lock API 返回 409。
4. context running 时主请求等待；context end 后继续。
5. internal context request 不等待自己。

### 10.4 手动验证

1. 打开上下文地图，developer 默认锁定。
2. 双击 developer 解锁，能选中，进入右侧模型引用。
3. 让上下文模型修改解锁 developer，确认 transcript 更新。
4. 锁定普通 user 节点，确认它不进 snapshot、工具不可读。
5. 上下文模型运行时，锁按钮不可操作。
6. 上下文模型运行时，在主 Codex 发消息，请求等待，完成后继续。

## 11. 实施顺序

1. 加 node id 透传和前端锁 meta。
2. 加 proxy/web session 锁状态持久化和锁 API。
3. 改 snapshot / draft / tools 使用锁策略。
4. 加 context running proxy gate。
5. 补测试。
6. 运行前后端测试。
7. 需要时用本地 UI 手动验一次。
