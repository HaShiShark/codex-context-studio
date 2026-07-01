from __future__ import annotations

import io
import json
import sys
from http import HTTPStatus
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import proxy_server  # noqa: E402


class FakeHeaders(dict[str, str]):
    def get(self, key: str, default: Any = None) -> Any:
        return super().get(key.lower(), default)

    def items(self):  # type: ignore[override]
        return super().items()


class FakeHandler:
    def __init__(self, raw_body: bytes) -> None:
        self.rfile = io.BytesIO(raw_body)
        self.headers = FakeHeaders(
            {
                "content-length": str(len(raw_body)),
                "content-encoding": "",
                "content-type": "application/json",
            }
        )
        self.status: HTTPStatus | None = None
        self.payload: dict[str, Any] | None = None

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        self.status = status
        self.payload = payload


class ForbiddenStore:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"remote compact handler must not touch STORE.{name}")


def forbidden_connection(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("remote compact handler must not open an upstream connection")


def test_remote_compact_handler_returns_gone_without_store_or_upstream() -> None:
    raw_body = json.dumps({"input": [{"type": "message", "role": "user", "content": "compact"}]}).encode("utf-8")
    handler = FakeHandler(raw_body)
    old_store = proxy_server.STORE
    old_http_connection = proxy_server.http.client.HTTPConnection
    old_https_connection = proxy_server.http.client.HTTPSConnection

    try:
        proxy_server.STORE = ForbiddenStore()  # type: ignore[assignment]
        proxy_server.http.client.HTTPConnection = forbidden_connection  # type: ignore[assignment]
        proxy_server.http.client.HTTPSConnection = forbidden_connection  # type: ignore[assignment]

        proxy_server.Handler._handle_compact(handler)  # type: ignore[arg-type]
    finally:
        proxy_server.STORE = old_store
        proxy_server.http.client.HTTPConnection = old_http_connection  # type: ignore[assignment]
        proxy_server.http.client.HTTPSConnection = old_https_connection  # type: ignore[assignment]

    assert handler.status == HTTPStatus.GONE
    assert handler.payload is not None
    error = handler.payload.get("error")
    assert isinstance(error, dict)
    assert error["code"] == "remote_compact_disabled"
    assert error["type"] == "remote_compact_disabled"
    assert error["message"] == (
        "remote compact disabled; local compact is handled through /v1/responses metadata"
    )


def main() -> None:
    tests = [
        test_remote_compact_handler_returns_gone_without_store_or_upstream,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} remote compact disabled tests passed")


if __name__ == "__main__":
    main()
