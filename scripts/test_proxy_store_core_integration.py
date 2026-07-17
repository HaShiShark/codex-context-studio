from __future__ import annotations

import copy
import json
import sys
import tempfile
from http import HTTPStatus
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend import proxy_fastapi  # noqa: E402
from backend.codex_request_protocol import apply_base_instructions_override  # noqa: E402
from backend.compact_controller import (  # noqa: E402
    LOCAL_COMPACT_PROMPT_PREFIX,
    LOCAL_COMPACT_SUMMARY_PREFIX,
    MANUAL_LOCAL_COMPACT_PROMPT,
)
from backend.proxy_core import ProxyState  # noqa: E402
from backend.proxy_routes_support import CONTEXT_CONTROL_NOTICE_TEXT  # noqa: E402
from backend.proxy_store import ProxySession, ProxyStore, read_message_text  # noqa: E402
from backend.transcript_codec import input_items_to_transcript, transcript_to_input_items  # noqa: E402


SESSION_ID = "sess-proxy-core"


def message(role: str, text: str, *, item_id: str | None = None) -> dict[str, Any]:
    item = {
        "type": "message",
        "role": role,
        "content": text,
    }
    if item_id:
        item["id"] = item_id
    return item


def typed_message(role: str, text: str) -> dict[str, Any]:
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


def new_store(temp_dir: str) -> ProxyStore:
    return ProxyStore(Path(temp_dir) / "proxy_state.json")


def proxy_items(session: ProxySession) -> list[Any]:
    return transcript_to_input_items(session.proxy_state.transcript)


def review_payload(
    review_id: str,
    base_version: int,
    items: list[Any],
    *,
    cancel_revision: int | None = None,
) -> dict[str, Any]:
    payload = {
        "id": review_id,
        "session_id": SESSION_ID,
        "status": "pending",
        "source": "test",
        "base_transcript_version": base_version,
        "created_at": "2026-07-08T00:00:00+00:00",
        "summary": "test review",
        "before": {"node_count": 2, "token_count": 20},
        "after": {"node_count": len(items), "token_count": 10},
        "proposed_transcript": input_items_to_transcript(items),
    }
    if cancel_revision is not None:
        payload["base_context_review_cancel_revision"] = cancel_revision
    return payload


def message_text(item: Any) -> str:
    assert isinstance(item, dict)
    return read_message_text(item)


def assert_no_legacy_payload_fields(payload: dict[str, Any]) -> None:
    forbidden = {
        "has_override",
        "active_context_source",
        "active_transcript",
        "raw_transcript",
        "edited_transcript",
        "pending_transcript",
        "override_base_transcript",
    }
    leaked = sorted(forbidden.intersection(payload))
    assert not leaked, f"legacy payload fields leaked: {leaked}"


def test_begin_request_and_complete_response_use_proxy_state() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        body = {
            "previous_response_id": "resp_prev_should_survive",
            "input": [
                message("developer", "be concise"),
                message("user", "hello"),
            ],
        }

        session, forwarded = store.begin_request(
            SESSION_ID,
            copy.deepcopy(body),
            {"x-codex-session-id": SESSION_ID},
        )

        assert forwarded["input"] == body["input"]
        assert forwarded["previous_response_id"] == "resp_prev_should_survive"
        assert session.transcript == session.proxy_state.transcript
        assert proxy_items(session) == body["input"]
        assert session.proxy_state.codex_input_cursor == body["input"]
        assert session.status == "running"
        assert session.request_log[-1]["kind"] == "proxy_core_request"

        assistant = message("assistant", "hi")
        store.complete_response(SESSION_ID, [assistant], "hi")

        session = store.sessions[SESSION_ID]
        assert proxy_items(session) == [*body["input"], assistant]
        assert session.proxy_state.codex_input_cursor == [*body["input"], assistant]
        assert session.transcript == session.proxy_state.transcript
        assert session.status == "mirror"
        assert_no_legacy_payload_fields(store.get_session(SESSION_ID) or {})


