# Hash Context Proxy — 最终设计方案

## 核心原则

1. **Transcript = input 数组 1:1**：transcript 是 input items 按 role 分节点的结构化投影，两者严格等价，没有过滤，没有特殊跳过。
2. **单一真相**：所有上下文的读取、编辑、展示都只面对 transcript；每轮向上游发送的 input 由 transcript 重组而来。
3. **无透传/重组分支**：系统只有一条路径，不判断"要不要透传"。无编辑时重组结果与原始 input 逐字节等价，自然透传。

---

## 状态模型

```
ProxyState
├── transcript: list[TranscriptNode]   # 唯一业务真相
├── codex_input_cursor: list[dict]     # 已确认吸收的 provider items，辅助游标
├── tail_conflict: bool                # 上次 pop fingerprint 不匹配的标记
├── compact_pending: bool              # compact 进行中
└── compact_kind: str                  # "auto" | "manual" | ""

TranscriptNode
├── id: str                            # 节点唯一 ID
├── role: str                          # 取自 item 原始 role 字段
├── items: list[NodeItem]              # [{"kind": ..., "providerItem": {...}}, ...]
└── source_map: dict[str, str]         # fingerprint → "items[n]"，pop 校验用
```

`codex_input_cursor` 不是业务状态，只用于算增量。它记录代理已确认吸收的 provider items 序列，包含 assistant 的输出 items（response 完成时 append）。

---

## TranscriptNode 归组规则

以 item 的结构类型判断，不靠 role 文本。

| input item 类型 | 归组方式 |
|---|---|
| `role: user` Message | 新建 user 节点，`current_assistant = None` |
| `role: developer` / `role: system` Message | 新建 developer 节点，`current_assistant = None` |
| `role: assistant` Message | 归入 `current_assistant`，不存在则新建 |
| Reasoning | 归入 `current_assistant`，不存在则新建 |
| FunctionCall / CustomToolCall / LocalShellCall / ToolSearchCall / WebSearchCall / ImageGenerationCall | 归入 `current_assistant`，不存在则新建 |
| FunctionCallOutput / CustomToolCallOutput / ToolSearchOutput | 按 `call_id` 找最近的 assistant 节点；找不到则挂最近 assistant；再没有则新建 assistant 节点 |
| Compaction / ContextCompaction | 新建节点，role 取原始值 |
| AdditionalTools | 新建 developer 节点（responses_lite 模式的工具前缀） |
| **其他任何类型** | 新建节点，role 取原始值；无 role 字段则 role = "unknown" |

没有任何 item 被跳过或过滤。

---

## Fingerprint

比较两个 item 是否"相同内容"时，必须先 normalize 再 hash，排除动态字段：

- **排除**：`id`（服务端生成的 UUID）
- **保留**：`role`、`content`、`call_id`、`name`、`arguments`、`type`、`output` 等语义字段

> ⚠️ Reasoning item 含 `encrypted_content`（不透明 Base64 blob）。需用真实请求日志验证：Codex 在后续请求里重放同一 reasoning item 时，`encrypted_content` 是否 byte-identical。若不稳定，该类 item 只能靠 `id` 或位置做匹配，须单独处理。

---

## 每轮请求处理流程

```
handle_request(state, body):

  1. 检测是否为 compact 请求
     → body.client_metadata["x-codex-turn-metadata"] 解析后 request_kind == "compaction"
     → 若是：state.compact_pending = True
             state.compact_kind = turn_meta["trigger"]  // "auto" | "manual"

  2. cursor diff
     new_input = body["input"]
     prefix_len = longest_common_prefix_len(cursor, new_input)  // fingerprint 比较
     pop    = cursor[prefix_len:]
     append = new_input[prefix_len:]

  3. 更新 transcript
     tail_conflict = False  // 每轮复位
     TranscriptDeltaApplier.pop(transcript, pop)    // fingerprint 校验
     TranscriptDeltaApplier.append(transcript, append)  // 归组追加

  4. compact 请求的 prompt 替换（仅在 compact_pending == True 时）
     → 找 transcript 中最后一个 user 节点，若内容是 Codex 原始 compact prompt
     → 替换为自定义 prompt（修改写入 transcript）

  5. 重组 input
     body["input"] = TranscriptCodec.to_input_items(transcript)

  6. 转发
```

