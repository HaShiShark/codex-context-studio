from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

try:
    from . import proxy_server as legacy
    from .realtime_events import (
        compact_update,
        error_event,
        session_list_update,
        session_status,
        snapshot,
        transcript_patch,
        transcript_update,
        usage_update,
    )
    from .realtime_hub import RealtimeHub, RealtimeSubscription
except ImportError:
    repo_root_for_import = Path(__file__).resolve().parents[1]
    if str(repo_root_for_import) not in sys.path:
        sys.path.insert(0, str(repo_root_for_import))
    from backend import proxy_server as legacy
    from backend.realtime_events import (
        compact_update,
        error_event,
        session_list_update,
        session_status,
        snapshot,
        transcript_patch,
        transcript_update,
        usage_update,
    )
    from backend.realtime_hub import RealtimeHub, RealtimeSubscription


STORE = legacy.STORE
HUB = RealtimeHub()


def _headers_dict(request: Request) -> dict[str, str]:
    return {key.lower(): value for key, value in request.headers.items()}


def _json_error(message: str, status: int | HTTPStatus) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=int(status))


def _session_version(session_id: str) -> int:
    session = STORE.get_session(session_id)
    if not session:
        return 0
    return int(session.get("transcript_version") or 0)


def _session_transcript(session_id: str) -> list[dict[str, Any]]:
    session = STORE.get_session(session_id)
    if not session:
        return []
    transcript = session.get("transcript")
    return copy.deepcopy(transcript) if isinstance(transcript, list) else []


def _session_is_compacting(session_id: str) -> bool:
    session = STORE.get_session(session_id)
    return bool(session and session.get("compact_pending"))