def test_lite_prompt_override_enters_transcript_and_cursor_without_changing_raw_log() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        raw_body = {
            "model": "gpt-5.6-sol",
            "input": [
                {"type": "additional_tools", "role": "developer", "tools": []},
                typed_message("developer", "official prompt"),
                typed_message("developer", "permissions and skills"),
                typed_message("user", "hello"),
            ],
            "tool_choice": "auto",
        }
        effective_body = apply_base_instructions_override(raw_body, "custom prompt")

        session, forwarded = store.begin_request(
            SESSION_ID,
            raw_body,
            {"x-codex-session-id": SESSION_ID},
            effective_body=effective_body,
        )

        assert "instructions" not in forwarded
        assert "tools" not in forwarded
        assert forwarded["tool_choice"] == "auto"
        assert forwarded["input"][1]["content"][0]["text"] == "custom prompt"
        assert forwarded["input"][2] == raw_body["input"][2]
        assert proxy_items(session) == forwarded["input"]
        assert session.proxy_state.codex_input_cursor == forwarded["input"]
        assert session.request_log[-1]["body"] == raw_body


def test_legacy_override_status_no_longer_drives_main_request_path() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        stale_input = [message("user", "old mirrored text")]
        store.sessions[SESSION_ID] = ProxySession(
            id=SESSION_ID,
            title="Legacy Override Session",
            proxy_state=ProxyState(
                transcript=input_items_to_transcript(stale_input),
                codex_input_cursor=copy.deepcopy(stale_input),
            ),
            status="override",
        )
        body = {
            "previous_response_id": "keep_me",
            "input": [message("user", "fresh codex input")],
        }

        session, forwarded = store.begin_request(
            SESSION_ID,
            copy.deepcopy(body),
            {"x-codex-session-id": SESSION_ID},
        )

        assert forwarded["input"] == body["input"]
        assert forwarded["previous_response_id"] == "keep_me"
        assert message_text(forwarded["input"][0]) == "fresh codex input"
        assert "old mirrored text" not in str(forwarded["input"])
        assert proxy_items(session) == body["input"]
        assert session.status == "running"
        assert session.request_log[-1]["kind"] == "proxy_core_request"
        assert_no_legacy_payload_fields(session.to_payload())


def test_replace_transcript_updates_proxy_state_without_legacy_payload_fields() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        original_input = [message("user", "original")]
        store.begin_request(
            SESSION_ID,
            {"input": copy.deepcopy(original_input)},
            {"x-codex-session-id": SESSION_ID},
        )
        replacement_input = [
            message("developer", "new instructions"),
            message("user", "replacement"),
        ]

        payload = store.replace_transcript(
            SESSION_ID,
            input_items_to_transcript(replacement_input),
        )

        session = store.sessions[SESSION_ID]
        assert payload["changed"] is True
        assert_no_legacy_payload_fields(payload)
        assert session.status == "mirror"
        assert proxy_items(session) == replacement_input
        assert session.proxy_state.codex_input_cursor == original_input

        next_input = [*original_input, message("user", "next turn")]
        _session, forwarded = store.begin_request(
            SESSION_ID,
            {"input": copy.deepcopy(next_input)},
            {"x-codex-session-id": SESSION_ID},
        )

        assert forwarded["input"] == [*replacement_input, message("user", "next turn")]
        assert proxy_items(session) == [*replacement_input, message("user", "next turn")]
        assert session.proxy_state.codex_input_cursor == next_input


