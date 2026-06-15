from __future__ import annotations

import http.client
import json
import sqlite3
import sys
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import closing
from pathlib import Path
from typing import Any

import zstandard

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import proxy_server
from backend import web_server


SESSION_ID = "fake-codex-session"
EDITED_TEXT = "HASH_CONTEXT_EDITED_CANONICAL"
CODEX_ORIGINAL_TEXT = "CODEX_ORIGINAL_INPUT_SHOULD_NOT_SURVIVE"
REMOTE_SUMMARY_TEXT = "REMOTE_COMPACT_SUMMARY"
REMOTE_COMPACTION_BLOB = "ENCRYPTED_COMPACTION_SUMMARY"
USER_PROMPT = "list the root files"
PRE_TOOL_TEXT = "I will inspect the root directory."
FINAL_TEXT = "The root contains README.md and proxy_server.py."
TOOL_CALL_ID = "call_root_ls"
DEVELOPER_INSTRUCTIONS = "developer instructions"
ENVIRONMENT_CONTEXT = "<environment_context><cwd>/repo</cwd></environment_context>"


def record(role: str, text: str) -> dict[str, Any]:
    return proxy_server.transcript_record(role, text, [proxy_server.provider_message(role, text)])


def provider_texts(records: list[dict[str, Any]]) -> list[str]:
    return [str(record.get("text") or "") for record in records]


class MockCompactUpstream(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    response_payload = {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": REMOTE_SUMMARY_TEXT}],
            },
            {
                "type": "compaction",
                "encrypted_content": REMOTE_COMPACTION_BLOB,
            },
        ]
    }

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_POST(self) -> None:
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        self.__class__.requests.append(
            {
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
                "raw_body": raw_body,
            }
        )
        payload = json.dumps(self.response_payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class MockModelsUpstream(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    response_payload = {
        "models": [
            {"slug": "gpt-test-codex", "provider": "Codex"},
            "gpt-test-mini",
        ]
    }

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:
        self.__class__.requests.append(
            {
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
            }
        )
        payload = json.dumps(self.response_payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class MockResponsesUpstream(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    response_event = {"type": "response.completed", "response": {"model": "gpt-test", "output": []}}

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_POST(self) -> None:
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        self.__class__.requests.append(
            {
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            }
        )
        payload = f"data: {json.dumps(self.response_event, ensure_ascii=False)}\n\n".encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_server(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def message_text(item: dict[str, Any]) -> str:
    return proxy_server.read_message_text(item)


def test_drop_unpaired_tool_items_preserves_only_complete_pairs() -> None:
    valid_call_id = "call_valid"
    dangling_call_id = "call_without_output"
    dangling_output_id = "call_without_call"
    input_items = [
        proxy_server.provider_message("user", USER_PROMPT),
        {
            "type": "function_call",
            "call_id": valid_call_id,
            "name": "shell_command",
            "arguments": json.dumps({"command": "Get-ChildItem -Force"}),
        },
        {
            "type": "function_call_output",
            "call_id": valid_call_id,
            "output": "README.md",
        },
        {
            "type": "function_call",
            "call_id": dangling_call_id,
            "name": "shell_command",
            "arguments": json.dumps({"command": "rg --files"}),
        },
        {
            "type": "function_call_output",
            "call_id": dangling_output_id,
            "output": "orphan output",
        },
        proxy_server.provider_message("assistant", FINAL_TEXT),
    ]

    sanitized = proxy_server.drop_unpaired_tool_items(input_items)
    serialized = json.dumps(sanitized, ensure_ascii=False)

    assert valid_call_id in serialized
    assert dangling_call_id not in serialized
    assert dangling_output_id not in serialized
    assert [item.get("type") for item in sanitized] == [
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]


def test_compact_without_override_preserves_codex_input() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        original_body = {
            "model": "gpt-test",
            "input": [
                proxy_server.provider_message("user", CODEX_ORIGINAL_TEXT),
                {
                    "type": "function_call",
                    "call_id": TOOL_CALL_ID,
                    "name": "shell_command",
                    "arguments": json.dumps({"command": "rg --files"}),
                },
                {
                    "type": "function_call_output",
                    "call_id": TOOL_CALL_ID,
                    "output": "README.md\nproxy_server.py",
                },
            ],
            "previous_response_id": "resp_from_codex",
        }

        _session, forwarded_body = store.begin_compact(
            SESSION_ID,
            original_body,
            {"x-codex-session-id": SESSION_ID},
        )

        assert forwarded_body == original_body
        session = store.get_session(SESSION_ID)
        assert session is not None
        assert session["status"] == "compacting"
        assert session["has_override"] is False


def test_compact_override_reinjects_fresh_initial_context() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        compacted_transcript = [
            proxy_server.transcript_record(
                "user",
                "first real user message",
                [proxy_server.provider_message("user", "first real user message")],
            ),
            proxy_server.transcript_record(
                "user",
                f"{proxy_server.LOCAL_COMPACT_SUMMARY_PREFIX}\n\nprevious compact summary",
                [
                    proxy_server.provider_message(
                        "user",
                        f"{proxy_server.LOCAL_COMPACT_SUMMARY_PREFIX}\n\nprevious compact summary",
                    )
                ],
            ),
        ]
        store.sessions[SESSION_ID] = proxy_server.ProxySession(
            id=SESSION_ID,
            title="Codex fake",
            transcript=proxy_server.clean_transcript(compacted_transcript),
            edited_transcript=[
                proxy_server.transcript_record(
                    "developer",
                    "stale developer instructions",
                    [proxy_server.provider_message("developer", "stale developer instructions")],
                ),
                proxy_server.transcript_record(
                    "user",
                    "<environment_context><cwd>/stale</cwd></environment_context>",
                    [
                        proxy_server.provider_message(
                            "user",
                            "<environment_context><cwd>/stale</cwd></environment_context>",
                        )
                    ],
                ),
                *compacted_transcript,
            ],
            status="override",
        )
        body = {
            "input": [
                proxy_server.provider_message("developer", "fresh developer instructions"),
                proxy_server.provider_message(
                    "user",
                    "<environment_context><cwd>/fresh</cwd></environment_context>",
                ),
                *proxy_server.transcript_to_input_items(compacted_transcript),
            ],
            "previous_response_id": "resp_from_stale_compact",
        }

        _session, forwarded = store.begin_compact(
            SESSION_ID,
            body,
            {"x-codex-session-id": SESSION_ID},
        )

        forwarded_transcript = proxy_server.input_items_to_transcript(forwarded["input"])
        texts = [record["text"] for record in forwarded_transcript]
        assert texts[:2] == [
            "fresh developer instructions",
            "<environment_context><cwd>/fresh</cwd></environment_context>",
        ]
        assert "stale developer instructions" not in texts
        assert "<environment_context><cwd>/stale</cwd></environment_context>" not in texts
        assert "previous_response_id" not in forwarded


def test_request_without_override_preserves_codex_body() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        original_body = {
            "model": "gpt-test",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "developer instructions"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "inspect this image"},
                        {"type": "input_image", "image_url": "file:///tmp/example.png", "detail": "high"},
                    ],
                },
                {
                    "type": "reasoning",
                    "id": "reasoning-id",
                    "summary": [{"type": "summary_text", "text": "looked at available tools"}],
                    "encrypted_content": "encrypted",
                },
                {
                    "type": "function_call",
                    "call_id": TOOL_CALL_ID,
                    "name": "shell_command",
                    "arguments": json.dumps({"command": "Get-Date"}),
                    "status": "completed",
                },
                {
                    "type": "function_call_output",
                    "call_id": TOOL_CALL_ID,
                    "output": "2026-05-05 23:59:00 +08:00",
                },
            ],
            "previous_response_id": "resp_from_codex",
            "tools": [{"type": "function", "name": "shell_command"}],
            "parallel_tool_calls": True,
            "stream": True,
        }

        _session, forwarded_body = store.begin_request(
            SESSION_ID,
            original_body,
            {"x-codex-session-id": SESSION_ID},
        )

        assert forwarded_body == original_body
        session = store.get_session(SESSION_ID)
        assert session is not None
        request_log = store.sessions[SESSION_ID].request_log
        assert request_log[-1]["kind"] == "mirror_passthrough"
        assert request_log[-1]["forwarded_body"] == original_body


def test_proxy_usage_summary_records_and_resets_by_session() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        store.sessions[SESSION_ID] = proxy_server.ProxySession(
            id=SESSION_ID,
            title="Codex fake",
            transcript=[record("user", USER_PROMPT)],
        )

        store.record_usage(
            SESSION_ID,
            "main",
            "gpt-test",
            {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 40},
                "output_tokens": 20,
                "output_tokens_details": {"reasoning_tokens": 5},
                "total_tokens": 120,
            },
        )
        store.record_usage(
            SESSION_ID,
            "context_workbench",
            "gpt-context",
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
            },
        )

        payload = store.get_session(SESSION_ID)
        assert payload is not None
        assert payload["active_transcript"] == payload["transcript"]
        assert "usage_events" not in payload
        summary = payload["usage_summary"]
        assert summary["request_count"] == 2
        assert summary["input_tokens"] == 110
        assert summary["cached_input_tokens"] == 40
        assert summary["non_cached_input_tokens"] == 70
        assert summary["output_tokens"] == 25
        assert summary["reasoning_tokens"] == 5
        assert summary["total_tokens"] == 135
        assert summary["known_cost_usd"] > 0
        assert summary["unknown_cost_request_count"] == 0
        assert summary["by_kind"]["main"]["request_count"] == 1
        assert summary["by_kind"]["context_workbench"]["request_count"] == 1
        listed_session = store.list_sessions()["sessions"][0]
        assert listed_session["usage_summary"]["request_count"] == 2
        assert "transcript" not in listed_session
        assert "active_transcript" not in listed_session
        assert "raw_transcript" not in listed_session

        with closing(sqlite3.connect(Path(temp_dir) / "proxy_state.sqlite3")) as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM usage_events WHERE session_id = ?",
                (SESSION_ID,),
            ).fetchone()[0]
            summary_row = conn.execute(
                "SELECT summary_json FROM usage_summaries WHERE session_id = ?",
                (SESSION_ID,),
            ).fetchone()
        assert event_count == 2
        assert summary_row is not None
        assert json.loads(summary_row[0])["request_count"] == 2

        reloaded_store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        reloaded_summary = reloaded_store.session_usage(SESSION_ID)["summary"]
        assert reloaded_summary["request_count"] == 2
        assert reloaded_summary["input_tokens"] == 110

        reset = store.reset_usage(SESSION_ID)
        assert reset["cleared_count"] == 2
        assert reset["summary"]["request_count"] == 0
        assert store.session_usage(SESSION_ID)["summary"]["request_count"] == 0


def test_proxy_store_can_save_only_one_session() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        store.sessions["session-one"] = proxy_server.ProxySession(
            id="session-one",
            title="One",
            transcript=[record("user", "one")],
        )
        store.sessions["session-two"] = proxy_server.ProxySession(
            id="session-two",
            title="Two",
            transcript=[record("user", "two")],
        )
        store.save()

        original_save_session = store._save_session_to_db
        saved_session_ids: list[str] = []

        def spy_save_session(conn: sqlite3.Connection, session: proxy_server.ProxySession) -> None:
            saved_session_ids.append(session.id)
            original_save_session(conn, session)

        store._save_session_to_db = spy_save_session  # type: ignore[method-assign]
        store.sessions["session-one"].title = "One changed"
        store.save("session-one")

        assert saved_session_ids == ["session-one"]


def test_proxy_usage_update_does_not_rewrite_session_payload() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        store.sessions[SESSION_ID] = proxy_server.ProxySession(
            id=SESSION_ID,
            title="Codex fake",
            transcript=[record("user", "hello")],
        )
        store.save()

        def fail_session_payload_save(conn: sqlite3.Connection, session: proxy_server.ProxySession) -> None:
            raise AssertionError("usage-only updates must not save full session payload")

        store._save_session_to_db = fail_session_payload_save  # type: ignore[method-assign]
        store.record_usage(SESSION_ID, "main", "gpt-test", {"input_tokens": 7, "output_tokens": 3})

        with closing(sqlite3.connect(Path(temp_dir) / "proxy_state.sqlite3")) as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM usage_events WHERE session_id = ?",
                (SESSION_ID,),
            ).fetchone()[0]
            summary = json.loads(
                conn.execute(
                    "SELECT summary_json FROM usage_summaries WHERE session_id = ?",
                    (SESSION_ID,),
                ).fetchone()[0]
            )
        assert event_count == 1
        assert summary["request_count"] == 1
        assert summary["input_tokens"] == 7


