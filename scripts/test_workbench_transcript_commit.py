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
    normalize_context_chat_history,
    normalize_context_records,
)
from agent_runtime.adapters import ProviderRequestContext  # noqa: E402
import backend.web_runtime as web_runtime  # noqa: E402
from backend.web_runtime import prepare_context_chat_history_for_model  # noqa: E402


class _FakeContextResponse:
    def __init__(self, *, output_text: str = "", function_calls=None, finish_reason=None) -> None:
        self.output_text = output_text
        self.function_calls = function_calls or []
        self.finish_reason = finish_reason


def _context_settings():
    from simple_agent.config import (  # noqa: WPS433
        CODEX_PROXY_PROVIDER_ID,
        DEFAULT_CODEX_PROXY_MODELS,
        DEFAULT_RESPONSE_PROVIDERS,
        Settings,
    )

    providers = [dict(provider) for provider in DEFAULT_RESPONSE_PROVIDERS]
    for provider in providers:
        if provider.get("id") == CODEX_PROXY_PROVIDER_ID:
            provider["models"] = [dict(model) for model in DEFAULT_CODEX_PROXY_MODELS]

    return Settings(
        model="gpt-5.4-mini",
        default_reasoning_effort="default",
        context_workbench_model="gpt-5.5",
        context_workbench_provider_id=CODEX_PROXY_PROVIDER_ID,
        project_root=ROOT,
        max_tool_rounds=4,
        tool_settings=[],
        response_providers=providers,
        active_provider_id="openai",
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


def test_context_workbench_draft_reports_changes_after_write() -> None:
    core_transcript = input_items_to_transcript(
        [
            {"type": "message", "role": "user", "content": "old context"},
        ]
    )
    draft = ContextWorkbenchDraft(core_transcript, [])

    assert not draft.has_changes

    draft.apply_write_nodes(
        [1],
        [{"after": 0, "role": "user", "content": "compressed context"}],
    )

    assert draft.has_changes


def test_context_chat_turn_returns_fallback_after_changed_draft_empty_final_response() -> None:
    core_transcript = input_items_to_transcript(
        [
            {"type": "message", "role": "user", "content": "old context"},
        ]
    )
    session = SessionState(
        session_id="session-empty-final-after-write",
        title="Empty Final After Write",
        transcript=core_transcript,
        context_workbench_history=[],
    )
    responses = [
        _FakeContextResponse(
            function_calls=[
                web_runtime.BridgedFunctionCall(
                    name="write_nodes",
                    call_id="call-write",
                    arguments=json.dumps(
                        {
                            "delete": [1],
                            "inserts": [
                                {
                                    "after": 0,
                                    "role": "user",
                                    "content": "compressed context",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                )
            ],
        ),
        _FakeContextResponse(output_text=""),
    ]
    original_stream = web_runtime.stream_context_codex_proxy_response_with_retry

    def fake_stream(*args, **kwargs):
        if not responses:
            raise AssertionError("unexpected extra context model call")
        return responses.pop(0)

    web_runtime.stream_context_codex_proxy_response_with_retry = fake_stream
    try:
        answer, used_model, draft, tool_events = web_runtime.run_context_chat_turn(
            _context_settings(),
            session,
            message="compress",
        )
    finally:
        web_runtime.stream_context_codex_proxy_response_with_retry = original_stream

    assert used_model == "gpt-5.5"
    assert answer == "Context edit applied: Delete #1, Insert 1 node(s)"
    assert draft.has_changes
    assert tool_events[0].name == "write_nodes"
    assert transcript_to_input_items(draft.committed_transcript()) == [
        {"type": "message", "role": "user", "content": "compressed context"},
    ]


def test_context_review_uses_private_rationale_field_and_one_model_round() -> None:
    core_transcript = input_items_to_transcript(
        [
            {"type": "message", "role": "user", "content": "long completed frontend analysis"},
            {"type": "message", "role": "user", "content": "current backend work"},
        ]
    )
    session = SessionState(
        session_id="session-auto-review",
        title="Automatic Review",
        transcript=core_transcript,
        context_workbench_history=[{"role": "user", "content": "manual history must not leak"}],
    )
    manual_registry = ContextWorkbenchToolRegistry(ContextWorkbenchDraft(core_transcript, []))
    review_registry = ContextWorkbenchToolRegistry(
        ContextWorkbenchDraft(core_transcript, []),
        review_mode=True,
    )
    manual_write_schema = next(schema for schema in manual_registry.schemas if schema["name"] == "write_nodes")
    review_write_schema = review_registry.schemas[0]
    assert "review_rationale" not in manual_write_schema["parameters"]["properties"]
    assert review_write_schema["parameters"]["required"] == ["review_rationale"]
    rationale_description = review_write_schema["parameters"]["properties"]["review_rationale"]["description"]
    assert "Never mention node numbers" in rationale_description

    calls = 0
    seen_request: dict[str, object] = {}
    original_stream = web_runtime.stream_context_codex_proxy_response_with_retry

    def fake_stream(request, **_kwargs):
        nonlocal calls, seen_request
        calls += 1
        seen_request = request
        return _FakeContextResponse(
            function_calls=[
                web_runtime.BridgedFunctionCall(
                    name="write_nodes",
                    call_id="call-auto-write",
                    arguments=json.dumps(
                        {
                            "delete": [1],
                            "inserts": [
                                {
                                    "after": 0,
                                    "role": "user",
                                    "content": "Detailed retained frontend decisions and constraints.",
                                }
                            ],
                            "review_rationale": "建议合并已经结束的前端探索；当前后端工作及其约束将保持不变，因此预计不会影响当前任务。",
                        },
                        ensure_ascii=False,
                    ),
                )
            ],
        )

    web_runtime.stream_context_codex_proxy_response_with_retry = fake_stream
    try:
        answer, _used_model, draft, _tool_events = web_runtime.run_context_chat_turn(
            _context_settings(),
            session,
            message="ignored for automatic review",
            context_review=True,
        )
    finally:
        web_runtime.stream_context_codex_proxy_response_with_retry = original_stream

    assert calls == 1
    assert "manual history must not leak" not in json.dumps(seen_request, ensure_ascii=False)
    assert "long completed frontend analysis" in json.dumps(seen_request, ensure_ascii=False)
    assert answer == "建议合并已经结束的前端探索；当前后端工作及其约束将保持不变，因此预计不会影响当前任务。"
    assert draft.has_changes


def test_context_review_replaces_image_payloads_without_mutating_transcript() -> None:
    image_data_url = "data:image/png;base64," + ("large-image-payload" * 100)
    core_transcript = input_items_to_transcript(
        [
            {
                "type": "custom_tool_call",
                "call_id": "call-image",
                "name": "exec",
                "input": "render the current UI",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call-image",
                "output": [
                    {"type": "input_text", "text": "Rendered UI screenshot."},
                    {"type": "input_image", "image_url": image_data_url},
                ],
            },
            {"type": "message", "role": "user", "content": "Keep the layout decision."},
        ]
    )
    session = SessionState(
        session_id="session-auto-review-image",
        title="Automatic Review With Image",
        transcript=core_transcript,
        context_workbench_history=[],
    )

    _instructions, _model, _draft, _registry, context_input = (
        web_runtime.build_context_review_proposal_runtime(_context_settings(), session)
    )
    serialized_review_input = str(context_input[0]["content"])
    serialized_live_transcript = json.dumps(session.transcript, ensure_ascii=False)

    assert image_data_url not in serialized_review_input
    assert "data:image" not in serialized_review_input
    assert '"image_present": true' in serialized_review_input
    assert "visual content is intentionally omitted" in serialized_review_input
    assert "Rendered UI screenshot." in serialized_review_input
    assert "Keep the layout decision." in serialized_review_input
    assert '"providerItems"' in serialized_review_input
    assert '"blocks"' not in serialized_review_input
    assert '"toolEvents"' not in serialized_review_input
    assert image_data_url in serialized_live_transcript


def test_analyze_now_uses_proposal_runtime_instead_of_manual_chat_runtime() -> None:
    core_transcript = input_items_to_transcript(
        [
            {"type": "message", "role": "user", "content": "completed exploration"},
            {"type": "message", "role": "user", "content": "current implementation"},
        ]
    )
    session = SessionState(
        session_id="session-analyze-now",
        title="Analyze Now",
        transcript=core_transcript,
        context_workbench_history=[{"role": "user", "content": "manual chat history"}],
    )
    seen_kwargs: dict[str, object] = {}
    original_turn = web_runtime.run_context_chat_turn

    def fake_turn(_settings, target_session, **kwargs):
        seen_kwargs.update(kwargs)
        draft = ContextWorkbenchDraft(target_session.transcript, [])
        draft.apply_write_nodes(
            [1],
            [{"after": 0, "role": "user", "content": "Retained completed exploration decisions."}],
        )
        return (
            "建议整理已经结束的探索；当前实现目标与约束将保持不变。",
            "gpt-5.5",
            draft,
            [],
        )

    web_runtime.run_context_chat_turn = fake_turn
    try:
        review = web_runtime.run_context_review_generation(
            _context_settings(),
            session,
            base_transcript_version=2,
            base_context_review_cancel_revision=1,
            source="manual",
        )
    finally:
        web_runtime.run_context_chat_turn = original_turn

    assert seen_kwargs["context_review"] is True
    assert seen_kwargs["message"] == ""
    assert review is not None
    assert review["summary"].startswith("建议整理")


def test_context_chat_turn_uses_selected_non_codex_provider_adapter_path() -> None:
    from simple_agent.config import CODEX_PROXY_PROVIDER_ID  # noqa: WPS433

    core_transcript = input_items_to_transcript(
        [
            {"type": "message", "role": "user", "content": "old context"},
        ]
    )
    session = SessionState(
        session_id="session-custom-context-provider",
        title="Custom Context Provider",
        transcript=core_transcript,
        context_workbench_history=[],
    )
    settings = _context_settings()
    settings.context_workbench_provider_id = "openai-chat"
    settings.context_workbench_model = "gpt-4.1-mini"

    responses = [
        _FakeContextResponse(
            function_calls=[
                web_runtime.BridgedFunctionCall(
                    name="write_nodes",
                    call_id="call-write",
                    arguments=json.dumps(
                        {
                            "delete": [1],
                            "inserts": [
                                {
                                    "after": 0,
                                    "role": "user",
                                    "content": "custom provider context",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                )
            ],
        ),
        _FakeContextResponse(output_text="Done"),
    ]
    seen_provider_ids: list[str] = []
    original_build = web_runtime.build_context_provider_request
    original_stream = web_runtime.stream_context_adapter_response_with_retry

    def fake_build(settings_arg, provider, **kwargs):
        seen_provider_ids.append(provider["id"])
        return object(), {"model": kwargs["request_model"]}, ProviderRequestContext(model=kwargs["request_model"])

    def fake_stream(*args, **kwargs):
        if not responses:
            raise AssertionError("unexpected extra custom provider call")
        return responses.pop(0)

    web_runtime.build_context_provider_request = fake_build
    web_runtime.stream_context_adapter_response_with_retry = fake_stream
    try:
        answer, used_model, draft, tool_events = web_runtime.run_context_chat_turn(
            settings,
            session,
            message="compress",
        )
    finally:
        web_runtime.build_context_provider_request = original_build
        web_runtime.stream_context_adapter_response_with_retry = original_stream

    assert CODEX_PROXY_PROVIDER_ID not in seen_provider_ids
    assert seen_provider_ids == ["openai-chat", "openai-chat"]
    assert used_model == "gpt-4.1-mini"
    assert answer == "Done"
    assert draft.has_changes
    assert tool_events[0].name == "write_nodes"
    assert transcript_to_input_items(draft.committed_transcript()) == [
        {"type": "message", "role": "user", "content": "custom provider context"},
    ]


def test_context_chat_response_payload_commits_changed_draft() -> None:
    core_transcript = input_items_to_transcript(
        [
            {"type": "message", "role": "user", "content": "old context"},
        ]
    )
    session = SessionState(
        session_id="session-payload-commit",
        title="Payload Commit",
        transcript=core_transcript,
        context_workbench_history=[],
    )
    draft = ContextWorkbenchDraft(core_transcript, [])
    draft.apply_write_nodes(
        [1],
        [{"after": 0, "role": "user", "content": "compressed context"}],
    )

    class FakeAppState:
        def apply_context_workbench_mutation(self, target_session, *, transcript):
            target_session.transcript = transcript
            return transcript

        def append_context_workbench_turn(self, target_session, *, user_message, answer, tool_events=None):
            return [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": answer, "toolEvents": tool_events or []},
            ]

    original_sync = web_runtime.safe_sync_proxy_session_transcript_if_known
    web_runtime.safe_sync_proxy_session_transcript_if_known = lambda *args, **kwargs: {
        "status": "skipped",
        "reason": "test",
    }
    try:
        payload = web_runtime.build_context_chat_response_payload(
            FakeAppState(),
            session,
            user_message="compress",
            answer="Context edit applied.",
            used_model="gpt-5.5",
            draft=draft,
            tool_events=[],
        )
    finally:
        web_runtime.safe_sync_proxy_session_transcript_if_known = original_sync

    assert transcript_to_input_items(payload["conversation"]) == [
        {"type": "message", "role": "user", "content": "compressed context"},
    ]
    assert payload["history"][-1]["content"] == "Context edit applied."


def test_context_workbench_history_keeps_tool_blocks_but_model_history_stays_light() -> None:
    raw_history = [
        {"role": "user", "content": "整理上下文"},
        {
            "role": "assistant",
            "content": "已经整理完成",
            "toolEvents": [
                {
                    "name": "write_nodes",
                    "arguments": {"node_numbers": [1]},
                    "output_preview": "updated",
                    "raw_output": "{\"ok\": true}",
                    "display_title": "write_nodes",
                    "display_detail": "node #1",
                    "display_result": "Updated node #1",
                    "status": "completed",
                }
            ],
            "blocks": [
                {
                    "kind": "tool",
                    "tool_event": {
                        "name": "write_nodes",
                        "arguments": {"node_numbers": [1]},
                        "output_preview": "updated",
                        "status": "completed",
                    },
                },
                {"kind": "text", "text": "已经整理完成"},
            ],
        },
    ]

    history = normalize_context_chat_history(raw_history)
    assert history[1]["toolEvents"][0]["name"] == "write_nodes"
    assert history[1]["blocks"][0]["kind"] == "tool"

    model_history = prepare_context_chat_history_for_model(history)
    assert model_history == [
        {"role": "user", "content": "整理上下文"},
        {"role": "assistant", "content": "已经整理完成"},
    ]


def main() -> None:
    tests = [
        test_workbench_commit_preserves_global_provider_item_order,
        test_locked_nodes_are_hidden_from_snapshot_but_preserved_on_commit,
        test_unlocked_developer_is_visible_and_tool_accessible,
        test_context_workbench_draft_reports_changes_after_write,
        test_context_chat_turn_returns_fallback_after_changed_draft_empty_final_response,
        test_context_review_uses_private_rationale_field_and_one_model_round,
        test_context_review_replaces_image_payloads_without_mutating_transcript,
        test_analyze_now_uses_proposal_runtime_instead_of_manual_chat_runtime,
        test_context_chat_turn_uses_selected_non_codex_provider_adapter_path,
        test_context_chat_response_payload_commits_changed_draft,
        test_context_workbench_history_keeps_tool_blocks_but_model_history_stays_light,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} workbench transcript commit tests passed")


if __name__ == "__main__":
    main()