def test_workbench_edit_keeps_cursor_and_tail_conflict_preserves_edit() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        user = message("user", "original user")
        assistant = message("assistant", "original assistant")
        store.begin_request(
            SESSION_ID,
            {"input": [copy.deepcopy(user), copy.deepcopy(assistant)]},
            {"x-codex-session-id": SESSION_ID},
        )

        edited_assistant = message("assistant", "edited assistant")
        store.replace_transcript(
            SESSION_ID,
            input_items_to_transcript([copy.deepcopy(user), copy.deepcopy(edited_assistant)]),
        )
        session = store.sessions[SESSION_ID]
        assert session.proxy_state.codex_input_cursor == [user, assistant]

        next_user = message("user", "next user")
        _session, forwarded = store.begin_request(
            SESSION_ID,
            {"input": [copy.deepcopy(user), copy.deepcopy(next_user)]},
            {"x-codex-session-id": SESSION_ID},
        )

        assert store.sessions[SESSION_ID].proxy_state.tail_conflict is True
        assert forwarded["input"] == [user, edited_assistant, next_user]
        assert store.sessions[SESSION_ID].proxy_state.codex_input_cursor == [user, next_user]


def test_persistence_writes_proxy_session_folder_layout() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        store.begin_request(
            SESSION_ID,
            {"input": [message("user", "persist me")]},
            {"x-codex-session-id": SESSION_ID},
        )
        session = store.sessions[SESSION_ID]
        session_dir = store._session_dir(session)

        index = json.loads((Path(temp_dir) / "index.json").read_text(encoding="utf-8"))
        session_json = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
        transcript_json = json.loads((session_dir / "transcript.json").read_text(encoding="utf-8"))
        cursor_json = json.loads((session_dir / "cursor.json").read_text(encoding="utf-8"))

        assert index["active_session_id"] == SESSION_ID
        assert session_json["id"] == SESSION_ID
        assert transcript_json["nodes"] == session.proxy_state.transcript
        assert cursor_json["items"] == session.proxy_state.codex_input_cursor
        forbidden = [
            session_dir / "storage.json",
            session_dir / "branches" / "transcript.jsonl",
            session_dir / "branches" / "edited.jsonl",
            session_dir / "branches" / "override_base.jsonl",
            session_dir / "pending" / "active.jsonl",
            session_dir / "transcript_tail.json",
            session_dir / "restore.json",
            session_dir / "revisions.jsonl",
        ]
        assert not [path for path in forbidden if path.exists()]


def test_transcript_replace_route_updates_proxy_state() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        original_store = proxy_fastapi.STORE
        try:
            proxy_fastapi.STORE = new_store(temp_dir)
            replacement_input = [
                message("developer", "new instructions"),
                message("user", "replacement"),
            ]
            with TestClient(proxy_fastapi.app) as client:
                response = client.post(
                    f"/api/proxy/sessions/{SESSION_ID}/transcript",
                    content=json.dumps({"transcript": input_items_to_transcript(replacement_input)}),
                    headers={"content-type": "application/json"},
                )

            assert response.status_code == HTTPStatus.OK
            payload = response.json()
            assert payload["changed"] is True
            assert_no_legacy_payload_fields(payload)
            session = proxy_fastapi.STORE.sessions[SESSION_ID]
            assert proxy_items(session) == replacement_input
            assert session.proxy_state.codex_input_cursor == []
        finally:
            proxy_fastapi.STORE = original_store


def test_session_reset_route_is_removed() -> None:
    with TestClient(proxy_fastapi.app) as client:
        response = client.post(f"/api/proxy/sessions/{SESSION_ID}/reset")

    assert response.status_code in {HTTPStatus.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED}


def test_transcript_replace_does_not_accept_legacy_record_payload() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        legacy_record_payload = [
            {
                "role": "user",
                "text": "legacy record should not migrate",
                "providerItems": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "legacy record should not migrate",
                    }
                ],
            }
        ]

        try:
            store.replace_transcript(SESSION_ID, legacy_record_payload)
        except ValueError as exc:
            assert "proxy core transcript nodes" in str(exc)
        else:
            raise AssertionError("legacy record payload was accepted")


