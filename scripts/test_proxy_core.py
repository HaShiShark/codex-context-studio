from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.proxy_core import (  # noqa: E402
    ProxyState,
    handle_request,
    handle_response_completed,
)
from backend.compact_controller import (  # noqa: E402
    LOCAL_COMPACT_PROMPT_PREFIX,
    LOCAL_COMPACT_SUMMARY_PREFIX,
    MANUAL_LOCAL_COMPACT_PROMPT,
)
from backend.transcript_codec import input_items_to_transcript, transcript_to_input_items  # noqa: E402


def message(role: str, text: str, *, item_id: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "message",
        "role": role,
        "content": text,
    }
    if item_id is not None:
        item["id"] = item_id
    return item


def typed_message(role: str, text: str) -> dict[str, Any]:
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


def function_call(arguments: str, *, item_id: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "function_call",
        "call_id": "call-1",
        "name": "lookup",
        "arguments": arguments,
    }


def request_function_call(arguments: str) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": "call-1",
        "name": "lookup",
        "arguments": arguments,
    }


def function_output(text: str) -> dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": text,
    }


def transcript_items(state: ProxyState) -> list[Any]:
    return transcript_to_input_items(state.transcript)


def test_new_thread_empty_cursor_appends_full_input() -> None:
    state = ProxyState()
    input_items = [
        message("developer", "be concise"),
        message("user", "hello"),
    ]

    forwarded = handle_request(state, {"input": copy.deepcopy(input_items)})

    assert forwarded["input"] == input_items
    assert transcript_items(state) == input_items
    assert state.codex_input_cursor == input_items
    assert state.tail_conflict is False


def test_same_request_retry_is_idempotent() -> None:
    state = ProxyState()
    input_items = [message("user", "hello")]

    handle_request(state, {"input": copy.deepcopy(input_items)})
    forwarded = handle_request(state, {"input": copy.deepcopy(input_items)})

    assert forwarded["input"] == input_items
    assert transcript_items(state) == input_items
    assert state.codex_input_cursor == input_items
    assert len(state.transcript) == 1
    assert len(state.transcript[0]["items"]) == 1


def test_response_completed_appends_assistant_to_cursor_and_transcript() -> None:
    state = ProxyState()
    user = message("user", "hello")
    assistant = message("assistant", "hi")
    handle_request(state, {"input": [user]})

    result = handle_response_completed(state, [assistant])

    assert result.appended == 1
    assert result.compact_handled is False
    assert transcript_items(state) == [user, assistant]
    assert state.codex_input_cursor == [user, assistant]


def test_response_completed_stores_next_request_shape_in_cursor() -> None:
    state = ProxyState()
    user = message("user", "hello")
    raw_response_message = {
        "id": "msg-dynamic",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "annotations": [],
                "logprobs": [],
                "text": "hi",
            }
        ],
        "phase": "final_answer",
    }
    next_request_message = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "hi"}],
        "phase": "final_answer",
    }
    handle_request(state, {"input": [user]})

    handle_response_completed(state, [raw_response_message])

    assert state.codex_input_cursor == [user, next_request_message]


def test_tool_continuation_pops_old_tail_and_appends_new_tail() -> None:
    state = ProxyState()
    user = message("user", "run lookup")
    old_call = function_call('{"q":"old"}', item_id="old-dynamic")
    new_call = function_call('{"q":"new"}', item_id="new-dynamic")
    request_new_call = request_function_call('{"q":"new"}')
    output = function_output("done")

    handle_request(state, {"input": [user, old_call]})
    forwarded = handle_request(state, {"input": [user, new_call, output]})

    assert forwarded["input"] == [user, request_new_call, output]
    assert transcript_items(state) == [user, request_new_call, output]
    assert state.codex_input_cursor == [user, request_new_call, output]
    assert state.tail_conflict is False


def test_pop_conflict_keeps_existing_tail_and_still_appends() -> None:
    state = ProxyState()
    user = message("user", "hello")
    stale_tail = message("assistant", "old tail")
    edited_tail = message("assistant", "edited tail")
    new_tail = message("assistant", "new tail")

    state.codex_input_cursor = [user, stale_tail]
    state.transcript = [
        {
            "id": "node-user",
            "role": "user",
            "items": [{"kind": "message", "providerItem": user, "inputIndex": 0}],
            "source_map": {},
        },
        {
            "id": "node-assistant",
            "role": "assistant",
            "items": [{"kind": "message", "providerItem": edited_tail, "inputIndex": 1}],
            "source_map": {},
        },
    ]

    forwarded = handle_request(state, {"input": [user, new_tail]})

    assert state.tail_conflict is True
    assert forwarded["input"] == [user, edited_tail, new_tail]
    assert transcript_items(state) == [user, edited_tail, new_tail]
    assert state.codex_input_cursor == [user, new_tail]


