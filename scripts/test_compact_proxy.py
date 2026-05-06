from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import zstandard

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import proxy_server
import web_server


SESSION_ID = "fake-codex-session"
EDITED_TEXT = "HASH_CONTEXT_EDITED_CANONICAL"
CODEX_ORIGINAL_TEXT = "CODEX_ORIGINAL_INPUT_SHOULD_NOT_SURVIVE"
REMOTE_SUMMARY_TEXT = "REMOTE_COMPACT_SUMMARY"
REMOTE_COMPACTION_BLOB = "ENCRYPTED_COMPACTION_SUMMARY"
USER_PROMPT = "list the root files"
PRE_TOOL_TEXT = "I will inspect the root directory."
FINAL_TEXT = "The root contains README.md and proxy_server.py."
TOOL_CALL_ID = "call_root_ls"


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
        payload = b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
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


def test_local_compact_without_override_preserves_codex_body() -> None:
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

        assert forwarded_body == original_body
        session = store.get_session(SESSION_ID)
        assert session is not None
        assert session["status"] == "compacting"
        request_log = store.sessions[SESSION_ID].request_log
        assert request_log[-1]["kind"] == "local_compact"
        assert request_log[-1]["forwarded_body"] == original_body


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

        assert forwarded == body
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
        stored = json.loads((Path(temp_dir) / "proxy_state.json").read_text(encoding="utf-8"))
        session = next(item for item in stored["sessions"] if item["id"] == SESSION_ID)
        assert session["request_log"][-1]["kind"] == "tool_output_passthrough"


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
        assert forwarded["input"] == body["input"]
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


def main() -> None:
    test_drop_unpaired_tool_items_preserves_only_complete_pairs()
    test_compact_without_override_preserves_codex_input()
    test_compact_override_reinjects_fresh_initial_context()
    test_request_without_override_preserves_codex_body()
    test_local_compact_without_override_preserves_codex_body()
    test_tool_turn_stays_single_assistant_record()
    test_codex_response_item_types_roundtrip()
    test_shell_tool_output_display_metadata_is_reconstructed()
    test_web_normalize_rebuilds_tool_display_from_provider_items()
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
    test_context_workbench_compressed_nodes_stay_independent_after_cleaning()
    test_context_workbench_hides_internal_prefix_nodes_from_editing()

    with tempfile.TemporaryDirectory() as temp_dir:
        upstream = start_server(MockModelsUpstream)
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
