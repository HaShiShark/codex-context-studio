from __future__ import annotations

import sys
from http import HTTPStatus
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend import proxy_fastapi  # noqa: E402


class ForbiddenStore:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"remote compact handler must not touch STORE.{name}")


def test_remote_compact_handler_returns_gone_without_store_or_upstream() -> None:
    old_store = proxy_fastapi.STORE

    try:
        proxy_fastapi.STORE = ForbiddenStore()  # type: ignore[assignment]
        with TestClient(proxy_fastapi.app) as client:
            response = client.post(
                "/v1/responses/compact",
                json={"input": [{"type": "message", "role": "user", "content": "compact"}]},
            )
    finally:
        proxy_fastapi.STORE = old_store

    assert response.status_code == HTTPStatus.GONE
    payload = response.json()
    error = payload.get("error")
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
