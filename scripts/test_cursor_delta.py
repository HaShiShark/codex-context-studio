from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.codex_input_cursor import (  # noqa: E402
    canonical_provider_item_for_request,
    compute_diff,
    fingerprint_provider_item,
)
from backend.transcript_delta_applier import TranscriptDeltaApplier  # noqa: E402


class TestTranscriptCodec:
    append_calls = 0

    @classmethod
    def append_input_items(cls, transcript: list[dict[str, Any]], input_items: list[dict[str, Any]]):
        cls.append_calls += 1
        for item in input_items:
            cls._append_one(transcript, item)
        return transcript

    @classmethod
    def to_transcript(cls, input_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        transcript: list[dict[str, Any]] = []
        for item in input_items:
            cls._append_one(transcript, item)
        return transcript

    @staticmethod
    def to_input_items(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            node_item["providerItem"]
            for node in transcript
            for node_item in node.get("items", [])
        ]

    @classmethod
    def _append_one(cls, transcript: list[dict[str, Any]], item: dict[str, Any]) -> None:
        kind = item.get("type") or ("message" if "role" in item else "unknown")
        role = item.get("role")

        if kind == "message" and role == "user":
            transcript.append(cls._node("user", item))
            return

        if kind == "message" and role in {"developer", "system"}:
            transcript.append(cls._node("developer", item))
            return

        if kind in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
            node = cls._find_assistant_by_call_id(transcript, item.get("call_id"))
            if node is None:
                node = cls._last_assistant(transcript)
            if node is None:
                transcript.append(cls._node("assistant", item))
            else:
                cls._append_to_node(node, item)
            return

        if role == "assistant" or kind in {
            "reasoning",
            "function_call",
            "custom_tool_call",
            "local_shell_call",
            "tool_search_call",
            "web_search_call",
            "image_generation_call",
        }:
            node = cls._last_assistant(transcript)
            if node is None:
                transcript.append(cls._node("assistant", item))
            else:
                cls._append_to_node(node, item)
            return

        transcript.append(cls._node(role or "unknown", item))

    @staticmethod
    def _node(role: str, item: dict[str, Any]) -> dict[str, Any]:
        node = {"id": f"node-{role}", "role": role, "items": [], "source_map": {}}
        TestTranscriptCodec._append_to_node(node, item)
        return node

    @staticmethod
    def _append_to_node(node: dict[str, Any], item: dict[str, Any]) -> None:
        node["items"].append({"kind": item.get("type", "message"), "providerItem": item})
        node["source_map"][fingerprint_provider_item(item)] = f"items[{len(node['items']) - 1}]"

    @staticmethod
    def _last_assistant(transcript: list[dict[str, Any]]) -> dict[str, Any] | None:
        for node in reversed(transcript):
            if node.get("role") == "assistant":
                return node
        return None

    @staticmethod
    def _find_assistant_by_call_id(
        transcript: list[dict[str, Any]],
        call_id: str | None,
    ) -> dict[str, Any] | None:
        if not call_id:
            return None
        for node in reversed(transcript):
            if node.get("role") != "assistant":
                continue
            for node_item in node.get("items", []):
                if node_item.get("providerItem", {}).get("call_id") == call_id:
                    return node
        return None


def test_fingerprint_excludes_id_but_keeps_semantics() -> None:
    first = {
        "id": "dynamic-a",
        "type": "function_call",
        "call_id": "call-1",
        "name": "lookup",
        "arguments": "{\"q\":\"alpha\"}",
        "output": None,
        "content": [{"type": "text", "text": "hello", "id": "nested-dynamic"}],
    }
    second = {
        **first,
        "id": "dynamic-b",
        "content": [{"type": "text", "text": "hello", "id": "nested-other"}],
    }
    changed_call_id = {**second, "call_id": "call-2"}
    changed_reasoning = {
        "id": "reasoning-a",
        "type": "reasoning",
        "encrypted_content": "stable-blob-a",
    }
    changed_reasoning_blob = {
        **changed_reasoning,
        "id": "reasoning-b",
        "encrypted_content": "stable-blob-b",
    }

    assert fingerprint_provider_item(first) == fingerprint_provider_item(second)
    assert fingerprint_provider_item(second) != fingerprint_provider_item(changed_call_id)
    assert fingerprint_provider_item(changed_reasoning) != fingerprint_provider_item(changed_reasoning_blob)


def test_tool_search_output_preserves_schema_property_named_id() -> None:
    tool_search_output = {
        "id": "ts-output-dynamic",
        "type": "tool_search_output",
        "call_id": "call-search",
        "status": "completed",
        "execution": "client",
        "tools": [
            {
                "type": "namespace",
                "name": "multi_agent_v1",
                "tools": [
                    {
                        "type": "function",
                        "name": "resume_agent",
                        "defer_loading": True,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "Agent id to resume.",
                                }
                            },
                            "required": ["id"],
                            "additionalProperties": False,
                        },
                    }
                ],
            }
        ],
    }

    canonical = canonical_provider_item_for_request(tool_search_output)

    assert "id" not in canonical
    assert canonical["tools"][0]["tools"][0]["parameters"]["properties"]["id"] == {
        "type": "string",
        "description": "Agent id to resume.",
    }


