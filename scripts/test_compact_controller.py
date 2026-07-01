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
                {"request_kind": "compaction", "trigger": "manual", "turn_id": "turn-manual"}
            )
        }
    }
    auto_body = {
        "client_metadata": {
            "x-codex-turn-metadata": json.dumps(
                {"request_kind": "compaction", "trigger": "auto", "turn_id": "turn-auto"}
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

    assert parse_compact_turn_metadata(normal_body) is None
    assert parse_compact_turn_metadata(invalid_body) is None
    assert parse_compact_turn_metadata(missing_body) is None
    assert is_compact_request(normal_body) is False


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


def test_auto_compact_excludes_last_in_progress_user() -> None:
    transcript = input_items_to_transcript(
        [
            message("user", "stable user"),
            message("assistant", "partial work"),
            message("user", "in progress user"),
        ]
    )

    result = CompactController.on_compact_success(
        transcript,
        [message("assistant", "auto summary")],
        "auto",
    )

    assert result.new_items == [
        message("user", "stable user"),
        typed_message("user", f"{LOCAL_COMPACT_SUMMARY_PREFIX}\n\nauto summary"),
    ]


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


def main() -> None:
    tests = [
        test_metadata_detects_manual_and_auto_compact,
        test_non_compact_metadata_does_not_trigger,
        test_replace_prompt_roundtrips_through_transcript_to_input_items,
        test_compact_success_manual_builds_retained_users_plus_summary,
        test_auto_compact_excludes_last_in_progress_user,
        test_summary_message_is_not_collected_again,
    ]
    for test in tests:
        test()
    print(f"OK: {len(tests)} compact controller tests passed")


if __name__ == "__main__":
    main()
