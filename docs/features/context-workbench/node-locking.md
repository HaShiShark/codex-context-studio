# 上下文地图节点锁定

本文记录当前已落地的“上下文地图节点锁定/解锁”和“上下文模型运行互斥”语义。后续维护时以本文描述的产品语义和当前代码实现为准，不再把它当成待实施计划。

## 1. 产品语义

1. 上下文地图中的每个 transcript node 都可以被锁定或解锁。
2. `developer` 节点默认锁定；其他节点默认不锁定。
3. 锁状态是会话级 UI/workbench 策略，不写入 provider item，不影响 cursor。
4. 双击节点编号或锁图标切换锁定状态。
5. 锁定节点不可选中，不进入上下文模型 snapshot，不分配给上下文模型可见的 Node #，上下文模型工具也不能读取、删除或修改。
6. 解锁后的 `developer` 节点和普通节点一致：可选中、进入 snapshot、获得 Node #、可被上下文模型工具读取和修改。
7. 上下文模型运行时，锁状态不能改变。
8. 主 Codex turn 运行时，上下文模型不能发起。
9. 上下文模型运行时，普通主 Codex 请求会在 proxy 层等待，直到上下文模型结束。
10. “主 Codex turn 运行中”表示整轮 agent turn 仍在进行中，不等同于某一个 `/v1/responses` stream 是否仍在运行。

## 2. 当前实现事实

- 前端 `MessageRecord.nodeId` 已保留 transcript node id；`normalizeConversation` 从 `TranscriptNode.id` 写入。
- 锁状态落在 proxy session metadata：`node_locks` 和 `node_lock_revision`。
- `node_locks` 只保存“和默认状态不同”的显式差异项：developer 默认 locked 不写入，developer unlocked 写 `false`，普通节点 locked 写 `true`。
- proxy API 已提供：
  - `POST /api/proxy/sessions/{session_id}/node-locks`
  - `POST /api/proxy/sessions/{session_id}/context-run`
  - `POST /api/proxy/sessions/{session_id}/main-turn`
- web backend 通过 `/api/proxy-session-node-lock` 转发前端锁切换请求。
- 前端上下文地图通过 `buildContextMapNodeMeta(messages, nodeLocks)` 统一计算 selectable、editable、locked、displayNodeNumber。
- 后端 snapshot、draft 和工具都使用同一套锁语义：locked 节点保留在 draft 里，但不暴露给上下文模型工具。
- context workbench commit 前会检查 `node_lock_revision`，发现锁状态已变化就拒绝 commit，要求用户重试。
- main-turn 运行态由 Codex lifecycle hook 和 turn-ended notify 维护，不靠 timeout 猜测。

## 3. 锁数据模型

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

1. key 使用 transcript node `id`。
2. 如果某个 node id 没有显式锁状态：
   - role 为 `developer`：默认 locked；
   - 其他 role：默认 unlocked。
3. 每次有效改变锁状态，`node_lock_revision += 1`。
4. transcript 替换、compact、节点删除后，会清理不存在的 node id 和等同默认状态的显式差异项。
5. 上下文模型 commit 应尽量保留未改节点的 id，否则锁状态会因为 id 改变而丢失。

集中判断语义：

```text
is_node_locked(node):
  if node.id in node_locks:
    return node_locks[node.id]
  return node.role == "developer"
```

对应实现集中在 `backend/node_locking.py`，前后端调用方都围绕这个语义展开。

## 4. 前端链路

主要文件：

- `react_app/src/types.ts`
- `react_app/src/utils.ts`
- `react_app/src/contextTokenWeight.ts`
- `react_app/src/components/ContextMapSidebar.tsx`
- `react_app/src/components/ContextMapNodeList.tsx`
- `react_app/src/WorkbenchWindow.tsx`

当前行为：

1. `MessageRecord.nodeId` 绑定 transcript node id。
2. locked 节点：
   - `selectable:false`
   - `editable:false`
   - `displayNodeNumber:null`
   - `locked:true`
3. unlocked 节点参与 Node # 计数。
4. 锁定节点时会从当前选择集合中移除，但展开状态可以保留。
5. 上下文模型运行时，锁切换按钮禁用。
6. 锁 API 失败时会回滚前端乐观状态，并刷新 session。

## 5. 后端 Snapshot / Draft / Tools

主要文件：

