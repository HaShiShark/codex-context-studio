"""HTTP transport for provider endpoints that do not edit conversation context."""

from collections.abc import AsyncIterator, Mapping

import httpx
from anyio import CancelScope


HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
})


def _blocked_headers(headers: Mapping[str, str], *, request: bool) -> set[str] | frozenset[str]:
    lowered = {key.lower(): value for key, value in headers.items()}
    blocked = HOP_HEADERS | {
        value.strip().lower() for value in lowered.get("connection", "").split(",")
    }
    if request:
        blocked |= {"host", "content-length", "x-codex-context-studio-internal", "x-codex-context-studio-session-id"}
    return blocked


def end_to_end_headers(headers: Mapping[str, str], *, request: bool = False) -> dict[str, str]:
    blocked = _blocked_headers(headers, request=request)
    forwarded = {key: value for key, value in headers.items() if key.lower() not in blocked}
    if request and not any(key.lower() == "accept-encoding" for key in forwarded):
        # httpx otherwise adds its own supported encodings. Raw response bytes
        # belong to the downstream client, whose decoder capabilities may differ.
        forwarded["accept-encoding"] = "identity"
    return forwarded


def response_header_pairs(headers: httpx.Headers) -> list[tuple[bytes, bytes]]:
    blocked = _blocked_headers(headers, request=False)
    return [(key.lower(), value) for key, value in headers.raw if key.decode("ascii").lower() not in blocked]


async def stream_provider_response(
    client: httpx.AsyncClient, response: httpx.Response,
) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        with CancelScope(shield=True):
            await response.aclose()
            await client.aclose()
