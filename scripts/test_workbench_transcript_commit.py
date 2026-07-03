from __future__ import annotations

import sys
from pathlib import Path
import json


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.transcript_codec import input_items_to_transcript, transcript_to_input_items  # noqa: E402
from backend.web_constants import SessionState  # noqa: E402
from backend.web_context import (  # noqa: E402
    ContextWorkbenchDraft,
    ContextWorkbenchToolRegistry,
    build_context_workspace_snapshot,
    normalize_context_records,
)


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


def test_locked_nodes_are_hidden_from_snapshot_but_preserved_on_commit() -> None:
    core_transcript = input_items_to_transcript(
        [
            {"type": "message", "role": "developer", "content": "developer instructions"},
            {"type": "message", "role": "user", "content": "locked user text"},
            {"type": "message", "role": "assistant", "content": "assistant answer"},
        ]
    )
    user_id = str(core_transcript[1]["id"])
    session = SessionState(
        session_id="session-lock-test",
        title="Lock Test",
        transcript=core_transcript,
        context_workbench_history=[],
        node_locks={user_id: True},
    )

    snapshot = build_context_workspace_snapshot(session)
    assert "developer instructions" not in snapshot
    assert "locked user text" not in snapshot
    assert "Node #1 | assistant" in snapshot

    draft = ContextWorkbenchDraft(core_transcript, [], {user_id: True})
    result = draft.apply_write_nodes([1], [])
    assert result["deleted"] == [1]

    committed_items = transcript_to_input_items(draft.committed_transcript())
    assert committed_items == [
        {"type": "message", "role": "developer", "content": "developer instructions"},
        {"type": "message", "role": "user", "content": "locked user text"},
    ]


def test_unlocked_developer_is_visible_and_tool_accessible() -> None:
    core_transcript = input_items_to_transcript(
        [
            {"type": "message", "role": "developer", "content": "developer instructions"},
            {"type": "message", "role": "user", "content": "user text"},
        ]
    )
    developer_id = str(core_transcript[0]["id"])
    session = SessionState(
        session_id="session-dev-unlocked",
        title="Developer Unlocked",
        transcript=core_transcript,
        context_workbench_history=[],
        node_locks={developer_id: False},
    )

    snapshot = build_context_workspace_snapshot(session)
    assert "Node #1 | developer" in snapshot
    assert "developer instructions" in snapshot

    draft = ContextWorkbenchDraft(core_transcript, [], {developer_id: False})
    registry = ContextWorkbenchToolRegistry(draft, session_title=session.title)
    execution = registry.execute("get_nodes", {"node_numbers": [1]})
    payload = json.loads(execution.output_text)

    assert execution.status != "error"
    assert payload["nodes"][0]["role"] == "developer"
    assert "developer instructions" in json.dumps(payload, ensure_ascii=False)


def main() -> None:
    tests = [
        test_workbench_commit_preserves_global_provider_item_order,
        test_locked_nodes_are_hidden_from_snapshot_but_preserved_on_commit,
        test_unlocked_developer_is_visible_and_tool_accessible,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} workbench transcript commit tests passed")


if __name__ == "__main__":
    main()