def test_workbench_compressed_transcript_does_not_restore_old_assistant_on_next_request() -> None:
    state = ProxyState()
    user = message("user", "ask")
    raw_response_message = {
        "id": "msg-dynamic",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "annotations": [],
                "logprobs": [],
                "text": "answer",
            }
        ],
        "phase": "final_answer",
    }
    summary = message("user", "summary")
    followup = message("user", "follow up")
    next_request_message = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "answer"}],
        "phase": "final_answer",
    }

    handle_request(state, {"input": [user]})
    handle_response_completed(state, [raw_response_message])
    state.transcript = input_items_to_transcript([summary])

    forwarded = handle_request(
        state,
        {"input": [copy.deepcopy(user), copy.deepcopy(next_request_message), copy.deepcopy(followup)]},
    )

    assert state.tail_conflict is False
    assert forwarded["input"] == [summary, followup]
    assert transcript_items(state) == [summary, followup]
    assert state.codex_input_cursor == [user, next_request_message, followup]


def test_workbench_compressed_transcript_ignores_phase_only_message_delta() -> None:
    state = ProxyState()
    user = message("user", "ask")
    raw_response_message = {
        "id": "msg-dynamic",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "annotations": [],
                "logprobs": [],
                "text": "answer",
            }
        ],
    }
    summary = message("user", "summary")
    followup = message("user", "follow up")
    next_request_message = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "answer"}],
        "phase": "final_answer",
    }

    handle_request(state, {"input": [user]})
    handle_response_completed(state, [raw_response_message])
    state.transcript = input_items_to_transcript([summary])

    forwarded = handle_request(
        state,
        {"input": [copy.deepcopy(user), copy.deepcopy(next_request_message), copy.deepcopy(followup)]},
    )

    assert state.tail_conflict is False
    assert forwarded["input"] == [summary, followup]
    assert transcript_items(state) == [summary, followup]
    assert state.codex_input_cursor == [user, next_request_message, followup]


def test_workbench_compressed_transcript_does_not_restore_reasoning_turn() -> None:
    state = ProxyState()
    user = message("user", "question")
    raw_reasoning = {
        "id": "rs-dynamic",
        "type": "reasoning",
        "summary": [],
        "content": [],
        "encrypted_content": "stable-reasoning",
    }
    raw_response_message = {
        "id": "msg-dynamic",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "annotations": [],
                "logprobs": [],
                "text": "old answer",
            }
        ],
        "phase": "final_answer",
    }
    summary = message("user", "summary")
    followup = message("user", "follow up")
    next_request_reasoning = {
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "stable-reasoning",
    }
    next_request_message = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "old answer"}],
        "phase": "final_answer",
    }

    handle_request(state, {"input": [user]})
    handle_response_completed(state, [raw_reasoning, raw_response_message])
    state.transcript = input_items_to_transcript([summary])

    forwarded = handle_request(
        state,
        {
            "input": [
                copy.deepcopy(user),
                copy.deepcopy(next_request_reasoning),
                copy.deepcopy(next_request_message),
                copy.deepcopy(followup),
            ],
        },
    )

    assert state.tail_conflict is False
    assert forwarded["input"] == [summary, followup]
    assert transcript_items(state) == [summary, followup]


def test_user_edited_transcript_survives_next_raw_codex_request() -> None:
    state = ProxyState()
    user_a = message("user", "A")
    user_b = message("user", "B")
    user_c = message("user", "C")
    user_d = message("user", "D")
    edited_b = message("user", "B edited")

    original_input = [user_a, user_b, user_c]
    handle_request(state, {"input": copy.deepcopy(original_input)})
    state.transcript = input_items_to_transcript([user_a, edited_b, user_c])

    forwarded = handle_request(
        state,
        {"input": [copy.deepcopy(user_a), copy.deepcopy(user_b), copy.deepcopy(user_c), copy.deepcopy(user_d)]},
    )

    assert forwarded["input"] == [user_a, edited_b, user_c, user_d]
    assert transcript_items(state) == [user_a, edited_b, user_c, user_d]
    assert state.codex_input_cursor == [user_a, user_b, user_c, user_d]


class CompactControllerSpy:
    replace_calls = 0
    success_calls = 0

    @classmethod
    def replace_compact_prompt(cls, state: ProxyState) -> None:
        cls.replace_calls += 1
        state.transcript[-1]["items"][-1]["providerItem"]["content"] = "custom compact prompt"

    @classmethod
    def on_compact_success(
        cls,
        state: ProxyState,
        response_items: list[Any],
        *,
        text: str = "",
    ) -> None:
        cls.success_calls += 1
        summary = message("user", f"summary: {text}")
        state.transcript = [
            {
                "id": "compact-summary",
                "role": "user",
                "items": [{"kind": "message", "providerItem": summary, "inputIndex": 0}],
                "source_map": {},
            }
        ]
        state.codex_input_cursor = [summary]
        state.compact_pending = False
        state.compact_kind = ""