def test_proxy_usage_routes_record_main_and_context_workbench() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        original_store = proxy_server.STORE
        original_chatgpt_base_url = proxy_server.CHATGPT_UPSTREAM_BASE_URL
        original_openai_base_url = proxy_server.OPENAI_UPSTREAM_BASE_URL
        MockResponsesUpstream.requests = []
        MockResponsesUpstream.response_event = {
            "type": "response.completed",
            "response": {
                "model": "gpt-test",
                "output": [],
                "usage": {
                    "input_tokens": 50,
                    "input_tokens_details": {"cached_tokens": 10},
                    "output_tokens": 15,
                    "total_tokens": 65,
                },
            },
        }
        upstream = start_server(MockResponsesUpstream)
        proxy = start_server(proxy_server.Handler)
        proxy_server.STORE = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        proxy_server.CHATGPT_UPSTREAM_BASE_URL = f"http://127.0.0.1:{upstream.server_port}/backend-api/codex"
        proxy_server.OPENAI_UPSTREAM_BASE_URL = f"http://127.0.0.1:{upstream.server_port}/v1"

        try:
            conn = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=20)
            request_body = {
                "model": "gpt-test",
                "input": [proxy_server.provider_message("user", USER_PROMPT)],
            }
            try:
                conn.request(
                    "POST",
                    "/v1/responses",
                    body=json.dumps(request_body).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer real-token",
                        "x-codex-session-id": SESSION_ID,
                    },
                )
                response = conn.getresponse()
                response.read()
                assert response.status == HTTPStatus.OK

                conn.request(
                    "POST",
                    "/v1/responses",
                    body=json.dumps(request_body).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer real-token",
                        "x-hash-context-internal": "context-workbench",
                        "x-hash-context-session-id": SESSION_ID,
                    },
                )
                response = conn.getresponse()
                response.read()
                assert response.status == HTTPStatus.OK

                conn.request("GET", f"/api/proxy/sessions/{SESSION_ID}/usage")
                response = conn.getresponse()
                usage_payload = json.loads(response.read().decode("utf-8"))
                assert response.status == HTTPStatus.OK
                summary = usage_payload["summary"]
                assert summary["request_count"] == 2
                assert summary["by_kind"]["main"]["input_tokens"] == 50
                assert summary["by_kind"]["context_workbench"]["input_tokens"] == 50

                conn.request("POST", f"/api/proxy/sessions/{SESSION_ID}/usage/reset", body=b"{}")
                response = conn.getresponse()
                reset_payload = json.loads(response.read().decode("utf-8"))
                assert response.status == HTTPStatus.OK
                assert reset_payload["cleared_count"] == 2
                assert reset_payload["summary"]["request_count"] == 0
            finally:
                conn.close()
        finally:
            proxy.shutdown()
            proxy.server_close()
            upstream.shutdown()
            upstream.server_close()
            proxy_server.STORE = original_store
            proxy_server.CHATGPT_UPSTREAM_BASE_URL = original_chatgpt_base_url
            proxy_server.OPENAI_UPSTREAM_BASE_URL = original_openai_base_url
            MockResponsesUpstream.response_event = {"type": "response.completed", "response": {"model": "gpt-test", "output": []}}


def test_sse_completed_response_captures_usage_payload() -> None:
    response_items: list[dict[str, Any]] = []
    text_parts: list[str] = []
    completed_responses: list[dict[str, Any]] = []
    buffer = (
        'data: {"type":"response.completed","response":{"model":"gpt-test","output":[],"usage":{"input_tokens":3,"output_tokens":2}}}\n\n'
    )

    remainder = proxy_server.parse_sse_buffer(buffer, response_items, text_parts, completed_responses)

    assert remainder == ""
    assert completed_responses[0]["usage"]["input_tokens"] == 3


def test_clean_transcript_preserves_repeated_input_context_records() -> None:
    input_mapped_transcript = [
        record("developer", DEVELOPER_INSTRUCTIONS),
        record("user", ENVIRONMENT_CONTEXT),
        record("user", "first real question"),
        record("assistant", "first answer"),
        record("developer", DEVELOPER_INSTRUCTIONS),
        record("user", ENVIRONMENT_CONTEXT),
        record("user", "second real question"),
    ]

    cleaned = proxy_server.clean_transcript(input_mapped_transcript)

    assert provider_texts(cleaned) == [
        DEVELOPER_INSTRUCTIONS,
        ENVIRONMENT_CONTEXT,
        "first real question",
        "first answer",
        DEVELOPER_INSTRUCTIONS,
        ENVIRONMENT_CONTEXT,
        "second real question",
    ]


def test_control_intercept_preserves_existing_transcript_when_input_is_prefix_only() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        existing = proxy_server.clean_transcript(
            [
                record("developer", DEVELOPER_INSTRUCTIONS),
                record("user", ENVIRONMENT_CONTEXT),
                record("user", "first real question"),
                record("assistant", "first answer"),
            ]
        )
        store.sessions[SESSION_ID] = proxy_server.ProxySession(
            id=SESSION_ID,
            title="Codex fake",
            transcript=existing,
            status="mirror",
        )
        body = {
            "input": [
                proxy_server.provider_message("developer", DEVELOPER_INSTRUCTIONS),
                proxy_server.provider_message("user", ENVIRONMENT_CONTEXT),
                proxy_server.provider_message("user", "ctx"),
            ]
        }

        store.record_control_intercept(SESSION_ID, body, {"x-codex-session-id": SESSION_ID}, "ctx")

        session = store.get_session(SESSION_ID)
        assert session is not None
        assert provider_texts(session["transcript"]) == provider_texts(existing)
        assert store.sessions[SESSION_ID].request_log[-1]["kind"] == "context_control_intercept"