def _transcript_patch_ops(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if previous == current:
        return []

    prefix = 0
    previous_len = len(previous)
    current_len = len(current)
    while prefix < previous_len and prefix < current_len and previous[prefix] == current[prefix]:
        prefix += 1

    suffix = 0
    while (
        suffix < previous_len - prefix
        and suffix < current_len - prefix
        and previous[previous_len - 1 - suffix] == current[current_len - 1 - suffix]
    ):
        suffix += 1

    delete_count = previous_len - prefix - suffix
    nodes = copy.deepcopy(current[prefix : current_len - suffix if suffix else current_len])
    return [
        {
            "op": "splice_nodes",
            "index": prefix,
            "delete_count": delete_count,
            "nodes": nodes,
        }
    ]


async def _publish_session_change(
    session_id: str,
    *,
    reason: str,
    before_version: int | None = None,
    before_transcript: list[dict[str, Any]] | None = None,
    compact_phase: str = "",
    transcript_mode: str = "none",
) -> None:
    session = STORE.get_session(session_id)
    if not session:
        return
    await HUB.publish(session_status(session, reason=reason))
    current_version = int(session.get("transcript_version") or 0)
    transcript_changed = before_version is None or current_version != before_version
    if transcript_mode == "full" and transcript_changed:
        await HUB.publish(transcript_update(session, reason=reason))
    elif transcript_mode == "patch" and transcript_changed:
        ops = _transcript_patch_ops(before_transcript or [], _session_transcript(session_id))
        if ops:
            await HUB.publish(
                transcript_patch(
                    session,
                    base_version=int(before_version or 0),
                    reason=reason,
                    ops=ops,
                )
            )
    if compact_phase:
        await HUB.publish(compact_update(session, phase=compact_phase))
    await HUB.publish(session_list_update(STORE.list_sessions()))


async def _publish_usage(session_id: str) -> None:
    usage = STORE.session_usage(session_id)
    if usage is not None:
        await HUB.publish(usage_update(session_id, usage.get("summary") or {}))


async def _read_json_body(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    legacy.proxy_log(f"fastapi body bytes={len(raw_body)} prefix={raw_body[:80]!r}")
    data = legacy.parse_json_request_body(raw_body, request.headers.get("content-encoding"))
    if not isinstance(data, dict):
        raise json.JSONDecodeError("request body must be an object", "", 0)
    return data


def _upstream_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _response_headers(headers: httpx.Headers) -> tuple[dict[str, str], str | None]:
    response_headers: dict[str, str] = {}
    media_type: str | None = None
    skipped = {"transfer-encoding", "connection", "content-encoding", "content-length"}
    for key, value in headers.items():
        lower_key = key.lower()
        if lower_key in skipped:
            continue
        if lower_key == "content-type":
            media_type = value
            continue
        response_headers[key] = value
    return response_headers, media_type


def _sse_event_bytes(event: dict[str, Any]) -> bytes:
    event_type = str(event.get("type") or "message")
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {payload}\n\n".encode("utf-8")


async def _context_control_stream(body: dict[str, Any], opened: bool, error: str = "") -> AsyncIterator[bytes]:
    response_id = f"resp_hash_context_{uuid.uuid4().hex}"
    message_id = f"msg_hash_context_{uuid.uuid4().hex}"
    model = str(body.get("model") or "gpt-5.5")
    text = legacy.CONTEXT_CONTROL_NOTICE_TEXT if opened else f"Hash Context: workbench unavailable. {error}".strip()
    item = {
        "type": "message",
        "role": "assistant",
        "id": message_id,
        "content": [{"type": "output_text", "text": text}],
    }
    events = [
        {"type": "response.created", "response": {"id": response_id, "model": model}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "content": [{"type": "output_text", "text": ""}]},
        },
        {
            "type": "response.output_text.delta",
            "output_index": 0,
            "content_index": 0,
            "item_id": message_id,
            "delta": text,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {"id": response_id, "model": model, "output": [item]}},
    ]
    for event in events:
        yield _sse_event_bytes(event)
    yield b"data: [DONE]\n\n"


async def _stream_upstream_response(
    *,
    client: httpx.AsyncClient,
    upstream_response: httpx.Response,
    session_id: str,
    capture_proxy_session: bool,
    is_internal_context: bool,
    forwarded_body: dict[str, Any],
    original_body: dict[str, Any],
) -> AsyncIterator[bytes]:
    response_items: list[dict[str, Any]] = []
    text_parts: list[str] = []
    completed_responses: list[dict[str, Any]] = []
    buffer = ""
    error_preview = bytearray()

    try:
        async for chunk in upstream_response.aiter_bytes():
            if upstream_response.status_code >= 400 and len(error_preview) < 4000:
                error_preview.extend(chunk[: 4000 - len(error_preview)])
            yield chunk
            buffer += chunk.decode("utf-8", errors="ignore")
            buffer = legacy.parse_sse_buffer(buffer, response_items, text_parts, completed_responses)

        if upstream_response.status_code >= 400:
            preview = error_preview.decode("utf-8", errors="replace")
            legacy.proxy_log(f"upstream error session={session_id} body={preview[:1000]!r}")
            if capture_proxy_session:
                before_version = _session_version(session_id)
                before_transcript = _session_transcript(session_id)
                was_compacting = _session_is_compacting(session_id)
                STORE.fail_response(session_id, preview[:1000] or upstream_response.reason_phrase)
                await _publish_session_change(
                    session_id,
                    reason="response_failed",
                    before_version=before_version,
                    before_transcript=before_transcript,
                    compact_phase="failed" if was_compacting else "",
                    transcript_mode="full" if was_compacting else "none",
                )
                await HUB.publish(error_event(session_id, preview[:1000] or upstream_response.reason_phrase))
            return

        if is_internal_context or capture_proxy_session:
            usage_kind = "context_workbench" if is_internal_context else "main"
            fallback_model = str(forwarded_body.get("model") or original_body.get("model") or "")
            for completed_response in completed_responses:
                STORE.record_usage(
                    session_id,
                    usage_kind,
                    str(completed_response.get("model") or fallback_model),
                    completed_response.get("usage"),
                )
                await _publish_usage(session_id)

        if capture_proxy_session:
            before_version = _session_version(session_id)
            before_transcript = _session_transcript(session_id)
            was_compacting = _session_is_compacting(session_id)
            STORE.complete_response(session_id, response_items, "".join(text_parts))
            await _publish_session_change(
                session_id,
                reason="response_completed",
                before_version=before_version,
                before_transcript=before_transcript,
                compact_phase="success" if was_compacting else "",
                transcript_mode="full" if was_compacting else "patch",
            )
    except Exception as exc:
        legacy.proxy_log(f"stream error session={session_id} error={type(exc).__name__}: {exc}")
        if capture_proxy_session:
            before_version = _session_version(session_id)
            before_transcript = _session_transcript(session_id)
            was_compacting = _session_is_compacting(session_id)
            STORE.fail_response(session_id, str(exc))
            await _publish_session_change(
                session_id,
                reason="response_failed",
                before_version=before_version,
                before_transcript=before_transcript,
                compact_phase="failed" if was_compacting else "",
                transcript_mode="full" if was_compacting else "none",
            )
            await HUB.publish(error_event(session_id, str(exc)))
        raise
    finally:
        await upstream_response.aclose()
        await client.aclose()


def _initialize_runtime() -> None:
    legacy.DATA_DIR.mkdir(parents=True, exist_ok=True)
    legacy.LOG_PATH.write_text("", encoding="utf-8")
    if legacy.preload_force_upstream_auth():
        legacy.proxy_log(f"preloaded force upstream auth for {legacy.FORCE_UPSTREAM_BASE_URL}")
    elif legacy.preload_codex_subscription_auth():
        legacy.proxy_log("preloaded Codex subscription auth from local auth.json")
    elif legacy.preload_openai_api_auth():
        legacy.proxy_log("preloaded OpenAI API auth from OPENAI_API_KEY")
    else:
        legacy.proxy_log("local Codex auth was not available at proxy startup")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _initialize_runtime()
    yield


app = FastAPI(title="Hash Context Proxy", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/proxy/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "backend": "fastapi"}


@app.get("/api/proxy/sessions")
async def list_sessions() -> dict[str, Any]:
    return STORE.list_sessions()


@app.get("/api/proxy/usage")
async def all_usage() -> dict[str, Any]:
    return STORE.all_usage()


@app.get("/api/proxy/sessions/{session_id:path}/usage")
async def session_usage(session_id: str) -> Response:
    usage = STORE.session_usage(session_id)
    if usage is None:
        return _json_error("session not found", HTTPStatus.NOT_FOUND)
    return JSONResponse(usage)


@app.get("/api/proxy/sessions/{session_id:path}")
async def get_session(session_id: str) -> Response:
    session = STORE.get_session(session_id)
    if session is None:
        return _json_error("session not found", HTTPStatus.NOT_FOUND)
    return JSONResponse(session)


@app.post("/api/proxy/sessions/{session_id:path}/transcript")
async def replace_transcript(session_id: str, request: Request) -> Response:
    try:
        payload = await _read_json_body(request)
    except json.JSONDecodeError:
        return _json_error("request body must be JSON", HTTPStatus.BAD_REQUEST)
    except ValueError as exc:
        return _json_error(str(exc), HTTPStatus.UNSUPPORTED_MEDIA_TYPE)

    transcript = payload.get("transcript")
    if not isinstance(transcript, list):
        return _json_error("transcript must be a list", HTTPStatus.BAD_REQUEST)
    before_version = _session_version(session_id)
    before_transcript = _session_transcript(session_id)
    try:
        session = STORE.replace_transcript(session_id, transcript)
    except ValueError as exc:
        return _json_error(str(exc), HTTPStatus.BAD_REQUEST)
    await _publish_session_change(
        session_id,
        reason="replace_transcript",
        before_version=before_version,
        before_transcript=before_transcript,
        transcript_mode="full",
    )
    return JSONResponse(session)


@app.post("/api/proxy/sessions/{session_id:path}/usage/reset")
async def reset_usage(session_id: str) -> Response:
    try:
        payload = STORE.reset_usage(session_id)
    except KeyError:
        return _json_error("session not found", HTTPStatus.NOT_FOUND)
    await _publish_usage(session_id)
    await HUB.publish(session_list_update(STORE.list_sessions()))
    return JSONResponse(payload)


@app.get("/v1/models")
async def models(request: Request) -> Response:
    headers = _headers_dict(request)
    upstream_base_url = legacy.upstream_base_url_for_request(headers)
    upstream_url = _upstream_url(upstream_base_url, "models")
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"
    legacy.proxy_log(f"models upstream={upstream_url}")
    try:
        upstream_headers = legacy.json_headers_for_upstream(headers)
        legacy.proxy_log(
            "models upstream headers "
            f"{json.dumps(legacy.safe_headers_for_log(upstream_headers), ensure_ascii=False, sort_keys=True)}"
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            upstream_response = await client.get(upstream_url, headers=upstream_headers)
    except Exception as exc:
        legacy.proxy_log(f"models error error={type(exc).__name__}: {exc}")
        return _json_error(str(exc), HTTPStatus.BAD_GATEWAY)

    body = upstream_response.content
    if upstream_response.status_code < 400:
        body = legacy.normalize_models_response_body(body)
    response_headers, media_type = _response_headers(upstream_response.headers)
    return Response(
        content=body,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=media_type or "application/json",
    )


@app.post("/v1/responses")
async def responses(request: Request) -> Response:
    try:
        body = await _read_json_body(request)
    except json.JSONDecodeError:
        return _json_error("request body must be JSON", HTTPStatus.BAD_REQUEST)
    except ValueError as exc:
        return _json_error(str(exc), HTTPStatus.UNSUPPORTED_MEDIA_TYPE)

    headers = _headers_dict(request)
    is_internal_context = legacy.is_internal_context_request(headers, body)
    is_title_generation = legacy.is_title_generation_request(body)
    capture_proxy_session = not is_internal_context and not is_title_generation

    if capture_proxy_session:
        control_command = legacy.context_control_command_from_input(body.get("input"))
        if control_command:
            session_id = legacy.session_id_for_request(body, headers)
            before_version = _session_version(session_id)
            before_transcript = _session_transcript(session_id)
            STORE.record_control_intercept(session_id, body, headers, control_command)
            await _publish_session_change(
                session_id,
                reason="context_control_intercept",
                before_version=before_version,
                before_transcript=before_transcript,
                transcript_mode="patch",
            )
            opened, error = await asyncio.to_thread(legacy.open_context_workbench, session_id)
            legacy.proxy_log(
                f"context control intercepted session={session_id} command={control_command!r} "
                f"opened={opened} error={error!r}"
            )
            return StreamingResponse(
                _context_control_stream(body, opened, error),
                media_type="text/event-stream; charset=utf-8",
                headers={"Cache-Control": "no-cache"},
            )

    if is_internal_context and not legacy.has_effective_upstream_auth(headers):
        legacy.proxy_log("internal context request missing cached upstream auth")
        return JSONResponse(
            {
                "error": {
                    "message": (
                        "Codex auth has not been captured by the local proxy yet. "
                        "Send one normal Codex message through this proxy first, then retry the context workbench."
                    ),
                    "type": "hash_context_auth_unavailable",
                    "code": "codex_auth_not_captured",
                }
            },
            status_code=int(HTTPStatus.SERVICE_UNAVAILABLE),
        )

    if is_internal_context:
        session_id = legacy.session_id_for_request(body, headers)
        headers_for_upstream = legacy.merge_codex_session_headers(
            headers,
            STORE.codex_session_headers(session_id),
            session_id=session_id,
        )
        forwarded_body = copy.deepcopy(body)
    elif capture_proxy_session:
        session_id = legacy.session_id_for_request(body, headers)
        headers_for_upstream = headers
        before_version = _session_version(session_id)
        before_transcript = _session_transcript(session_id)
        session, forwarded_body = STORE.begin_request(session_id, body, headers)
        await _publish_session_change(
            session_id,
            reason="begin_request",
            before_version=before_version,
            before_transcript=before_transcript,
            compact_phase="pending" if session.status == "compacting" else "",
            transcript_mode="none" if session.status == "compacting" else "patch",
        )
    else:
        session_id = legacy.session_id_for_request(body, headers)
        headers_for_upstream = headers
        forwarded_body = copy.deepcopy(body)

    upstream_base_url = legacy.upstream_base_url_for_request(headers_for_upstream)
    upstream_url = _upstream_url(upstream_base_url, "responses")
    effective_headers = legacy.apply_cached_upstream_auth(headers_for_upstream)
    effective_lowered = {key.lower(): value for key, value in effective_headers.items()}
    auth_kind = "chatgpt" if effective_lowered.get("chatgpt-account-id") else "api-key-or-bearer"
    legacy.proxy_log(
        f"request session={session_id} auth={auth_kind} "
        f"internal={is_internal_context} title_generation={is_title_generation} "
        f"capture={capture_proxy_session} upstream={upstream_url}"
    )

    payload = json.dumps(forwarded_body, ensure_ascii=False).encode("utf-8")
    upstream_headers = legacy.upstream_headers_for_request(headers_for_upstream, accept="text/event-stream")
    legacy.proxy_log(
        f"upstream headers session={session_id} "
        f"{json.dumps(legacy.safe_headers_for_log(upstream_headers), ensure_ascii=False, sort_keys=True)}"
    )

    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=30.0, read=None, write=60.0, pool=60.0))
    try:
        upstream_request = client.build_request(
            "POST",
            upstream_url,
            content=payload,
            headers=upstream_headers,
        )
        upstream_response = await client.send(upstream_request, stream=True)
    except Exception as exc:
        await client.aclose()
        legacy.proxy_log(f"request error session={session_id} error={type(exc).__name__}: {exc}")
        if capture_proxy_session:
            before_version = _session_version(session_id)
            before_transcript = _session_transcript(session_id)
            was_compacting = _session_is_compacting(session_id)
            STORE.fail_response(session_id, str(exc))
            await _publish_session_change(
                session_id,
                reason="response_failed",
                before_version=before_version,
                before_transcript=before_transcript,
                compact_phase="failed" if was_compacting else "",
                transcript_mode="full" if was_compacting else "none",
            )
            await HUB.publish(error_event(session_id, str(exc)))
        return _json_error(str(exc), HTTPStatus.BAD_GATEWAY)

    legacy.proxy_log(
        f"upstream status session={session_id} "
        f"status={upstream_response.status_code} reason={upstream_response.reason_phrase}"
    )
    response_headers, media_type = _response_headers(upstream_response.headers)
    return StreamingResponse(
        _stream_upstream_response(
            client=client,
            upstream_response=upstream_response,
            session_id=session_id,
            capture_proxy_session=capture_proxy_session,
            is_internal_context=is_internal_context,
            forwarded_body=forwarded_body,
            original_body=body,
        ),
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=media_type or "text/event-stream",
    )


