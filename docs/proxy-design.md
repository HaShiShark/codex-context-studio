# Hash Context Proxy - 最终设计方案

> 本版按 `docs/user-intent.md` 和后续关于 cursor / compact / ctx 的纠正重新校准。
> 后续代理核心、Workbench、子代理调研都以本文为技术准绳；旧文档和旧代码只能用于背景或外围能力参考。

## 0. 最容易误解的点

1. `transcript` 是业务真相，`codex_input_cursor` 是机器游标。cursor 必须落盘，但不是第二份 transcript，不展示、不编辑、不交给上下文模型改。
2. `Transcript = input 1:1` 指 provider items 不丢失、可从 transcript 无损重组上游 input。用户编辑后，transcript 可以和 Codex 原始 raw input 不同；cursor 仍代表 Codex 侧原始锚点。
3. 普通 Workbench 编辑只改 transcript，不改 cursor。compact 成功后才允许同时重置 transcript 和 cursor，因为那是主动模拟 compact 后锚点。
4. 自动 compact 的 summarization 输入是压缩前全量 transcript，包括当前最新 user；只有 compact 成功后的模拟状态才排除“当前正在运行的最新 user”。
5. `ctx` 首选由 hook 层拦截，成功时不会进入 Codex 上下文；代理内拦截只是 fallback，fallback 里 `ctx` 和打开工作台提示进入上下文也可以接受。
6. `transcript_editor.py` / `revision_id` / 节点防错字段是后续可演进的编辑器实现方向，不是当前判断核心代理是否完成的硬性主路径。

## 1. 核心原则

### 1.1 Transcript 是唯一业务真相

`transcript` 是 Codex `input` provider items 的结构化投影。用户看它，Workbench 编辑它，最终转发上游的 `body["input"]` 也从它重组。

要求：

- input 中的 provider item 不能被丢弃。
- 奇怪、未知、非预期 item 也要保留，必要时 wrap 成可还原结构。
- 前端展示的是 transcript 的真实结构，不是聊天摘要，也不是过滤后的历史。
- 不存在 override / restore / pending restore / 多份互相竞争的 transcript。

### 1.2 Cursor 是 diff 锚点

`codex_input_cursor` 记录“代理上一次已经确认吸收的 Codex provider items 序列”。

它的语义：

- 用于下一轮 `cursor` vs `new_input` 计算 diff。
- 需要落盘，重启后不能丢。
- 不给用户展示，不给上下文模型编辑。
- 普通 Workbench commit 不更新 cursor。
- 正常请求吸收后推进到 Codex 当前 raw input；正常响应完成后追加 assistant output items。
- compact 成功后可被主动重置为模拟后的 compact 前缀。

### 1.3 代理只有一条主路径

没有“未编辑就透传、编辑后才重组”的分支。每轮都走：

```text
Codex raw input
-> cursor diff
-> pop/append transcript
-> 如是 compact，替换 compact prompt
-> 从 transcript 重组 body["input"]
-> 转发上游
-> 响应完成后追加 assistant output 或处理 compact success
```

无编辑时，重组出的 input 应自然等价于原始 input；有编辑时，上游看到的是编辑后的 transcript 加本轮 Codex 新增尾巴。

## 2. 状态模型和落盘

```text
ProxyState
├── transcript: list[TranscriptNode]      # 唯一业务真相
├── codex_input_cursor: list[ProviderItem]# Codex raw input diff 锚点
├── tail_conflict: bool                   # 上次 pop 尾部 fingerprint 不匹配
├── compact_pending: bool                 # compact 请求进行中
├── compact_kind: "auto" | "manual" | ""
└── compact_error: str | null

TranscriptNode
├── id: str
├── role: str
├── items: list[NodeItem]
└── source_map: dict[fingerprint, "items[n]"]

NodeItem
├── kind: str
├── providerItem: ProviderItem
└── inputIndex: int
```

推荐落盘布局：