- `backend/web_context.py`
- `backend/web_runtime.py`
- `backend/web_state.py`
- `backend/web_handler.py`

当前行为：

1. `editable_context_node_entries(transcript, node_locks)` 只返回未锁节点。
2. `selected_display_node_numbers` 和 snapshot 使用同一套未锁节点编号。
3. `build_context_workspace_snapshot` 跳过 locked 节点。
4. `ContextWorkbenchDraft` 初始化时保留 locked 节点，但标记为不可编辑且不分配 Node #。
5. `_nodes_by_number` 只能解析未锁节点。
6. `committed_transcript()` 返回所有仍 active 的节点，所以 locked 节点不会因为不可见而被丢掉。
7. 上下文模型 prompt 明确说明 locked 节点不可见、不可读、不可编辑；解锁后的 developer 节点按普通节点处理。
8. commit 前检查 session 当前 `node_lock_revision` 是否等于 draft 开始时的 revision。

## 6. Proxy 持久化和 API

主要文件：

- `backend/proxy_store.py`
- `backend/proxy_session_storage.py`
- `backend/proxy_fastapi.py`
- `backend/realtime_events.py`

`POST /api/proxy/sessions/{session_id}/node-locks`

```json
{
  "node_id": "node_abc",
  "locked": true,
  "expected_revision": 3
}
```

行为：

1. session 不存在返回 404。
2. `node_id` 不存在返回 400。
3. `expected_revision` 不匹配返回 409。
4. context model 正在运行时返回 409。
5. 成功后更新 revision，并通过 realtime 事件发布 session 状态。

`POST /api/proxy/sessions/{session_id}/context-run`

```json
{
  "request_id": "ctx_turn_xxx",
  "running": true
}
```

行为：

1. `running=true`：标记该 session 的 context model 正在运行。
2. `running=false`：只有 `request_id` 匹配时才释放。
3. 释放后通知等待中的主 Codex 请求继续。

`POST /api/proxy/sessions/{session_id}/main-turn`

```json
{
  "turn_id": "codex_turn_xxx",
  "running": true
}
```

行为：

1. `UserPromptSubmit` hook 对普通 prompt 标记 main turn running。
2. `turn-ended` notify 清除 running。
3. `ctx/context` 控制命令不标记 main turn running。
4. main-turn 运行态不持久化，服务重启后不会误认为主 turn 仍在运行。

## 7. 关键竞态处理

### 用户在 context 模型运行中改锁

处理：

- 前端禁用；
- web API 拒绝；
- proxy API 拒绝；
- commit 前检查 `node_lock_revision`。

### 主 Codex 在 context 模型运行中进入 proxy

处理：

- 普通主 Codex 请求在 `STORE.begin_request` 前等待 context idle；
- internal context 请求不等待，避免自己等自己。

### 锁定节点被 commit 丢失

处理：

- draft 保留 locked 节点；
- locked 节点不可见不可编辑，但 `committed_transcript()` 会带回所有 active 节点。

### Node # 不一致

处理：

- 前端和后端都按“未锁节点才编号”的语义计算；
- 已有前端和后端测试覆盖 developer 默认锁、解锁 developer、锁定普通节点后的编号变化。

## 8. 回归测试重点

已覆盖或应持续保持覆盖：

1. developer 默认 locked，不参与 Node #。
2. developer 显式 unlocked 后获得 Node #。
3. 普通 user locked 后不参与 Node #，后续节点重新编号。
4. snapshot 排除 locked 节点。
5. unlocked developer 出现在 snapshot。
6. context tools 找不到 locked 节点。
7. locked 节点在 `committed_transcript()` 中被保留。
8. node_locks metadata 持久化。
9. transcript replace 后清理不存在 node id。
10. context running 时 node-lock API 返回 409。
11. context running 时主请求等待；context end 后继续。
12. internal context request 不等待自己。

## 9. 维护检查点

改相关逻辑前先确认：

- 锁状态是否仍然只存在 session metadata，不进入 provider item？
- 默认锁规则是否仍是 developer locked、其他 unlocked？
- snapshot、draft、前端 Node # 是否仍使用同一套未锁节点集合？
- locked 节点是否仍被 commit 保留？
- context model 运行时是否仍禁止改锁？
- 普通主请求是否仍在 `begin_request` 前等待 context idle？
- main-turn 状态是否仍来自 Codex lifecycle 信号，而不是 stream 状态或 timeout？