@app.post("/v1/responses/compact")
async def compact_disabled() -> Response:
    return JSONResponse(
        {
            "error": {
                "message": "remote compact disabled; local compact is handled through /v1/responses metadata",
                "type": "remote_compact_disabled",
                "code": "remote_compact_disabled",
            }
        },
        status_code=int(HTTPStatus.GONE),
    )


def _snapshot_payload(session_id: str) -> dict[str, Any]:
    session_list = STORE.list_sessions()
    target_session_id = session_id or str(session_list.get("active_session_id") or "")
    session = STORE.get_session(target_session_id) if target_session_id else None
    return snapshot(session=session, session_list=session_list, session_id=target_session_id)


async def _send_subscription_events(
    websocket: WebSocket,
    subscription: RealtimeSubscription,
) -> None:
    while True:
        event = await subscription.queue.get()
        await websocket.send_json(event)


@app.websocket("/api/proxy/ws")
async def proxy_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    subscription: RealtimeSubscription | None = None
    sender_task: asyncio.Task[None] | None = None
    client_id = ""
    await websocket.send_json(await HUB.direct_event({"type": "connection_ack"}))

    try:
        while True:
            raw = await websocket.receive_json()
            if not isinstance(raw, dict):
                await websocket.send_json(await HUB.direct_event(error_event("", "message must be a JSON object")))
                continue

            message_type = str(raw.get("type") or "")
            if message_type == "ping":
                await websocket.send_json(await HUB.direct_event({"type": "pong"}))
                continue

            if message_type != "subscribe":
                await websocket.send_json(await HUB.direct_event(error_event("", f"unsupported message type: {message_type}")))
                continue

            client_id = str(raw.get("client_id") or client_id or "")
            session_id = str(raw.get("session_id") or "")
            last_event_id = int(raw.get("last_event_id") or 0)

            if sender_task is not None:
                sender_task.cancel()
            if subscription is not None:
                await HUB.unsubscribe(subscription.id)

            subscription = await HUB.subscribe(session_id)
            for event in await HUB.replay_after(
                last_event_id,
                session_id,
                up_to_event_id=subscription.start_event_id,
            ):
                await websocket.send_json(event)

            await websocket.send_json(await HUB.direct_event(_snapshot_payload(session_id)))
            sender_task = asyncio.create_task(_send_subscription_events(websocket, subscription))
            legacy.proxy_log(f"ws subscribed client={client_id} session={session_id}")
    except WebSocketDisconnect:
        legacy.proxy_log(f"ws disconnected client={client_id}")
    finally:
        if sender_task is not None:
            sender_task.cancel()
        if subscription is not None:
            await HUB.unsubscribe(subscription.id)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=legacy.HOST)
    parser.add_argument("--port", type=int, default=legacy.PORT)
    args = parser.parse_args()

    print(f"Hash Context proxy listening on http://{args.host}:{args.port} (FastAPI)")
    if legacy.FORCE_UPSTREAM_BASE_URL:
        print(f"Force upstream: {legacy.FORCE_UPSTREAM_BASE_URL.rstrip('/')}/responses")
    else:
        print(f"OpenAI API upstream: {legacy.OPENAI_UPSTREAM_BASE_URL.rstrip()}/responses")
        print(f"ChatGPT upstream: {legacy.CHATGPT_UPSTREAM_BASE_URL.rstrip()}/responses")
    uvicorn.run(app, host=args.host, port=args.port, log_level=os.environ.get("HASH_CONTEXT_UVICORN_LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