def test_restart_restores_transcript_and_cursor_for_next_diff() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        user = message("user", "hello")
        assistant = message("assistant", "hi")
        store.begin_request(
            SESSION_ID,
            {"input": [copy.deepcopy(user)]},
            {"x-codex-session-id": SESSION_ID},
        )
        store.complete_response(SESSION_ID, [copy.deepcopy(assistant)], "hi")

        reloaded = new_store(temp_dir)
        assert SESSION_ID in reloaded.sessions
        session = reloaded.sessions[SESSION_ID]
        assert proxy_items(session) == [user, assistant]
        assert session.proxy_state.codex_input_cursor == [user, assistant]

        next_user = message("user", "continue")
        _session, forwarded = reloaded.begin_request(
            SESSION_ID,
            {"input": [copy.deepcopy(user), copy.deepcopy(assistant), copy.deepcopy(next_user)]},
            {"x-codex-session-id": SESSION_ID},
        )

        assert forwarded["input"] == [user, assistant, next_user]
        assert proxy_items(reloaded.sessions[SESSION_ID]) == [user, assistant, next_user]
        assert reloaded.sessions[SESSION_ID].proxy_state.codex_input_cursor == [user, assistant, next_user]


def test_control_intercept_fallback_keeps_control_turn_in_context() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        first_user = message("user", "real request")
        store.begin_request(
            SESSION_ID,
            {"input": [copy.deepcopy(first_user)]},
            {"x-codex-session-id": SESSION_ID},
        )

        control_user = message("user", "ctx")
        control_input = [copy.deepcopy(first_user), copy.deepcopy(control_user)]
        session = store.record_control_intercept(
            SESSION_ID,
            {"input": copy.deepcopy(control_input)},
            {"x-codex-session-id": SESSION_ID},
            "ctx",
        )

        assert proxy_items(session) == control_input
        assert session.proxy_state.codex_input_cursor == control_input
        assert session.request_log[-1]["kind"] == "context_control_intercept"

        notice = message("assistant", CONTEXT_CONTROL_NOTICE_TEXT)
        next_user = message("user", "continue after opening context")
        next_input = [*copy.deepcopy(control_input), copy.deepcopy(notice), copy.deepcopy(next_user)]

        session, forwarded = store.begin_request(
            SESSION_ID,
            {"input": copy.deepcopy(next_input)},
            {"x-codex-session-id": SESSION_ID},
        )

        assert forwarded["input"] == next_input
        assert proxy_items(session) == next_input
        assert session.proxy_state.codex_input_cursor == next_input


def test_compact_request_is_handled_by_proxy_core_state() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        compact_prompt = f"{LOCAL_COMPACT_PROMPT_PREFIX}\nSummarize."
        body = {
            "previous_response_id": "resp_compact_should_survive",
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction","compaction":{"trigger":"manual","phase":"pre_turn"}}',
            },
            "input": [
                message("developer", "developer context"),
                message("user", "keep this request"),
                message("assistant", "previous answer"),
                message("user", compact_prompt),
            ],
        }

        session, forwarded = store.begin_request(
            SESSION_ID,
            copy.deepcopy(body),
            {"x-codex-session-id": SESSION_ID},
        )

        assert session.status == "compacting"
        assert session.proxy_state.compact_pending is True
        assert session.proxy_state.compact_kind == "manual"
        assert forwarded["previous_response_id"] == "resp_compact_should_survive"
        assert message_text(forwarded["input"][-1]) == MANUAL_LOCAL_COMPACT_PROMPT
        assert session.proxy_state.codex_input_cursor == body["input"]
        assert session.request_log[-1]["kind"] == "proxy_core_compact"

        store.complete_response(
            SESSION_ID,
            [message("assistant", "compact summary")],
            "fallback text should not be used",
        )

        session = store.sessions[SESSION_ID]
        expected_summary = f"{LOCAL_COMPACT_SUMMARY_PREFIX}\n\ncompact summary"
        assert proxy_items(session) == [
            message("user", "keep this request"),
            typed_message("user", expected_summary),
        ]
        assert session.proxy_state.compact_pending is False
        assert session.proxy_state.compact_kind == ""
        assert session.status == "mirror"
        assert_no_legacy_payload_fields(session.to_payload())


