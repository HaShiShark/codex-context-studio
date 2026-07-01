from __future__ import annotations

import copy
import io
import sys
import tempfile
from http import HTTPStatus
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import proxy_server  # noqa: E402
from backend.compact_controller import (  # noqa: E402
    LOCAL_COMPACT_PROMPT_PREFIX,
    LOCAL_COMPACT_SUMMARY_PREFIX,
    MANUAL_LOCAL_COMPACT_PROMPT,
)
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


def new_store(temp_dir: str) -> proxy_server.ProxyStore:
    return proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")


def proxy_items(session: proxy_server.ProxySession) -> list[Any]:
    return transcript_to_input_items(session.proxy_state.transcript)


def message_text(item: Any) -> str:
    assert isinstance(item, dict)
    return proxy_server.read_message_text(item)


class FakeHandler:
    def __init__(self, path: str) -> None:
        self.path = path
        self.headers: dict[str, str] = {
            "content-type": "application/json",
            "content-encoding": "",
            "content-length": "0",
        }
        self.rfile = io.BytesIO(b"")
        self.status: HTTPStatus | None = None
        self.payload: dict[str, Any] | None = None

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        self.status = status
        self.payload = payload

    def _read_json(self) -> dict[str, Any]:
        raw_body = self.rfile.read(int(self.headers.get("content-length", "0") or "0"))
        if not raw_body:
            return {}
        data = proxy_server.json.loads(raw_body.decode("utf-8"))
        assert isinstance(data, dict)
        return data


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


def test_legacy_override_status_no_longer_drives_main_request_path() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = new_store(temp_dir)
        stale_input = [message("user", "old mirrored text")]
        store.sessions[SESSION_ID] = proxy_server.ProxySession(
            id=SESSION_ID,
            title="Legacy Override Session",
            proxy_state=proxy_server.ProxyState(
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

        index = proxy_server.json_loads_value((Path(temp_dir) / "index.json").read_text(encoding="utf-8"), {})
        session_json = proxy_server.json_loads_value((session_dir / "session.json").read_text(encoding="utf-8"), {})
        transcript_json = proxy_server.json_loads_value((session_dir / "transcript.json").read_text(encoding="utf-8"), {})
        cursor_json = proxy_server.json_loads_value((session_dir / "cursor.json").read_text(encoding="utf-8"), {})

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
        original_store = proxy_server.STORE
        try:
            proxy_server.STORE = new_store(temp_dir)
            replacement_input = [
                message("developer", "new instructions"),
                message("user", "replacement"),
            ]
            payload_bytes = proxy_server.json.dumps(
                {"transcript": input_items_to_transcript(replacement_input)}
            ).encode("utf-8")
            handler = FakeHandler(f"/api/proxy/sessions/{SESSION_ID}/transcript")
            handler.rfile = io.BytesIO(payload_bytes)
            handler.headers["content-length"] = str(len(payload_bytes))

            proxy_server.Handler.do_POST(handler)  # type: ignore[arg-type]

            assert handler.status is None or handler.status == HTTPStatus.OK
            assert handler.payload is not None
            assert handler.payload["changed"] is True
            assert_no_legacy_payload_fields(handler.payload)
            session = proxy_server.STORE.sessions[SESSION_ID]
            assert proxy_items(session) == replacement_input
            assert session.proxy_state.codex_input_cursor == []
        finally:
            proxy_server.STORE = original_store


def test_session_reset_route_is_removed() -> None:
    handler = FakeHandler(f"/api/proxy/sessions/{SESSION_ID}/reset")

    proxy_server.Handler.do_POST(handler)  # type: ignore[arg-type]

    assert handler.status == HTTPStatus.NOT_FOUND
    assert handler.payload == {"error": "not found"}


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

        notice = message("assistant", proxy_server.CONTEXT_CONTROL_NOTICE_TEXT)
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
                "x-codex-turn-metadata": '{"request_kind":"compaction","trigger":"manual"}',
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
                    "x-codex-turn-metadata": '{"request_kind":"compaction","trigger":"manual"}',
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


def main() -> None:
    tests = [
        test_begin_request_and_complete_response_use_proxy_state,
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
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} proxy store core integration tests passed")


if __name__ == "__main__":
    main()
