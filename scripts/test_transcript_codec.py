from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.transcript_codec import (  # noqa: E402
    NON_DICT_PROVIDER_ITEM_MARKER,
    append_input_items,
    input_items_to_transcript,
    transcript_to_input_items,
)


def message(role: str, text: str) -> dict[str, Any]:
    return {"type": "message", "role": role, "content": text}


def item_types(node: dict[str, Any]) -> list[str]:
    return [item["providerItem"].get("type", item["kind"]) for item in node["items"]]


def test_unknown_and_non_dict_items_roundtrip() -> None:
    unknown_item = {"type": "unexpected_context_blob", "role": "context", "payload": {"x": 1}}
    role_without_message_type = {"role": "user", "content": "not a Message item"}
    input_items: list[Any] = [
        message("user", "hello"),
        unknown_item,
        "raw-string-item",
        42,
        None,
        role_without_message_type,
    ]

    transcript = input_items_to_transcript(input_items)

    assert [node["role"] for node in transcript] == [
        "user",
        "context",
        "unknown",
        "unknown",
        "unknown",
        "user",
    ]
    assert transcript[2]["items"][0]["providerItem"][NON_DICT_PROVIDER_ITEM_MARKER] is True
    assert transcript_to_input_items(transcript) == input_items


def test_assistant_tool_call_and_output_group_together() -> None:
    call_id = "call_weather"
    input_items = [
        message("user", "check weather"),
        message("assistant", "I will call a tool."),
        {
            "type": "function_call",
            "call_id": call_id,
            "name": "get_weather",
            "arguments": '{"city":"Shanghai"}',
        },
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": "sunny",
        },
        message("assistant", "It is sunny."),
    ]

    transcript = input_items_to_transcript(input_items)

    assert [node["role"] for node in transcript] == ["user", "assistant"]
    assert item_types(transcript[1]) == [
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert transcript_to_input_items(transcript) == input_items


def test_orphan_tool_output_uses_recent_assistant_or_creates_one() -> None:
    unmatched_output = {
        "type": "function_call_output",
        "call_id": "missing_call",
        "output": "late output",
    }
    input_items = [
        message("assistant", "previous answer"),
        message("user", "new question"),
        unmatched_output,
    ]

    transcript = input_items_to_transcript(input_items)

    assert [node["role"] for node in transcript] == ["assistant", "user"]
    assert transcript[0]["items"][1]["providerItem"] == unmatched_output
    assert transcript_to_input_items(transcript) == input_items

    only_output = {
        "type": "function_call_output",
        "call_id": "fully_orphaned",
        "output": "orphan",
    }
    orphan_transcript = input_items_to_transcript([only_output])
    assert [node["role"] for node in orphan_transcript] == ["assistant"]
    assert orphan_transcript[0]["items"][0]["providerItem"] == only_output
    assert transcript_to_input_items(orphan_transcript) == [only_output]


def test_mcp_and_local_shell_outputs_group_with_assistant() -> None:
    mcp_output = {
        "type": "mcp_tool_call_output",
        "call_id": "mcp-1",
        "output": {"content": [{"type": "text", "text": "mcp ok"}]},
    }
    local_output = {
        "type": "local_shell_call_output",
        "call_id": "shell-1",
        "output": "shell ok",
    }
    input_items = [
        message("assistant", "previous answer"),
        mcp_output,
        local_output,
        message("user", "next"),
    ]

    transcript = input_items_to_transcript(input_items)

    assert [node["role"] for node in transcript] == ["assistant", "user"]
    assert item_types(transcript[0]) == [
        "message",
        "mcp_tool_call_output",
        "local_shell_call_output",
    ]
    assert transcript_to_input_items(transcript) == input_items


def test_compaction_is_independent_node() -> None:
    compaction = {
        "type": "context_compaction",
        "role": "context",
        "summary": "compressed context",
    }
    input_items = [
        message("assistant", "before compact"),
        compaction,
        message("assistant", "after compact"),
    ]

    transcript = input_items_to_transcript(input_items)

    assert [node["role"] for node in transcript] == ["assistant", "context", "assistant"]
    assert transcript[1]["items"][0]["providerItem"] == compaction
    assert transcript_to_input_items(transcript) == input_items


def test_agent_message_is_subagent_node() -> None:
    agent_message = {
        "type": "agent_message",
        "author": "worker",
        "recipient": "root",
        "content": [{"type": "input_text", "text": "worker result"}],
    }
    input_items = [
        message("assistant", "before worker"),
        agent_message,
        message("assistant", "after worker"),
    ]

    transcript = input_items_to_transcript(input_items)

    assert [node["role"] for node in transcript] == ["assistant", "subagent", "assistant"]
    assert transcript[1]["items"][0]["providerItem"] == agent_message
    assert transcript_to_input_items(transcript) == input_items


def test_additional_tools_creates_developer_node() -> None:
    additional_tools = {
        "type": "additional_tools",
        "tools": [{"type": "function", "name": "shell_command"}],
    }
    input_items = [
        additional_tools,
        message("developer", "use tools carefully"),
        message("user", "list files"),
    ]

    transcript = input_items_to_transcript(input_items)

    assert [node["role"] for node in transcript] == ["developer", "developer", "user"]
    assert transcript[0]["items"][0]["providerItem"] == additional_tools
    assert transcript_to_input_items(transcript) == input_items


def test_system_message_preserves_original_role() -> None:
    input_items = [
        message("system", "system policy"),
        message("developer", "developer instructions"),
    ]

    transcript = input_items_to_transcript(input_items)

    assert [node["role"] for node in transcript] == ["system", "developer"]
    assert transcript_to_input_items(transcript) == input_items


def test_append_preserves_existing_user_node_id() -> None:
    input_items = [message("user", "hello")]
    transcript = input_items_to_transcript(input_items)
    user_node_id = transcript[0]["id"]

    result = append_input_items(transcript, [message("assistant", "hi")])

    assert result is transcript
    assert transcript[0]["id"] == user_node_id
    assert [node["role"] for node in transcript] == ["user", "assistant"]
    assert transcript_to_input_items(transcript) == [
        *input_items,
        message("assistant", "hi"),
    ]


def test_append_assistant_tool_keeps_existing_assistant_node() -> None:
    call_id = "call_weather"
    input_items = [
        message("user", "check weather"),
        message("assistant", "I will call a tool."),
        {
            "type": "function_call",
            "call_id": call_id,
            "name": "get_weather",
            "arguments": '{"city":"Shanghai"}',
        },
    ]
    transcript = input_items_to_transcript(input_items)
    assistant_id = transcript[1]["id"]

    append_items = [
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": "sunny",
        },
        message("assistant", "It is sunny."),
    ]
    result = append_input_items(transcript, append_items)

    assert result is transcript
    assert transcript[1]["id"] == assistant_id
    assert len(transcript) == 2
    assert item_types(transcript[1]) == [
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert transcript_to_input_items(transcript) == [*input_items, *append_items]


def test_append_roundtrip_order_for_orphan_output_compaction_and_unknown() -> None:
    input_items = [
        message("assistant", "previous answer"),
        message("user", "new question"),
    ]
    transcript = input_items_to_transcript(input_items)
    assistant_id = transcript[0]["id"]
    user_id = transcript[1]["id"]

    append_items = [
        {
            "type": "function_call_output",
            "call_id": "missing_call",
            "output": "late output",
        },
        {"type": "context_compaction", "role": "context", "summary": "compressed"},
        {"type": "surprise_item", "payload": True},
    ]
    result = append_input_items(transcript, append_items)

    assert result is transcript
    assert transcript[0]["id"] == assistant_id
    assert transcript[1]["id"] == user_id
    assert [node["role"] for node in transcript] == [
        "assistant",
        "user",
        "context",
        "unknown",
    ]
    assert item_types(transcript[0]) == ["message", "function_call_output"]
    assert transcript_to_input_items(transcript) == [*input_items, *append_items]


def main() -> None:
    tests = [
        test_unknown_and_non_dict_items_roundtrip,
        test_assistant_tool_call_and_output_group_together,
        test_orphan_tool_output_uses_recent_assistant_or_creates_one,
        test_mcp_and_local_shell_outputs_group_with_assistant,
        test_compaction_is_independent_node,
        test_agent_message_is_subagent_node,
        test_additional_tools_creates_developer_node,
        test_system_message_preserves_original_role,
        test_append_preserves_existing_user_node_id,
        test_append_assistant_tool_keeps_existing_assistant_node,
        test_append_roundtrip_order_for_orphan_output_compaction_and_unknown,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} transcript codec tests passed")


if __name__ == "__main__":
    main()
