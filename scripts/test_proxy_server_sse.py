from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import proxy_server  # noqa: E402


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
        buffer = proxy_server.parse_sse_buffer(
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


def main() -> None:
    tests = [
        test_output_item_done_and_completed_survive_chunk_boundaries,
        test_completed_output_does_not_drop_done_reasoning_item,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} proxy server SSE tests passed")


if __name__ == "__main__":
    main()
