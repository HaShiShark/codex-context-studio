#!/usr/bin/env python3
"""Capture Codex Responses API request bodies.

Run this as a tiny local model provider, point Codex at it, then submit a turn.
The script writes every captured POST body to JSON so we can inspect the exact
`input` Codex sends before any project proxy normalization.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MODEL = "codex-request-capture"
SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "openai-api-key",
    "api-key",
    "x-api-key",
    "chatgpt-account-id",
}


def utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def safe_path_label(path: str) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", path.strip("/")) or "root"
    return label[:80]


def iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def input_summary(body: Any) -> dict[str, Any]:
    input_items = body.get("input") if isinstance(body, dict) else None
    if not isinstance(input_items, list):
        return {
            "input_is_list": False,
            "input_count": 0,
            "top_level_id_count": 0,
            "nested_id_item_count": 0,
            "items_by_type_role_id": {},
            "id_samples": [],
        }

    by_type_role_id: dict[str, int] = {}
    id_samples: list[dict[str, Any]] = []
    top_level_id_count = 0
    nested_id_item_count = 0

    for index, item in enumerate(input_items):
        if not isinstance(item, dict):
            key = f"<{type(item).__name__}>|<none>|top_id=False"
            by_type_role_id[key] = by_type_role_id.get(key, 0) + 1
            continue

        top_id = item.get("id") not in (None, "")
        has_any_id = any(
            isinstance(child, dict) and child.get("id") not in (None, "")
            for child in iter_dicts(item)
        )
        if top_id:
            top_level_id_count += 1
        if has_any_id:
            nested_id_item_count += 1

        item_type = str(item.get("type") or "")
        role = str(item.get("role") or "")
        key = f"{item_type}|{role}|top_id={top_id}"
        by_type_role_id[key] = by_type_role_id.get(key, 0) + 1

        if top_id and len(id_samples) < 25:
            id_samples.append(
                {
                    "index": index,
                    "type": item.get("type"),
                    "role": item.get("role"),
                    "id": item.get("id"),
                    "call_id": item.get("call_id"),
                    "name": item.get("name"),
                    "status": item.get("status"),
                }
            )

    return {
        "input_is_list": True,
        "input_count": len(input_items),
        "top_level_id_count": top_level_id_count,
        "nested_id_item_count": nested_id_item_count,
        "items_by_type_role_id": dict(sorted(by_type_role_id.items())),
        "id_samples": id_samples,
    }


def safe_headers(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADER_NAMES:
            result[key] = "<redacted>"
        else:
            result[key] = value
    return result


class CaptureState:
    def __init__(self, out_dir: Path, model: str, max_requests: int | None) -> None:
        self.out_dir = out_dir
        self.model = model
        self.max_requests = max_requests
        self.count = 0
        self.lock = threading.Lock()

    def next_index(self) -> int:
        with self.lock:
            self.count += 1
            return self.count

    def should_stop(self) -> bool:
        return self.max_requests is not None and self.count >= self.max_requests


class CaptureHandler(BaseHTTPRequestHandler):
    server_version = "CodexRequestCapture/1.0"

    @property
    def state(self) -> CaptureState:
        return self.server.capture_state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[{utc_stamp()}] {self.client_address[0]} {fmt % args}\n")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.endswith("/models") or path == "/models":
            self._write_json(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": self.state.model,
                            "object": "model",
                            "created": 0,
                            "owned_by": "capture",
                        }
                    ],
                }
            )
            return
        self._write_json({"ok": True, "service": "codex-request-capture"})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_POST(self) -> None:
        raw_body = self._read_body()
        index = self.state.next_index()
        path = urlparse(self.path).path
        headers = safe_headers(self.headers)
        try:
            body_json: Any = json.loads(raw_body.decode("utf-8"))
        except Exception:
            body_json = None

        capture = {
            "captured_at": utc_stamp(),
            "request_index": index,
            "method": "POST",
            "path": self.path,
            "headers": headers,
            "body_sha256": hashlib.sha256(raw_body).hexdigest(),
            "body_raw": raw_body.decode("utf-8", errors="replace"),
            "body_json": body_json,
            "summary": input_summary(body_json),
        }
        output_path = self.state.out_dir / f"{utc_stamp()}-{index:03d}-{safe_path_label(path)}.json"
        output_path.write_text(json.dumps(capture, ensure_ascii=False, indent=2), encoding="utf-8")

        summary = capture["summary"]
        print(
            "captured "
            f"#{index} path={self.path} file={output_path} "
            f"input_count={summary['input_count']} "
            f"top_level_id_count={summary['top_level_id_count']} "
            f"nested_id_item_count={summary['nested_id_item_count']}",
            flush=True,
        )

        self._write_responses_api_reply(body_json)

        if self.state.should_stop():
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _write_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write_responses_api_reply(self, request_body: Any) -> None:
        response_id = f"resp_capture_{uuid4().hex}"
        model = self.state.model
        if isinstance(request_body, dict) and isinstance(request_body.get("model"), str):
            model = request_body["model"]

        message_item = {
            "id": f"msg_capture_{uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Captured request body."}],
        }
        completed = {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "model": model,
                "output": [message_item],
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 1,
                    "total_tokens": 1,
                },
            },
        }

        wants_stream = not isinstance(request_body, dict) or request_body.get("stream") is not False
        if not wants_stream:
            self._write_json(completed["response"])
            return

        events = [
            {"type": "response.created", "response": {"id": response_id, "model": model}},
            {"type": "response.output_item.done", "output_index": 0, "item": message_item},
            completed,
        ]
        payload = "".join(
            f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events
        ) + "data: [DONE]\n\n"
        data = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture Codex Responses API request bodies.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(".tmp-tests") / "codex-request-captures",
        help="Directory for captured JSON files.",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=1,
        help="Stop after this many POST requests. Use 0 to run until Ctrl+C.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    max_requests = None if args.max_requests == 0 else max(1, args.max_requests)
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), CaptureHandler)
    server.capture_state = CaptureState(out_dir, args.model, max_requests)  # type: ignore[attr-defined]

    print(f"capture server: http://{args.host}:{args.port}/v1")
    print(f"model id: {args.model}")
    print(f"captures: {out_dir}")
    print("POST a Codex turn to this provider, then inspect the newest JSON file.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