```text
~/.hash-context-codex/
├── index.json
└── sessions/
    └── <session_id>/
        ├── session.json      # session 元数据 + tail_conflict/compact_* 等轻状态
        ├── transcript.json   # 用户可见可编辑的唯一业务真相
        ├── cursor.json       # 不可见不可编辑的 diff 锚点
        └── workbench.jsonl   # 副模型自己的轻量对话历史
```

不应恢复旧项目里的 `storage.json`、`branches/raw.jsonl`、`edited.jsonl`、`override_base.jsonl`、`pending/active.jsonl`、`restore.json`、`revisions.jsonl` 等状态模型。

## 3. TranscriptNode 归组规则

以 provider item 的结构类型为准，role 只作为节点语义，不用于丢弃 item。

| provider item 类型 | 归组方式 |
|---|---|
| `type=message, role=user` | 新建 user 节点，结束当前 assistant 聚合 |
| `type=message, role=developer/system` | 新建对应 role 节点，结束当前 assistant 聚合 |
| `type=message, role=assistant` | 归入当前 assistant 节点；没有则新建 |
| reasoning | 归入当前 assistant 节点；没有则新建 |
| function/custom/local shell/tool/web/image call | 归入当前 assistant 节点；没有则新建 |
| tool/function call output | 按 `call_id` 找最近 assistant 节点；找不到则挂最近 assistant；再没有则新建 assistant |
| compaction/context compaction | 新建节点，role 取原始 role 或类型语义 |
| additional tools / responses_lite 前缀 | 新建 developer 类节点 |
| 其他未知 item | 新建节点，role 取原始 role；没有 role 则 `unknown` |

原则：没有任何 provider item 因“看不懂”被跳过。非 dict item 也要 wrap，保证可还原。

## 4. Fingerprint

fingerprint 用于判断两个 provider item 是否是同一个语义 item。

规则：

- 排除动态字段：`id`。
- 保留语义字段：`type`、`role`、`content`、`call_id`、`name`、`arguments`、`output`、`encrypted_content` 等。
- 对 dict/list 做稳定 JSON 序列化后 hash。

待验证点：reasoning 的 `encrypted_content` 在 Codex 跨轮重放时是否 byte-identical。没有真实日志前，不要擅自把它从 fingerprint 中移除。

## 5. 每轮请求处理

### 5.1 主流程

```text
new_input = body["input"]
cursor = state.codex_input_cursor
transcript = state.transcript

prefix_len = longest_common_prefix(cursor, new_input)
pop = cursor[prefix_len:]
append = new_input[prefix_len:]

先 pop transcript
再 append transcript
如是 compact，请替换最后的 compact prompt
body["input"] = transcript_to_input_items(transcript)
转发上游
```

请求被成功吸收后：

```text
state.codex_input_cursor = new_input
```

注意：compact 请求会在同一轮里把 transcript 中的内置 compact prompt 替换成自定义 prompt。compact 成功后会重置 transcript/cursor；compact 失败必须回滚到 compact 前状态。实现上需要保留 inflight checkpoint，避免失败后留下半更新状态。

### 5.2 Pop 规则

`pop` 表示 Codex 这轮 raw input 相比 cursor 撤回或替换了旧尾巴。

删除必须保守：

```text
for expected in reversed(pop):
  tail = transcript 最后一个 provider item
  if fingerprint(tail) == fingerprint(expected):
    删除 tail
  else:
    tail_conflict = true
    停止 pop
```

不要为了追上 Codex raw input 而强删 transcript。宁可多留旧尾巴，也不能误删用户编辑过的内容。

### 5.3 Append 规则

`append` 表示 Codex 这轮 raw input 相比 cursor 新增的 provider items。

append 必须走 TranscriptNode 归组规则，而不是把所有 item 拼成纯文本。append 后：

```text
transcript = 用户编辑后的旧 transcript + Codex 新增 provider items
```

## 6. 响应处理

代理边转发 SSE 给 Codex client，边解析完整 response items。

```text
if normal response completed:
  TranscriptDeltaApplier.append(transcript, response_items)
  codex_input_cursor.extend(response_items)

if compact_pending and response completed:
  CompactController.on_compact_success(...)
```

