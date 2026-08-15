from __future__ import annotations

import copy
import json
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
    AUTO_LOCAL_COMPACT_PROMPT,
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


def with_turn_id(item: dict[str, Any], turn_id: str) -> dict[str, Any]:
    next_item = copy.deepcopy(item)
    next_item["internal_chat_message_metadata_passthrough"] = {"turn_id": turn_id}
    return next_item


def request_body(input_items: list[Any], *, turn_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"input": copy.deepcopy(input_items)}
    if turn_id is not None:
        body["client_metadata"] = {
            "turn_id": turn_id,
            "x-codex-turn-metadata": json.dumps({"request_kind": "turn", "turn_id": turn_id}),
        }
    return body


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


def test_response_completed_preserves_plaintext_collaboration_marker() -> None:
    state = ProxyState()
    user = message("user", "delegate this")
    raw_response_call = {
        "id": "fc-dynamic",
        "type": "function_call",
        "name": "spawn_agent",
        "namespace": "collaboration",
        "arguments": '{"message":"inspect","task_name":"worker"}',
        "encrypted_function_args": [],
        "call_id": "call-1",
    }
    projected_call = {
        key: copy.deepcopy(value)
        for key, value in raw_response_call.items()
        if key != "id"
    }
    output = function_output("spawned")
    handle_request(state, {"input": [copy.deepcopy(user)]})

    handle_response_completed(state, [copy.deepcopy(raw_response_call)])

    assert transcript_items(state) == [user, projected_call]
    assert state.codex_input_cursor == [user, projected_call]

    next_input = [copy.deepcopy(user), copy.deepcopy(projected_call), copy.deepcopy(output)]
    forwarded = handle_request(state, {"input": next_input})

    assert forwarded["input"] == next_input
    assert transcript_items(state) == next_input
    assert state.tail_conflict is False


def test_response_completed_preserves_ids_after_id_bearing_request() -> None:
    state = ProxyState()
    user = message("user", "hello", item_id="user-stable")
    raw_response_message = {
        "id": "msg-stable",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {
                "id": "part-stable",
                "type": "output_text",
                "annotations": [],
                "logprobs": [],
                "text": "hi",
            }
        ],
    }
    next_request_message = {
        "id": "msg-stable",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "hi"}],
    }
    handle_request(state, {"input": [user]})

    handle_response_completed(state, [raw_response_message])

    assert state.request_item_ids is True
    assert transcript_items(state) == [user, next_request_message]
    assert state.codex_input_cursor == [user, next_request_message]


def test_tool_continuation_pops_old_tail_and_appends_new_tail() -> None:
    state = ProxyState()
    user = message("user", "run lookup")
    old_call = function_call('{"q":"old"}', item_id="old-dynamic")
    new_call = function_call('{"q":"new"}', item_id="new-dynamic")
    output = function_output("done")

    handle_request(state, {"input": [user, old_call]})
    forwarded = handle_request(state, {"input": [user, new_call, output]})

    assert forwarded["input"] == [user, new_call, output]
    assert transcript_items(state) == [user, new_call, output]
    assert state.codex_input_cursor == [user, new_call, output]
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


def test_workbench_compressed_transcript_surfaces_phase_delta_as_conflict() -> None:
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

    assert state.tail_conflict is True
    assert forwarded["input"] == [summary, next_request_message, followup]
    assert transcript_items(state) == [summary, next_request_message, followup]
    assert state.codex_input_cursor == [user, next_request_message, followup]


def test_workbench_replaced_assistant_does_not_restore_old_assistant_when_cursor_matches_codex_shape() -> None:
    state = ProxyState()
    first_turn_id = "turn-1"
    second_turn_id = "turn-2"
    user = with_turn_id(message("user", "question"), first_turn_id)
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
    next_request_message = with_turn_id(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "old answer"}],
            "phase": "final_answer",
        },
        first_turn_id,
    )
    edited_assistant = message("assistant", "1234")
    followup = with_turn_id(message("user", "what did you say"), second_turn_id)

    handle_request(state, request_body([user], turn_id=first_turn_id))
    handle_response_completed(state, [raw_response_message])
    state.transcript = input_items_to_transcript([copy.deepcopy(user), copy.deepcopy(edited_assistant)])

    forwarded = handle_request(
        state,
        request_body([user, next_request_message, followup], turn_id=second_turn_id),
    )

    assert state.tail_conflict is False
    assert forwarded["input"] == [user, edited_assistant, followup]
    assert transcript_items(state) == [user, edited_assistant, followup]
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


def test_reasoning_null_content_survives_request_rebuild() -> None:
    state = ProxyState()
    user = message("user", "question")
    reasoning = {
        "type": "reasoning",
        "summary": [],
        "content": None,
        "encrypted_content": "stable-reasoning",
    }
    followup = message("user", "follow up")

    handle_request(state, {"input": [copy.deepcopy(user), copy.deepcopy(reasoning)]})
    forwarded = handle_request(
        state,
        {
            "input": [
                copy.deepcopy(user),
                copy.deepcopy(reasoning),
                copy.deepcopy(followup),
            ],
        },
    )

    assert forwarded["input"][1] == reasoning
    assert transcript_items(state)[1] == reasoning


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
            "x-codex-turn-metadata": '{"request_kind":"compaction","compaction":{"trigger":"manual","phase":"pre_turn"}}',
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
                "compaction": {"trigger": "auto", "phase": "mid_turn"},
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
            "x-codex-turn-metadata": '{"request_kind":"compaction","compaction":{"trigger":"manual","phase":"pre_turn"}}',
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


