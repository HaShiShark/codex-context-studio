from __future__ import annotations

import json
import sys
from http import HTTPStatus
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend import proxy_fastapi, proxy_routes_support  # noqa: E402


class FakeModelsResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.content = json.dumps(payload).encode("utf-8")
        self.headers: dict[str, str] = {"content-type": "application/json"}


class FakeAsyncClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, headers: dict[str, str]) -> FakeModelsResponse:
        return FakeModelsResponse(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {"error": "upstream unavailable", "url": url, "auth": bool(headers.get("ChatGPT-Account-ID"))},
        )


def reset_cached_upstream_auth() -> None:
    with proxy_routes_support._UPSTREAM_AUTH_LOCK:  # type: ignore[attr-defined]
        proxy_routes_support._UPSTREAM_AUTH_HEADERS.clear()  # type: ignore[attr-defined]


def test_models_preserves_upstream_error_without_chatgpt_auth() -> None:
    original_client = proxy_fastapi.httpx.AsyncClient
    try:
        reset_cached_upstream_auth()
        proxy_fastapi.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        with TestClient(proxy_fastapi.app) as client:
            response = client.get("/v1/models")
    finally:
        proxy_fastapi.httpx.AsyncClient = original_client
        reset_cached_upstream_auth()

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.json()["error"] == "upstream unavailable"


def test_models_serves_local_fallback_when_chatgpt_auth_upstream_fails() -> None:
    original_client = proxy_fastapi.httpx.AsyncClient
    try:
        reset_cached_upstream_auth()
        proxy_fastapi.httpx.AsyncClient = FakeAsyncClient  # type: ignore[assignment]
        with TestClient(proxy_fastapi.app) as client:
            response = client.get(
                "/v1/models",
                headers={
                    "Authorization": "Bearer test-token",
                    "ChatGPT-Account-ID": "account-test",
                },
            )
    finally:
        proxy_fastapi.httpx.AsyncClient = original_client
        reset_cached_upstream_auth()

    payload = response.json()
    assert response.status_code == HTTPStatus.OK
    assert payload["object"] == "list"
    assert [model["id"] for model in payload["data"]][:2] == ["gpt-5.5", "gpt-5.4"]
    assert all(model["object"] == "model" for model in payload["data"])


def main() -> None:
    tests = [
        test_models_preserves_upstream_error_without_chatgpt_auth,
        test_models_serves_local_fallback_when_chatgpt_auth_upstream_fails,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} proxy models tests passed")


if __name__ == "__main__":
    main()
