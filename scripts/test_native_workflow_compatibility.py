from __future__ import annotations

import asyncio
import copy
import gzip
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import proxy_fastapi, proxy_routes_support
from backend.codex_input_cursor import response_items_to_request_items
from backend.compact_controller import collect_user_message_items
from backend.proxy_store import ProxyStore
from backend.transcript_codec import input_items_to_transcript, transcript_to_input_items
from backend.web_context import validate_context_provider_items


def message(role: str, text: str) -> dict:
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


class ByteChunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, cancel: bool = False):
        self.chunks = chunks
        self.cancel = cancel
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.cancel:
            raise asyncio.CancelledError()

    async def aclose(self):
        self.closed = True


class NativeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(proxy_routes_support, "proxy_log"))

    def test_proxy_does_not_negotiate_compression_for_native_client(self):
        original_client = httpx.AsyncClient
        payload = b'{"data":[{"b64_json":"synthetic"}]}'
        observed = []

        async def upstream(request):
            encoding = request.headers.get("accept-encoding", "")
            observed.append(encoding)
            compressed = "gzip" in encoding
            return httpx.Response(
                200, headers={"content-type": "application/json", **({"content-encoding": "gzip"} if compressed else {})},
                stream=ByteChunks([gzip.compress(payload) if compressed else payload]),
            )

        async def run():
            with patch.object(proxy_fastapi.httpx, "AsyncClient", side_effect=lambda **kw: original_client(transport=httpx.MockTransport(upstream), **kw)), \
                 patch.object(proxy_routes_support, "upstream_base_url_for_request", return_value="https://provider.test"), \
                 patch.object(proxy_routes_support, "remember_upstream_auth"), \
                 patch.object(proxy_routes_support, "apply_cached_upstream_auth", side_effect=lambda h: h):
                async with original_client(transport=httpx.ASGITransport(app=proxy_fastapi.app)) as native:
                    request = native.build_request("POST", "http://studio/v1/images/generations", content=b"{}")
                    del request.headers["accept-encoding"]
                    response = await native.send(request, stream=True)
                    # Match Codex's JSON parser: consume wire bytes without httpx auto-decompression.
                    raw = b"".join([chunk async for chunk in response.aiter_raw()])
                    self.assertEqual(raw, payload)
                    self.assertEqual(json.loads(raw)["data"][0]["b64_json"], "synthetic")
                    self.assertNotIn("content-encoding", response.headers)
            self.assertEqual(observed, ["identity"])
        asyncio.run(run())

    def test_provider_routes_preserve_wire_payload_and_do_not_capture_context(self):
        original_client = httpx.AsyncClient
        received = []
        reply = b'{"result":"native reply"}'
        wire_reply = gzip.compress(reply)

        async def handler(request):
            received.append((request, await request.aread()))
            return httpx.Response(
                202,
                headers=[("content-type", "application/json"), ("content-encoding", "gzip"),
                         ("content-length", str(len(wire_reply))), ("set-cookie", "a=1"),
                         ("set-cookie", "b=2"), ("connection", "x-private"), ("x-private", "drop")],
                stream=ByteChunks([wire_reply[:4], wire_reply[4:]]),
            )

        with ExitStack() as stack:
            stack.enter_context(patch.object(proxy_fastapi.httpx, "AsyncClient", side_effect=lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw)))
            stack.enter_context(patch.object(proxy_routes_support, "upstream_base_url_for_request", return_value="https://provider.test/base"))
            stack.enter_context(patch.object(proxy_routes_support, "remember_upstream_auth"))
            stack.enter_context(patch.object(proxy_routes_support, "apply_cached_upstream_auth", side_effect=lambda h: h))
            begin = stack.enter_context(patch.object(proxy_fastapi.STORE, "begin_request", side_effect=AssertionError("must not capture native endpoints")))
            client = TestClient(proxy_fastapi.app)  # No lifespan: no user runtime/config access.
            for endpoint, body, content_type in [
                ("images/generations", b'{"prompt":"image"}', "application/json"),
                ("images/edits", b"--boundary\r\n\x00\xffimage\r\n--boundary--", "multipart/form-data; boundary=boundary"),
                ("alpha/search", b'{"query":"search"}', "application/json"),
                ("future/native", b"\x00\xff", "application/octet-stream"),
            ]:
                with self.subTest(endpoint=endpoint):
                    response = client.post(f"/v1/{endpoint}?a=1&a=2", content=body, headers={
                        "Content-Type": content_type, "Authorization": "Bearer synthetic",
                        "Connection": "x-private", "X-Private": "drop",
                    })
                    self.assertEqual(response.status_code, 202)
                    self.assertEqual(response.content, reply)
                    self.assertEqual(response.headers.get_list("set-cookie"), ["a=1", "b=2"])
                    self.assertNotIn("x-private", response.headers)
                    request, actual_body = received[-1]
                    self.assertEqual(actual_body, body)
                    self.assertEqual(str(request.url), f"https://provider.test/base/{endpoint}?a=1&a=2")
                    self.assertEqual(request.headers["content-type"], content_type)
                    self.assertEqual(request.headers["authorization"], "Bearer synthetic")
                    self.assertNotIn("x-private", request.headers)
            self.assertEqual(client.post("/v1/responses/compact").status_code, 410)
            begin.assert_not_called()

    def test_provider_error_is_not_replaced_with_success(self):
        original_client = httpx.AsyncClient
        async def handler(request):
            return httpx.Response(429, headers={"retry-after": "7"}, stream=ByteChunks([b"quota exceeded"]))
        with patch.object(proxy_fastapi.httpx, "AsyncClient", side_effect=lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw)), \
             patch.object(proxy_routes_support, "upstream_base_url_for_request", return_value="https://provider.test"), \
             patch.object(proxy_routes_support, "remember_upstream_auth"), \
             patch.object(proxy_routes_support, "apply_cached_upstream_auth", side_effect=lambda h: h):
            response = TestClient(proxy_fastapi.app).post("/v1/images/generations", content=b"{}")
        self.assertEqual((response.status_code, response.text, response.headers["retry-after"]), (429, "quota exceeded", "7"))

    def test_sse_all_byte_boundaries_and_line_endings(self):
        item = message("assistant", "中文🙂")
        event = {"type": "response.completed", "response": {"output": [item]}}
        for newline in ("\n", "\r\n", "\r"):
            raw = ("data: " + json.dumps(event, ensure_ascii=False) + newline * 2).encode()
            for boundary in range(1, len(raw)):
                decoder = proxy_routes_support.ResponseSSEDecoder()
                items, text, completed = [], [], []
                for chunk in (raw[:boundary], raw[boundary:]):
                    decoder.feed(chunk, items, text, completed)
                decoder.feed(b"", items, text, completed, final=True)
                self.assertEqual(items, [item])
                self.assertEqual(len(completed), 1)

    def test_response_keeps_request_projection_and_old_callbacks_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProxyStore(Path(directory) / "state.json")
            user = {**message("user", "hello"), "id": "msg-user", "internal_chat_message_metadata_passthrough": {"turn_id": "turn-a"}}
            body = {"input": [user]}
            session, _ = store.begin_request("session", body, {}, request_id="a")
            store.begin_request("session", body, {}, request_id="b")
            before = copy.deepcopy(session.proxy_state)
            self.assertFalse(store.complete_response("session", [message("assistant", "stale")], "", request_id="a"))
            self.assertFalse(store.fail_response("session", "stale failure", request_id="a"))
            self.assertEqual(session.proxy_state, before)
            self.assertEqual((session.inflight_request_id, session.status), ("b", "running"))
            assistant = {**message("assistant", "reply"), "id": "msg-answer", "internal_chat_message_metadata_passthrough": {"turn_id": "turn-a"}}
            self.assertTrue(store.complete_response("session", [assistant], "", request_id="b"))
            self.assertEqual(transcript_to_input_items(session.proxy_state.transcript)[-1], assistant)
            self.assertFalse(store.complete_response("session", [assistant], "", request_id="b"))

    def test_cancelled_stream_clears_its_request_and_preserves_main_turn_lock(self):
        async def run(store):
            session, _ = store.begin_request("session", {"input": [message("user", "hello")]}, {}, request_id="a")
            session.main_turn_id = "native-turn"
            before = copy.deepcopy(session.proxy_state.transcript)
            byte_stream = ByteChunks([b'data: {"type":"response.created"}\n\n'], cancel=True)
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=byte_stream)))
            response = await client.send(client.build_request("POST", "https://provider.test"), stream=True)
            with patch.object(proxy_fastapi, "STORE", store), patch.object(proxy_fastapi, "_publish_session_change", new=AsyncMock()):
                stream = proxy_fastapi._stream_upstream_response(
                    client=client, upstream_response=response, session_id="session", request_id="a",
                    capture_proxy_session=True, is_internal_context=False, forwarded_body={}, original_body={},
                )
                await anext(stream)
                with self.assertRaises(asyncio.CancelledError):
                    await anext(stream)
            self.assertEqual(session.proxy_state.transcript, before)
            self.assertEqual(session.status, "mirror")
            self.assertIsNone(session.inflight_before_request)
            self.assertEqual(session.inflight_request_id, "")
            self.assertEqual(session.main_turn_id, "native-turn")
            self.assertTrue(byte_stream.closed and client.is_closed)
        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(run(ProxyStore(Path(directory) / "state.json")))

    def test_compact_cancellation_restores_checkpoint(self):
        from backend.compact_controller import LOCAL_COMPACT_PROMPT_PREFIX
        with tempfile.TemporaryDirectory() as directory:
            store = ProxyStore(Path(directory) / "state.json")
            body = {"input": [message("user", "hello")]}
            session, _ = store.begin_request("session", body, {}, request_id="a")
            store.complete_response("session", [message("assistant", "reply")], "", request_id="a")
            before = copy.deepcopy(session.proxy_state)
            compact_body = {
                "input": [*session.proxy_state.codex_input_cursor, message("user", LOCAL_COMPACT_PROMPT_PREFIX)],
                "client_metadata": {"x-codex-turn-metadata": json.dumps({
                    "request_kind": "compaction", "compaction": {"trigger": "manual", "phase": "pre_turn"},
                })},
            }
            store.begin_request("session", compact_body, {}, request_id="compact")
            self.assertTrue(session.proxy_state.compact_pending)
            store.fail_response("session", "", request_id="compact", cancelled=True)
            self.assertEqual(session.proxy_state.transcript, before.transcript)
            self.assertEqual(session.proxy_state.codex_input_cursor, before.codex_input_cursor)
            self.assertFalse(session.proxy_state.compact_pending)

    def test_cancel_while_connecting_cleans_request_and_query_reaches_upstream(self):
        original_client = httpx.AsyncClient
        clients = []
        async def run(store):
            async def handler(request):
                self.assertEqual(request.url.query, b"api-version=test&flag=1")
                raise asyncio.CancelledError()
            def make_client(**kwargs):
                client = original_client(transport=httpx.MockTransport(handler), **kwargs)
                clients.append(client)
                return client
            request = Request({"type": "http", "method": "POST", "path": "/v1/responses",
                               "query_string": b"api-version=test&flag=1", "headers": [],
                               "scheme": "http", "server": ("testserver", 80)})
            with ExitStack() as stack:
                stack.enter_context(patch.object(proxy_fastapi, "STORE", store))
                stack.enter_context(patch.object(proxy_fastapi, "_read_json_body", new=AsyncMock(return_value={"input": [message("user", "hello")]})))
                stack.enter_context(patch.object(proxy_fastapi, "_publish_session_change", new=AsyncMock()))
                stack.enter_context(patch.object(proxy_fastapi, "_capture_first_codex_instructions"))
                stack.enter_context(patch.object(proxy_fastapi, "_apply_codex_system_prompt_override", side_effect=lambda body: body))
                stack.enter_context(patch.object(proxy_fastapi.httpx, "AsyncClient", side_effect=make_client))
                for name, value in {"session_id_for_request": "session", "codex_passthrough_reason": "",
                                    "upstream_base_url_for_request": "https://provider.test",
                                    "upstream_headers_for_request": {}, "apply_cached_upstream_auth": {},
                                    "write_request_capture": None, "proxy_log": None}.items():
                    stack.enter_context(patch.object(proxy_routes_support, name, return_value=value))
                with self.assertRaises(asyncio.CancelledError):
                    await proxy_fastapi.responses(request)
            session = store.sessions["session"]
            self.assertIsNone(session.inflight_before_request)
            self.assertEqual(session.inflight_request_id, "")
            self.assertEqual(session.status, "mirror")
            self.assertTrue(clients[0].is_closed)
        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(run(ProxyStore(Path(directory) / "state.json")))

    def test_named_standalone_output_is_legal_but_orphan_paired_output_is_not(self):
        item = {"type": "function_call_output", "id": "notification-id", "name": "notify", "namespace": "slack", "output": "hello"}
        validate_context_provider_items([item])
        projected = response_items_to_request_items([item], include_ids=True)
        self.assertEqual(projected, [item])
        with self.assertRaises(ValueError):
            validate_context_provider_items([{**item, "call_id": "missing-call"}])
        with self.assertRaises(ValueError):
            validate_context_provider_items([{"type": "function_call_output", "output": "broken"}])

    def test_compact_retained_content_respects_budget_and_discards_media(self):
        multi = {**message("user", "a" * 24), "id": "user", "content": [
            {"type": "input_text", "text": "a" * 24}, {"type": "input_text", "text": "b" * 24}],
            "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["user.text", "user.text"], "turn_id": "turn"}}
        retained = collect_user_message_items(input_items_to_transcript([multi]), max_user_tokens=2)
        self.assertEqual(retained[0]["content"], [{"type": "input_text", "text": "a" * 8}])
        self.assertEqual(retained[0]["id"], "user")
        self.assertEqual(retained[0]["internal_chat_message_metadata_passthrough"]["content_item_kinds"], ["user.text"])
        media = {**multi, "content": [{"type": "input_text", "text": "describe"}, {"type": "input_image", "file_id": "file-image"}]}
        retained = collect_user_message_items(input_items_to_transcript([media]), max_user_tokens=100)
        self.assertEqual(retained[0]["content"], [{"type": "input_text", "text": "describe"}])
        self.assertEqual(media["content"][1]["file_id"], "file-image")

    def test_image_file_reference_survives_response_projection(self):
        item = {"type": "message", "role": "user", "content": [{"type": "input_image", "file_id": "file-image", "detail": "original"}]}
        self.assertEqual(response_items_to_request_items([item]), [item])


if __name__ == "__main__":
    unittest.main()