def test_compact_failure_rolls_back_transcript_and_cursor() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        original_items = [
            message("developer", "developer context"),
            message("user", "keep this request"),
        ]
        store.begin_request(
            SESSION_ID,
            {"input": copy.deepcopy(original_items)},
            {"x-codex-session-id": SESSION_ID},
        )
        before_transcript = copy.deepcopy(store.sessions[SESSION_ID].proxy_state.transcript)
        before_cursor = copy.deepcopy(store.sessions[SESSION_ID].proxy_state.codex_input_cursor)

        compact_prompt = f"{LOCAL_COMPACT_PROMPT_PREFIX}\nSummarize."
        store.begin_request(
            SESSION_ID,
            {
                "client_metadata": {
                    "x-codex-turn-metadata": '{"request_kind":"compaction","compaction":{"trigger":"manual","phase":"pre_turn"}}',
                },
                "input": [*copy.deepcopy(original_items), message("user", compact_prompt)],
            },
            {"x-codex-session-id": SESSION_ID},
        )

        store.fail_response(SESSION_ID, "upstream failed")
        session = store.sessions[SESSION_ID]
        assert session.proxy_state.transcript == before_transcript
        assert session.proxy_state.codex_input_cursor == before_cursor
        assert session.proxy_state.compact_pending is False
        assert session.proxy_state.compact_kind == ""
        assert session.last_error == "upstream failed"


def test_prune_sessions_missing_from_codex_deletes_only_missing_proxy_sessions() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        keep_id = "019f1c86-0574-73a0-8f5e-b40cb6184de9"
        delete_id = "019f1c7c-a3ed-7b52-9258-e9f577da3d55"
        fallback_id = "session-fallback"
        for session_id in (keep_id, delete_id, fallback_id):
            store.begin_request(
                session_id,
                {"input": [message("user", session_id)]},
                {"x-codex-session-id": session_id},
            )

        delete_dir = store.storage.session_dir(delete_id)
        fallback_dir = store.storage.session_dir(fallback_id)
        assert delete_dir.exists()
        assert fallback_dir.exists()

        result = store.prune_sessions_missing_from_codex({keep_id})

        assert result["status"] == "ok"
        assert result["deleted_session_ids"] == [delete_id]
        assert keep_id in store.sessions
        assert fallback_id in store.sessions
        assert delete_id not in store.sessions
        assert not delete_dir.exists()
        assert fallback_dir.exists()

        index = json.loads((Path(temp_dir) / "index.json").read_text(encoding="utf-8"))
        indexed_ids = {item["id"] for item in index["sessions"]}
        assert keep_id in indexed_ids
        assert fallback_id in indexed_ids
        assert delete_id not in indexed_ids


def test_node_locks_persist_and_cleanup_with_transcript() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        initial_input = [
            message("developer", "developer instructions"),
            message("user", "lock me"),
        ]
        store.begin_request(
            SESSION_ID,
            {"input": copy.deepcopy(initial_input)},
            {"x-codex-session-id": SESSION_ID},
        )
        store.complete_response(SESSION_ID, [message("assistant", "done")], "done")

        session = store.sessions[SESSION_ID]
        user_node_id = str(session.proxy_state.transcript[1]["id"])
        payload = store.set_node_lock(
            SESSION_ID,
            user_node_id,
            True,
            expected_revision=0,
        )

        assert payload["node_locks"] == {user_node_id: True}
        assert payload["node_lock_revision"] == 1

        reloaded = new_store(temp_dir)
        reloaded_session = reloaded.sessions[SESSION_ID]
        assert reloaded_session.node_locks == {user_node_id: True}
        assert reloaded_session.node_lock_revision == 1

        replacement = input_items_to_transcript([message("developer", "developer instructions")])
        cleanup_payload = reloaded.replace_transcript(SESSION_ID, replacement)
        assert cleanup_payload["node_locks"] == {}
        assert reloaded.sessions[SESSION_ID].node_locks == {}


