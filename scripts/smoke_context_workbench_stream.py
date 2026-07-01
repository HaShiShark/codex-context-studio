from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any


def post_json(base_url: str, path: str, payload: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body) if body.strip() else {}


def get_json(base_url: str, path: str, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body) if body.strip() else {}


def create_test_session(base_url: str) -> str:
    session_id = f"codex-smoke-context-{uuid.uuid4().hex[:8]}"
    transcript = [
        {
            "role": "user",
            "text": "hello",
            "attachments": [],
            "toolEvents": [],
            "blocks": [{"kind": "text", "text": "hello"}],
            "providerItems": [{"type": "message", "role": "user", "content": "hello"}],
        },
        {
            "role": "assistant",
            "text": "alpha beta gamma",
            "attachments": [],
            "toolEvents": [],
            "blocks": [{"kind": "text", "text": "alpha beta gamma"}],
            "providerItems": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "alpha beta gamma"}],
                }
            ],
        },
    ]
    post_json(
        base_url,
        "/api/proxy-sync-session",
        {
            "session_id": session_id,
            "title": "context stream smoke test",
            "transcript": transcript,
            "is_running": False,
        },
    )
    return session_id


def delete_test_session(base_url: str, session_id: str) -> None:
    try:
        post_json(base_url, "/api/delete-session", {"session_id": session_id})
    except Exception as exc:  # noqa: BLE001
        print(f"warning: failed to clean up {session_id}: {exc}", file=sys.stderr)


