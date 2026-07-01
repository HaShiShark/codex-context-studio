from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


class ShowHandler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_POST(self) -> None:
        self.__class__.hits.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def run_hook(payload: dict[str, Any], *, port: int) -> dict[str, Any]:
    env = os.environ.copy()
    env["HASH_CONTEXT_CONTROL_PORT"] = str(port)
    env["HASH_CONTEXT_HOST"] = "127.0.0.1"
    with tempfile.TemporaryDirectory() as temp_home:
        env["USERPROFILE"] = temp_home
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "codex-context-hook.ps1"),
            ],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            timeout=15,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), "hook did not write JSON"
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


def test_ctx_prompt_is_blocked_before_codex_request() -> None:
    ShowHandler.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), ShowHandler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        output = run_hook({"prompt": "ctx"}, port=port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert output["continue"] is False
    assert output["decision"] == "block"
    assert output["suppressOutput"] is True
    assert ShowHandler.hits == ["/show"]


def test_regular_prompt_continues() -> None:
    ShowHandler.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), ShowHandler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        output = run_hook({"prompt": "hello"}, port=port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert output["continue"] is True
    assert output["suppressOutput"] is True
    assert ShowHandler.hits == []


def main() -> None:
    if sys.platform != "win32":
        print("skip - context hook powershell test is Windows-only")
        return
    tests = [
        test_ctx_prompt_is_blocked_before_codex_request,
        test_regular_prompt_continues,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} context hook tests passed")


if __name__ == "__main__":
    main()