### Pop 规则

从 transcript 尾部逐项检查 fingerprint：
- 匹配 → 移除该节点/item
- 不匹配 → 停止 pop，标记 `tail_conflict = True`，跳过剩余 pop，直接进入 append

保守策略：宁可多留旧尾巴，也不误删可能是用户编辑过的内容。

---

## 响应处理流程

```
handle_response(state, sse_stream):

  边转发 SSE 给 Codex 边解析：

  response_items = []
  for event in sse_stream:
    转发给 Codex client（实时）

    if event.type == "response.output_item.done":
      response_items.append(event.item)

    elif event.type == "response.completed":
      if state.compact_pending:
        CompactController.on_compact_success(state, response_items)
      else:
        // 正常 turn：assistant output items 追加到 cursor
        state.codex_input_cursor.extend(response_items)
        推送 transcript_update 到前端
      break
```

> SSE 解析器必须处理跨 chunk 边界：一个 SSE event 可能跨多个 HTTP chunk，不能按 chunk 切割。

---

## Compact 处理（仅 local compact）

### 检测

```python
turn_meta = json.loads(body["client_metadata"]["x-codex-turn-metadata"])
is_compact = turn_meta.get("request_kind") == "compaction"
compact_kind = turn_meta.get("trigger")  # "auto" | "manual"
```

local compact **没有** `CompactionTrigger` item，`CompactionTrigger` 仅出现在 remote compact v2（不使用）。

### Compact 请求中的 prompt 替换

Codex 在 local compact input 末尾追加一条 `role: user` message，内容是内置 summarization prompt。代理在步骤 4 中将其替换为自定义 prompt，修改直接写入 transcript，再重组 input 转发。transcript 存储的是替换后的 prompt。

### Compact 成功后重建 transcript 和 cursor

**为什么要模拟**：不模拟也能工作——compact 完成后直接清空 cursor，下一轮 Codex 请求发来时 compute_diff 会把完整 input 全部 append 进 transcript。但前端会有空窗期（compact 完成到 Codex 下一轮请求之间，transcript 为空）。因此 compact 成功后立刻构建模拟状态，供前端即时展示。

**模拟内容**：只重建 [N 条 user + summary]，不包含 developer/context items（这些等下一轮 Codex 请求进来后由 append 补全）。

**手动 compact：**
```
从 compact 前 transcript 提取所有 user message（排除 summary message）
按 20k token 预算从末尾选取 N 条 → selected_user_msgs

new_items = selected_user_msgs + [summary as user message]

state.transcript = TranscriptCodec.to_transcript(new_items)   // 前端立即可见
state.codex_input_cursor = new_items                           // diff 锚点
state.compact_pending = False

// 下一轮 Codex 发：new_items + [developer context] + [新 user msg]
// compute_diff: prefix = new_items，append = [developer + 新 user] → 自然接上
```

**自动 compact（mid-turn，assistant 还在运行）：**
```
从 compact 前 transcript 提取所有 user message（排除 summary message，排除最后一条进行中的 user）
按 20k token 预算从末尾选取 N 条 → selected_user_msgs

new_items = selected_user_msgs + [summary as user message]

state.transcript = TranscriptCodec.to_transcript(new_items)   // 前端立即可见
state.codex_input_cursor = new_items                           // diff 锚点
state.compact_pending = False

// Codex 继续跑完这一轮
// 下一轮发：new_items + [被排除的最后那条 user] + [assistant output]
// compute_diff: prefix = new_items，append = [user + assistant] → 自然接上
```