def test_auto_mid_turn_compact_simulates_current_user_as_cursor_prefix() -> None:
    state = ProxyState()
    built_in_prompt = f"{LOCAL_COMPACT_PROMPT_PREFIX}\nSummarize the active turn."
    compact_body = {
        "client_metadata": {
            "x-codex-turn-metadata": {
                "request_kind": "compaction",
                "compaction": {"trigger": "auto", "phase": "mid_turn"},
            },
        },
        "input": [
            message("user", "active request"),
            message("assistant", "partial response before compact"),
            message("user", built_in_prompt),
        ],
    }

    forwarded = handle_request(state, compact_body)
    assert state.compact_kind == "auto"
    assert forwarded["input"][-1]["content"] == AUTO_LOCAL_COMPACT_PROMPT

    handle_response_completed(state, [message("assistant", "compact summary")])
    expected_summary = typed_message(
        "user",
        f"{LOCAL_COMPACT_SUMMARY_PREFIX}\n\ncompact summary",
    )
    simulated_input = [message("user", "active request"), expected_summary]
    assert state.codex_input_cursor == simulated_input
    assert transcript_to_input_items(state.transcript) == simulated_input
    assert not any(node["role"] == "assistant" for node in state.transcript)

    continuation_input = copy.deepcopy(simulated_input)
    transcript_before_continuation = copy.deepcopy(state.transcript)
    forwarded_continuation = handle_request(state, {"input": continuation_input})
    assert forwarded_continuation["input"] == continuation_input
    assert state.transcript == transcript_before_continuation
    assert state.tail_conflict is False

    handle_response_completed(state, [message("assistant", "final response after compact")])

    assistant_nodes = [node for node in state.transcript if node["role"] == "assistant"]
    assert len(assistant_nodes) == 1
    assert transcript_to_input_items(state.transcript) == [
        message("user", "active request"),
        expected_summary,
        message("assistant", "final response after compact"),
    ]


def test_lite_compact_prefix_matches_when_stable_and_full_pop_recovers_when_changed() -> None:
    state = ProxyState()
    built_in_prompt = f"{LOCAL_COMPACT_PROMPT_PREFIX}\nSummarize the active turn."
    tools_v1 = {
        "type": "additional_tools",
        "role": "developer",
        "tools": [{"type": "function", "name": "exec", "description": "v1"}],
    }
    base_developer = typed_message("developer", "Codex base instructions")
    runtime_developer = typed_message("developer", "permissions and skills")
    compact_body = {
        "client_metadata": {
            "x-codex-turn-metadata": {
                "request_kind": "compaction",
                "compaction": {"trigger": "auto", "phase": "mid_turn"},
            },
        },
        "input": [
            tools_v1,
            base_developer,
            runtime_developer,
            message("user", "active request"),
            message("assistant", "partial response before compact"),
            message("user", built_in_prompt),
        ],
    }

    handle_request(state, compact_body)
    handle_response_completed(state, [message("assistant", "compact summary")])
    expected_summary = typed_message(
        "user",
        f"{LOCAL_COMPACT_SUMMARY_PREFIX}\n\ncompact summary",
    )
    simulated_input = [
        tools_v1,
        base_developer,
        runtime_developer,
        message("user", "active request"),
        expected_summary,
    ]
    assert state.codex_input_cursor == simulated_input
    assert transcript_to_input_items(state.transcript) == simulated_input

    stable_transcript = copy.deepcopy(state.transcript)
    handle_request(state, {"input": copy.deepcopy(simulated_input)})
    assert state.transcript == stable_transcript
    assert state.tail_conflict is False

    tools_v2 = copy.deepcopy(tools_v1)
    tools_v2["tools"][0]["description"] = "v2"
    rebuilt_input = [tools_v2, *simulated_input[1:]]
    forwarded = handle_request(state, {"input": rebuilt_input})

    assert forwarded["input"] == rebuilt_input
    assert state.codex_input_cursor == rebuilt_input
    assert transcript_to_input_items(state.transcript) == rebuilt_input
    assert state.tail_conflict is False


def main() -> None:
    tests = [
        test_new_thread_empty_cursor_appends_full_input,
        test_same_request_retry_is_idempotent,
        test_response_completed_appends_assistant_to_cursor_and_transcript,
        test_response_completed_stores_next_request_shape_in_cursor,
        test_response_completed_preserves_plaintext_collaboration_marker,
        test_response_completed_preserves_ids_after_id_bearing_request,
        test_tool_continuation_pops_old_tail_and_appends_new_tail,
        test_pop_conflict_keeps_existing_tail_and_still_appends,
        test_workbench_compressed_transcript_does_not_restore_old_assistant_on_next_request,
        test_workbench_compressed_transcript_surfaces_phase_delta_as_conflict,
        test_workbench_replaced_assistant_does_not_restore_old_assistant_when_cursor_matches_codex_shape,
        test_workbench_compressed_transcript_does_not_restore_reasoning_turn,
        test_reasoning_null_content_survives_request_rebuild,
        test_user_edited_transcript_survives_next_raw_codex_request,
        test_compact_metadata_sets_pending_and_uses_prompt_replacement_hook,
        test_compact_metadata_without_controller_does_not_remote_compact,
        test_auto_loaded_compact_controller_replaces_prompt_and_simulates_success,
        test_auto_mid_turn_compact_simulates_current_user_as_cursor_prefix,
        test_lite_compact_prefix_matches_when_stable_and_full_pop_recovers_when_changed,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} proxy core tests passed")


if __name__ == "__main__":
    main()
