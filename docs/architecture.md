# Hash Context Proxy - Architecture

## Overall Flow

```mermaid
flowchart LR
    CD["Codex CLI / Desktop"]
    PX["Responses Proxy\n:8787"]
    WEB["Web Backend\n:8765"]
    FE["React Workbench"]
    OAI["OpenAI API / ChatGPT Codex backend"]

    CD -- "POST /v1/responses\nfull raw input array" --> PX
    PX -- "rebuilt request input" --> OAI
    OAI -- "SSE stream" --> PX
    PX -- "SSE stream" --> CD

    FE -- "HTTP edit / lock / settings APIs" --> WEB
    WEB -- "HTTP proxy control APIs" --> PX
    PX -- "WebSocket realtime events only" --> FE
```

The proxy WebSocket only supports realtime subscriptions (`ping` and
`subscribe`). Transcript replacement, node locking, context-run state, and
main-turn state all use HTTP APIs.

---

## Request Handling

```mermaid
flowchart TD
    A["Receive Codex request\nbody.input = full_input_array"]
    B["Detect local compact from\nclient_metadata / x-codex-turn-metadata"]
    D["compute_diff(cursor, new_input)\npop = cursor[prefix_len:]\nappend = new_input[prefix_len:]"]

    F["Reset tail_conflict for this request"]
    G{"pop non-empty?"}
    H["Conservative transcript pop\ncompare expected provider item\nwith transcript tail fingerprint"]
    I{"tail matches?"}
    J["Remove matching tail items"]
    K["Stop popping\nset tail_conflict = true\npreserve edited transcript tail"]
    L["Append new provider items\nthrough TranscriptCodec grouping"]

    M{"local compact request?"}
    N["Replace Codex compact prompt\nwith configured local compact prompt"]

    O["Rebuild body.input\nfrom canonical transcript"]
    P["Forward upstream"]

    A --> B --> D --> F --> G
    G -- "yes" --> H --> I
    I -- "yes" --> J --> L
    I -- "no" --> K --> L
    G -- "no" --> L
    L --> M
    M -- "yes" --> N --> O --> P
    M -- "no" --> O --> P
```

There is no reset branch when `prefix_len == 0`. If the old cursor tail cannot
be safely matched against the transcript tail, the proxy preserves the local
transcript tail and appends the new raw input suffix.

---

## Response Handling

```mermaid
flowchart TD
    S["SSE stream starts"]
    LOOP["Read next SSE event"]
    FWD["Forward event to Codex client"]
    T{"event type"}
    COL["Collect output items and text deltas"]
    DONE{"response.completed?"}
    C{"compact_pending?"}
    CS["CompactController.on_compact_success\nbuild simulated compact state\ntranscript = new compact transcript\ncursor = compact prefix"]
    CA["Normal completion\nappend assistant output items\nextend cursor"]
    PUSH["Publish session / transcript events"]
    END["End"]

    S --> LOOP --> FWD --> T
    T -- "output item or text delta" --> COL --> DONE
    T -- "other event" --> DONE
    DONE -- "no" --> LOOP
    DONE -- "yes" --> C
    C -- "yes" --> CS --> PUSH --> END
    C -- "no" --> CA --> PUSH --> END
```

The proxy parses SSE across chunk boundaries. Response items are projected into
request-item shape before they are appended to the cursor.

---

## Local Compact

```mermaid
flowchart TD
    REQ["/v1/responses with\nrequest_kind = compaction"]
    SNAP["Save inflight checkpoint"]
    PROMPT["Replace last compact prompt"]
    SEND["Send summary request upstream"]
    OK{"response.completed?"}
    SUMMARY["Extract assistant summary"]
    SIM["Simulate compacted state\nrecent user messages + summary user message"]
    FAIL["Restore checkpoint\nrecord compact_error"]
    PUB["Publish full transcript update"]

    REQ --> SNAP --> PROMPT --> SEND --> OK
    OK -- "success" --> SUMMARY --> SIM --> PUB
    OK -- "failure" --> FAIL --> PUB
```

Remote compact is disabled at `POST /v1/responses/compact`. The only supported
compact path is local compact metadata on `/v1/responses`.

---

## Transcript Grouping

```mermaid
flowchart TD
    ITEM["provider item"]
    R{"item type / role"}

    USR["new user node\ncurrent_assistant = None"]
    DEV["new developer or system node\ncurrent_assistant = None"]
    ASS["append to current assistant\nor create assistant"]
    TOUT["attach tool output by call_id\nfallback to recent assistant\nor create assistant"]
    CMP["new compaction/context node"]
    OTHER["new role-specific or unknown node\npreserve item"]

    ITEM --> R
    R -- "message role=user" --> USR
    R -- "message role=developer/system\nadditional_tools" --> DEV
    R -- "assistant message/reasoning/tool call" --> ASS
    R -- "tool/function output" --> TOUT
    R -- "compaction/context_compaction" --> CMP
    R -- "unknown or non-dict" --> OTHER
```

No provider item is skipped because it is unfamiliar. Unknown and non-dict items
are preserved so transcript can rebuild provider input losslessly.

---

## Module Responsibilities

```mermaid
flowchart TB
    subgraph Core["Pure proxy core"]
        TC["transcript_codec.py\nprovider items <-> transcript"]
        CI["codex_input_cursor.py\nfingerprint + prefix diff"]
        DA["transcript_delta_applier.py\nconservative pop + grouped append"]
        CO["compact_controller.py\nlocal compact prompt + compact simulation"]
        PC["proxy_core.py\nunified request/response state transitions"]
    end

    subgraph Runtime["Runtime shells"]
        STORE["proxy_store.py\nsession state, persistence, locks, turn gates"]
        FASTAPI["proxy_fastapi.py\nHTTP, SSE, upstream auth, realtime events"]
        WEB["web_runtime.py / web_context.py\ncontext model snapshot, tools, commit"]
        REACT["React workbench\ncontext map, locks, manual context model"]
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

## Workbench Commit Flow

```mermaid
flowchart TD
    UI["User chats with context model"]
    SNAP["Web backend builds lightweight snapshot\nonly unlocked nodes get Node #"]
    TOOL["Context model uses tools\nagainst an in-memory draft"]
    REV{"node_lock_revision unchanged?"}
    COMMIT["Commit draft transcript"]
    POST["POST /api/proxy/sessions/{id}/transcript"]
    PUB["Proxy publishes realtime transcript update"]
    REJ["Reject commit and ask user to retry"]

    UI --> SNAP --> TOOL --> REV
    REV -- "yes" --> COMMIT --> POST --> PUB
    REV -- "no" --> REJ
```

The draft is temporary and belongs to one context-model turn. The committed
object is still the canonical proxy transcript; there is no second persistent
transcript or override layer.
