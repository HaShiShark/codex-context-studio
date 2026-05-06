# Codex Proxy Notes

## MVP Boundary

第一版不修改 `D:\opensource\codex`。接入方式是外层包装器：

```powershell
npm run codex
```

包装器会启动本地窗口服务，然后通过 Codex 自己支持的 `-c` 临时覆盖 provider：

```powershell
codex `
  -c "model_providers.hash-context.name=OpenAI" `
  -c "model_providers.hash-context.base_url=http://127.0.0.1:8787/v1" `
  -c "model_providers.hash-context.requires_openai_auth=true" `
  -c "model_providers.hash-context.wire_api=responses" `
  -c "model_providers.hash-context.supports_websockets=false" `
  -c "model_provider=hash-context"
```

包装器还会启用 `UserPromptSubmit` hook。用户输入 `context` 或 `ctx` 时，hook 会打开 Electron 小窗，并阻止这条控制命令发送给模型。

## Local Services

- `proxy_server.py`: Codex-compatible Responses proxy, port `8787`.
- `web_server.py`: HashCode backend, port `8765`.
- Vite dev server: React frontend, port `5174`.
- `electron/context-window.cjs`: Electron shell and local service supervisor.
- Electron control server: show/hide window API, port `8790`.

## Proxy API

- `POST /v1/responses`: Codex-compatible Responses SSE entry.
- `POST /v1/responses/compact`: Codex remote compact entry; the proxy swaps in its canonical transcript before forwarding.
- `GET /v1/models`: minimal Codex compatibility response.
- `GET /api/proxy/sessions`: list captured sessions.
- `GET /api/proxy/sessions/:id`: read transcript, running status, override status.
- `POST /api/proxy/sessions/:id/override`: save edited transcript.
- `POST /api/proxy/sessions/:id/reset`: clear override and return to mirror mode.

## Transcript Rules

- Top-level transcript only keeps `user` and `assistant` records.
- Tool calls, tool results, reasoning summaries, and provider raw items stay inside assistant records.
- UI edits transcript, not provider wire format directly.
- Before sending a request, the proxy compiles transcript back into Responses `input`.
- When a record's `text` is edited, compilation uses that text for message content while preserving structured tool/function items where possible.

## Session States

- `mirror`: no local edit; proxy stores and transparently forwards.
- `running`: current turn is generating; UI is read-only.
- `compacting`: Codex requested remote compaction; UI is read-only until the compact output is installed.
- `override`: user applied an edited transcript; later requests use the edited transcript.
- `error`: request failed; partial transcript and error details are kept for inspection.

## Key Constraints

- MVP supports OpenAI Responses HTTP SSE and Responses compact.
- `supports_websockets=false`.
- Codex may send local proxy requests with `Content-Encoding: zstd`; the proxy decodes those bodies before JSON parsing and forwards plain JSON upstream.
- Edited context affects the next request, not the currently running request.
- Override requests remove `previous_response_id`.
- API key auth goes to `https://api.openai.com/v1`.
- ChatGPT auth goes to `https://chatgpt.com/backend-api/codex`.
