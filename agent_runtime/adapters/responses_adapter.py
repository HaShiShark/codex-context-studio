from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent_runtime.adapters.base import BaseAdapter, ProviderRequestContext, ToolSpec
from agent_runtime.core.stream_events import (
    AdapterStreamEvent,
    ProviderDoneEvent,
    TextDeltaEvent,
    ToolCallReadyEvent,
)


@dataclass(slots=True)
class ResponsesStreamResult:
    output_text: str
    function_calls: list[Any]
    finish_reason: str | None = None
    canonical_items: Sequence[Any] = ()


class ResponsesAdapter(BaseAdapter[dict[str, Any]]):
    """OpenAI Responses API request and stream translation."""

    provider_name = "openai_responses"

    def __init__(
        self,
        client: Any,
        *,
        instructions: str | Callable[[], str],
        request_input: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
        tools: Sequence[ToolSpec | Mapping[str, Any]]
        | Callable[[], Sequence[ToolSpec | Mapping[str, Any]]] = (),
        sanitize_text: Callable[[Any], str] | None = None,
        sanitize_value: Callable[[Any], Any] | None = None,
    ) -> None:
        self.client = client
        self._instructions = instructions
        self._request_input = request_input
        self._tools = tools
        self._sanitize_text = sanitize_text or _default_sanitize_text
        self._sanitize_value = sanitize_value or _default_sanitize_value

    def build_request(
        self,
        context_or_turn_items: ProviderRequestContext | list[dict[str, Any]],
        request_model: str | None = None,
        request_reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Build either from AgentCore turn items or a provider context."""

        if isinstance(context_or_turn_items, ProviderRequestContext):
            return self._build_context_request(context_or_turn_items)

        if request_model is None:
            raise ValueError("Responses request requires a model")

        if self._request_input is None:
            raise RuntimeError("ResponsesAdapter requires request_input for AgentCore")

        request: dict[str, Any] = {
            "model": request_model,
            "instructions": self._get_instructions(),
            "input": self._request_input(context_or_turn_items),
            "tools": self._get_tools(),
        }
        if request_reasoning_effort:
            request["reasoning"] = {"effort": request_reasoning_effort}
        return request

    def stream_response(
        self,
        request: Mapping[str, Any] | None = None,
        context: ProviderRequestContext | None = None,
        *,
        on_text_delta: Callable[[str], None] | None = None,
        on_reasoning_start: Callable[[], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        on_reasoning_done: Callable[[], None] | None = None,
        **request_kwargs: Any,
    ) -> ResponsesStreamResult | Iterable[AdapterStreamEvent]:
        """Stream Responses output.

        AgentCore calls this as ``stream_response(**request, on_text_delta=...)``
        and expects a ResponsesStreamResult. BaseAdapter callers can pass a
        request mapping and receive provider-neutral stream events.
        """

        del on_reasoning_start, on_reasoning_delta, on_reasoning_done

        if request is not None:
            merged_request = {**dict(request), **request_kwargs}
            if on_text_delta is None:
                return self._stream_events(merged_request, context)
            return self._stream_agent_core_response(
                merged_request,
                on_text_delta=on_text_delta,
            )

        return self._stream_agent_core_response(
            request_kwargs,
            on_text_delta=on_text_delta,
        )

    def _build_context_request(
        self,
        context: ProviderRequestContext,
    ) -> dict[str, Any]:
        if context.model is None:
            raise ValueError("Responses request requires a model")

        request: dict[str, Any] = {
            key: self._sanitize_value(value)
            for key, value in dict(context.provider_config).items()
            if key not in {"instructions"}
        }
        request.update(
            {
                "model": context.model,
                "instructions": str(
                    context.provider_config.get("instructions", self._get_instructions())
                ),
                "input": self._sanitize_value(
                    [
                        *context.transcript,
                        *context.current_turn,
                    ]
                ),
                "tools": [
                    self._normalize_tool_schema(tool)
                    for tool in (context.tools or self._get_tools())
                ],
            }
        )
        if context.reasoning_effort:
            request["reasoning"] = {"effort": context.reasoning_effort}
        return request

    def _stream_agent_core_response(
        self,
        request: Mapping[str, Any],
        *,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> ResponsesStreamResult:
        output_chunks: list[str] = []
        function_calls: list[Any] = []
        saw_text_delta = False

        with self.client.responses.stream(**dict(request)) as stream:
            for event in stream:
                if event.type == "response.output_text.delta":
                    saw_text_delta = True
                    safe_delta = self._sanitize_text(event.delta)
                    output_chunks.append(safe_delta)
                    if on_text_delta is not None:
                        on_text_delta(safe_delta)
                elif event.type == "response.output_text.done":
                    if not saw_text_delta:
                        safe_text = self._sanitize_text(event.text)
                        output_chunks.append(safe_text)
                        if on_text_delta is not None and safe_text:
                            on_text_delta(safe_text)
                elif event.type == "response.output_item.done":
                    if getattr(event.item, "type", None) == "function_call":
                        function_calls.append(event.item)
                elif event.type == "error":
                    raise RuntimeError(
                        self._sanitize_text(
                            f"response stream error: {event.message}"
                        )
                    )
                elif event.type == "response.failed":
                    error = getattr(event.response, "error", None)
                    if error and getattr(error, "message", None):
                        raise RuntimeError(
                            self._sanitize_text(
                                f"response failed: {error.message}"
                            )
                        )
                    raise RuntimeError("response failed")

        return ResponsesStreamResult(
            output_text="".join(output_chunks),
            function_calls=function_calls,
        )

    def _stream_events(
        self,
        request: Mapping[str, Any],
        context: ProviderRequestContext | None = None,
    ) -> Iterable[AdapterStreamEvent]:
        del context

        output_chunks: list[str] = []
        saw_text_delta = False

        with self.client.responses.stream(**dict(request)) as stream:
            for event in stream:
                if event.type == "response.output_text.delta":
                    saw_text_delta = True
                    safe_delta = self._sanitize_text(event.delta)
                    output_chunks.append(safe_delta)
                    yield TextDeltaEvent(delta=safe_delta, provider_raw=event)
                elif event.type == "response.output_text.done":
                    if not saw_text_delta:
                        safe_text = self._sanitize_text(event.text)
                        output_chunks.append(safe_text)
                        if safe_text:
                            yield TextDeltaEvent(delta=safe_text, provider_raw=event)
                elif event.type == "response.output_item.done":
                    item = event.item
                    if getattr(item, "type", None) == "function_call":
                        raw_arguments = self._sanitize_text(
                            getattr(item, "arguments", "") or "{}"
                        )
                        yield ToolCallReadyEvent(
                            name=self._sanitize_text(getattr(item, "name", "")),
                            arguments=self._parse_tool_arguments(raw_arguments),
                            call_id=self._sanitize_text(getattr(item, "call_id", "")),
                            raw_arguments=raw_arguments,
                            provider_raw=item,
                        )
                elif event.type == "error":
                    raise RuntimeError(
                        self._sanitize_text(
                            f"response stream error: {event.message}"
                        )
                    )
                elif event.type == "response.failed":
                    error = getattr(event.response, "error", None)
                    if error and getattr(error, "message", None):
                        raise RuntimeError(
                            self._sanitize_text(
                                f"response failed: {error.message}"
                            )
                        )
                    raise RuntimeError("response failed")

        yield ProviderDoneEvent(output_text="".join(output_chunks))

    def _get_instructions(self) -> str:
        if callable(self._instructions):
            return self._instructions()
        return self._instructions

    def _get_tools(self) -> list[dict[str, Any]]:
        tools = self._tools() if callable(self._tools) else self._tools
        return [self._normalize_tool_schema(tool) for tool in tools]

    @staticmethod
    def _normalize_tool_schema(tool: ToolSpec | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(tool, ToolSpec):
            return {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.parameters),
            }
        return dict(tool)

    def _parse_tool_arguments(self, raw_arguments: str) -> Mapping[str, Any]:
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return {}

        if isinstance(arguments, Mapping):
            return self._sanitize_value(dict(arguments))
        return {}


def _default_sanitize_text(value: Any) -> str:
    return str(value)


def _default_sanitize_value(value: Any) -> Any:
    return value


__all__ = [
    "ResponsesAdapter",
    "ResponsesStreamResult",
]