def stream_context_turn(
    base_url: str,
    session_id: str,
    timeout: int,
    *,
    sync_during_stream: bool = True,
) -> dict[str, Any]:
    transcript = [
        {
            "role": "user",
            "text": "hello",
            "attachments": [],
            "toolEvents": [],
            "blocks": [{"kind": "text", "text": "hello"}],
            "providerItems": [{"type": "message", "role": "user", "content": "hello"}],
        },
        {
            "role": "assistant",
            "text": "alpha beta gamma",
            "attachments": [],
            "toolEvents": [],
            "blocks": [{"kind": "text", "text": "alpha beta gamma"}],
            "providerItems": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "alpha beta gamma"}],
                }
            ],
        },
    ]
    payload = {
        "session_id": session_id,
        "message": (
            "Call get_nodes for Node #2. Then call write_nodes to delete Node #2 "
            'and insert after Node #2 an assistant node with content "alpha summary". Reply done.'
        ),
        "selected_node_indexes": [],
        "reasoning_effort": "none",
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/context-chat-stream",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc

    tools: list[str] = []
    events = 0
    buffer = ""
    started = time.monotonic()
    sync_done = threading.Event()

    def sync_same_session() -> None:
        time.sleep(0.5)
        try:
            post_json(
                base_url,
                "/api/proxy-sync-session",
                {
                    "session_id": session_id,
                    "title": "context stream smoke test",
                    "transcript": transcript,
                    "is_running": False,
                },
            )
        finally:
            sync_done.set()

    sync_thread: threading.Thread | None = None
    if sync_during_stream:
        sync_thread = threading.Thread(target=sync_same_session, daemon=True)
        sync_thread.start()

    with response:
        while True:
            chunk = response.read(2048)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in buffer:
                raw_line, buffer = buffer.split("\n", 1)
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                events += 1
                event = json.loads(raw_line)
                event_type = event.get("type")
                if event_type == "tool_event":
                    tool_event = event.get("tool_event") if isinstance(event.get("tool_event"), dict) else {}
                    tools.append(str(tool_event.get("name") or ""))
                    continue
                if event_type == "error":
                    raise RuntimeError(str(event.get("error") or "context stream returned error"))
                if event_type == "done":
                    if sync_thread is not None:
                        sync_thread.join(timeout=5)
                    return {
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                        "event_count": events,
                        "tools": tools,
                        "sync_during_stream": sync_done.is_set() if sync_during_stream else False,
                        "answer": str(event.get("answer") or ""),
                        "conversation": event.get("conversation") if isinstance(event.get("conversation"), list) else [],
                    }

    tail = buffer.strip()
    if tail:
        event = json.loads(tail)
        if event.get("type") == "done":
            if sync_thread is not None:
                sync_thread.join(timeout=5)
            return {
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "event_count": events + 1,
                "tools": tools,
                "sync_during_stream": sync_done.is_set() if sync_during_stream else False,
                "answer": str(event.get("answer") or ""),
                "conversation": event.get("conversation") if isinstance(event.get("conversation"), list) else [],
            }

    raise RuntimeError(f"context stream ended without done; events={events}; tools={tools}")


def verify_external_sync_preserves_workbench_history(base_url: str, session_id: str) -> dict[str, Any]:
    changed_transcript = [
        {
            "role": "user",
            "text": "fresh external turn",
            "attachments": [],
            "toolEvents": [],
            "blocks": [{"kind": "text", "text": "fresh external turn"}],
            "providerItems": [{"type": "message", "role": "user", "content": "fresh external turn"}],
        },
        {
            "role": "assistant",
            "text": "fresh external answer",
            "attachments": [],
            "toolEvents": [],
            "blocks": [{"kind": "text", "text": "fresh external answer"}],
            "providerItems": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "fresh external answer"}],
                }
            ],
        },
        {
            "role": "user",
            "text": "new live question",
            "attachments": [],
            "toolEvents": [],
            "blocks": [{"kind": "text", "text": "new live question"}],
            "providerItems": [{"type": "message", "role": "user", "content": "new live question"}],
        },
    ]
    synced = post_json(
        base_url,
        "/api/proxy-sync-session",
        {
            "session_id": session_id,
            "title": "context stream smoke test changed",
            "transcript": changed_transcript,
            "is_running": False,
        },
    )
    synced_history = synced.get("context_workbench_history")
    if not isinstance(synced_history, list) or len(synced_history) < 2:
        raise RuntimeError("proxy sync did not preserve context workbench history after transcript change")
    if "pending_context_restore" in synced or "context_revision_history" in synced:
        raise RuntimeError("proxy sync returned stale restore/revision payload")

    init_payload = get_json(base_url, "/api/init")
    histories = init_payload.get("context_workbench_histories")
    saved_history = histories.get(session_id) if isinstance(histories, dict) else None
    if not isinstance(saved_history, list) or len(saved_history) < 2:
        raise RuntimeError("init did not preserve context workbench history after transcript change")
    stale_init_keys = sorted(
        {"pending_context_restores", "context_revision_histories"}.intersection(init_payload)
    )
    if stale_init_keys:
        raise RuntimeError(f"init still exposes stale restore/revision payload: {stale_init_keys}")
    conversations = init_payload.get("conversations")
    saved_conversation = conversations.get(session_id) if isinstance(conversations, dict) else None
    if not isinstance(saved_conversation, list) or len(saved_conversation) != len(changed_transcript):
        raise RuntimeError("init did not expose the externally synced transcript")

    return {
        "history_preserved": True,
        "history_count_after_sync": len(saved_history),
        "conversation_count_after_sync": len(saved_conversation),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--skip-concurrent-sync", action="store_true")
    args = parser.parse_args()

    session_id = create_test_session(args.base_url)
    try:
        result = stream_context_turn(
            args.base_url,
            session_id,
            args.timeout,
            sync_during_stream=not args.skip_concurrent_sync,
        )
        required_tools = {"get_nodes", "write_nodes"}
        missing_tools = sorted(required_tools.difference(result["tools"]))
        if missing_tools:
            raise RuntimeError(f"stream completed but missing tools: {missing_tools}; saw={result['tools']}")
        if len(result["conversation"]) < 2:
            raise RuntimeError("stream completed but returned an incomplete conversation")
        sync_state = verify_external_sync_preserves_workbench_history(args.base_url, session_id)
        print(
            json.dumps(
                {
                    "ok": True,
                    "session_id": session_id,
                    "elapsed_seconds": result["elapsed_seconds"],
                    "event_count": result["event_count"],
                    "tools": result["tools"],
                    "sync_during_stream": result["sync_during_stream"],
                    "external_sync_state": sync_state,
                    "answer_preview": result["answer"][:200],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        delete_test_session(args.base_url, session_id)


if __name__ == "__main__":
    raise SystemExit(main())