def test_compact_metadata_sets_pending_and_uses_prompt_replacement_hook() -> None:
    CompactControllerSpy.replace_calls = 0
    CompactControllerSpy.success_calls = 0
    state = ProxyState()
    body = {
        "client_metadata": {
            "x-codex-turn-metadata": '{"request_kind":"compaction","trigger":"manual"}',
        },
        "input": [
            message("user", "keep me"),
            message("user", "codex built-in compact prompt"),
        ],
    }

    forwarded = handle_request(
        state,
        body,
        compact_controller=CompactControllerSpy,
    )

    assert state.compact_pending is True
    assert state.compact_kind == "manual"
    assert CompactControllerSpy.replace_calls == 1
    assert forwarded["input"][-1]["content"] == "custom compact prompt"
    assert state.codex_input_cursor == body["input"]

    result = handle_response_completed(
        state,
        [message("assistant", "compact summary")],
        text="compact summary",
        compact_controller=CompactControllerSpy,
    )

    assert result.compact_handled is True
    assert result.compact_controller_used is True
    assert CompactControllerSpy.success_calls == 1
    assert state.compact_pending is False
    assert state.codex_input_cursor == [message("user", "summary: compact summary")]


def test_compact_metadata_without_controller_does_not_remote_compact() -> None:
    state = ProxyState()
    original_input = [message("user", "codex built-in compact prompt")]
    body = {
        "client_metadata": {
            "x-codex-turn-metadata": {
                "request_kind": "compaction",
                "trigger": "auto",
            },
        },
        "input": copy.deepcopy(original_input),
    }

    forwarded = handle_request(state, body, compact_controller=None)

    assert state.compact_pending is True
    assert state.compact_kind == "auto"
    assert forwarded["input"] == original_input
    assert transcript_items(state) == original_input

    result = handle_response_completed(
        state,
        [message("assistant", "remote-looking compact response")],
        text="ignored",
        compact_controller=None,
    )

    assert result.compact_handled is True
    assert result.compact_controller_used is False
    assert state.compact_pending is False
    assert state.compact_kind == ""
    assert transcript_items(state) == original_input
    assert state.codex_input_cursor == original_input


def test_auto_loaded_compact_controller_replaces_prompt_and_simulates_success() -> None:
    state = ProxyState()
    built_in_prompt = f"{LOCAL_COMPACT_PROMPT_PREFIX}\nSummarize the conversation."
    body = {
        "client_metadata": {
            "x-codex-turn-metadata": '{"request_kind":"compaction","trigger":"manual"}',
        },
        "input": [
            message("developer", "developer context"),
            message("user", "keep this user request"),
            message("assistant", "previous answer"),
            message("user", built_in_prompt),
        ],
    }

    forwarded = handle_request(state, body)

    assert state.compact_pending is True
    assert state.compact_kind == "manual"
    assert state.compact_error is None
    assert forwarded["input"][-1]["content"] == MANUAL_LOCAL_COMPACT_PROMPT
    assert state.codex_input_cursor == body["input"]

    result = handle_response_completed(
        state,
        [message("assistant", "real compact summary")],
        text="fallback summary should not be used",
    )

    expected_summary = f"{LOCAL_COMPACT_SUMMARY_PREFIX}\n\nreal compact summary"
    expected_items = [
        message("user", "keep this user request"),
        typed_message("user", expected_summary),
    ]
    assert result.compact_handled is True
    assert result.compact_controller_used is True
    assert state.compact_pending is False
    assert state.compact_kind == ""
    assert state.compact_error is None
    assert transcript_items(state) == expected_items
    assert state.codex_input_cursor == expected_items


def main() -> None:
    tests = [
        test_new_thread_empty_cursor_appends_full_input,
        test_same_request_retry_is_idempotent,
        test_response_completed_appends_assistant_to_cursor_and_transcript,
        test_tool_continuation_pops_old_tail_and_appends_new_tail,
        test_pop_conflict_keeps_existing_tail_and_still_appends,
        test_workbench_compressed_transcript_does_not_restore_old_assistant_on_next_request,
        test_workbench_compressed_transcript_ignores_phase_only_message_delta,
        test_workbench_compressed_transcript_does_not_restore_reasoning_turn,
        test_user_edited_transcript_survives_next_raw_codex_request,
        test_compact_metadata_sets_pending_and_uses_prompt_replacement_hook,
        test_compact_metadata_without_controller_does_not_remote_compact,
        test_auto_loaded_compact_controller_replaces_prompt_and_simulates_success,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} proxy core tests passed")


if __name__ == "__main__":
    main()
