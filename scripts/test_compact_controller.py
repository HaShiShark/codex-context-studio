from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.compact_controller import (  # noqa: E402
    AUTO_LOCAL_COMPACT_PROMPT,
    LOCAL_COMPACT_PROMPT_PREFIX,
    LOCAL_COMPACT_SUMMARY_PREFIX,
    MANUAL_LOCAL_COMPACT_PROMPT,
    CompactController,
    build_local_compact_summary_text,
    is_compact_request,
    parse_compact_turn_metadata,
)
from backend.transcript_codec import (  # noqa: E402
    input_items_to_transcript,
    transcript_to_input_items,
)


def message(role: str, text: str) -> dict[str, Any]:
    return {"type": "message", "role": role, "content": text}


def typed_message(role: str, text: str) -> dict[str, Any]:
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


def message_with_content(role: str, content: Any) -> dict[str, Any]:
    return {"id": f"{role}-dynamic-id", "type": "message", "role": role, "content": content}


def test_metadata_detects_manual_and_auto_compact() -> None:
    manual_body = {
        "client_metadata": {
            "x-codex-turn-metadata": json.dumps(
                {
                    "request_kind": "compaction",
                    "turn_id": "turn-manual",
                    "compaction": {"trigger": "manual", "phase": "pre_turn"},
                }
            )
        }
    }
    auto_body = {
        "client_metadata": {
            "x-codex-turn-metadata": json.dumps(
                {
                    "request_kind": "compaction",
                    "turn_id": "turn-auto",
                    "compaction": {"trigger": "auto", "phase": "mid_turn"},
                }
            )
        }
    }

    manual = parse_compact_turn_metadata(manual_body)
    auto = CompactController.parse_turn_metadata(auto_body)

    assert manual is not None
    assert manual.trigger == "manual"
    assert manual.raw["turn_id"] == "turn-manual"
    assert auto is not None
    assert auto.trigger == "auto"
    assert is_compact_request(manual_body) is True
    assert CompactController.is_compact_request(auto_body) is True


def test_non_compact_metadata_does_not_trigger() -> None:
    normal_body = {
        "client_metadata": {
            "x-codex-turn-metadata": json.dumps(
                {"request_kind": "completion", "trigger": "manual"}
            )
        }
    }
    invalid_body = {"client_metadata": {"x-codex-turn-metadata": "{not-json"}}
    missing_body = {"input": []}
    obsolete_flat_body = {
        "client_metadata": {
            "x-codex-turn-metadata": json.dumps(
                {"request_kind": "compaction", "trigger": "auto"}
            )
        }
    }

    assert parse_compact_turn_metadata(normal_body) is None
    assert parse_compact_turn_metadata(invalid_body) is None
    assert parse_compact_turn_metadata(missing_body) is None
    assert is_compact_request(normal_body) is False
    assert parse_compact_turn_metadata(obsolete_flat_body) is None