def test_fingerprint_distinguishes_missing_schema_property_named_id() -> None:
    valid_tool_search_output = {
        "type": "tool_search_output",
        "call_id": "call-search",
        "status": "completed",
        "execution": "client",
        "tools": [
            {
                "type": "namespace",
                "name": "multi_agent_v1",
                "tools": [
                    {
                        "type": "function",
                        "name": "resume_agent",
                        "parameters": {
                            "type": "object",
                            "properties": {"id": {"type": "string"}},
                            "required": ["id"],
                            "additionalProperties": False,
                        },
                    }
                ],
            }
        ],
    }
    broken_tool_search_output = {
        **valid_tool_search_output,
        "tools": [
            {
                "type": "namespace",
                "name": "multi_agent_v1",
                "tools": [
                    {
                        "type": "function",
                        "name": "resume_agent",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "required": ["id"],
                            "additionalProperties": False,
                        },
                    }
                ],
            }
        ],
    }

    diff = compute_diff([broken_tool_search_output], [valid_tool_search_output])

    assert diff.prefix_len == 0
    assert diff.pop == [broken_tool_search_output]
    assert diff.append == [canonical_provider_item_for_request(valid_tool_search_output)]


def test_full_prefix_match_is_idempotent() -> None:
    cursor = [{"id": "a", "role": "user", "content": "hello"}]
    new_input = [{"id": "b", "role": "user", "content": "hello"}]

    diff = compute_diff(cursor, new_input)

    assert diff.prefix_len == 1
    assert diff.pop == []
    assert diff.append == []


def test_response_item_fingerprint_matches_next_request_shape() -> None:
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
                "text": "hello",
            }
        ],
        "phase": "final_answer",
    }
    next_request_message = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "hello"}],
        "phase": "final_answer",
    }

    diff = compute_diff([raw_response_message], [next_request_message])

    assert diff.prefix_len == 1
    assert diff.pop == []
    assert diff.append == []


def test_message_phase_is_not_part_of_cursor_identity() -> None:
    cursor_message = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "hello"}],
    }
    next_request_message = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "hello"}],
        "phase": "final_answer",
    }

    diff = compute_diff([cursor_message], [next_request_message])

    assert diff.prefix_len == 1
    assert diff.pop == []
    assert diff.append == []


def test_reasoning_empty_content_matches_next_request_shape() -> None:
    raw_response_reasoning = {
        "id": "rs-dynamic",
        "type": "reasoning",
        "summary": [],
        "content": [],
        "encrypted_content": "stable-reasoning",
    }
    next_request_reasoning = {
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "stable-reasoning",
    }

    diff = compute_diff([raw_response_reasoning], [next_request_reasoning])

    assert diff.prefix_len == 1
    assert diff.pop == []
    assert diff.append == []


def test_reasoning_plain_text_content_matches_next_request_shape() -> None:
    raw_response_reasoning = {
        "type": "reasoning",
        "summary": [],
        "content": [{"type": "text", "text": "hidden chain"}],
        "encrypted_content": "stable-reasoning",
    }
    next_request_reasoning = {
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "stable-reasoning",
    }

    diff = compute_diff([raw_response_reasoning], [next_request_reasoning])

    assert diff.prefix_len == 1
    assert diff.pop == []
    assert diff.append == []


def test_reasoning_text_content_is_kept() -> None:
    cursor = [
        {
            "type": "reasoning",
            "summary": [],
            "content": [{"type": "reasoning_text", "text": "visible reasoning"}],
            "encrypted_content": "stable-reasoning",
        }
    ]
    new_input = [
        {
            "type": "reasoning",
            "summary": [],
            "encrypted_content": "stable-reasoning",
        }
    ]

    diff = compute_diff(cursor, new_input)

    assert diff.prefix_len == 0


