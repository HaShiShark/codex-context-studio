from __future__ import annotations

import sys
from http import HTTPStatus
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _request(path: str, proxy_payload: dict[str, object] | None) -> tuple[dict[str, object], HTTPStatus]:
    import backend.web_handler as web_handler

    original_get = web_handler.get_codex_proxy_control_json
    captured: dict[str, object] = {}
    handler = web_handler.StudioHTTPRequestHandler.__new__(web_handler.StudioHTTPRequestHandler)
    handler.path = path
    handler._send_json = lambda payload, status=HTTPStatus.OK: captured.update(  # type: ignore[method-assign]
        payload=payload,
        status=status,
    )

    try:
        web_handler.get_codex_proxy_control_json = lambda *_args, **_kwargs: proxy_payload
        handler.do_GET()
    finally:
        web_handler.get_codex_proxy_control_json = original_get

    return captured["payload"], captured["status"]  # type: ignore[return-value]


def main() -> None:
    for path in ("/api/proxy/sessions", "/api/proxy/usage"):
        payload, status = _request(path, {})
        assert payload == {}, (path, payload)
        assert status == HTTPStatus.OK, (path, status)

        payload, status = _request(path, None)
        assert "error" in payload, (path, payload)
        assert status == HTTPStatus.BAD_GATEWAY, (path, status)

    print("ok - web proxy passthrough status tests passed")


if __name__ == "__main__":
    main()