def test_node_locks_store_only_default_overrides() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        initial_input = [
            message("developer", "developer instructions"),
            message("user", "normal user text"),
        ]
        store.begin_request(
            SESSION_ID,
            {"input": copy.deepcopy(initial_input)},
            {"x-codex-session-id": SESSION_ID},
        )

        session = store.sessions[SESSION_ID]
        developer_node_id = str(session.proxy_state.transcript[0]["id"])
        user_node_id = str(session.proxy_state.transcript[1]["id"])

        payload = store.set_node_lock(SESSION_ID, developer_node_id, True, expected_revision=0)
        assert payload["node_locks"] == {}
        assert payload["node_lock_revision"] == 0

        payload = store.set_node_lock(SESSION_ID, developer_node_id, False, expected_revision=0)
        assert payload["node_locks"] == {developer_node_id: False}
        assert payload["node_lock_revision"] == 1

        payload = store.set_node_lock(SESSION_ID, developer_node_id, True, expected_revision=1)
        assert payload["node_locks"] == {}
        assert payload["node_lock_revision"] == 2

        payload = store.set_node_lock(SESSION_ID, user_node_id, False, expected_revision=2)
        assert payload["node_locks"] == {}
        assert payload["node_lock_revision"] == 2

        payload = store.set_node_lock(SESSION_ID, user_node_id, True, expected_revision=2)
        assert payload["node_locks"] == {user_node_id: True}
        assert payload["node_lock_revision"] == 3


def test_node_lock_allows_main_running_but_rejects_context_runs() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        store.begin_request(
            SESSION_ID,
            {"input": [message("user", "hello")]},
            {"x-codex-session-id": SESSION_ID},
        )
        user_node_id = str(store.sessions[SESSION_ID].proxy_state.transcript[0]["id"])

        payload = store.set_node_lock(SESSION_ID, user_node_id, True, expected_revision=0)
        assert payload["node_locks"] == {user_node_id: True}
        assert payload["node_lock_revision"] == 1

        store.complete_response(SESSION_ID, [message("assistant", "done")], "done")
        store.set_context_run_state(SESSION_ID, "ctx-1", True)
        assert store.wait_context_idle(SESSION_ID, timeout_seconds=0.01) is False

        try:
            store.set_node_lock(SESSION_ID, user_node_id, True)
        except RuntimeError as exc:
            assert str(exc) == "context_model_running"
        else:
            raise AssertionError("node lock changed while context model was running")

        store.set_context_run_state(SESSION_ID, "ctx-1", False)
        assert store.wait_context_idle(SESSION_ID, timeout_seconds=0.01) is True
        payload = store.set_node_lock(SESSION_ID, user_node_id, False, expected_revision=1)
        assert payload["node_locks"] == {}
        assert payload["node_lock_revision"] == 2


def test_pending_context_review_metadata_and_apply_replace_transcript() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        original_input = [
            message("developer", "keep instructions"),
            message("user", "long old context"),
        ]
        store.begin_request(
            SESSION_ID,
            {"input": copy.deepcopy(original_input)},
            {"x-codex-session-id": SESSION_ID},
        )
        base_version = store.sessions[SESSION_ID].transcript_version
        proposed_input = [
            message("developer", "keep instructions"),
            message("user", "compressed context"),
        ]

        payload = store.set_pending_context_review(
            SESSION_ID,
            review_payload("review-1", base_version, proposed_input),
        )

        assert payload["pending_context_review"]["id"] == "review-1"
        assert payload["pending_context_review"]["proposed_transcript"]
        listed_review = store.list_sessions()["sessions"][0]["pending_context_review"]
        assert listed_review["id"] == "review-1"
        assert "proposed_transcript" not in listed_review

        applied = store.apply_pending_context_review(SESSION_ID, "review-1")
        assert applied["changed"] is True
        assert applied["pending_context_review"] is None
        assert applied["transcript_version"] == base_version + 1
        assert proxy_items(store.sessions[SESSION_ID]) == proposed_input