assistant output items 必须同时进入 transcript 和 cursor。否则：

- 前端看不到 assistant 输出。
- 下一轮 Codex raw input 带回 assistant 时 cursor 无法对齐。

SSE 解析器必须处理跨 chunk 的 event，不能按 HTTP chunk 硬切。

## 7. Local Compact

只使用 local compact。remote compact 看不到完整内容，不是本项目主路径。

### 7.1 检测

只看 `/v1/responses` 请求里的 metadata：

```text
body.client_metadata["x-codex-turn-metadata"]
request_kind == "compaction"
trigger == "manual" | "auto"
```

local compact 没有 `CompactionTrigger` item；那属于 remote compact/v2 路径，不使用。

### 7.2 Compact 请求阶段：总结输入必须全量

检测到 compact 后仍走主流程：

```text
diff cursor/new_input
pop/append transcript
找到最后一条 Codex 内置 compact prompt
替换成自定义 summarization prompt
从替换后的 transcript 重组 input
发给上游总结
```

关键纠正：

> 自动 compact 发给上游 summarization 的输入是 compact 前全量 transcript，包括当前最新 user、正在运行相关上下文、工具调用信息等。这里不能排除最新 user。

“排除当前正在运行的最新 user”只发生在 compact 成功后的本地模拟状态，不发生在送上游总结时。

### 7.3 Compact 成功后：模拟新 transcript/cursor

为什么要模拟：不模拟也能靠下一轮 Codex input 全量 append 接上，但 compact 成功到下一轮请求之间，前端会看到空窗或旧状态。成功后立即模拟可以让前端马上展示 compact 后上下文。

summary 必须是普通 `role=user` message，放在 selected users 后面：

```text
recent old user messages + summary user message
```

不能把 summary 放前面；不能把 developer/context items 塞进模拟状态。developer/context 等下一轮 Codex 请求进来再 append。

#### 手动 compact

```text
source = compact 前 transcript
selected_user_msgs = 从 source 中选择最近若干 user message
排除已有 LOCAL_COMPACT_SUMMARY_PREFIX summary

new_items = selected_user_msgs + [summary as role=user message]

state.transcript = TranscriptCodec.to_transcript(new_items)
state.codex_input_cursor = new_items
compact_pending = false
compact_kind = ""
```

下一轮 Codex 可能发：

```text
new_items + developer/context + 新 user
```

diff 会自然 append 后面的 developer/context 和新 user。

#### 自动 compact

自动 compact 可能发生在 assistant mid-turn。compact 成功后的模拟状态必须排除当前正在运行那轮的最新 user：

```text
source = compact 前 transcript
selected_user_msgs = 最近若干 user message
排除已有 summary
排除最后一条“当前正在运行”的 user

new_items = selected_user_msgs + [summary as role=user message]

state.transcript = TranscriptCodec.to_transcript(new_items)
state.codex_input_cursor = new_items
compact_pending = false
compact_kind = ""
```

下一轮 Codex 可能发：

```text
new_items + 被排除的最新 user + 压缩前还在运行/随后完成的 assistant items
```

diff：

```text
prefix = new_items
append = 最新 user + assistant items
```

这样当前 user/assistant 会自然 append 回来，不会被 summary 吞掉，也不会因为 cursor prefix 已包含最新 user 而漏 append。

### 7.4 Compact 失败

compact 请求失败时：

```text
transcript 回到 compact 前
cursor 回到 compact 前
compact_pending = false
compact_kind = ""
compact_error = 错误信息
```

不能清空 transcript/cursor，不能留下只替换了 prompt 的半状态。

## 8. Workbench 和上下文模型

Workbench 必须存在，它是“副模型作为 agent 操作 transcript”。

### 8.1 Workbench 的业务边界

- Workbench 读取、展示、编辑的业务对象是 transcript。
- Workbench 不直接编辑 cursor。
- Workbench 不创建第二份持久 transcript。
- 单轮工具循环可以有内存 draft；循环结束统一 commit 到 transcript。
- commit 后 cursor 仍不变。

