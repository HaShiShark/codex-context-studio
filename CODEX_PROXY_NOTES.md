# Codex Proxy Notes

## Current Boundary

This project does not modify the official Codex source. It runs local services
and points Codex at a Responses-compatible proxy:

```powershell
codex `
  -c "model_providers.hash-context.name=Hash Context" `
  -c "model_providers.hash-context.base_url=http://127.0.0.1:8787/v1" `
  -c "model_providers.hash-context.requires_openai_auth=true" `
  -c "model_providers.hash-context.wire_api=responses" `
  -c "model_providers.hash-context.supports_websockets=false" `
  -c "model_provider=hash-context"
```

The wrapper also installs a `UserPromptSubmit` hook. When the user enters
`ctx` or `context`, the hook opens the workbench and blocks that control command
from being sent to the model. The proxy still has a fallback interceptor for
cases where the hook does not run.

## Local Services

- `backend/proxy_fastapi.py`: Codex-compatible Responses proxy on port `8787`.
- `backend/web_server.py`: Hash Context web backend on port `8765`.
- Vite dev server: React frontend on port `5174`.
- `electron/context-window.cjs`: Electron shell and local service supervisor.
- Electron control server: show/hide window API on port `8790`.

## Proxy API

- `POST /v1/responses`: main Responses SSE entry. Captured Codex requests go
  through the unified transcript/cursor path before being forwarded upstream.
- `POST /v1/responses/compact`: remote compact is disabled and returns
  `410 remote_compact_disabled`. Local compact is detected on `/v1/responses`
  through Codex turn metadata.
- `GET /v1/models`: forwards upstream `/models`, normalizes model payloads, and
  falls back to a local list only when ChatGPT-auth model lookup fails.
- `GET /api/proxy/sessions`: list captured sessions.
- `GET /api/proxy/sessions/{id}`: read transcript, running state, compact state,
  lock state, and usage summary.
- `POST /api/proxy/sessions/{id}/transcript`: replace the canonical transcript
  with proxy-core transcript nodes.
- `POST /api/proxy/sessions/{id}/node-locks`: toggle a transcript node lock.
- `POST /api/proxy/sessions/{id}/context-run`: mark the context model as running
  or idle so main Codex requests can wait safely.
- `POST /api/proxy/sessions/{id}/main-turn`: mark the full Codex agent turn as
  running or finished from Codex lifecycle hooks.

There is no `/override` or `/reset` session API.

## Transcript Rules

- `transcript` is the canonical business state. The UI and context workbench edit
  transcript nodes, not a separate override layer.
- `codex_input_cursor` is only the raw Codex input diff anchor. Normal workbench
  edits do not update it.
- Every captured request follows one path: cursor diff, conservative pop/append,
  optional local compact prompt replacement, rebuild `body.input`, then forward.
- `TranscriptNode` keeps `{ id, role, items, source_map }`. Roles can include
  `user`, `assistant`, `developer`, `system`, `subagent`, `compaction`,
  `context`, or `unknown`.
- Provider items are preserved losslessly. Unknown and non-dict items are wrapped
  instead of dropped.

## Session States

- `mirror`: no upstream request is currently streaming.
- `running`: a normal captured `/v1/responses` request is streaming.
- `compacting`: a local compact request is streaming.
- `error`: the last captured request failed; transcript and error details remain
  available for inspection.

User edits do not create an `override` state. They replace the canonical
transcript, and the next request is rebuilt from that transcript plus Codex's
new raw input tail.

## Key Constraints

- The supported compact path is local compact only. Remote compact is explicitly
  disabled because it does not expose the full content this project needs.
- `supports_websockets=false` for the Codex provider. The proxy WebSocket is only
  for frontend realtime events, not for Codex upstream traffic.
- Codex may send local proxy requests with `Content-Encoding: zstd`; the proxy
  decodes those bodies before JSON parsing and forwards plain JSON upstream.
- Edited context affects the next request, not a request already streaming.
- `previous_response_id` is preserved by the current unified proxy path.
- API key auth goes to `https://api.openai.com/v1`.
- ChatGPT auth goes to `https://chatgpt.com/backend-api/codex`.