def test_pending_context_review_stale_apply_clears_review() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        original_input = [message("user", "original")]
        store.begin_request(
            SESSION_ID,
            {"input": copy.deepcopy(original_input)},
            {"x-codex-session-id": SESSION_ID},
        )
        store.replace_transcript(SESSION_ID, input_items_to_transcript([message("user", "new live text")]))
        base_version = store.sessions[SESSION_ID].transcript_version
        store.set_pending_context_review(
            SESSION_ID,
            review_payload("review-stale", base_version, [message("user", "proposal")]),
        )
        store.sessions[SESSION_ID].transcript_version += 1

        try:
            store.apply_pending_context_review(SESSION_ID, "review-stale")
        except RuntimeError as exc:
            assert str(exc) == "context_review_stale"
        else:
            raise AssertionError("stale context review should not apply")

        assert store.sessions[SESSION_ID].pending_context_review is None
        assert proxy_items(store.sessions[SESSION_ID]) == [message("user", "new live text")]


def test_pending_context_review_survives_restart_until_same_session_requests() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        store.begin_request(
            SESSION_ID,
            {"input": [message("user", "hello")]},
            {"x-codex-session-id": SESSION_ID},
        )
        session = store.sessions[SESSION_ID]
        first_request_at = session.last_proxy_request_at
        store.set_pending_context_review(
            SESSION_ID,
            review_payload(
                "review-persisted",
                session.transcript_version,
                [message("user", "compressed")],
                cancel_revision=session.context_review_cancel_revision,
            ),
        )

        reloaded = new_store(temp_dir)
        restored = reloaded.get_session(SESSION_ID) or {}
        assert restored["pending_context_review"]["id"] == "review-persisted"
        assert restored["last_proxy_request_at"] == first_request_at

        reloaded.begin_request(
            SESSION_ID,
            {"input": [message("user", "hello"), message("user", "continue")]},
            {"x-codex-session-id": SESSION_ID},
        )
        continued = reloaded.get_session(SESSION_ID) or {}
        assert continued["pending_context_review"] is None
        assert continued["context_review_cancel_revision"] > restored["context_review_cancel_revision"]


def test_legacy_review_copy_is_not_restored() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        store.begin_request(
            SESSION_ID,
            {"input": [message("user", "hello")]},
            {"x-codex-session-id": SESSION_ID},
        )
        session = store.sessions[SESSION_ID]
        store.set_pending_context_review(
            SESSION_ID,
            review_payload(
                "review-old-copy",
                session.transcript_version,
                [message("user", "compressed")],
            ),
        )
        metadata_path = store.storage.session_json_path(SESSION_ID)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["pending_context_review"].pop("review_schema_version", None)
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        reloaded = new_store(temp_dir)
        payload = reloaded.get_session(SESSION_ID) or {}
        assert payload["pending_context_review"] is None


def test_late_review_is_rejected_after_main_turn_cancellation() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        store.begin_request(
            SESSION_ID,
            {"input": [message("user", "hello")]},
            {"x-codex-session-id": SESSION_ID},
        )
        session = store.sessions[SESSION_ID]
        review = review_payload(
            "review-late",
            session.transcript_version,
            [message("user", "compressed")],
            cancel_revision=session.context_review_cancel_revision,
        )
        store.set_main_turn_state(SESSION_ID, "turn-next", True)
        store.set_main_turn_state(SESSION_ID, "turn-next", False)

        try:
            store.set_pending_context_review(SESSION_ID, review)
        except RuntimeError as exc:
            assert str(exc) == "context_review_cancelled"
        else:
            raise AssertionError("cancelled automatic review should not be persisted")


def test_pending_context_review_is_cleared_when_main_turn_starts() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        store.begin_request(
            SESSION_ID,
            {"input": [message("user", "hello")]},
            {"x-codex-session-id": SESSION_ID},
        )
        base_version = store.sessions[SESSION_ID].transcript_version
        store.set_pending_context_review(
            SESSION_ID,
            review_payload("review-main-turn", base_version, [message("user", "proposal")]),
        )

        payload = store.set_main_turn_state(SESSION_ID, "turn-1", True)

        assert payload["is_main_turn_running"] is True
        assert payload["pending_context_review"] is None
        assert store.sessions[SESSION_ID].pending_context_review is None