def test_pop_removes_matching_tail_item() -> None:
    user = {"type": "message", "role": "user", "content": "hello"}
    assistant = {"type": "message", "role": "assistant", "content": "hi"}
    transcript = TestTranscriptCodec.to_transcript([user, assistant])

    result = TranscriptDeltaApplier.pop(transcript, [assistant])

    assert result.removed == 1
    assert result.tail_conflict is False
    assert TestTranscriptCodec.to_input_items(transcript) == [user]


def test_pop_conflict_does_not_delete_tail() -> None:
    user = {"type": "message", "role": "user", "content": "hello"}
    actual_tail = {"type": "message", "role": "assistant", "content": "edited"}
    stale_tail = {"type": "message", "role": "assistant", "content": "old"}
    transcript = TestTranscriptCodec.to_transcript([user, actual_tail])

    result = TranscriptDeltaApplier.pop(transcript, [stale_tail])

    assert result.removed == 0
    assert result.tail_conflict is True
    assert TestTranscriptCodec.to_input_items(transcript) == [user, actual_tail]


def test_append_uses_codec_grouping() -> None:
    TestTranscriptCodec.append_calls = 0
    user = {"type": "message", "role": "user", "content": "hello"}
    assistant = {"type": "message", "role": "assistant", "content": "thinking"}
    call = {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"}
    output = {"type": "function_call_output", "call_id": "call-1", "output": "done"}
    transcript = TestTranscriptCodec.to_transcript([user])

    result = TranscriptDeltaApplier.append(
        transcript,
        [assistant, call, output],
        codec=TestTranscriptCodec,
    )

    assert result.appended == 3
    assert TestTranscriptCodec.append_calls == 1
    assert [node["role"] for node in transcript] == ["user", "assistant"]
    assert len(transcript[-1]["items"]) == 3
    assert TestTranscriptCodec.to_input_items(transcript) == [user, assistant, call, output]


def test_tool_continuation_tail_replacement() -> None:
    user = {"type": "message", "role": "user", "content": "run lookup"}
    old_call = {
        "id": "old-dynamic",
        "type": "function_call",
        "call_id": "call-1",
        "name": "lookup",
        "arguments": "{\"q\":\"old\"}",
    }
    new_call = {
        "id": "new-dynamic",
        "type": "function_call",
        "call_id": "call-1",
        "name": "lookup",
        "arguments": "{\"q\":\"new\"}",
    }
    output = {"type": "function_call_output", "call_id": "call-1", "output": "result"}

    cursor = [user, old_call]
    new_input = [user, new_call, output]
    transcript = TestTranscriptCodec.to_transcript(cursor)

    diff = compute_diff(cursor, new_input)
    pop_result = TranscriptDeltaApplier.pop(transcript, diff.pop)
    append_result = TranscriptDeltaApplier.append(
        transcript,
        diff.append,
        codec=TestTranscriptCodec,
    )

    assert diff.prefix_len == 1
    assert pop_result.removed == 1
    assert pop_result.tail_conflict is False
    assert append_result.appended == 2
    assert TestTranscriptCodec.to_input_items(transcript) == new_input


def test_append_supports_repository_codec_when_present() -> None:
    try:
        from backend import transcript_codec
    except ImportError:
        return

    if not (
        hasattr(transcript_codec, "input_items_to_transcript")
        and hasattr(transcript_codec, "transcript_to_input_items")
    ):
        return

    user = {"type": "message", "role": "user", "content": "hello"}
    assistant = {"type": "message", "role": "assistant", "content": "hi"}
    transcript = transcript_codec.input_items_to_transcript([user])

    result = TranscriptDeltaApplier.append(transcript, [assistant])

    assert result.appended == 1
    assert transcript_codec.transcript_to_input_items(transcript) == [user, assistant]


def main() -> None:
    tests = [
        test_fingerprint_excludes_id_but_keeps_semantics,
        test_full_prefix_match_is_idempotent,
        test_response_item_fingerprint_matches_next_request_shape,
        test_message_phase_is_not_part_of_cursor_identity,
        test_reasoning_empty_content_matches_next_request_shape,
        test_reasoning_plain_text_content_matches_next_request_shape,
        test_reasoning_text_content_is_kept,
        test_pop_removes_matching_tail_item,
        test_pop_conflict_does_not_delete_tail,
        test_append_uses_codec_grouping,
        test_tool_continuation_tail_replacement,
        test_append_supports_repository_codec_when_present,
    ]
    for test in tests:
        test()
    print(f"OK: {len(tests)} cursor/delta tests passed")


if __name__ == "__main__":
    main()
