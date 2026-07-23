from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from backend.web_handler import StudioHTTPRequestHandler
from backend.token_count import estimate_token_count


def _handler_with_capture() -> tuple[StudioHTTPRequestHandler, dict[str, object]]:
    handler = StudioHTTPRequestHandler.__new__(StudioHTTPRequestHandler)
    captured: dict[str, object] = {}
    handler._send_json = lambda payload, status=200: captured.update(  # type: ignore[method-assign]
        payload=payload,
        status=status,
    )
    return handler, captured


def test_token_count_route_contract() -> None:
    handler, captured = _handler_with_capture()
    routes = handler._post_routes()
    if routes.get("/api/token-counts") != handler._handle_token_counts_post:
        raise AssertionError("token count endpoint is not registered")

    routes["/api/token-counts"](
        {
            "items": [
                {"id": "node-1", "text": "hello world", "tool_text": "tool output"},
                {"id": "", "text": "ignored"},
                "ignored",
            ]
        }
    )
    payload = captured.get("payload")
    if not isinstance(payload, dict):
        raise AssertionError("token count endpoint did not return a JSON object")
    items = payload.get("items")
    expected = [
        {
            "id": "node-1",
            "tokens": estimate_token_count("hello world"),
            "tool_tokens": estimate_token_count("tool output"),
        }
    ]
    if items != expected:
        raise AssertionError(f"unexpected token count response: {items!r}")


def test_token_count_route_bounds_batch_size() -> None:
    handler, captured = _handler_with_capture()
    handler._handle_token_counts_post(
        {
            "items": [
                {"id": f"node-{index}", "text": str(index), "tool_text": ""}
                for index in range(1030)
            ]
        }
    )
    payload = captured.get("payload")
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or len(items) != 1024:
        raise AssertionError("token count endpoint must cap a request at 1024 items")


def test_token_count_route_rejects_non_list_items() -> None:
    handler, _ = _handler_with_capture()
    try:
        handler._handle_token_counts_post({"items": {}})
    except ValueError as exc:
        if str(exc) != "items must be a list":
            raise AssertionError(f"unexpected validation message: {exc}") from exc
        return
    raise AssertionError("token count endpoint accepted a non-list items payload")


if __name__ == "__main__":
    test_token_count_route_contract()
    test_token_count_route_bounds_batch_size()
    test_token_count_route_rejects_non_list_items()
    print("ok - web token count contract tests passed")