### 8.2 上下文模型输入要轻

“所有读写展示面对 transcript”不是说每次都把完整 transcript 塞给副模型。

副模型的建议工作方式：

- 每轮开始生成轻量 snapshot：node index、role、preview、必要统计。
- 模型需要细节时调用 `get_node(nodes=[...])` 获取完整节点。
- 主编辑工具是节点级 `write_node`，支持删除、替换、压缩、插入。
- item/event 级工具可保留，但不是主旋律。
- 同一工具循环中，工具结果可以累计在 input 里帮助推理；循环结束后，下一轮只保留 user/assistant 文本对话，developer snapshot 重新生成。

### 8.3 编辑器防错是实现细节，不是当前完成度硬门槛

未来可以引入专门的 `transcript_editor.py`：

```text
revision_id
node_idx
node_id
item_range
replacement
```

它的作用是防止模型用过期快照误改节点。但当前判断核心代理是否符合方案时，硬要求是：

```text
Workbench commit 改 transcript
cursor 不变
不产生旧 override/restore/多 transcript 状态
```

不要因为代码还没有独立 `transcript_editor.py` 就判定核心代理未实现。

## 9. ctx 打开 Workbench

首选路径是 Codex hook：

```text
用户输入 ctx/context
UserPromptSubmit hook 捕获
打开 Workbench
返回 block/continue=false
Codex 不发送这条请求
ctx 不进入 Codex 上下文
也不会产生 fake assistant notice
```

代理内拦截是 fallback：

```text
hook 未生效
Codex 已经把 ctx 作为请求发到代理
代理识别为控制命令
打开 Workbench
不转发上游
返回 fake SSE notice 给 Codex client
```

fallback 里不需要再强行清理 `ctx` 或 `Hash Context: opened workbench.`。如果下一轮 Codex raw input 带回它们，就按普通上下文进入 transcript/cursor。它们只是一点本地控制痕迹，不值得为此破坏 cursor/raw input 一致性。

## 10. 明确禁止恢复的旧逻辑

除非用户以后明确重新引入，否则不要恢复：

- `override`
- `override_transcript`
- `edited_transcript`
- `has_override`
- remote compact 主路径
- restore flow
- `pending_restore`
- 恢复历史摘要
- 多份互相竞争的 transcript
- 为兼容旧前端而保留的旧状态 API

## 11. 模块边界

推荐核心模块：

```text
backend/
├── transcript_codec.py          # provider items <-> transcript 无损互转
├── codex_input_cursor.py        # fingerprint + longest common prefix + diff
├── transcript_delta_applier.py  # conservative pop + grouped append
├── compact_controller.py        # prompt replacement + compact success simulation
├── proxy_core.py                # 请求/响应统一主路径
├── proxy_session_storage.py     # transcript/cursor/session/workbench 落盘
└── proxy_server.py              # HTTP/SSE/ctx fallback/路由外壳
```

可选后续模块：

```text
transcript_editor.py             # 节点级编辑防错和批量操作，可后置
```

`proxy_server.py` 可以保留 HTTP、SSE、鉴权转发、ctx fallback、WebSocket 推送等外壳逻辑；核心的 transcript/cursor/compact 状态转换应尽量委托给上面的纯模块。

## 12. 检查清单

改代理相关代码前先检查：

- transcript 是否仍能无损重组成 provider input？
- cursor 是否仍只作为 diff 锚点，不被普通编辑修改？
- 请求处理是否仍只有一条主路径？
- response completed 是否同时追加 transcript 和 cursor？
- compact 是否只走 local compact？
- 自动 compact 的总结输入是否全量？
- 自动 compact 成功后的模拟状态是否才排除最新运行 user？
- compact 失败是否回滚 transcript/cursor？
- Workbench 是否没有落盘第二份 transcript？
- 代码里是否又出现 override/restore/pending restore 等旧状态？
- ctx hook 是否仍是首选；fallback 是否保持简单？