def test_persisted_running_status_is_not_treated_as_live_request() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        store.begin_request(
            SESSION_ID,
            {"input": [message("user", "hello")]},
            {"x-codex-session-id": SESSION_ID},
        )
        live_payload = store.get_session(SESSION_ID) or {}
        assert live_payload["status"] == "running"
        assert live_payload["is_running"] is True

        reloaded = new_store(temp_dir)
        stale_payload = reloaded.get_session(SESSION_ID) or {}
        assert stale_payload["status"] == "mirror"
        assert stale_payload["is_running"] is False


def test_main_turn_state_survives_request_boundaries_but_not_restart() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        start_payload = store.set_main_turn_state(SESSION_ID, "turn-1", True)
        assert start_payload["is_main_turn_running"] is True
        assert start_payload["main_turn_id"] == "turn-1"

        store.begin_request(
            SESSION_ID,
            {"input": [message("user", "hello")]},
            {"x-codex-session-id": SESSION_ID},
        )
        store.complete_response(SESSION_ID, [message("assistant", "done")], "done")
        after_request_payload = store.get_session(SESSION_ID) or {}
        assert after_request_payload["status"] == "mirror"
        assert after_request_payload["is_running"] is False
        assert after_request_payload["is_main_turn_running"] is True

        reloaded = new_store(temp_dir)
        reloaded_payload = reloaded.get_session(SESSION_ID) or {}
        assert reloaded_payload["is_main_turn_running"] is False

        finish_payload = store.set_main_turn_state(SESSION_ID, "turn-1", False)
        assert finish_payload["is_main_turn_running"] is False


def test_main_turn_finish_does_not_create_empty_session() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        try:
            store.set_main_turn_state(SESSION_ID, "turn-1", False)
        except KeyError:
            pass
        else:
            raise AssertionError("finishing a missing main turn should not create a session")

        payload = store.get_session(SESSION_ID)
        assert payload is None


def main() -> None:
    tests = [
        test_begin_request_and_complete_response_use_proxy_state,
        test_lite_prompt_override_enters_transcript_and_cursor_without_changing_raw_log,
        test_legacy_override_status_no_longer_drives_main_request_path,
        test_replace_transcript_updates_proxy_state_without_legacy_payload_fields,
        test_workbench_edit_keeps_cursor_and_tail_conflict_preserves_edit,
        test_persistence_writes_proxy_session_folder_layout,
        test_transcript_replace_route_updates_proxy_state,
        test_session_reset_route_is_removed,
        test_transcript_replace_does_not_accept_legacy_record_payload,
        test_restart_restores_transcript_and_cursor_for_next_diff,
        test_control_intercept_fallback_keeps_control_turn_in_context,
        test_compact_request_is_handled_by_proxy_core_state,
        test_compact_failure_rolls_back_transcript_and_cursor,
        test_prune_sessions_missing_from_codex_deletes_only_missing_proxy_sessions,
        test_node_locks_persist_and_cleanup_with_transcript,
        test_node_locks_store_only_default_overrides,
        test_node_lock_allows_main_running_but_rejects_context_runs,
        test_pending_context_review_metadata_and_apply_replace_transcript,
        test_pending_context_review_stale_apply_clears_review,
        test_pending_context_review_is_cleared_when_main_turn_starts,
        test_pending_context_review_survives_restart_until_same_session_requests,
        test_legacy_review_copy_is_not_restored,
        test_late_review_is_rejected_after_main_turn_cancellation,
        test_persisted_running_status_is_not_treated_as_live_request,
        test_main_turn_state_survives_request_boundaries_but_not_restart,
        test_main_turn_finish_does_not_create_empty_session,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} proxy store core integration tests passed")


if __name__ == "__main__":
    main()