def test_codex_local_session_sync_does_not_fallback_to_prefix_only_transcript() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        previous_sessions_dir = web_server.CODEX_LOCAL_SESSIONS_DIR
        previous_proxy_state_file = web_server.PROXY_STATE_FILE
        try:
            temp_path = Path(temp_dir)
            web_server.CODEX_LOCAL_SESSIONS_DIR = temp_path / "missing-sessions"
            web_server.PROXY_STATE_FILE = temp_path / "proxy_state.json"
            web_server.PROXY_STATE_FILE.write_text(
                json.dumps(
                    {
                        "active_session_id": SESSION_ID,
                        "sessions": [
                            {
                                "id": SESSION_ID,
                                "updated_at": "2026-05-09T00:00:00Z",
                                "request_log": [
                                    {
                                        "body": {
                                            "input": [
                                                proxy_server.provider_message("developer", DEVELOPER_INSTRUCTIONS),
                                                proxy_server.provider_message("user", ENVIRONMENT_CONTEXT),
                                            ]
                                        }
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            assert web_server.latest_proxy_instruction_prefix_records()
            assert web_server.codex_local_session_transcript(SESSION_ID) == []
        finally:
            web_server.CODEX_LOCAL_SESSIONS_DIR = previous_sessions_dir
            web_server.PROXY_STATE_FILE = previous_proxy_state_file


def test_prefix_only_codex_local_session_is_not_conversation() -> None:
    transcript = web_server.normalize_transcript(
        [
            record("developer", DEVELOPER_INSTRUCTIONS),
            record("user", ENVIRONMENT_CONTEXT),
        ]
    )

    assert not web_server.transcript_has_conversation_records(transcript)


def test_local_compact_without_override_replaces_manual_prompt() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        original_body = {
            "model": "gpt-test",
            "input": [
                proxy_server.provider_message("user", USER_PROMPT),
                proxy_server.provider_message("assistant", FINAL_TEXT),
                proxy_server.provider_message("user", f"{proxy_server.LOCAL_COMPACT_PROMPT_PREFIX}\nSummarize."),
            ],
            "previous_response_id": "resp_from_codex",
            "tools": [{"type": "function", "name": "shell_command"}],
            "parallel_tool_calls": True,
            "stream": True,
        }

        _session, forwarded_body = store.begin_request(
            SESSION_ID,
            original_body,
            {"x-codex-session-id": SESSION_ID},
        )

        assert forwarded_body["input"][:-1] == original_body["input"][:-1]
        assert message_text(forwarded_body["input"][-1]) == proxy_server.MANUAL_LOCAL_COMPACT_PROMPT
        assert forwarded_body["previous_response_id"] == original_body["previous_response_id"]
        session = store.get_session(SESSION_ID)
        assert session is not None
        assert session["status"] == "compacting"
        request_log = store.sessions[SESSION_ID].request_log
        assert request_log[-1]["kind"] == "local_compact"
        assert message_text(request_log[-1]["forwarded_body"]["input"][-1]) == proxy_server.MANUAL_LOCAL_COMPACT_PROMPT


def test_replacement_compact_prompts_still_count_as_compact_prompts() -> None:
    assert proxy_server.is_local_compact_prompt_text(proxy_server.MANUAL_LOCAL_COMPACT_PROMPT)
    assert proxy_server.is_local_compact_prompt_text(proxy_server.AUTO_LOCAL_COMPACT_PROMPT)


def test_clean_transcript_keeps_latest_local_compact_summary() -> None:
    old_summary = f"{proxy_server.LOCAL_COMPACT_SUMMARY_PREFIX}\n\nold summary"
    new_summary = f"{proxy_server.LOCAL_COMPACT_SUMMARY_PREFIX}\n\nnew summary"

    cleaned = proxy_server.clean_transcript(
        [
            record("user", USER_PROMPT),
            record("user", old_summary),
            record("assistant", "work after old summary"),
            record("user", new_summary),
        ]
    )

    assert provider_texts(cleaned) == [USER_PROMPT, "work after old summary", new_summary]


def test_local_auto_compact_replaces_prompt_with_concise_continuation_prompt() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        original_body = {
            "model": "gpt-test",
            "input": [
                proxy_server.provider_message("user", USER_PROMPT),
                proxy_server.provider_message("assistant", PRE_TOOL_TEXT),
                {
                    "type": "function_call",
                    "call_id": TOOL_CALL_ID,
                    "name": "shell_command",
                    "arguments": json.dumps({"command": "Get-ChildItem -Force"}),
                },
                {
                    "type": "function_call_output",
                    "call_id": TOOL_CALL_ID,
                    "output": "README.md\nproxy_server.py",
                },
                proxy_server.provider_message("user", f"{proxy_server.LOCAL_COMPACT_PROMPT_PREFIX}\nSummarize."),
            ],
            "previous_response_id": "resp_from_codex",
        }

        _session, forwarded_body = store.begin_request(
            SESSION_ID,
            original_body,
            {"x-codex-session-id": SESSION_ID},
        )

        assert message_text(forwarded_body["input"][-1]) == proxy_server.AUTO_LOCAL_COMPACT_PROMPT
        assert proxy_server.MANUAL_LOCAL_COMPACT_PROMPT not in json.dumps(forwarded_body, ensure_ascii=False)
        session = store.get_session(SESSION_ID)
        assert session is not None
        assert session["status"] == "compacting"
        assert store.sessions[SESSION_ID].request_log[-1]["kind"] == "local_compact"


def test_local_compact_prompt_replacement_preserves_input_item_shape() -> None:
    prompt_item = {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": f"{proxy_server.LOCAL_COMPACT_PROMPT_PREFIX}\nSummarize."},
        ],
        "id": "prompt-message-id",
    }
    replaced = proxy_server.replace_last_local_compact_prompt_input(
        [proxy_server.provider_message("user", USER_PROMPT), prompt_item],
        proxy_server.MANUAL_LOCAL_COMPACT_PROMPT,
    )

    assert replaced[0] == proxy_server.provider_message("user", USER_PROMPT)
    assert replaced[1]["id"] == "prompt-message-id"
    assert replaced[1]["content"] == [
        {"type": "input_text", "text": proxy_server.MANUAL_LOCAL_COMPACT_PROMPT}
    ]


def test_same_turn_local_compact_with_assistant_text_uses_auto_prompt() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        turn_metadata = '{"turn_id":"turn-auto-text"}'
        first_body = {
            "input": [
                proxy_server.provider_message("user", USER_PROMPT),
            ],
        }
        store.begin_request(
            SESSION_ID,
            first_body,
            {"x-codex-session-id": SESSION_ID, "x-codex-turn-metadata": turn_metadata},
        )
        compact_body = {
            "input": [
                proxy_server.provider_message("user", USER_PROMPT),
                proxy_server.provider_message("assistant", "partial assistant text before auto compact"),
                proxy_server.provider_message("user", f"{proxy_server.LOCAL_COMPACT_PROMPT_PREFIX}\nSummarize."),
            ],
            "previous_response_id": "resp_same_turn",
        }

        _session, forwarded_body = store.begin_request(
            SESSION_ID,
            compact_body,
            {"x-codex-session-id": SESSION_ID, "x-codex-turn-metadata": turn_metadata},
        )

        assert message_text(forwarded_body["input"][-1]) == proxy_server.AUTO_LOCAL_COMPACT_PROMPT


def test_new_turn_local_compact_with_assistant_text_uses_manual_prompt() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        store.sessions[SESSION_ID] = proxy_server.ProxySession(
            id=SESSION_ID,
            title="Codex fake",
            transcript=[record("user", USER_PROMPT), record("assistant", FINAL_TEXT)],
            last_turn_metadata_header='{"turn_id":"previous-turn"}',
            status="mirror",
        )
        body = {
            "input": [
                proxy_server.provider_message("user", USER_PROMPT),
                proxy_server.provider_message("assistant", FINAL_TEXT),
                proxy_server.provider_message("user", f"{proxy_server.LOCAL_COMPACT_PROMPT_PREFIX}\nSummarize."),
            ],
            "previous_response_id": "resp_previous_turn",
        }

        _session, forwarded_body = store.begin_request(
            SESSION_ID,
            body,
            {"x-codex-session-id": SESSION_ID, "x-codex-turn-metadata": '{"turn_id":"manual-compact-turn"}'},
        )

        assert message_text(forwarded_body["input"][-1]) == proxy_server.MANUAL_LOCAL_COMPACT_PROMPT


def test_running_status_alone_does_not_force_auto_compact_prompt() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        store.sessions[SESSION_ID] = proxy_server.ProxySession(
            id=SESSION_ID,
            title="Codex fake",
            transcript=[record("user", USER_PROMPT)],
            status="running",
        )
        body = {
            "input": [
                proxy_server.provider_message("user", USER_PROMPT),
                proxy_server.provider_message("assistant", FINAL_TEXT),
                proxy_server.provider_message("user", f"{proxy_server.LOCAL_COMPACT_PROMPT_PREFIX}\nSummarize."),
            ],
            "previous_response_id": "resp_from_codex",
        }

        _session, forwarded_body = store.begin_request(
            SESSION_ID,
            body,
            {"x-codex-session-id": SESSION_ID},
        )

        assert message_text(forwarded_body["input"][-1]) == proxy_server.MANUAL_LOCAL_COMPACT_PROMPT


def test_override_local_compact_replaces_prompt_and_removes_previous_response_id() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        store.sessions[SESSION_ID] = proxy_server.ProxySession(
            id=SESSION_ID,
            title="Codex fake",
            transcript=[record("user", CODEX_ORIGINAL_TEXT)],
            edited_transcript=[record("user", EDITED_TEXT)],
            status="override",
        )
        body = {
            "input": [
                proxy_server.provider_message("user", CODEX_ORIGINAL_TEXT),
                proxy_server.provider_message("user", f"{proxy_server.LOCAL_COMPACT_PROMPT_PREFIX}\nSummarize."),
            ],
            "previous_response_id": "resp_from_raw_context",
        }

        _session, forwarded_body = store.begin_request(
            SESSION_ID,
            body,
            {"x-codex-session-id": SESSION_ID},
        )

        forwarded_text = json.dumps(forwarded_body, ensure_ascii=False)
        assert EDITED_TEXT in forwarded_text
        assert CODEX_ORIGINAL_TEXT not in forwarded_text
        assert message_text(forwarded_body["input"][-1]) == proxy_server.MANUAL_LOCAL_COMPACT_PROMPT
        assert "previous_response_id" not in forwarded_body
        assert store.sessions[SESSION_ID].request_log[-1]["kind"] == "local_compact"


def test_assistant_provider_items_are_deduped_by_logical_tool_identity() -> None:
    previous_call = {
        "type": "function_call",
        "call_id": TOOL_CALL_ID,
        "name": "shell_command",
        "arguments": "{}",
    }
    updated_call = {
        "type": "function_call",
        "call_id": TOOL_CALL_ID,
        "name": "shell_command",
        "arguments": json.dumps({"command": "Get-ChildItem -Force"}),
    }
    previous_output = {
        "type": "function_call_output",
        "call_id": TOOL_CALL_ID,
        "output": "partial",
    }
    updated_output = {
        "type": "function_call_output",
        "call_id": TOOL_CALL_ID,
        "output": "README.md\nproxy_server.py",
    }

    merged = proxy_server.append_assistant_response_record(
        [proxy_server.transcript_record("assistant", PRE_TOOL_TEXT, [previous_call, previous_output])],
        PRE_TOOL_TEXT,
        [updated_call, updated_output],
    )

    provider_items = merged[-1]["providerItems"]
    assert provider_items[0].get("type") == "message"
    assert provider_items[0].get("role") == "assistant"
    assert message_text(provider_items[0]) == PRE_TOOL_TEXT
    assert provider_items[1:] == [updated_call, updated_output]
    assert merged[-1]["toolEvents"][0]["raw_output"] == "README.md\nproxy_server.py"


def test_tool_output_request_with_override_does_not_restore_raw_context() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        store.sessions[SESSION_ID] = proxy_server.ProxySession(
            id=SESSION_ID,
            title="Codex fake",
            transcript=[record("user", CODEX_ORIGINAL_TEXT)],
            edited_transcript=[record("user", EDITED_TEXT)],
            status="override",
        )
        tool_body = {
            "input": [
                proxy_server.provider_message("user", CODEX_ORIGINAL_TEXT),
                proxy_server.provider_message("user", USER_PROMPT),
                {
                    "type": "function_call",
                    "call_id": TOOL_CALL_ID,
                    "name": "shell_command",
                    "arguments": json.dumps({"command": "Get-ChildItem -Force"}),
                },
                {
                    "type": "function_call_output",
                    "call_id": TOOL_CALL_ID,
                    "output": "README.md\nproxy_server.py",
                },
            ],
            "previous_response_id": "resp_from_raw_context",
        }

        store.begin_request(SESSION_ID, tool_body, {"x-codex-session-id": SESSION_ID})
        session = store.sessions[SESSION_ID]
        forwarded_text = json.dumps(session.request_log[-1]["forwarded_body"], ensure_ascii=False)
        pending_texts = provider_texts(session.pending_transcript or [])

        assert session.request_log[-1]["kind"] == "override_tool_output_rewrite"
        assert EDITED_TEXT in forwarded_text
        assert CODEX_ORIGINAL_TEXT not in forwarded_text
        assert "previous_response_id" not in session.request_log[-1]["forwarded_body"]
        assert EDITED_TEXT in pending_texts
        assert CODEX_ORIGINAL_TEXT not in pending_texts

        store.complete_response(SESSION_ID, [{"type": "message", "role": "assistant", "content": []}], FINAL_TEXT)
        payload = store.get_session(SESSION_ID)
        assert payload is not None
        edited_texts = provider_texts(payload["edited_transcript"] or [])
        assert EDITED_TEXT in edited_texts
        assert CODEX_ORIGINAL_TEXT not in edited_texts
        assert FINAL_TEXT in edited_texts[-1]


def test_auto_compact_summary_is_not_duplicated_during_override_tool_continuations() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        summary_text = f"{proxy_server.LOCAL_COMPACT_SUMMARY_PREFIX}\n\nAUTO_SUMMARY"
        summary_record = record("user", summary_text)
        assistant_items = [
            proxy_server.provider_message("assistant", PRE_TOOL_TEXT),
            {
                "type": "function_call",
                "call_id": TOOL_CALL_ID,
                "name": "shell_command",
                "arguments": json.dumps({"command": "Get-ChildItem -Force"}),
            },
            {
                "type": "function_call_output",
                "call_id": TOOL_CALL_ID,
                "output": "README.md\nproxy_server.py",
            },
        ]
        assistant_record = proxy_server.transcript_record("assistant", PRE_TOOL_TEXT, assistant_items)
        store.sessions[SESSION_ID] = proxy_server.ProxySession(
            id=SESSION_ID,
            title="Codex fake",
            transcript=[record("user", USER_PROMPT), summary_record],
            edited_transcript=[record("user", USER_PROMPT), summary_record, assistant_record],
            status="override",
        )
        tool_body = {
            "input": [
                proxy_server.provider_message("user", USER_PROMPT),
                proxy_server.provider_message("user", summary_text),
                *assistant_items,
                proxy_server.provider_message("user", summary_text),
                *assistant_items,
            ],
            "previous_response_id": "resp_after_auto_compact",
        }

        forwarded_lengths = []
        for _ in range(3):
            store.begin_request(SESSION_ID, tool_body, {"x-codex-session-id": SESSION_ID})
            forwarded_input = store.sessions[SESSION_ID].request_log[-1]["forwarded_body"]["input"]
            forwarded_lengths.append(len(forwarded_input))
            forwarded_transcript = proxy_server.input_items_to_transcript(forwarded_input)
            assert sum(
                1
                for item in forwarded_transcript
                if proxy_server.is_local_compact_summary_text(str(item.get("text") or ""))
            ) == 1

        session = store.sessions[SESSION_ID]
        pending = session.pending_transcript or []
        assert [item["role"] for item in pending] == ["user", "user", "assistant"]
        assert sum(
            1
            for item in pending
            if proxy_server.is_local_compact_summary_text(str(item.get("text") or ""))
        ) == 1
        assert pending[-1]["text"] == PRE_TOOL_TEXT
        assert forwarded_lengths == [forwarded_lengths[0]] * len(forwarded_lengths)


def test_tool_turn_stays_single_assistant_record() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        first_body = {
            "input": [proxy_server.provider_message("user", USER_PROMPT)],
        }
        session, _forwarded = store.begin_request(SESSION_ID, first_body, {"x-codex-session-id": SESSION_ID})
        first_response_items = [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": PRE_TOOL_TEXT}],
            },
            {
                "type": "function_call",
                "call_id": TOOL_CALL_ID,
                "name": "shell_command",
                "arguments": json.dumps({"command": "Get-ChildItem -Force"}),
            },
        ]
        store.complete_response(session.id, first_response_items, PRE_TOOL_TEXT)

        tool_output = "README.md\nproxy_server.py"
        second_body = {
            "input": [
                proxy_server.provider_message("user", USER_PROMPT),
                *first_response_items,
                {
                    "type": "function_call_output",
                    "call_id": TOOL_CALL_ID,
                    "output": tool_output,
                },
            ],
        }
        store.begin_request(SESSION_ID, second_body, {"x-codex-session-id": SESSION_ID})
        final_response_items = [
            {
                "type": "message",
                "role": "assistant",
                "content": [],
            }
        ]
        store.complete_response(SESSION_ID, final_response_items, FINAL_TEXT)

        session_payload = store.get_session(SESSION_ID)
        assert session_payload is not None
        transcript = session_payload["transcript"]
        assert [record["role"] for record in transcript] == ["user", "assistant"]
        assistant = transcript[-1]
        assert PRE_TOOL_TEXT in assistant["text"]
        assert FINAL_TEXT in assistant["text"]
        assert len(assistant["toolEvents"]) == 1
        assert [item.get("type") for item in assistant["providerItems"]] == [
            "message",
            "function_call",
            "function_call_output",
            "message",
        ]
        assert [block.get("kind") for block in assistant["blocks"]] == ["text", "tool", "text"]
        assert proxy_server.transcript_to_input_items(transcript) == [
            proxy_server.provider_message("user", USER_PROMPT),
            *assistant["providerItems"],
        ]


def test_codex_response_item_types_roundtrip() -> None:
    developer = {
        "type": "message",
        "role": "developer",
        "content": [{"type": "input_text", "text": "developer instructions"}],
    }
    user = {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "inspect this image"},
            {"type": "input_image", "image_url": "file:///tmp/example.png", "detail": "high"},
        ],
    }
    items = [
        developer,
        user,
        {
            "type": "reasoning",
            "id": "reasoning-id",
            "summary": [{"type": "summary_text", "text": "looked at available tools"}],
            "encrypted_content": "encrypted",
        },
        {
            "type": "local_shell_call",
            "id": "local-shell-id",
            "call_id": "local-shell-call-id",
            "status": "completed",
            "action": {"type": "exec", "command": ["echo", "hello"]},
        },
        {
            "type": "function_call_output",
            "call_id": "local-shell-call-id",
            "output": "hello",
        },
        {
            "type": "custom_tool_call",
            "call_id": "custom-tool-call-id",
            "name": "apply_patch",
            "input": "*** Begin Patch\n*** End Patch",
        },
        {
            "type": "custom_tool_call_output",
            "call_id": "custom-tool-call-id",
            "output": [{"type": "input_text", "text": "patched"}],
        },
        {
            "type": "tool_search_call",
            "call_id": "tool-search-call-id",
            "status": "completed",
            "execution": "client",
            "arguments": {"query": "calendar"},
        },
        {
            "type": "tool_search_output",
            "call_id": "tool-search-call-id",
            "status": "completed",
            "execution": "client",
            "tools": [{"name": "calendar_create_event"}],
        },
        {
            "type": "web_search_call",
            "id": "web-search-id",
            "status": "completed",
            "action": {"type": "search", "query": "weather"},
        },
        {
            "type": "image_generation_call",
            "id": "image-generation-id",
            "status": "completed",
            "revised_prompt": "a diagram",
            "result": "image-bytes",
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done"}],
        },
    ]

    transcript = proxy_server.input_items_to_transcript(items)
    assert [record["role"] for record in transcript] == ["developer", "user", "assistant"]
    assert transcript[0]["providerItems"] == [developer]
    assert transcript[1]["providerItems"] == [user]

    assistant = transcript[2]
    assert [item.get("type") for item in assistant["providerItems"]] == [item["type"] for item in items[2:]]
    assert len(assistant["toolEvents"]) == 5
    assert [block.get("kind") for block in assistant["blocks"]] == [
        "reasoning",
        "tool",
        "tool",
        "tool",
        "tool",
        "tool",
        "text",
    ]
    assert proxy_server.transcript_to_input_items(transcript) == items

    web_record = web_server.compile_record_from_provider_items(
        {"role": "assistant", "attachments": []},
        items[2:],
    )
    assert len(web_record["toolEvents"]) == 5
    assert [block.get("kind") for block in web_record["blocks"]] == [
        "reasoning",
        "tool",
        "tool",
        "tool",
        "tool",
        "tool",
        "text",
    ]
    assert web_record["toolEvents"][0]["call_id"] == "local-shell-call-id"
    assert web_record["toolEvents"][0]["raw_output"] == "hello"


def test_shell_tool_output_display_metadata_is_reconstructed() -> None:
    output = "Exit code: 1\nWall time: 0.1 seconds\nOutput:\nboom"
    provider_items = [
        {
            "type": "function_call",
            "call_id": TOOL_CALL_ID,
            "name": "shell_command",
            "arguments": json.dumps({"command": "Get-Date"}),
        },
        {
            "type": "function_call_output",
            "call_id": TOOL_CALL_ID,
            "output": output,
        },
    ]

    proxy_record = proxy_server.input_items_to_transcript(provider_items)[0]
    proxy_event = proxy_record["toolEvents"][0]
    assert proxy_event["display_detail"] == "Get-Date"
    assert proxy_event["raw_output"] == output
    assert proxy_event["status"] == "error"
    assert [block.get("kind") for block in proxy_record["blocks"]] == ["tool"]
    assert proxy_server.clean_transcript([proxy_record]) == [proxy_record]

    web_record = web_server.compile_record_from_provider_items(
        {"role": "assistant", "attachments": []},
        provider_items,
    )
    web_event = web_record["toolEvents"][0]
    assert web_event["display_detail"] == "Get-Date"
    assert web_event["raw_output"] == output
    assert web_event["status"] == "error"


def test_web_normalize_rebuilds_tool_display_from_provider_items() -> None:
    provider_items = [
        {
            "type": "function_call",
            "call_id": TOOL_CALL_ID,
            "name": "shell_command",
            "arguments": json.dumps({"command": "Get-ChildItem -Force"}),
        },
        {
            "type": "function_call_output",
            "call_id": TOOL_CALL_ID,
            "output": "1",
        },
    ]
    stale_record = {
        "role": "assistant",
        "text": "",
        "attachments": [],
        "toolEvents": [
            {"name": "shell_command", "call_id": TOOL_CALL_ID, "raw_output": "old output"},
            {"name": "tool", "call_id": "wrong_call", "raw_output": "extra output"},
        ],
        "blocks": [
            {"kind": "tool", "tool_event": {"name": "shell_command", "call_id": TOOL_CALL_ID, "raw_output": "old output"}},
            {"kind": "tool", "tool_event": {"name": "tool", "call_id": "wrong_call", "raw_output": "extra output"}},
        ],
        "providerItems": provider_items,
    }

    normalized = web_server.normalize_transcript([stale_record])

    assert len(normalized) == 1
    assistant = normalized[0]
    assert assistant["providerItems"] == provider_items
    assert len(assistant["toolEvents"]) == 1
    assert assistant["toolEvents"][0]["call_id"] == TOOL_CALL_ID
    assert assistant["toolEvents"][0]["raw_output"] == "1"
    assert [block.get("kind") for block in assistant["blocks"]] == ["tool"]
    assert "extra output" not in json.dumps(assistant, ensure_ascii=False)


def test_context_workbench_compresses_tool_output_without_duplicate_tool() -> None:
    transcript = web_server.normalize_transcript(
        [
            {
                "role": "assistant",
                "text": "",
                "attachments": [],
                "toolEvents": [],
                "blocks": [],
                "providerItems": [
                    {
                        "type": "function_call",
                        "call_id": TOOL_CALL_ID,
                        "name": "shell_command",
                        "arguments": json.dumps({"command": "Get-ChildItem -Force"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": TOOL_CALL_ID,
                        "output": "very long output",
                    },
                ],
            }
        ]
    )
    draft = web_server.ContextWorkbenchDraft(transcript, [0])
    node = draft._nodes_by_number([1])[0]

    draft.compress_item(node, item_number=2, compressed_content="1", style="keep only count")
    committed = draft.committed_transcript()

    assistant = committed[0]
    assert [item.get("type") for item in assistant["providerItems"]] == [
        "function_call",
        "function_call_output",
    ]
    assert assistant["providerItems"][1]["call_id"] == TOOL_CALL_ID
    assert assistant["providerItems"][1]["output"] == "1"
    assert len(assistant["toolEvents"]) == 1
    assert assistant["toolEvents"][0]["raw_output"] == "1"
    assert [block.get("kind") for block in assistant["blocks"]] == ["tool"]


def test_context_workbench_rejects_tool_output_call_id_drift() -> None:
    transcript = web_server.normalize_transcript(
        [
            {
                "role": "assistant",
                "text": "",
                "attachments": [],
                "toolEvents": [],
                "blocks": [],
                "providerItems": [
                    {
                        "type": "function_call",
                        "call_id": TOOL_CALL_ID,
                        "name": "shell_command",
                        "arguments": json.dumps({"command": "Get-Date"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": TOOL_CALL_ID,
                        "output": "2026-05-05 23:23:59 +08:00",
                    },
                ],
            }
        ]
    )
    draft = web_server.ContextWorkbenchDraft(transcript, [0])
    node = draft._nodes_by_number([1])[0]

    try:
        draft.replace_item(
            node,
            item_number=2,
            replacement_item={
                "type": "function_call_output",
                "call_id": "wrong_call",
                "output": "1",
            },
            reason="bad replacement",
        )
    except ValueError as exc:
        assert "call_id" in str(exc)
    else:
        raise AssertionError("call_id drift should be rejected")


def test_context_workbench_deletes_multiple_tool_items_atomically() -> None:
    transcript = web_server.normalize_transcript(
        [
            {
                "role": "assistant",
                "text": "",
                "attachments": [],
                "toolEvents": [],
                "blocks": [],
                "providerItems": [
                    {"type": "message", "role": "assistant", "content": "before"},
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "thinking"}],
                        "encrypted_content": "encrypted",
                    },
                    {
                        "type": "function_call",
                        "call_id": TOOL_CALL_ID,
                        "name": "shell_command",
                        "arguments": json.dumps({"command": "Get-Date"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": TOOL_CALL_ID,
                        "output": "2026-05-05 23:23:59 +08:00",
                    },
                    {"type": "message", "role": "assistant", "content": "after"},
                ],
            }
        ]
    )
    draft = web_server.ContextWorkbenchDraft(transcript, [0])
    node = draft._nodes_by_number([1])[0]

    draft.delete_items(node, item_numbers=[2, 3], reason="remove tool trace")
    committed = draft.committed_transcript()

    assistant = committed[0]
    assert [item.get("type") for item in assistant["providerItems"]] == ["message", "message"]
    assert assistant["text"] == "beforeafter"
    assert assistant["toolEvents"] == []
    assert [block.get("kind") for block in assistant["blocks"]] == ["text", "text"]


def test_context_workbench_finds_tool_outputs_lightly() -> None:
    long_output = "X" * 5000
    transcript = web_server.normalize_transcript(
        [
            {
                "role": "assistant",
                "text": "",
                "attachments": [],
                "toolEvents": [],
                "blocks": [],
                "providerItems": [
                    {
                        "type": "function_call",
                        "call_id": TOOL_CALL_ID,
                        "name": "shell_command",
                        "arguments": json.dumps({"command": "Get-ChildItem -Force"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": TOOL_CALL_ID,
                        "output": long_output,
                    },
                    {"type": "message", "role": "assistant", "content": "done"},
                ],
            }
        ]
    )
    draft = web_server.ContextWorkbenchDraft(transcript, [0])
    registry = web_server.ContextWorkbenchToolRegistry(draft)

    execution = registry.execute(
        "find_context_items",
        {"selector": {"tool_output_only": True}},
    )
    payload = json.loads(execution.output_text)

    assert execution.status == "completed"
    assert payload["matched_count"] == 1
    assert payload["items"][0]["item_ref"] == "node:1:item:2"
    assert payload["items"][0]["item_type"] == "function_call_output"
    assert long_output not in execution.output_text
    assert len(execution.output_text) < 3000


def test_override_merge_does_not_reappend_existing_latest_turn_tail() -> None:
    edited = [
        record("assistant", "ready"),
        record("user", "compressed T01-T03"),
        record("user", "T04 task"),
        record("assistant", "T04 answer"),
    ]
    mirrored = [
        record("assistant", "ready"),
        record("user", "T01 task"),
        record("assistant", "T01 answer"),
        record("user", "T02 task"),
        record("assistant", "T02 answer"),
    ]
    source = [
        *mirrored,
        record("user", "T04 task"),
        record("assistant", "T04 answer"),
    ]

    merged = proxy_server.merge_override_transcript(edited, mirrored, source)

    assert provider_texts(merged) == provider_texts(edited)



def test_override_merge_updates_latest_assistant_without_readding_user_turn() -> None:
    call_item = {
        "type": "function_call",
        "call_id": TOOL_CALL_ID,
        "name": "shell_command",
        "arguments": json.dumps({"command": "Get-ChildItem web_server.py"}),
    }
    output_item = {
        "type": "function_call_output",
        "call_id": TOOL_CALL_ID,
        "output": "web_server.py 3006 lines",
    }
    raw_before_assistant = [
        record("developer", "instructions"),
        record("user", "original project listing request"),
        record("assistant", "very long project listing"),
        record("user", "web_sever能继续拆吗"),
    ]
    edited = [
        record("developer", "instructions"),
        record("user", "compressed project listing"),
        record("user", "web_sever能继续拆吗"),
        record("assistant", "我先看一下 web_server.py 的体量和职责边界"),
    ]
    mirrored = [
        *raw_before_assistant,
        record("assistant", "我先看一下 web_server.py 的体量和职责边界"),
    ]
    source = [
        *raw_before_assistant,
        proxy_server.transcript_record(
            "assistant",
            "我先看一下 web_server.py 的体量和职责边界",
            [
                proxy_server.provider_message("assistant", "我先看一下 web_server.py 的体量和职责边界"),
                call_item,
                output_item,
            ],
        ),
    ]

    merged = proxy_server.merge_override_transcript(edited, mirrored, source)

    assert provider_texts(merged) == [
        "instructions",
        "compressed project listing",
        "web_sever能继续拆吗",
        "我先看一下 web_server.py 的体量和职责边界",
    ]
    assert provider_texts(merged).count("web_sever能继续拆吗") == 1
    assert any(
        item.get("type") == "function_call_output"
        and item.get("output") == "web_server.py 3006 lines"
        for item in merged[-1].get("providerItems", [])
    )



def test_override_base_advances_so_source_tail_is_merged_once() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        raw_base = [
            record("assistant", "ready"),
            record("user", "T01 task"),
            record("assistant", "T01 answer"),
            record("user", "T02 task"),
            record("assistant", "T02 answer"),
        ]
        edited_base = [
            record("assistant", "ready"),
            record("user", "compressed T01-T02"),
        ]
        store.sessions[SESSION_ID] = proxy_server.ProxySession(
            id=SESSION_ID,
            title="Codex fake",
            transcript=proxy_server.clean_transcript(raw_base),
        )
        store.override(SESSION_ID, edited_base)

        first_source = [*raw_base, record("user", "T04 task")]
        first_body = {"input": proxy_server.transcript_to_input_items(first_source)}
        store.begin_request(SESSION_ID, first_body, {"x-codex-session-id": SESSION_ID})
        session = store.sessions[SESSION_ID]

        assert provider_texts(session.edited_transcript or []) == [
            "ready",
            "compressed T01-T02",
            "T04 task",
        ]
        assert provider_texts(session.override_base_transcript or []) == provider_texts(first_source)

        store.begin_request(SESSION_ID, first_body, {"x-codex-session-id": SESSION_ID})
        assert provider_texts(store.sessions[SESSION_ID].edited_transcript or []) == [
            "ready",
            "compressed T01-T02",
            "T04 task",
        ]

        store.complete_response(
            SESSION_ID,
            [proxy_server.provider_message("assistant", "T04 answer")],
            "T04 answer",
        )
        completed = store.sessions[SESSION_ID]
        assert provider_texts(completed.edited_transcript or []) == [
            "ready",
            "compressed T01-T02",
            "T04 task",
            "T04 answer",
        ]
        assert provider_texts(completed.override_base_transcript or []) == [
            *provider_texts(first_source),
            "T04 answer",
        ]

        next_source = [*first_source, record("assistant", "T04 answer"), record("user", "T05 task")]
        next_body = {"input": proxy_server.transcript_to_input_items(next_source)}
        store.begin_request(SESSION_ID, next_body, {"x-codex-session-id": SESSION_ID})

        assert provider_texts(store.sessions[SESSION_ID].edited_transcript or []) == [
            "ready",
            "compressed T01-T02",
            "T04 task",
            "T04 answer",
            "T05 task",
        ]



def test_context_node_details_return_editable_blocks_without_provider_payload() -> None:
    long_output = "tool-output-" + "X" * 4000
    transcript = web_server.normalize_transcript(
        [
            {
                "role": "assistant",
                "text": "done",
                "attachments": [],
                "toolEvents": [],
                "blocks": [],
                "providerItems": [
                    {
                        "type": "function_call",
                        "call_id": TOOL_CALL_ID,
                        "name": "shell_command",
                        "arguments": json.dumps({"command": "Get-ChildItem"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": TOOL_CALL_ID,
                        "output": long_output,
                    },
                    {"type": "message", "role": "assistant", "content": "done"},
                ],
            }
        ]
    )

    detail = web_server.context_record_details_payload(transcript[0], node_number=1)
    encoded = json.dumps(detail, ensure_ascii=False)

    assert "text" not in detail
    assert "provider_items" not in detail
    assert "items" not in detail
    assert detail["content_source"] == "blocks"
    assert encoded.count(long_output) == 1
    tool_block = detail["blocks"][0]
    text_block = detail["blocks"][1]
    assert tool_block["kind"] == "tool"
    assert tool_block["call_item_ref"] == "node:1:item:1"
    assert tool_block["output_item_ref"] == "node:1:item:2"
    assert tool_block["arguments_ref"] == "block:1.arguments"
    assert tool_block["output_ref"] == "block:1.output"
    assert tool_block["output"] == long_output
    assert text_block["kind"] == "text"
    assert text_block["content_ref"] == "block:2.content"
    assert text_block["item_ref"] == "node:1:item:3"
    assert text_block["content"] == "done"


def test_context_workbench_batch_replaces_tool_outputs_compactly() -> None:
    old_output_one = "A" * 4000
    old_output_two = "B" * 3000
    transcript = web_server.normalize_transcript(
        [
            {
                "role": "assistant",
                "text": "",
                "attachments": [],
                "toolEvents": [],
                "blocks": [],
                "providerItems": [
                    {
                        "type": "function_call",
                        "call_id": "call_one",
                        "name": "shell_command",
                        "arguments": json.dumps({"command": "Get-Date"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_one",
                        "output": old_output_one,
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_two",
                        "name": "shell_command",
                        "arguments": json.dumps({"command": "Get-ChildItem"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_two",
                        "output": old_output_two,
                    },
                    {"type": "message", "role": "assistant", "content": "done"},
                ],
            }
        ]
    )
    draft = web_server.ContextWorkbenchDraft(transcript, [0])
    registry = web_server.ContextWorkbenchToolRegistry(draft)

    execution = registry.execute(
        "edit_context_items",
        {
            "selector": {"tool_output_only": True},
            "operation": {"type": "replace_content", "content": "1"},
            "reason": "replace bulky outputs",
        },
    )
    payload = json.loads(execution.output_text)
    committed = draft.committed_transcript()
    provider_items = committed[0]["providerItems"]

    assert execution.status == "completed"
    assert payload["payload_kind"] == "batch_mutation_result"
    assert payload["matched_count"] == 2
    assert payload["changed_count"] == 2
    assert payload["token_delta_estimate"]["saved"] > 0
    assert [item.get("type") for item in provider_items] == [
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert provider_items[1]["call_id"] == "call_one"
    assert provider_items[1]["output"] == "1"
    assert provider_items[3]["call_id"] == "call_two"
    assert provider_items[3]["output"] == "1"
    assert [event["raw_output"] for event in committed[0]["toolEvents"]] == ["1", "1"]
    assert old_output_one not in execution.output_text
    assert old_output_two not in execution.output_text
    assert len(execution.output_text) < 8000
    assert len(draft.operations) == 1


def test_context_workbench_batch_deletes_tool_pairs_compactly() -> None:
    transcript = web_server.normalize_transcript(
        [
            {
                "role": "assistant",
                "text": "",
                "attachments": [],
                "toolEvents": [],
                "blocks": [],
                "providerItems": [
                    {"type": "message", "role": "assistant", "content": "before"},
                    {
                        "type": "function_call",
                        "call_id": "call_one",
                        "name": "shell_command",
                        "arguments": json.dumps({"command": "Get-Date"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_one",
                        "output": "2026-05-05",
                    },
                    {
                        "type": "custom_tool_call",
                        "call_id": "call_two",
                        "name": "apply_patch",
                        "input": "*** Begin Patch\n*** End Patch",
                    },
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "call_two",
                        "output": "patched",
                    },
                    {"type": "message", "role": "assistant", "content": "after"},
                ],
            }
        ]
    )
    draft = web_server.ContextWorkbenchDraft(transcript, [0])
    registry = web_server.ContextWorkbenchToolRegistry(draft)

    execution = registry.execute(
        "edit_context_items",
        {
            "selector": {"tool_call_only": True},
            "operation": {"type": "delete"},
            "reason": "remove tool traces",
        },
    )
    payload = json.loads(execution.output_text)
    committed = draft.committed_transcript()
    provider_items = committed[0]["providerItems"]

    assert execution.status == "completed"
    assert payload["matched_count"] == 2
    assert payload["changed_count"] == 4
    assert [item.get("type") for item in provider_items] == ["message", "message"]
    assert committed[0]["toolEvents"] == []
    assert committed[0]["text"] == "beforeafter"
    assert len(execution.output_text) < 6000


def test_context_workbench_tool_schemas_have_valid_array_shapes() -> None:
    registry = web_server.ContextWorkbenchToolRegistry(web_server.ContextWorkbenchDraft([], []))
    missing_items: list[str] = []
    union_types: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            raw_type = value.get("type")
            if raw_type == "array" and "items" not in value:
                missing_items.append(path)
            if isinstance(raw_type, list):
                union_types.append(path)
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    for schema in registry.schemas:
        walk(schema, schema.get("name") or "tool")

    assert missing_items == []
    assert union_types == []


def test_sse_completed_output_replaces_added_item_skeleton() -> None:
    response_items: list[dict[str, Any]] = []
    text_parts: list[str] = []
    buffer = (
        'data: {"type":"response.output_item.added","item":{"type":"message","role":"assistant","content":[]}}\n\n'
        'data: {"type":"response.output_text.delta","delta":"final answer"}\n\n'
        'data: {"type":"response.completed","response":{"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"final answer"}]}]}}\n\n'
    )

    remainder = proxy_server.parse_sse_buffer(buffer, response_items, text_parts)

    assert remainder == ""
    assert text_parts == ["final answer"]
    assert response_items == [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "final answer"}],
        }
    ]


def test_sse_output_item_done_updates_function_call_arguments() -> None:
    response_items: list[dict[str, Any]] = []
    text_parts: list[str] = []
    buffer = (
        'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"shell_command","arguments":"","status":"in_progress"}}\n\n'
        'data: {"type":"response.function_call_arguments.delta","output_index":0,"item_id":"fc_1","delta":"{\\"command\\":"}\n\n'
        'data: {"type":"response.function_call_arguments.delta","output_index":0,"item_id":"fc_1","delta":"\\"Get-Date\\"}"}\n\n'
        'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"shell_command","arguments":"{\\"command\\":\\"Get-Date\\"}","status":"completed"}}\n\n'
    )

    remainder = proxy_server.parse_sse_buffer(buffer, response_items, text_parts)

    assert remainder == ""
    assert response_items == [
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "shell_command",
            "arguments": '{"command":"Get-Date"}',
            "status": "completed",
        }
    ]


def test_override_tool_output_requests_are_passed_through() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        edited_transcript = [
            proxy_server.transcript_record(
                "user",
                "edited context",
                [proxy_server.provider_message("user", "edited context")],
            )
        ]
        store.override(SESSION_ID, edited_transcript)
        body = {
            "input": [
                proxy_server.provider_message("user", "run the date"),
                {
                    "type": "function_call",
                    "call_id": TOOL_CALL_ID,
                    "name": "shell_command",
                    "arguments": json.dumps({"command": "Get-Date"}),
                },
                {
                    "type": "function_call_output",
                    "call_id": TOOL_CALL_ID,
                    "output": "2026-05-05 23:23:59 +08:00",
                },
            ],
            "previous_response_id": "resp_tool_turn",
        }

        _session, forwarded = store.begin_request(SESSION_ID, body, {"x-codex-session-id": SESSION_ID})

        forwarded_text = json.dumps(forwarded, ensure_ascii=False)
        assert forwarded != body
        assert "edited context" in forwarded_text
        assert "run the date" in forwarded_text
        assert "2026-05-05 23:23:59 +08:00" in forwarded_text
        assert "previous_response_id" not in forwarded
        store.complete_response(
            SESSION_ID,
            [proxy_server.provider_message("assistant", "done")],
            "done",
        )
        completed = store.get_session(SESSION_ID)
        assert completed is not None
        visible = completed["transcript"]
        assert [item.get("type") for item in visible[-1]["providerItems"]] == [
            "function_call",
            "function_call_output",
            "message",
        ]
        tool_event = visible[-1]["toolEvents"][0]
        assert tool_event["raw_output"] == "2026-05-05 23:23:59 +08:00"
        assert tool_event["display_result"] == "2026-05-05 23:23:59 +08:00"
        assert tool_event["display_detail"] == "Get-Date"
        request_log = proxy_server.load_jsonl_state(
            store._session_dir(store.sessions[SESSION_ID]) / "request_log.jsonl",
            [],
        )
        assert request_log[-1]["kind"] == "override_tool_output_rewrite"


def test_proxy_override_deleted_tools_are_not_reintroduced_by_next_request() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        old_assistant_items = [
            proxy_server.provider_message("assistant", PRE_TOOL_TEXT),
            {
                "type": "function_call",
                "call_id": TOOL_CALL_ID,
                "name": "shell_command",
                "arguments": json.dumps({"command": "Get-ChildItem -Force"}),
            },
            {
                "type": "function_call_output",
                "call_id": TOOL_CALL_ID,
                "output": "very long stale output",
            },
            proxy_server.provider_message("assistant", FINAL_TEXT),
        ]
        old_transcript = [
            proxy_server.transcript_record(
                "user",
                USER_PROMPT,
                [proxy_server.provider_message("user", USER_PROMPT)],
            ),
            proxy_server.transcript_record(
                "assistant",
                f"{PRE_TOOL_TEXT}\n\n{FINAL_TEXT}",
                old_assistant_items,
            ),
        ]
        edited_transcript = [
            old_transcript[0],
            proxy_server.transcript_record(
                "assistant",
                f"{PRE_TOOL_TEXT}\n\n{FINAL_TEXT}",
                [
                    proxy_server.provider_message("assistant", PRE_TOOL_TEXT),
                    proxy_server.provider_message("assistant", FINAL_TEXT),
                ],
            ),
        ]
        store.sessions[SESSION_ID] = proxy_server.ProxySession(
            id=SESSION_ID,
            title="Codex fake",
            transcript=proxy_server.clean_transcript(old_transcript),
        )
        store.save()
        store.override(SESSION_ID, edited_transcript)
        next_body = {
            "input": [
                *proxy_server.transcript_to_input_items(old_transcript),
                proxy_server.provider_message("user", "continue from edited context"),
            ],
            "previous_response_id": "resp_from_stale_codex_input",
        }

        _session, forwarded = store.begin_request(
            SESSION_ID,
            next_body,
            {"x-codex-session-id": SESSION_ID},
        )

        serialized = json.dumps(forwarded, ensure_ascii=False)
        assert "very long stale output" not in serialized
        assert "function_call" not in serialized
        assert "continue from edited context" in serialized
        assert "previous_response_id" not in forwarded
        assert store.sessions[SESSION_ID].request_log[-1]["kind"] == "override_rewrite"


def test_proxy_override_reinjects_fresh_initial_context_after_compact() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        compacted_transcript = [
            proxy_server.transcript_record(
                "user",
                "first real user message",
                [proxy_server.provider_message("user", "first real user message")],
            ),
            proxy_server.transcript_record(
                "user",
                f"{proxy_server.LOCAL_COMPACT_SUMMARY_PREFIX}\n\nprevious compact summary",
                [
                    proxy_server.provider_message(
                        "user",
                        f"{proxy_server.LOCAL_COMPACT_SUMMARY_PREFIX}\n\nprevious compact summary",
                    )
                ],
            ),
        ]
        store.sessions[SESSION_ID] = proxy_server.ProxySession(
            id=SESSION_ID,
            title="Codex fake",
            transcript=proxy_server.clean_transcript(compacted_transcript),
            edited_transcript=[
                proxy_server.transcript_record(
                    "developer",
                    "stale developer instructions",
                    [proxy_server.provider_message("developer", "stale developer instructions")],
                ),
                proxy_server.transcript_record(
                    "user",
                    "<environment_context><cwd>/stale</cwd></environment_context>",
                    [
                        proxy_server.provider_message(
                            "user",
                            "<environment_context><cwd>/stale</cwd></environment_context>",
                        )
                    ],
                ),
                *compacted_transcript,
            ],
            status="override",
        )
        next_body = {
            "input": [
                proxy_server.provider_message("developer", "fresh developer instructions"),
                proxy_server.provider_message(
                    "user",
                    "<environment_context><cwd>/fresh</cwd></environment_context>",
                ),
                *proxy_server.transcript_to_input_items(compacted_transcript),
                proxy_server.provider_message("developer", "plan mode developer instructions"),
                proxy_server.provider_message("user", "continue after compact"),
            ],
            "previous_response_id": "resp_from_stale_codex_input",
        }

        _session, forwarded = store.begin_request(
            SESSION_ID,
            next_body,
            {"x-codex-session-id": SESSION_ID},
        )

        forwarded_transcript = proxy_server.input_items_to_transcript(forwarded["input"])
        assert [record["role"] for record in forwarded_transcript] == [
            "developer",
            "user",
            "user",
            "user",
            "developer",
            "user",
        ]
        texts = [record["text"] for record in forwarded_transcript]
        assert texts[:2] == [
            "fresh developer instructions",
            "<environment_context><cwd>/fresh</cwd></environment_context>",
        ]
        assert texts[-2:] == ["plan mode developer instructions", "continue after compact"]
        assert "stale developer instructions" not in texts
        assert "<environment_context><cwd>/stale</cwd></environment_context>" not in texts
        assert "previous_response_id" not in forwarded
        assert store.sessions[SESSION_ID].request_log[-1]["kind"] == "override_rewrite"


def test_proxy_override_preserves_injected_context_before_latest_user_on_mismatch() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        edited_compacted_transcript = [
            proxy_server.transcript_record(
                "user",
                "first real user message",
                [proxy_server.provider_message("user", "first real user message")],
            ),
            proxy_server.transcript_record(
                "user",
                f"{proxy_server.LOCAL_COMPACT_SUMMARY_PREFIX}\n\nedited compact summary",
                [
                    proxy_server.provider_message(
                        "user",
                        f"{proxy_server.LOCAL_COMPACT_SUMMARY_PREFIX}\n\nedited compact summary",
                    )
                ],
            ),
        ]
        stale_source_compacted_transcript = [
            edited_compacted_transcript[0],
            proxy_server.transcript_record(
                "user",
                f"{proxy_server.LOCAL_COMPACT_SUMMARY_PREFIX}\n\nstale source compact summary",
                [
                    proxy_server.provider_message(
                        "user",
                        f"{proxy_server.LOCAL_COMPACT_SUMMARY_PREFIX}\n\nstale source compact summary",
                    )
                ],
            ),
        ]
        store.sessions[SESSION_ID] = proxy_server.ProxySession(
            id=SESSION_ID,
            title="Codex fake",
            transcript=proxy_server.clean_transcript(edited_compacted_transcript),
            edited_transcript=proxy_server.clean_transcript(edited_compacted_transcript),
            status="override",
        )
        next_body = {
            "input": [
                *proxy_server.transcript_to_input_items(stale_source_compacted_transcript),
                proxy_server.provider_message("developer", "fresh developer instructions"),
                proxy_server.provider_message(
                    "user",
                    "<environment_context><cwd>/fresh</cwd></environment_context>",
                ),
                proxy_server.provider_message("user", "continue after compact"),
            ],
            "previous_response_id": "resp_from_mismatched_codex_input",
        }

        _session, forwarded = store.begin_request(
            SESSION_ID,
            next_body,
            {"x-codex-session-id": SESSION_ID},
        )

        forwarded_transcript = proxy_server.input_items_to_transcript(forwarded["input"])
        assert [record["role"] for record in forwarded_transcript] == [
            "user",
            "user",
            "developer",
            "user",
            "user",
        ]
        texts = [record["text"] for record in forwarded_transcript]
        assert texts[1].endswith("edited compact summary")
        assert "stale source compact summary" not in json.dumps(forwarded, ensure_ascii=False)
        assert texts[-3:] == [
            "fresh developer instructions",
            "<environment_context><cwd>/fresh</cwd></environment_context>",
            "continue after compact",
        ]
        assert "previous_response_id" not in forwarded


def test_context_sync_writes_known_proxy_override() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        state_path = Path(temp_dir) / "proxy_state.json"
        marker_path = Path(temp_dir) / "context_edit_markers.json"
        original_store = proxy_server.STORE
        original_proxy_base_url = web_server.CODEX_PROXY_BASE_URL
        original_proxy_state_file = web_server.PROXY_STATE_FILE
        original_marker_file = web_server.CONTEXT_EDIT_MARKERS_FILE

        proxy_server.STORE = proxy_server.ProxyStore(state_path)
        proxy = start_server(proxy_server.Handler)
        web_server.CODEX_PROXY_BASE_URL = f"http://127.0.0.1:{proxy.server_port}/v1"
        web_server.PROXY_STATE_FILE = state_path
        web_server.CONTEXT_EDIT_MARKERS_FILE = marker_path

        try:
            old_transcript = [
                proxy_server.transcript_record(
                    "user",
                    USER_PROMPT,
                    [proxy_server.provider_message("user", USER_PROMPT)],
                ),
                proxy_server.transcript_record(
                    "assistant",
                    FINAL_TEXT,
                    [
                        {
                            "type": "function_call",
                            "call_id": TOOL_CALL_ID,
                            "name": "shell_command",
                            "arguments": json.dumps({"command": "Get-ChildItem -Force"}),
                        },
                        {
                            "type": "function_call_output",
                            "call_id": TOOL_CALL_ID,
                            "output": "stale output",
                        },
                        proxy_server.provider_message("assistant", FINAL_TEXT),
                    ],
                ),
            ]
            proxy_server.STORE.sessions[SESSION_ID] = proxy_server.ProxySession(
                id=SESSION_ID,
                title="Codex fake",
                transcript=proxy_server.clean_transcript(old_transcript),
            )
            proxy_server.STORE.save()

            class FakeSession:
                pass

            fake_session = FakeSession()
            fake_session.session_id = SESSION_ID
            fake_session.context_revisions = []
            edited_transcript = [
                old_transcript[0],
                proxy_server.transcript_record(
                    "assistant",
                    FINAL_TEXT,
                    [proxy_server.provider_message("assistant", FINAL_TEXT)],
                ),
            ]

            result = web_server.sync_proxy_session_override_if_known(fake_session, edited_transcript)

            assert result["status"] == "synced"
            assert result["changed"] is True
            assert result["has_override"] is True
            payload = proxy_server.STORE.get_session(SESSION_ID)
            assert payload is not None
            serialized = json.dumps(payload["transcript"], ensure_ascii=False)
            assert "stale output" not in serialized
            assert "function_call" not in serialized
            markers = json.loads(marker_path.read_text(encoding="utf-8"))
            assert markers[SESSION_ID]["node_count"] == 2
        finally:
            proxy.shutdown()
            proxy.server_close()
            web_server.CODEX_PROXY_BASE_URL = original_proxy_base_url
            web_server.PROXY_STATE_FILE = original_proxy_state_file
            web_server.CONTEXT_EDIT_MARKERS_FILE = original_marker_file
            proxy_server.STORE = original_store


def test_compaction_summary_visible_text_without_encrypted_content() -> None:
    transcript = proxy_server.input_items_to_transcript(
        [
            {
                "type": "compaction_summary",
                "summary": [{"type": "summary_text", "text": REMOTE_SUMMARY_TEXT}],
                "encrypted_content": REMOTE_COMPACTION_BLOB,
            }
        ]
    )

    assert len(transcript) == 1
    assert transcript[0]["role"] == "compaction"
    assert transcript[0]["text"] == REMOTE_SUMMARY_TEXT
    assert transcript[0]["blocks"] == [{"kind": "text", "text": REMOTE_SUMMARY_TEXT}]
    assert REMOTE_COMPACTION_BLOB not in transcript[0]["text"]


def test_compaction_visible_text_falls_back_to_encrypted_content() -> None:
    transcript = proxy_server.input_items_to_transcript(
        [
            {
                "type": "compaction",
                "encrypted_content": REMOTE_COMPACTION_BLOB,
            }
        ]
    )

    assert len(transcript) == 1
    assert transcript[0]["role"] == "compaction"
    assert transcript[0]["text"] == REMOTE_COMPACTION_BLOB
    assert transcript[0]["blocks"] == [{"kind": "text", "text": REMOTE_COMPACTION_BLOB}]


def test_local_compact_response_replaces_transcript_with_readable_summary() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        compact_prompt = (
            proxy_server.LOCAL_COMPACT_PROMPT_PREFIX
            + "\n\nInclude current progress and key decisions."
        )
        body = {
            "input": [
                proxy_server.provider_message("developer", "developer instructions"),
                proxy_server.provider_message(
                    "user",
                    "<environment_context><cwd>/tmp</cwd></environment_context>",
                ),
                proxy_server.provider_message("user", "first real user message"),
                proxy_server.provider_message(
                    "assistant",
                    "assistant answer that should be summarized",
                ),
                proxy_server.provider_message("user", compact_prompt),
            ]
        }

        _session, forwarded = store.begin_request(SESSION_ID, body, {"x-codex-session-id": SESSION_ID})
        session = store.get_session(SESSION_ID)

        assert session is not None
        assert session["status"] == "compacting"
        assert forwarded["input"][:-1] == body["input"][:-1]
        assert message_text(forwarded["input"][-1]) == proxy_server.MANUAL_LOCAL_COMPACT_PROMPT
        assert len(session["transcript"]) == 4
        assert session["transcript"][-1]["text"] == "assistant answer that should be summarized"

        store.complete_response(
            SESSION_ID,
            [proxy_server.provider_message("assistant", REMOTE_SUMMARY_TEXT)],
            REMOTE_SUMMARY_TEXT,
        )
        completed = store.get_session(SESSION_ID)

        assert completed is not None
        assert completed["status"] == "mirror"
        assert [record["role"] for record in completed["transcript"]] == ["user", "user"]
        assert completed["transcript"][0]["text"] == "first real user message"
        assert completed["transcript"][1]["text"].startswith(
            f"{proxy_server.LOCAL_COMPACT_SUMMARY_PREFIX}\n\n"
        )
        assert REMOTE_SUMMARY_TEXT in completed["transcript"][1]["text"]
        assert "assistant answer that should be summarized" not in json.dumps(
            completed["transcript"],
            ensure_ascii=False,
        )


def test_local_compact_keeps_recent_user_messages_with_token_budget() -> None:
    previous_limit = proxy_server.LOCAL_COMPACT_USER_MESSAGE_MAX_TOKENS
    try:
        proxy_server.LOCAL_COMPACT_USER_MESSAGE_MAX_TOKENS = 4
        transcript = [
            record("user", "older user message that should be dropped"),
            record("assistant", "assistant output that should be summarized"),
            record("user", "middle12"),
            record("user", "latest12"),
        ]

        compacted = proxy_server.local_compacted_transcript(transcript, REMOTE_SUMMARY_TEXT)

        assert provider_texts(compacted) == [
            "middle12",
            "latest12",
            f"{proxy_server.LOCAL_COMPACT_SUMMARY_PREFIX}\n\n{REMOTE_SUMMARY_TEXT}",
        ]
    finally:
        proxy_server.LOCAL_COMPACT_USER_MESSAGE_MAX_TOKENS = previous_limit


def test_local_compact_truncates_oldest_selected_user_message_at_budget() -> None:
    previous_limit = proxy_server.LOCAL_COMPACT_USER_MESSAGE_MAX_TOKENS
    try:
        proxy_server.LOCAL_COMPACT_USER_MESSAGE_MAX_TOKENS = 3
        transcript = [
            record("user", "older user message"),
            record("user", "latest12"),
        ]

        compacted = proxy_server.local_compacted_transcript(transcript, REMOTE_SUMMARY_TEXT)

        assert provider_texts(compacted)[:2] == ["olde", "latest12"]
    finally:
        proxy_server.LOCAL_COMPACT_USER_MESSAGE_MAX_TOKENS = previous_limit


def test_context_workbench_compressed_nodes_stay_independent_after_cleaning() -> None:
    transcript = [
        proxy_server.transcript_record(
            "assistant",
            "previous assistant answer",
            [proxy_server.provider_message("assistant", "previous assistant answer")],
        ),
        proxy_server.transcript_record(
            "user",
            "user follow-up",
            [proxy_server.provider_message("user", "user follow-up")],
        ),
        proxy_server.transcript_record(
            "assistant",
            "assistant follow-up",
            [proxy_server.provider_message("assistant", "assistant follow-up")],
        ),
    ]
    draft = web_server.ContextWorkbenchDraft(web_server.normalize_transcript(transcript), [1, 2])
    nodes = draft._nodes_by_number([2, 3])

    draft.compress_nodes(
        nodes,
        summary_markdown="用户询问是否有压缩摘要；助手确认有。",
        style="tight summary",
        title="",
    )
    committed = draft.committed_transcript()
    cleaned = proxy_server.clean_transcript(committed)

    assert [record["role"] for record in cleaned] == ["assistant", "user"]
    assert cleaned[0]["text"] == "previous assistant answer"
    assert cleaned[1]["text"] == "用户询问是否有压缩摘要；助手确认有。"
    assert "assistant follow-up" not in json.dumps(cleaned, ensure_ascii=False)


def test_context_workbench_compress_nodes_replaces_tool_heavy_node() -> None:
    tool_output = "very long frontend scan output"
    assistant_items = [
        proxy_server.provider_message("assistant", "I will inspect the frontend."),
        {
            "type": "function_call",
            "call_id": TOOL_CALL_ID,
            "name": "shell_command",
            "arguments": json.dumps({"command": "Get-ChildItem react_app -Force"}),
        },
        {
            "type": "function_call_output",
            "call_id": TOOL_CALL_ID,
            "output": tool_output,
        },
        proxy_server.provider_message("assistant", "Frontend is React + Vite."),
    ]
    transcript = [
        proxy_server.transcript_record(
            "user",
            "inspect frontend",
            [proxy_server.provider_message("user", "inspect frontend")],
        ),
        proxy_server.transcript_record(
            "assistant",
            "I will inspect the frontend.\n\nFrontend is React + Vite.",
            assistant_items,
        ),
    ]
    draft = web_server.ContextWorkbenchDraft(web_server.normalize_transcript(transcript), [1])
    nodes = draft._nodes_by_number([2])

    draft.compress_nodes(
        nodes,
        summary_markdown="Frontend discussion compressed: React + Vite.",
        style="tight summary",
        title="",
    )
    committed = proxy_server.clean_transcript(draft.committed_transcript())
    serialized = json.dumps(committed, ensure_ascii=False)

    assert [record["role"] for record in committed] == ["user", "user"]
    assert committed[1]["text"] == "Frontend discussion compressed: React + Vite."
    assert "function_call" not in serialized
    assert tool_output not in serialized
    assert "I will inspect the frontend" not in serialized


def test_context_workbench_hides_internal_prefix_nodes_from_editing() -> None:
    transcript = web_server.normalize_transcript(
        [
            proxy_server.transcript_record(
                "developer",
                "developer instructions",
                [proxy_server.provider_message("developer", "developer instructions")],
            ),
            proxy_server.transcript_record(
                "user",
                "<environment_context><cwd>/tmp</cwd></environment_context>",
                [proxy_server.provider_message("user", "<environment_context><cwd>/tmp</cwd></environment_context>")],
            ),
            proxy_server.transcript_record(
                "user",
                "first real user message",
                [proxy_server.provider_message("user", "first real user message")],
            ),
            proxy_server.transcript_record(
                "assistant",
                "assistant answer",
                [proxy_server.provider_message("assistant", "assistant answer")],
            ),
        ]
    )

    class FakeSession:
        pass

    fake_session = FakeSession()
    fake_session.title = "Fake"
    fake_session.scope = "chat"
    fake_session.transcript = transcript

    draft = web_server.ContextWorkbenchDraft(transcript, [0, 1, 2])

    assert draft.selected_node_numbers == [1]
    assert [item["node_number"] for item in draft.current_overview_items()] == [1, 2]
    assert [item["role"] for item in draft.current_overview_items()] == ["user", "assistant"]

    snapshot = web_server.build_context_workspace_snapshot(fake_session, selected_indexes=[0, 1, 2])
    assert "当前节点数：2" in snapshot
    assert "当前选中节点：1" in snapshot
    assert "developer instructions" not in snapshot
    assert "<environment_context>" not in snapshot
    assert "- Node #1 | user" in snapshot

    suggestions = web_server.context_workbench_suggestions_payload(fake_session)
    assert [item["node_number"] for item in suggestions["nodes"]] == [1, 2]
    assert suggestions["stats"]["total_token_count"] > sum(
        item["token_count"] for item in suggestions["nodes"]
    )

    committed = draft.committed_transcript()
    assert [record["role"] for record in committed] == ["developer", "user", "user", "assistant"]
    assert committed[0]["text"] == "developer instructions"
    assert committed[1]["text"].startswith("<environment_context>")


def test_context_workbench_prompt_cache_key_is_stable_and_bounded() -> None:
    session_id = "019e392a-9e23-7a33-badc-e5862b781d3f"

    assert web_server.context_workbench_prompt_cache_key(session_id) == f"hash-context:{session_id}"

    unsafe_key = web_server.context_workbench_prompt_cache_key(" session/with spaces/and/slashes " + "x" * 80)
    assert unsafe_key.startswith("hash-context:session-with-spaces-and-slashes")
    assert len(unsafe_key) <= 61
    assert "/" not in unsafe_key
    assert " " not in unsafe_key


def test_internal_context_reuses_codex_session_headers() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        store = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        turn_metadata = json.dumps(
            {
                "session_id": SESSION_ID,
                "thread_id": SESSION_ID,
                "turn_id": "turn-1",
            }
        )
        store.begin_request(
            SESSION_ID,
            {"input": [proxy_server.provider_message("user", "hello")]},
            {
                "authorization": "Bearer real-token",
                "chatgpt-account-id": "account-1",
                "session-id": SESSION_ID,
                "thread-id": SESSION_ID,
                "x-client-request-id": SESSION_ID,
                "x-codex-turn-metadata": turn_metadata,
                "x-codex-window-id": f"{SESSION_ID}:0",
            },
        )

        internal_headers = proxy_server.merge_codex_session_headers(
            {"authorization": "Bearer not-needed", "x-hash-context-session-id": SESSION_ID},
            store.codex_session_headers(SESSION_ID),
            session_id=SESSION_ID,
        )
        upstream_headers = proxy_server.response_headers_for_upstream(internal_headers)

        assert upstream_headers["session-id"] == SESSION_ID
        assert upstream_headers["thread-id"] == SESSION_ID
        assert upstream_headers["x-client-request-id"] == SESSION_ID
        assert upstream_headers["x-codex-turn-metadata"] == turn_metadata
        assert upstream_headers["x-codex-window-id"] == f"{SESSION_ID}:0"
        assert "x-hash-context-session-id" not in {key.lower() for key in upstream_headers}


def test_internal_context_synthesizes_codex_session_headers_for_old_sessions() -> None:
    internal_headers = proxy_server.merge_codex_session_headers(
        {"authorization": "Bearer not-needed", "x-hash-context-session-id": SESSION_ID},
        {},
        session_id=SESSION_ID,
    )
    upstream_headers = proxy_server.response_headers_for_upstream(internal_headers)

    assert upstream_headers["session-id"] == SESSION_ID
    assert upstream_headers["thread-id"] == SESSION_ID
    assert upstream_headers["x-client-request-id"] == SESSION_ID
    assert json.loads(upstream_headers["x-codex-turn-metadata"])["thread_id"] == SESSION_ID
    assert "x-hash-context-session-id" not in {key.lower() for key in upstream_headers}


def main() -> None:
    test_drop_unpaired_tool_items_preserves_only_complete_pairs()
    test_compact_without_override_preserves_codex_input()
    test_compact_override_reinjects_fresh_initial_context()
    test_request_without_override_preserves_codex_body()
    test_proxy_usage_summary_records_and_resets_by_session()
    test_proxy_store_can_save_only_one_session()
    test_proxy_usage_update_does_not_rewrite_session_payload()
    test_proxy_usage_routes_record_main_and_context_workbench()
    test_sse_completed_response_captures_usage_payload()
    test_local_compact_without_override_replaces_manual_prompt()
    test_replacement_compact_prompts_still_count_as_compact_prompts()
    test_clean_transcript_keeps_latest_local_compact_summary()
    test_local_auto_compact_replaces_prompt_with_concise_continuation_prompt()
    test_local_compact_prompt_replacement_preserves_input_item_shape()
    test_same_turn_local_compact_with_assistant_text_uses_auto_prompt()
    test_new_turn_local_compact_with_assistant_text_uses_manual_prompt()
    test_running_status_alone_does_not_force_auto_compact_prompt()
    test_override_local_compact_replaces_prompt_and_removes_previous_response_id()
    test_assistant_provider_items_are_deduped_by_logical_tool_identity()
    test_auto_compact_summary_is_not_duplicated_during_override_tool_continuations()
    test_tool_turn_stays_single_assistant_record()
    test_codex_response_item_types_roundtrip()
    test_shell_tool_output_display_metadata_is_reconstructed()
    test_web_normalize_rebuilds_tool_display_from_provider_items()
    test_override_merge_does_not_reappend_existing_latest_turn_tail()
    test_override_merge_updates_latest_assistant_without_readding_user_turn()
    test_override_base_advances_so_source_tail_is_merged_once()
    test_context_node_details_return_editable_blocks_without_provider_payload()
    test_context_workbench_compresses_tool_output_without_duplicate_tool()
    test_context_workbench_rejects_tool_output_call_id_drift()
    test_context_workbench_deletes_multiple_tool_items_atomically()
    test_context_workbench_finds_tool_outputs_lightly()
    test_context_workbench_batch_replaces_tool_outputs_compactly()
    test_context_workbench_batch_deletes_tool_pairs_compactly()
    test_context_workbench_tool_schemas_have_valid_array_shapes()
    test_sse_completed_output_replaces_added_item_skeleton()
    test_sse_output_item_done_updates_function_call_arguments()
    test_override_tool_output_requests_are_passed_through()
    test_proxy_override_deleted_tools_are_not_reintroduced_by_next_request()
    test_proxy_override_reinjects_fresh_initial_context_after_compact()
    test_proxy_override_preserves_injected_context_before_latest_user_on_mismatch()
    test_context_sync_writes_known_proxy_override()
    test_compaction_summary_visible_text_without_encrypted_content()
    test_compaction_visible_text_falls_back_to_encrypted_content()
    test_local_compact_response_replaces_transcript_with_readable_summary()
    test_local_compact_keeps_recent_user_messages_with_token_budget()
    test_local_compact_truncates_oldest_selected_user_message_at_budget()
    test_context_workbench_compressed_nodes_stay_independent_after_cleaning()
    test_context_workbench_compress_nodes_replaces_tool_heavy_node()
    test_context_workbench_hides_internal_prefix_nodes_from_editing()
    test_context_workbench_prompt_cache_key_is_stable_and_bounded()
    test_internal_context_reuses_codex_session_headers()
    test_internal_context_synthesizes_codex_session_headers_for_old_sessions()

    with tempfile.TemporaryDirectory() as temp_dir:
        upstream = start_server(MockModelsUpstream)
        proxy_server.CHATGPT_UPSTREAM_BASE_URL = f"http://127.0.0.1:{upstream.server_port}/backend-api/codex"
        proxy_server.OPENAI_UPSTREAM_BASE_URL = f"http://127.0.0.1:{upstream.server_port}/v1"
        proxy_server.STORE = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        proxy = start_server(proxy_server.Handler)
        with proxy_server._UPSTREAM_AUTH_LOCK:
            proxy_server._UPSTREAM_AUTH_HEADERS.clear()
        proxy_server.remember_upstream_auth(
            {
                "Authorization": "Bearer real-codex-token",
                "ChatGPT-Account-ID": "fake-account",
            }
        )

        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=20)
        try:
            conn.request(
                "GET",
                "/v1/models",
                headers={"Authorization": "Bearer not-needed"},
            )
            response = conn.getresponse()
            response_body = response.read()
        finally:
            conn.close()

        assert response.status == HTTPStatus.OK, response_body.decode("utf-8", errors="replace")
        parsed_models = json.loads(response_body.decode("utf-8"))
        assert [item["id"] for item in parsed_models["data"]] == ["gpt-test-codex", "gpt-test-mini"]
        assert len(MockModelsUpstream.requests) == 1
        upstream_request = MockModelsUpstream.requests[0]
        assert upstream_request["path"] == "/backend-api/codex/models"
        assert upstream_request["headers"]["authorization"] == "Bearer real-codex-token"
        assert upstream_request["headers"]["chatgpt-account-id"] == "fake-account"

        with proxy_server._UPSTREAM_AUTH_LOCK:
            proxy_server._UPSTREAM_AUTH_HEADERS.clear()
        proxy.shutdown()
        upstream.shutdown()

    with tempfile.TemporaryDirectory() as temp_dir:
        MockResponsesUpstream.requests = []
        upstream = start_server(MockResponsesUpstream)
        proxy = start_server(proxy_server.Handler)
        proxy_server.CHATGPT_UPSTREAM_BASE_URL = f"http://127.0.0.1:{upstream.server_port}/backend-api/codex"
        proxy_server.OPENAI_UPSTREAM_BASE_URL = f"http://127.0.0.1:{upstream.server_port}/v1"
        proxy_server.STORE = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        with proxy_server._UPSTREAM_AUTH_LOCK:
            proxy_server._UPSTREAM_AUTH_HEADERS.clear()
        proxy_server.remember_upstream_auth(
            {
                "Authorization": "Bearer real-openai-api-key",
            }
        )

        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=20)
        try:
            conn.request(
                "POST",
                "/v1/responses",
                body=json.dumps(
                    {
                        "model": "gpt-test",
                        "input": [proxy_server.provider_message("user", "internal context edit")],
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer not-needed",
                    "x-hash-context-internal": "context-workbench",
                },
            )
            response = conn.getresponse()
            response_body = response.read()
        finally:
            conn.close()

        assert response.status == HTTPStatus.OK, response_body.decode("utf-8", errors="replace")
        assert proxy_server.STORE.list_sessions()["sessions"] == []
        assert len(MockResponsesUpstream.requests) == 1
        upstream_request = MockResponsesUpstream.requests[0]
        assert upstream_request["path"] == "/v1/responses"
        assert upstream_request["headers"]["authorization"] == "Bearer real-openai-api-key"
        assert "chatgpt-account-id" not in upstream_request["headers"]
        assert "x-hash-context-internal" not in upstream_request["headers"]

        with proxy_server._UPSTREAM_AUTH_LOCK:
            proxy_server._UPSTREAM_AUTH_HEADERS.clear()
        proxy.shutdown()
        upstream.shutdown()

    with tempfile.TemporaryDirectory() as temp_dir:
        MockResponsesUpstream.requests = []
        upstream = start_server(MockResponsesUpstream)
        proxy = start_server(proxy_server.Handler)
        proxy_server.CHATGPT_UPSTREAM_BASE_URL = f"http://127.0.0.1:{upstream.server_port}/backend-api/codex"
        proxy_server.OPENAI_UPSTREAM_BASE_URL = f"http://127.0.0.1:{upstream.server_port}/v1"
        proxy_server.STORE = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        with proxy_server._UPSTREAM_AUTH_LOCK:
            proxy_server._UPSTREAM_AUTH_HEADERS.clear()
        proxy_server.remember_upstream_auth(
            {
                "Authorization": "Bearer real-codex-token",
                "ChatGPT-Account-ID": "fake-account",
            }
        )

        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=20)
        try:
            conn.request(
                "POST",
                "/v1/responses",
                body=json.dumps(
                    {
                        "model": "gpt-test",
                        "input": [proxy_server.provider_message("user", "internal context edit")],
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer not-needed",
                    "x-hash-context-internal": "context-workbench",
                },
            )
            response = conn.getresponse()
            response_body = response.read()
        finally:
            conn.close()

        assert response.status == HTTPStatus.OK, response_body.decode("utf-8", errors="replace")
        assert proxy_server.STORE.list_sessions()["sessions"] == []
        assert len(MockResponsesUpstream.requests) == 1
        upstream_request = MockResponsesUpstream.requests[0]
        assert upstream_request["path"] == "/backend-api/codex/responses"
        assert upstream_request["headers"]["authorization"] == "Bearer real-codex-token"
        assert upstream_request["headers"]["chatgpt-account-id"] == "fake-account"
        assert "x-hash-context-internal" not in upstream_request["headers"]

        with proxy_server._UPSTREAM_AUTH_LOCK:
            proxy_server._UPSTREAM_AUTH_HEADERS.clear()
        proxy.shutdown()
        upstream.shutdown()

    with tempfile.TemporaryDirectory() as temp_dir:
        MockResponsesUpstream.requests = []
        upstream = start_server(MockResponsesUpstream)
        proxy = start_server(proxy_server.Handler)
        proxy_server.CHATGPT_UPSTREAM_BASE_URL = f"http://127.0.0.1:{upstream.server_port}/backend-api/codex"
        proxy_server.OPENAI_UPSTREAM_BASE_URL = f"http://127.0.0.1:{upstream.server_port}/v1"
        proxy_server.STORE = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")
        with proxy_server._UPSTREAM_AUTH_LOCK:
            proxy_server._UPSTREAM_AUTH_HEADERS.clear()

        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=20)
        try:
            conn.request(
                "POST",
                "/v1/responses",
                body=json.dumps(
                    {
                        "model": "gpt-test",
                        "input": [proxy_server.provider_message("user", "internal context edit")],
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer not-needed",
                    "x-hash-context-internal": "context-workbench",
                },
            )
            response = conn.getresponse()
            response_body = response.read()
        finally:
            conn.close()

        parsed_error = json.loads(response_body.decode("utf-8"))
        assert response.status == HTTPStatus.SERVICE_UNAVAILABLE, parsed_error
        assert parsed_error["error"]["code"] == "codex_auth_not_captured"
        assert proxy_server.STORE.list_sessions()["sessions"] == []
        assert len(MockResponsesUpstream.requests) == 0

        proxy.shutdown()
        upstream.shutdown()

    with tempfile.TemporaryDirectory() as temp_dir:
        upstream = start_server(MockCompactUpstream)
        proxy = start_server(proxy_server.Handler)
        upstream_base = f"http://127.0.0.1:{upstream.server_port}/backend-api/codex"
        proxy_server.CHATGPT_UPSTREAM_BASE_URL = upstream_base
        proxy_server.OPENAI_UPSTREAM_BASE_URL = f"http://127.0.0.1:{upstream.server_port}/v1"
        proxy_server.STORE = proxy_server.ProxyStore(Path(temp_dir) / "proxy_state.json")

        edited_transcript = [
            proxy_server.transcript_record(
                "user",
                EDITED_TEXT,
                [proxy_server.provider_message("user", EDITED_TEXT)],
            )
        ]
        proxy_server.STORE.override(SESSION_ID, edited_transcript)

        codex_compact_body = {
            "model": "gpt-test",
            "input": [proxy_server.provider_message("user", CODEX_ORIGINAL_TEXT)],
            "instructions": "INSTRUCTIONS_SENT_BY_CODEX",
            "tools": [{"type": "function", "name": "shell_command"}],
            "parallel_tool_calls": True,
            "reasoning": {"effort": "high", "summary": "auto"},
            "text": {"verbosity": "low"},
            "previous_response_id": "resp_should_be_removed",
        }
        raw = json.dumps(codex_compact_body).encode("utf-8")
        compressed = zstandard.ZstdCompressor().compress(raw)

        conn = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=20)
        try:
            conn.request(
                "POST",
                "/v1/responses/compact",
                body=compressed,
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "zstd",
                    "Authorization": "Bearer fake",
                    "ChatGPT-Account-ID": "fake-account",
                    "x-codex-session-id": SESSION_ID,
                },
            )
            response = conn.getresponse()
            response_body = response.read()
        finally:
            conn.close()

        assert response.status == HTTPStatus.OK, response_body.decode("utf-8", errors="replace")
        assert json.loads(response_body.decode("utf-8")) == MockCompactUpstream.response_payload

        assert len(MockCompactUpstream.requests) == 1
        upstream_request = MockCompactUpstream.requests[0]
        assert upstream_request["path"] == "/backend-api/codex/responses/compact"
        assert "content-encoding" not in upstream_request["headers"]

        forwarded_body = upstream_request["body"]
        assert forwarded_body["model"] == codex_compact_body["model"]
        assert forwarded_body["instructions"] == codex_compact_body["instructions"]
        assert forwarded_body["tools"] == codex_compact_body["tools"]
        assert forwarded_body["parallel_tool_calls"] is True
        assert forwarded_body["reasoning"] == codex_compact_body["reasoning"]
        assert forwarded_body["text"] == codex_compact_body["text"]
        assert "previous_response_id" not in forwarded_body

        forwarded_input_text = message_text(forwarded_body["input"][0])
        assert forwarded_input_text == EDITED_TEXT
        assert CODEX_ORIGINAL_TEXT not in json.dumps(forwarded_body, ensure_ascii=False)

        session = proxy_server.STORE.get_session(SESSION_ID)
        assert session is not None
        assert session["status"] == "override"
        assert session["transcript"][0]["role"] == "assistant"
        assert session["transcript"][0]["text"] == REMOTE_SUMMARY_TEXT
        provider_items = session["transcript"][0]["providerItems"]
        assert any(item.get("type") == "compaction" for item in provider_items)
        assert REMOTE_COMPACTION_BLOB not in session["transcript"][0]["text"]

        proxy.shutdown()
        upstream.shutdown()

    print("compact proxy HTTP smoke ok")


if __name__ == "__main__":
    main()
