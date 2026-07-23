from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent_runtime.adapters.base import (
    BaseAdapter,
    ProviderRequestContext,
    ToolSpec,
    clear_stream_cancel,
    provider_replay_payload,
    register_stream_cancel,
    to_plain_value,
)
from agent_runtime.core.canonical_types import CanonicalItem, ProviderRaw
from agent_runtime.core.stream_events import (
    AdapterStreamEvent,
    ProviderDoneEvent,
    ReasoningDeltaEvent,
    ReasoningDoneEvent,
    ReasoningStartEvent,
    TextDeltaEvent,
    ToolCallReadyEvent,
)


@dataclass(slots=True)
class ResponsesStreamResult:
    output_text: str
    function_calls: list[Any]
    finish_reason: str | None = None
    canonical_items: Sequence[Any] = ()
    usage: Mapping[str, Any] | None = None


class ResponsesAdapter(BaseAdapter[dict[str, Any]]):
    """OpenAI Responses API request and stream translation."""

    provider_name = "openai_responses"

    def __init__(
        self,
        client: Any,
        *,
        instructions: str | Callable[[], str],
        tools: Sequence[ToolSpec | Mapping[str, Any]]
        | Callable[[], Sequence[ToolSpec | Mapping[str, Any]]] = (),
        sanitize_text: Callable[[Any], str] | None = None,
        sanitize_value: Callable[[Any], Any] | None = None,
    ) -> None:
        self.client = client
        self._instructions = instructions
        self._tools = tools
        self._sanitize_text = sanitize_text or _default_sanitize_text
        self._sanitize_value = sanitize_value or _default_sanitize_value

    def build_request(
        self,
        context: ProviderRequestContext,
    ) -> dict[str, Any]:
        return self._build_context_request(context)

    def stream_response(
        self,
        request: Mapping[str, Any],
        context: ProviderRequestContext | None = None,
    ) -> Iterable[AdapterStreamEvent]:
        return self._stream_events(request, context)

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
        instructions = context.provider_config.get("instructions")
        if instructions is None:
            instructions = _compile_prompt_blocks(context.prompt_blocks)
        if not instructions:
            instructions = self._get_instructions()
        request.update(
            {
                "model": context.model,
                "instructions": str(instructions),
                "input": self._sanitize_value(
                    _compile_responses_input(context.transcript, context.current_turn)
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

    def _stream_events(
        self,
        request: Mapping[str, Any],
        context: ProviderRequestContext | None = None,
    ) -> Iterable[AdapterStreamEvent]:
        output_chunks: list[str] = []
        saw_text_delta = False
        reasoning_active = False
        output_items: list[Any] = []
        final_response: Any | None = None

        manager = self.client.responses.stream(**dict(request))
        with manager as stream:
            registered = register_stream_cancel(context, stream)
            try:
                for event in stream:
                    event_type = _get_value(event, "type", "")
                    if event_type == "response.output_text.delta":
                        saw_text_delta = True
                        safe_delta = self._sanitize_text(_get_value(event, "delta", ""))
                        output_chunks.append(safe_delta)
                        yield TextDeltaEvent(delta=safe_delta, provider_raw=event)
                    elif event_type == "response.output_text.done":
                        if not saw_text_delta:
                            safe_text = self._sanitize_text(_get_value(event, "text", ""))
                            output_chunks.append(safe_text)
                            if safe_text:
                                yield TextDeltaEvent(delta=safe_text, provider_raw=event)
                    elif event_type in {
                        "response.reasoning_summary_text.delta",
                        "response.reasoning_text.delta",
                    }:
                        if not reasoning_active:
                            reasoning_active = True
                            yield ReasoningStartEvent(provider_raw=event)
                        yield ReasoningDeltaEvent(
                            delta=self._sanitize_text(_get_value(event, "delta", "")),
                            provider_raw=event,
                        )
                    elif event_type in {
                        "response.reasoning_summary_text.done",
                        "response.reasoning_text.done",
                    }:
                        if reasoning_active:
                            reasoning_active = False
                            yield ReasoningDoneEvent(provider_raw=event)
                    elif event_type == "response.output_item.done":
                        item = _get_value(event, "item")
                        if item is not None:
                            output_items.append(item)
                        if _get_value(item, "type") == "function_call":
                            raw_arguments = self._sanitize_text(
                                _get_value(item, "arguments", "") or "{}"
                            )
                            yield ToolCallReadyEvent(
                                name=self._sanitize_text(_get_value(item, "name", "")),
                                arguments=self._parse_tool_arguments(raw_arguments),
                                call_id=self._sanitize_text(_get_value(item, "call_id", "")),
                                raw_arguments=raw_arguments,
                                provider_raw=item,
                            )
                    elif event_type == "response.completed":
                        final_response = _get_value(event, "response")
                    elif event_type == "error":
                        raise RuntimeError(
                            self._sanitize_text(
                                f"response stream error: {_get_value(event, 'message', '')}"
                            )
                        )
                    elif event_type == "response.failed":
                        response = _get_value(event, "response")
                        error = _get_value(response, "error")
                        if error and _get_value(error, "message"):
                            raise RuntimeError(
                                self._sanitize_text(
                                    f"response failed: {_get_value(error, 'message')}"
                                )
                            )
                        raise RuntimeError("response failed")

                get_final_response = getattr(stream, "get_final_response", None)
                if final_response is None and callable(get_final_response):
                    final_response = get_final_response()
            finally:
                if registered:
                    clear_stream_cancel(context)

        if reasoning_active:
            yield ReasoningDoneEvent(provider_raw=final_response)

        final_output_items = _as_sequence(_get_value(final_response, "output"))
        if final_output_items:
            output_items = list(final_output_items)
        canonical_items = tuple(_canonical_output_item(item) for item in output_items)
        usage = _to_mapping(_get_value(final_response, "usage"))
        finish_reason = _coerce_optional_text(
            _get_value(final_response, "status")
        )
        yield ProviderDoneEvent(
            output_text="".join(output_chunks),
            finish_reason=finish_reason,
            usage=usage,
            canonical_items=canonical_items,
            provider_raw=final_response,
        )

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


def _compile_prompt_blocks(prompt_blocks: Sequence[Any]) -> str:
    sections: list[str] = []
    for block in prompt_blocks:
        text = str(_get_value(block, "text", "") or "").strip()
        if text:
            sections.append(text)
    return "\n\n".join(sections)


def _compile_responses_input(
    transcript: Sequence[Any],
    current_turn: Sequence[Any],
) -> list[Any]:
    items: list[Any] = []
    for value in (*transcript, *current_turn):
        canonical_items = _get_value(value, "canonical_items")
        if canonical_items and not _looks_like_responses_or_canonical_item(value):
            for item in _as_sequence(canonical_items):
                items.extend(_responses_input_items(item))
            continue
        items.extend(_responses_input_items(value))
    return items


def _responses_input_items(item: Any) -> list[Any]:
    provider_payload = provider_replay_payload(item, "openai_responses")
    if provider_payload is not None:
        return [provider_payload]

    plain = to_plain_value(item)
    if not isinstance(plain, Mapping):
        return [plain]

    item_type = str(plain.get("type") or "")
    if item_type == "tool_call":
        return [
            {
                "type": "function_call",
                "call_id": str(plain.get("call_id") or ""),
                "name": str(plain.get("name") or ""),
                "arguments": _arguments_json(plain.get("arguments")),
            }
        ]
    if item_type == "tool_result":
        return [
            {
                "type": "function_call_output",
                "call_id": str(plain.get("call_id") or ""),
                "output": plain.get("output", ""),
            }
        ]
    if item_type == "message":
        return [
            {
                "type": "message",
                "role": str(plain.get("role") or "user"),
                "content": plain.get("content", ""),
            }
        ]
    if item_type == "reasoning" and isinstance(plain.get("content"), Mapping):
        return [{"type": "reasoning", **dict(plain["content"])}]
    if item_type == "provider_item":
        raise ValueError("Responses provider_item is missing replayable provider_raw payload")

    # Already-native Responses items (message/function_call/output/reasoning and
    # future provider items) pass through without narrowing unknown fields.
    return [dict(plain)]


def _canonical_output_item(item: Any) -> CanonicalItem:
    plain = to_plain_value(item)
    if not isinstance(plain, Mapping):
        plain = {"type": "unknown", "value": plain}
    payload = dict(plain)
    item_type = str(payload.get("type") or "")
    provider_raw = ProviderRaw(
        provider_id="openai_responses",
        event_type="response.output_item.done",
        payload=payload,
    )

    if item_type == "message":
        return CanonicalItem(
            type="message",
            role="assistant",
            content=payload.get("content"),
            provider_raw=provider_raw,
        )
    if item_type == "reasoning":
        return CanonicalItem(
            type="reasoning",
            content={
                key: value
                for key, value in payload.items()
                if key not in {"type", "id", "status"}
            },
            provider_raw=provider_raw,
        )
    if item_type == "function_call":
        raw_arguments = str(payload.get("arguments") or "{}")
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            arguments = {}
        return CanonicalItem(
            type="tool_call",
            name=str(payload.get("name") or ""),
            call_id=str(payload.get("call_id") or ""),
            arguments=arguments if isinstance(arguments, Mapping) else {},
            provider_raw=provider_raw,
            metadata={"raw_arguments": raw_arguments},
        )
    return CanonicalItem(
        type="provider_item",
        provider_raw=provider_raw,
    )


def _looks_like_responses_or_canonical_item(value: Any) -> bool:
    return _get_value(value, "type") not in (None, "")


def _get_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _as_sequence(value: Any) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return (value,)


def _to_mapping(value: Any) -> Mapping[str, Any] | None:
    plain = to_plain_value(value)
    return dict(plain) if isinstance(plain, Mapping) else None


def _coerce_optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _arguments_json(value: Any) -> str:
    if isinstance(value, str):
        return value or "{}"
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def _default_sanitize_text(value: Any) -> str:
    return str(value)


def _default_sanitize_value(value: Any) -> Any:
    return value


__all__ = [
    "ResponsesAdapter",
    "ResponsesStreamResult",
]