区分手动/自动：`compact_kind == "auto"` 时排除最后一条进行中的 user message。

### Summary message 识别

compact summary 在 input/transcript 中是普通 `role: user` Message，内容以 `LOCAL_COMPACT_SUMMARY_PREFIX` 固定前缀开头。collect_user_msgs 时需排除此类 message：

```python
def is_summary_message(text: str) -> bool:
    return text.startswith(LOCAL_COMPACT_SUMMARY_PREFIX)
```

前端可通过此前缀做差异化展示，role 仍为 `user`。

### Compact 失败

保留旧 transcript 和 cursor 不变，清除 `compact_pending`，可标记 `compact_error` 供前端展示。

---

## 编辑工具

编辑操作防错字段：

```
EditRequest:
  revision_id: str        # 单调递增整数，每次成功编辑后 +1
  node_idx: int           # 节点下标（display index）
  node_id: str            # 节点 UUID，防下标偏移
  item_range: [int, int]  # 可选，节点内 item 级操作
  replacement: list       # 替换内容
```

校验：`revision_id` 匹配且 `node_id == transcript[node_idx].id`，否则拒绝。编辑成功后 `revision += 1`，下一轮 rebuild input 自动包含编辑内容。

---

## 边界情况汇总

| 情况 | 处理方式 |
|---|---|
| 完全相同的重试请求 | prefix 覆盖全部 → pop/append 均空 → transcript 不变 → 幂等 |
| 正常新消息追加 | pop 空，append 新 items → 追加到 transcript |
| 工具调用续写（前同尾变） | pop 旧尾，append 新尾 → 自然替换 |
| pop fingerprint 不匹配 | 跳过 pop，仅 append，标 `tail_conflict = True` |
| 新 thread 接入（cursor 为空） | prefix_len=0, pop=[], append=完整 input → transcript 全量追加，无需特殊处理 |
| local compact 完成后下一轮 | cursor 已在 compact 成功时主动重建，compute_diff 自然接上 |
| 孤立 FunctionCallOutput | 按 call_id 反查 assistant 节点；找不到则挂最近 assistant |
| responses_lite 模式头部 | AdditionalTools + developer Message 在 input 最前，进 transcript 为 developer 节点 |
| Compaction / ContextCompaction item | 按原始 role 新建节点，进 transcript |
| Reasoning encrypted_content 稳定性 | **待验证**：用真实日志确认跨轮重放时 blob 是否 byte-identical |

---

## 模块划分

```
backend/
├── transcript_codec.py          # input items ↔ transcript 无损互转，归组状态机
├── codex_input_cursor.py        # compute_diff，longest_common_prefix，fingerprint normalize
├── transcript_delta_applier.py  # pop (fingerprint 校验) + append (归组追加)
├── compact_controller.py        # compact 状态机，cursor 重建，prompt 替换，collect_user_msgs
├── transcript_editor.py         # 编辑操作，revision_id + node_id 双重防错
└── proxy_core.py                # 每轮统一路径，串联上述模块
```

`proxy_server.py` 只保留 HTTP 服务骨架、SSE 流转发+解析、WebSocket 推送，业务逻辑全部委托给 `proxy_core.py`。

---

## 实现顺序

```
Phase 1（纯数据层，可单元测试）
  transcript_codec.py          — 归组状态机，用真实请求 JSON 做 fixture
  codex_input_cursor.py        — compute_diff + fingerprint normalize
  transcript_delta_applier.py

Phase 2（代理核心）
  compact_controller.py        — on_compact_success + cursor 重建 + prompt 替换
  proxy_core.py                — handle_request + handle_response

Phase 3（HTTP 服务）
  proxy_server.py 重写         — SSE 解析器 + WebSocket 协议

Phase 4（编辑）
  transcript_editor.py
```

**Phase 1 开始前需验证：Reasoning item 的 `encrypted_content` 跨轮是否 byte-identical。**
