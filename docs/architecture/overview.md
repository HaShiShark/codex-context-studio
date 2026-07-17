# Codex Context Studio - 架构

## 整体流程

```mermaid
flowchart LR
    CD["Codex CLI / Desktop"]
    PX["Responses 代理\n:8787"]
    WEB["Web 后端\n:8765"]
    FE["React 工作台"]
    OAI["OpenAI API / ChatGPT Codex 后端"]

    CD -- "POST /v1/responses\n完整原始 input 数组" --> PX
    PX -- "重建后的请求 input" --> OAI
    OAI -- "SSE 流" --> PX
    PX -- "SSE 流" --> CD

    FE -- "HTTP 编辑 / 锁定 / 设置 API" --> WEB
    WEB -- "HTTP 代理控制 API" --> PX
    PX -- "仅 WebSocket 实时事件" --> FE
```

代理 WebSocket 只支持实时订阅（`ping` 和 `subscribe`）。Transcript 替换、节点锁定、context-run 状态和 main-turn 状态均使用 HTTP API。

---

## 请求处理

```mermaid
flowchart TD
    A["接收 Codex 请求\nbody.input = full_input_array"]
    B["从 client_metadata /\nx-codex-turn-metadata 检测本地 compact"]
    D["compute_diff(cursor, new_input)\npop = cursor[prefix_len:]\nappend = new_input[prefix_len:]"]

    F["为本次请求重置 tail_conflict"]
    G{"pop 非空？"}
    H["保守弹出 transcript 尾部\n比较预期 provider item 与 transcript 尾部 fingerprint"]
    I{"尾部匹配？"}
    J["删除匹配的尾部 item"]
    K["停止弹出\n设置 tail_conflict = true\n保留已编辑的 transcript 尾部"]
    L["通过 TranscriptCodec 分组\n追加新的 provider item"]

    M{"本地 compact 请求？"}
    N["将 Codex compact prompt\n替换为配置的本地 compact prompt"]

    O["根据规范 transcript\n重建 body.input"]
    P["转发到上游"]

    A --> B --> D --> F --> G
    G -- "是" --> H --> I
    I -- "是" --> J --> L
    I -- "否" --> K --> L
    G -- "否" --> L
    L --> M
    M -- "是" --> N --> O --> P
    M -- "否" --> O --> P
```

当 `prefix_len == 0` 时不存在重置分支。如果旧 cursor 尾部无法与 transcript 尾部安全匹配，代理会保留本地 transcript 尾部，并追加新的原始 input 后缀。

---

## 响应处理

```mermaid
flowchart TD
    S["SSE 流开始"]
    LOOP["读取下一条 SSE 事件"]
    FWD["将事件转发给 Codex 客户端"]
    T{"事件类型"}
    COL["收集 output item 和文本 delta"]
    DONE{"response.completed？"}
    C{"compact_pending？"}
    CS["CompactController.on_compact_success\n构建模拟 compact 状态\ntranscript = 新 compact transcript\ncursor = compact 前缀"]
    CA["常规完成\n追加 assistant output item\n扩展 cursor"]
    PUSH["发布 session / transcript 事件"]
    END["结束"]

    S --> LOOP --> FWD --> T
    T -- "output item 或文本 delta" --> COL --> DONE
    T -- "其他事件" --> DONE
    DONE -- "否" --> LOOP
    DONE -- "是" --> C
    C -- "是" --> CS --> PUSH --> END
    C -- "否" --> CA --> PUSH --> END
```

代理会跨 chunk 边界解析 SSE。响应 item 在追加到 cursor 前，会先投影成 request-item 结构。

---

## 本地 Compact

```mermaid
flowchart TD
    REQ["/v1/responses 请求\nrequest_kind = compaction"]
    SNAP["保存进行中检查点"]
    PROMPT["替换最后一条 compact prompt"]
    SEND["向上游发送摘要请求"]
    OK{"response.completed？"}
    SUMMARY["提取 assistant 摘要"]
    SIM["模拟压缩后的状态\n近期用户消息 + 摘要用户消息"]
    FAIL["恢复检查点\n记录 compact_error"]
    PUB["发布完整 transcript 更新"]

    REQ --> SNAP --> PROMPT --> SEND --> OK
    OK -- "成功" --> SUMMARY --> SIM --> PUB
    OK -- "失败" --> FAIL --> PUB
```

`POST /v1/responses/compact` 上的远程 compact 已禁用。唯一支持的 compact 路径，是 `/v1/responses` 上的本地 compact metadata。

---

## Transcript 分组

```mermaid
flowchart TD
    ITEM["provider item"]
    R{"item 类型 / 角色"}

    USR["新建 user 节点\ncurrent_assistant = None"]
    DEV["新建 developer 或 system 节点\ncurrent_assistant = None"]
    ASS["追加到当前 assistant\n或新建 assistant"]
    TOUT["按 call_id 关联工具输出\n回退到最近的 assistant\n或新建 assistant"]
    CMP["新建 compaction/context 节点"]
    OTHER["新建对应角色或 unknown 节点\n保留 item"]

    ITEM --> R
    R -- "message role=user" --> USR
    R -- "message role=developer/system\nadditional_tools" --> DEV
    R -- "assistant message/reasoning/tool call" --> ASS
    R -- "tool/function output" --> TOUT
    R -- "compaction/context_compaction" --> CMP
    R -- "unknown 或非 dict" --> OTHER
```

不会因为不认识某个 provider item 就跳过它。未知 item 和非 dict item 都会保留，以便 transcript 无损重建 provider input。

---

## 模块职责

```mermaid
flowchart TB
    subgraph Core["纯代理核心"]
        TC["transcript_codec.py\nprovider item <-> transcript"]
        CI["codex_input_cursor.py\nfingerprint + 前缀 diff"]
        DA["transcript_delta_applier.py\n保守 pop + 分组 append"]
        CO["compact_controller.py\n本地 compact prompt + compact 模拟"]
        PC["proxy_core.py\n统一的请求/响应状态转换"]
    end

    subgraph Runtime["运行时外壳"]
        STORE["proxy_store.py\nsession 状态、持久化、锁和轮次闸门"]
        FASTAPI["proxy_fastapi.py\nHTTP、SSE、上游鉴权、实时事件"]
        WEB["web_runtime.py / web_context.py\n上下文模型快照、工具、提交"]
        REACT["React 工作台\n上下文地图、锁、手动上下文模型"]
    end

    TC --> DA
    CI --> PC
    DA --> PC
    CO --> PC
    PC --> STORE
    STORE --> FASTAPI
    WEB --> FASTAPI
    REACT --> WEB
```

---

## 工作台提交流程

```mermaid
flowchart TD
    UI["用户与上下文模型对话"]
    SNAP["Web 后端构建轻量快照\n仅未锁定节点获得 Node #"]
    TOOL["上下文模型使用工具\n操作内存中的 draft"]
    REV{"node_lock_revision 未变化？"}
    COMMIT["提交 draft transcript"]
    POST["POST /api/proxy/sessions/{id}/transcript"]
    PUB["代理发布实时 transcript 更新"]
    REJ["拒绝提交并要求用户重试"]

    UI --> SNAP --> TOOL --> REV
    REV -- "是" --> COMMIT --> POST --> PUB
    REV -- "否" --> REJ
```

Draft 是临时对象，只属于上下文模型的单个轮次。提交后的对象仍是规范的代理 transcript；不存在第二份持久 transcript 或 override 层。
