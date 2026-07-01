from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import proxy_fastapi  # noqa: E402
from backend import proxy_routes_support  # noqa: E402


def sse_event(payload: dict[str, Any]) -> str:
    event_type = str(payload.get("type") or "message")
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {data}\n\n"


def feed_chunks(chunks: list[str]) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]], str]:
    response_items: list[dict[str, Any]] = []
    text_parts: list[str] = []
    completed_responses: list[dict[str, Any]] = []
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        buffer = proxy_routes_support.parse_sse_buffer(
            buffer,
            response_items,
            text_parts,
            completed_responses,
        )
    return response_items, text_parts, completed_responses, buffer


def test_output_item_done_and_completed_survive_chunk_boundaries() -> None:
    function_call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "shell_command",
        "arguments": '{"command":"Get-Date"}',
        "status": "completed",
    }
    final_message = {
        "type": "message",
        "role": "assistant",
        "id": "msg_1",
        "content": [{"type": "output_text", "text": "done"}],
    }
    stream = (
        sse_event(
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": function_call,
            }
        )
        + sse_event(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "model": "gpt-test",
                    "output": [function_call, final_message],
                },
            }
        )
    )
    chunks = [stream[:17], stream[17:83], stream[83:141], stream[141:]]

    response_items, text_parts, completed_responses, remainder = feed_chunks(chunks)

    assert remainder == ""
    assert text_parts == []
    assert response_items == [function_call, final_message]
    assert len(completed_responses) == 1
    assert completed_responses[0]["output"] == [function_call, final_message]


def test_completed_output_does_not_drop_done_reasoning_item() -> None:
    reasoning = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "stable-reasoning",
    }
    final_message = {
        "type": "message",
        "role": "assistant",
        "id": "msg_1",
        "content": [{"type": "output_text", "text": "done"}],
    }
    stream = (
        sse_event(
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": reasoning,
            }
        )
        + sse_event(
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": final_message,
            }
        )
        + sse_event(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "model": "gpt-test",
                    "output": [final_message],
                },
            }
        )
    )

    response_items, _text_parts, _completed_responses, remainder = feed_chunks([stream])

    assert remainder == ""
    assert response_items == [reasoning, final_message]


class FakeAsyncClient:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def aclose(self) -> None:
        self.events.append("client_closed")


class FakeAsyncUpstreamResponse:
    status_code = 200
    reason_phrase = "OK"

    def __init__(self, chunks: list[bytes], events: list[str]) -> None:
        self.chunks = chunks
        self.events = events

    async def aiter_bytes(self) -> Any:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.events.append("upstream_closed")


class FakeHub:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def publish(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = str(event.get("type") or "")
        self.events.append(f"publish:{event_type}")
        return event


class FakeStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.session = {
            "id": "session-stream-order",
            "status": "running",
            "is_running": True,
            "last_error": "",
            "transcript": [],
            "transcript_version": 0,
            "compact_pending": True,
            "compact_kind": "auto",
            "usage_summary": {},
        }

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        if session_id != self.session["id"]:
            return None
        return self.session

    def record_usage(self, session_id: str, kind: str, model: str, raw_usage: Any) -> None:
        self.events.append("record_usage")

    def session_usage(self, session_id: str) -> dict[str, Any] | None:
        return {"summary": {}}

    def complete_response(self, session_id: str, items: list[dict[str, Any]], text: str) -> None:
        self.events.append("complete_response")
        self.session["status"] = "mirror"
        self.session["is_running"] = False
        self.session["transcript"] = items
        self.session["transcript_version"] = 1
        self.session["compact_pending"] = False
        self.session["compact_kind"] = ""

    def fail_response(self, session_id: str, message: str) -> None:
        self.events.append("fail_response")
        self.session["status"] = "error"
        self.session["last_error"] = message

    def list_sessions(self) -> dict[str, Any]:
        return {"active_session_id": self.session["id"], "sessions": [self.session]}


async def run_fastapi_completed_stream_order_test() -> None:
    events: list[str] = []
    assistant = {
        "type": "message",
        "role": "assistant",
        "id": "msg_stream_order",
        "content": [{"type": "output_text", "text": "done"}],
    }
    completed = sse_event(
        {
            "type": "response.completed",
            "response": {
                "id": "resp_stream_order",
                "model": "gpt-test",
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                "output": [assistant],
            },
        }
    ).encode("utf-8")

    previous_store = proxy_fastapi.STORE
    previous_hub = proxy_fastapi.HUB
    proxy_fastapi.STORE = FakeStore(events)  # type: ignore[assignment]
    proxy_fastapi.HUB = FakeHub(events)  # type: ignore[assignment]
    try:
        stream = proxy_fastapi._stream_upstream_response(
            client=FakeAsyncClient(events),  # type: ignore[arg-type]
            upstream_response=FakeAsyncUpstreamResponse([completed], events),  # type: ignore[arg-type]
            session_id="session-stream-order",
            capture_proxy_session=True,
            is_internal_context=False,
            forwarded_body={"model": "gpt-test"},
            original_body={"model": "gpt-test"},
        )

        first_chunk = await stream.__anext__()
        events.append("yield_returned")

        assert first_chunk == completed
        assert events.index("complete_response") < events.index("yield_returned")
        assert "fail_response" not in events

        try:
            await stream.__anext__()
        except StopAsyncIteration:
            pass
    finally:
        proxy_fastapi.STORE = previous_store
        proxy_fastapi.HUB = previous_hub


def test_fastapi_stream_commits_completed_response_before_yield() -> None:
    asyncio.run(run_fastapi_completed_stream_order_test())


def main() -> None:
    tests = [
        test_output_item_done_and_completed_survive_chunk_boundaries,
        test_completed_output_does_not_drop_done_reasoning_item,
        test_fastapi_stream_commits_completed_response_before_yield,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} proxy SSE tests passed")


if __name__ == "__main__":
    main()