def test_replace_prompt_roundtrips_through_transcript_to_input_items() -> None:
    original_prompt = f"{LOCAL_COMPACT_PROMPT_PREFIX}\nSummarize the conversation."
    compact_prompt_item = message_with_content(
        "user",
        [
            {"type": "input_text", "text": original_prompt, "cache_control": {"type": "ephemeral"}},
            {"type": "input_image", "image_url": "data:image/png;base64,abc"},
        ],
    )
    transcript = input_items_to_transcript(
        [
            message("developer", "dev context"),
            message("user", "real request"),
            compact_prompt_item,
        ]
    )

    result = CompactController.replace_last_compact_prompt(transcript, "manual")
    output_items = transcript_to_input_items(result.new_transcript)
    replaced_item = output_items[-1]

    assert result.replaced is True
    assert output_items[:-1] == [
        message("developer", "dev context"),
        message("user", "real request"),
    ]
    assert replaced_item["id"] == "user-dynamic-id"
    assert replaced_item["content"][0]["type"] == "input_text"
    assert replaced_item["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert replaced_item["content"][0]["text"] == MANUAL_LOCAL_COMPACT_PROMPT
    assert replaced_item["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,abc"}


def test_compact_success_manual_builds_retained_users_plus_summary() -> None:
    previous_summary = build_local_compact_summary_text("old summary")
    transcript = input_items_to_transcript(
        [
            message("developer", "developer context is not retained"),
            message("user", "first user"),
            message("assistant", "first answer"),
            message("user", previous_summary),
            message("user", "second user"),
            message("user", AUTO_LOCAL_COMPACT_PROMPT),
        ]
    )

    result = CompactController.on_compact_success(
        transcript,
        [message("assistant", "new compact summary")],
        "manual",
    )
    new_items = result.new_items

    assert result.retained_user_count == 2
    assert transcript_to_input_items(result.new_transcript) == new_items
    assert new_items == [
        message("user", "first user"),
        message("user", "second user"),
        typed_message("user", f"{LOCAL_COMPACT_SUMMARY_PREFIX}\n\nnew compact summary"),
    ]


def test_auto_mid_turn_compact_simulates_active_user_without_assistant() -> None:
    transcript = input_items_to_transcript(
        [
            message("user", "stable user"),
            message("user", "in progress user"),
            message("assistant", "partial work"),
            message("user", AUTO_LOCAL_COMPACT_PROMPT),
        ]
    )
    result = CompactController.on_compact_success(
        transcript,
        [message("assistant", "auto summary")],
        "auto",
    )

    assert result.new_items == [
        message("user", "stable user"),
        message("user", "in progress user"),
        typed_message("user", f"{LOCAL_COMPACT_SUMMARY_PREFIX}\n\nauto summary"),
    ]
    assert transcript_to_input_items(result.new_transcript) == result.new_items
    assert all(node["role"] == "user" for node in result.new_transcript)


def test_lite_compact_simulation_keeps_only_leading_protocol_context() -> None:
    additional_tools = {
        "type": "additional_tools",
        "role": "developer",
        "tools": [{"type": "function", "name": "exec"}],
    }
    base_developer = typed_message("developer", "Codex base instructions")
    runtime_developer = typed_message("developer", "permissions and skills")
    transcript = input_items_to_transcript(
        [
            additional_tools,
            base_developer,
            runtime_developer,
            message("user", "active request"),
            message("assistant", "partial work"),
            message("developer", "non-prefix world state"),
            message("user", AUTO_LOCAL_COMPACT_PROMPT),
        ]
    )

    result = CompactController.on_compact_success(
        transcript,
        [message("assistant", "Lite summary")],
        "auto",
    )

    assert result.new_items == [
        additional_tools,
        base_developer,
        runtime_developer,
        message("user", "active request"),
        typed_message("user", f"{LOCAL_COMPACT_SUMMARY_PREFIX}\n\nLite summary"),
    ]
    assert transcript_to_input_items(result.new_transcript) == result.new_items
    assert not any(node["role"] == "assistant" for node in result.new_transcript)
    assert all(
        item.get("content") != "non-prefix world state"
        for item in result.new_items
        if isinstance(item, dict)
    )


def test_summary_message_is_not_collected_again() -> None:
    existing_summary = build_local_compact_summary_text("summary from prior compact")
    transcript = input_items_to_transcript(
        [
            message("user", "before compact"),
            message("user", existing_summary),
            message("user", "after compact"),
        ]
    )

    result = CompactController.on_compact_success(
        transcript,
        [message("assistant", "second summary")],
        "manual",
    )

    assert result.new_items == [
        message("user", "before compact"),
        message("user", "after compact"),
        typed_message("user", f"{LOCAL_COMPACT_SUMMARY_PREFIX}\n\nsecond summary"),
    ]


def test_auto_mid_turn_active_user_participates_in_history_budget() -> None:
    active_user = "active-" + ("x" * 200)
    transcript = input_items_to_transcript(
        [
            message("user", "older user"),
            message("user", active_user),
            message("assistant", "partial work"),
            message("user", AUTO_LOCAL_COMPACT_PROMPT),
        ]
    )

    result = CompactController.on_compact_success(
        transcript,
        [message("assistant", "summary")],
        "auto",
        max_user_tokens=5,
    )

    retained_text = result.new_items[0]["content"]
    assert retained_text == active_user[:20]
    assert result.retained_user_count == 1


def main() -> None:
    tests = [
        test_metadata_detects_manual_and_auto_compact,
        test_non_compact_metadata_does_not_trigger,
        test_replace_prompt_roundtrips_through_transcript_to_input_items,
        test_compact_success_manual_builds_retained_users_plus_summary,
        test_auto_mid_turn_compact_simulates_active_user_without_assistant,
        test_lite_compact_simulation_keeps_only_leading_protocol_context,
        test_summary_message_is_not_collected_again,
        test_auto_mid_turn_active_user_participates_in_history_budget,
    ]
    for test in tests:
        test()
    print(f"OK: {len(tests)} compact controller tests passed")


if __name__ == "__main__":
    main()
