from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


def _sanitize_text(value: Any) -> str:
    return "".join(
        char if not (0xD800 <= ord(char) <= 0xDFFF) else "\ufffd"
        for char in str(value)
    )


def _http_error_message(exc: urllib_error.HTTPError) -> str:
    try:
        raw_detail = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001
        raw_detail = ""
    return _sanitize_text(raw_detail or exc.reason or "request failed")


class SSEJSONStream:
    def __init__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: int = 120,
    ) -> None:
        self.url = url
        self.headers = dict(headers)
        self.payload = dict(payload)
        self.timeout = timeout
        self._response: Any | None = None

    def __enter__(self) -> SSEJSONStream:
        body = json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
        request = urllib_request.Request(
            self.url,
            data=body,
            headers={
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                **self.headers,
            },
            method="POST",
        )
        try:
            self._response = urllib_request.urlopen(request, timeout=self.timeout)
        except urllib_error.HTTPError as exc:
            raise RuntimeError(_http_error_message(exc)) from exc
        except urllib_error.URLError as exc:
            raise RuntimeError(_sanitize_text(exc.reason or str(exc))) from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._response is not None:
            self._response.close()
            self._response = None

    def __iter__(self) -> Iterator[dict[str, Any]]:
        auto_close = False
        if self._response is None:
            self.__enter__()
            auto_close = True

        data_lines: list[str] = []
        try:
            assert self._response is not None
            for raw_line in self._response:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")

                if not line:
                    payload = self._flush_data_lines(data_lines)
                    data_lines = []
                    if payload is None:
                        continue
                    yield payload
                    continue

                if line.startswith(":"):
                    continue

                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                    continue

            payload = self._flush_data_lines(data_lines)
            if payload is not None:
                yield payload
        finally:
            if auto_close:
                self.__exit__(None, None, None)

    @staticmethod
    def _flush_data_lines(data_lines: list[str]) -> dict[str, Any] | None:
        if not data_lines:
            return None

        payload = "\n".join(data_lines).strip()
        if not payload or payload == "[DONE]":
            return None

        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"SSE JSON parse failed: {_sanitize_text(exc.msg)}") from exc

        if not isinstance(parsed, dict):
            return {"data": parsed}
        return parsed


class ClaudeMessagesResource:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def stream(self, **request: Any) -> SSEJSONStream:
        return SSEJSONStream(
            f"{self.base_url}/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            payload=request,
        )


class ClaudeRESTClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.messages = ClaudeMessagesResource(base_url, api_key)


class GeminiRESTClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def stream_generate_content(self, **request: Any) -> SSEJSONStream:
        request_body = dict(request)
        model = _sanitize_text(request_body.pop("model", "")).strip()
        if not model:
            raise RuntimeError("Gemini request requires a model")

        encoded_model = urllib_parse.quote(model, safe="/")
        return SSEJSONStream(
            f"{self.base_url}/models/{encoded_model}:streamGenerateContent?alt=sse",
            headers={
                "x-goog-api-key": self.api_key,
            },
            payload=request_body,
        )

    def generate_content(self, **request: Any) -> dict[str, Any]:
        request_body = dict(request)
        model = _sanitize_text(request_body.pop("model", "")).strip()
        if not model:
            raise RuntimeError("Gemini request requires a model")

        encoded_model = urllib_parse.quote(model, safe="/")
        body = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        http_request = urllib_request.Request(
            f"{self.base_url}/models/{encoded_model}:generateContent",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(http_request, timeout=120) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            raise RuntimeError(_http_error_message(exc)) from exc
        except urllib_error.URLError as exc:
            raise RuntimeError(_sanitize_text(exc.reason or str(exc))) from exc

        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Gemini JSON parse failed: {_sanitize_text(exc.msg)}") from exc
        if not isinstance(parsed, dict):
            return {"data": parsed}
        return parsed


__all__ = [
    "ClaudeRESTClient",
    "GeminiRESTClient",
    "SSEJSONStream",
]
