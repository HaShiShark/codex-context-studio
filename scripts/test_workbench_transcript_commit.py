from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.transcript_codec import input_items_to_transcript, transcript_to_input_items  # noqa: E402
from backend.web_context import ContextWorkbenchDraft, normalize_context_records  # noqa: E402


def test_workbench_commit_preserves_global_provider_item_order() -> None:
    input_items = [
        {"type": "message", "role": "assistant", "content": "A"},
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "lookup",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "ok",
        },
        {"type": "message", "role": "user", "content": "U"},
    ]
    core_transcript = input_items_to_transcript(input_items)
    records = normalize_context_records(core_transcript)

    committed = ContextWorkbenchDraft(records, []).committed_transcript()

    assert transcript_to_input_items(committed) == input_items
    assert [
        node_item["inputIndex"]
        for node in committed
        for node_item in node["items"]
    ] == [0, 1, 2, 3]


def main() -> None:
    tests = [
        test_workbench_commit_preserves_global_provider_item_order,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} workbench transcript commit tests passed")


if __name__ == "__main__":
    main()
