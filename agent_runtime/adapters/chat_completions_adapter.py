from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
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


_CORE_REQUEST_KEYS = {
    "messages",
    "model",
    "stream",
    "tool_choice",
    "tools",
    "stream_options",
}

_MESSAGE_ITEM_TYPES = {"message", None}
_TOOL_CALL_ITEM_TYPES = {"tool_call", "function_call"}
_TOOL_RESULT_ITEM_TYPES = {"tool_result", "function_call_output"}


class ChatCompletionsAdapter(BaseAdapter[dict[str, Any]]):
    """OpenAI Chat Completions request and stream translation."""

    provider_name = "openai_chat_completions"

    def __init__(self, client: Any) -> None:
        self.client = client

    def build_request(self, context: ProviderRequestContext) -> dict[str, Any]:
        if context.model is None:
            raise ValueError("Chat Completions request requires a model")

        request = {
            key: value
            for key, value in dict(context.provider_config).items()
            if key not in _CORE_REQUEST_KEYS
        }
        tools = [self._normalize_tool_schema(tool) for tool in context.tools]
        request.update(
            {
                "model": context.model,
                "messages": self._build_messages(context),
                "stream": True,
                "stream_options": {
                    **dict(context.provider_config.get("stream_options") or {}),
                    "include_usage": True,
                },
            }
        )
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        if context.reasoning_effort and "reasoning_effort" not in request:
            request["reasoning_effort"] = context.reasoning_effort
        return request

    def stream_response(
        self,
        request: dict[str, Any],
        context: ProviderRequestContext | None = None,
    ) -> Iterable[AdapterStreamEvent]:
        output_chunks: list[str] = []
        tool_call_parts: dict[int, dict[str, Any]] = {}
        emitted_tool_call_indexes: set[int] = set()
        completed_tool_calls: list[ToolCallReadyEvent] = []
        finish_reason: str | None = None
        usage: Mapping[str, Any] | None = None
        last_chunk: Any | None = None
        assistant_message: dict[str, Any] = {"role": "assistant", "content": ""}
        reasoning_active = False

        stream = self.client.chat.completions.create(**dict(request))
        registered = register_stream_cancel(context, stream)
        try:
            for chunk in stream:
                last_chunk = chunk
                chunk_usage = _to_mapping(_get_value(chunk, "usage"))
                if chunk_usage is not None:
                    usage = {**dict(usage or {}), **dict(chunk_usage)}

                for choice in _as_sequence(_get_value(chunk, "choices")):
                    choice_finish_reason = _get_value(choice, "finish_reason")
                    if choice_finish_reason:
                        finish_reason = str(choice_finish_reason)

                    delta = _get_value(choice, "delta", {})
                    _accumulate_assistant_delta(assistant_message, delta)
                    reasoning_delta = _reasoning_delta_text(delta)
                    if reasoning_delta:
                        if not reasoning_active:
                            reasoning_active = True
                            yield ReasoningStartEvent(provider_raw=chunk)
                        yield ReasoningDeltaEvent(
                            delta=reasoning_delta,
                            provider_raw=chunk,
                        )
                    content = _get_value(delta, "content")
                    if content:
                        if reasoning_active:
                            reasoning_active = False
                            yield ReasoningDoneEvent(provider_raw=chunk)
                        safe_delta = str(content)
                        output_chunks.append(safe_delta)
                        yield TextDeltaEvent(delta=safe_delta, provider_raw=chunk)

                    for fallback_index, tool_call_delta in enumerate(
                        _as_sequence(_get_value(delta, "tool_calls"))
                    ):
                        self._accumulate_tool_call_delta(
                            tool_call_parts,
                            tool_call_delta,
                            fallback_index,
                        )

                    if choice_finish_reason == "tool_calls":
                        for event in self._flush_tool_calls(
                            tool_call_parts,
                            emitted_tool_call_indexes,
                            provider_raw=chunk,
                        ):
                            completed_tool_calls.append(event)
                            yield event
        finally:
            if registered:
                clear_stream_cancel(context)

        if reasoning_active:
            yield ReasoningDoneEvent(provider_raw=last_chunk)

        for event in self._flush_tool_calls(
            tool_call_parts,
            emitted_tool_call_indexes,
            provider_raw=last_chunk,
        ):
            completed_tool_calls.append(event)
            yield event

        yield ProviderDoneEvent(
            output_text="".join(output_chunks),
            finish_reason=finish_reason,
            usage=usage,
            canonical_items=self._done_canonical_items(
                assistant_message=_finalize_assistant_message(
                    assistant_message,
                    tool_call_parts,
                ),
                tool_calls=completed_tool_calls,
            ),
            provider_raw=last_chunk,
        )

    def _build_messages(
        self,
        context: ProviderRequestContext,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []

        for block in context.prompt_blocks:
            prompt_message = self._prompt_block_to_message(block)
            if prompt_message is not None:
                messages.append(prompt_message)

        for record in context.transcript:
            self._append_transcript_record(messages, record)

        for item in context.current_turn:
            self._append_canonical_item(messages, item)

        return messages

    def _prompt_block_to_message(
        self,
        block: Any,
    ) -> dict[str, Any] | None:
        text = _get_value(block, "text")
        if text is None:
            return None
        return {"role": "system", "content": str(text)}

    def _append_transcript_record(
        self,
        messages: list[dict[str, Any]],
        record: Any,
    ) -> None:
        if self._looks_like_canonical_item(record):
            self._append_canonical_item(messages, record)
            return

        canonical_items = _as_sequence(_get_value(record, "canonical_items"))
        has_message_canonical_item = any(
            _get_value(item, "type") in _MESSAGE_ITEM_TYPES
            and _get_value(item, "role") in ("user", "assistant")
            for item in canonical_items
        )

        role = _get_value(record, "role")
        record_content = _first_present_value(record, ("text", "content"))
        if (
            not has_message_canonical_item
            and role in ("user", "assistant")
            and record_content is not None
        ):
            messages.append(
                {
                    "role": role,
                    "content": self._message_content(record_content),
                }
            )

        for item in canonical_items:
            self._append_canonical_item(messages, item)

    def _append_canonical_item(
        self,
        messages: list[dict[str, Any]],
        item: Any,
    ) -> None:
        provider_payload = provider_replay_payload(item, "openai_chat_completions")
        if isinstance(provider_payload, Mapping):
            messages.append(dict(provider_payload))
            return

        item_type = _get_value(item, "type")
        role = _get_value(item, "role")

        if item_type in _TOOL_CALL_ITEM_TYPES:
            self._append_tool_call(messages, item)
            return

        if item_type in _TOOL_RESULT_ITEM_TYPES or role == "tool":
            self._append_tool_result(messages, item)
            return

        if item_type not in _MESSAGE_ITEM_TYPES:
            return

        if role in ("developer", "system"):
            role = "system"
        if role not in ("user", "assistant", "system"):
            return

        message: dict[str, Any] = {
            "role": role,
            "content": self._message_content(
                _first_present_value(item, ("content", "text"))
            ),
        }
        if role == "assistant":
            tool_calls = _as_sequence(_get_value(item, "tool_calls"))
            if not tool_calls:
                tool_calls = _as_sequence(
                    _get_value(_get_value(item, "metadata", {}), "tool_calls")
                )
            if tool_calls:
                message["tool_calls"] = [
                    self._normalize_message_tool_call(tool_call)
                    for tool_call in tool_calls
                ]
        messages.append(message)

    def _append_tool_call(
        self,
        messages: list[dict[str, Any]],
        item: Any,
    ) -> None:
        if not messages or messages[-1].get("role") != "assistant":
            messages.append({"role": "assistant", "content": "", "tool_calls": []})

        assistant_message = messages[-1]
        assistant_message.setdefault("content", "")
        tool_calls = assistant_message.setdefault("tool_calls", [])
        if isinstance(tool_calls, list):
            tool_calls.append(self._normalize_message_tool_call(item))

    def _append_tool_result(
        self,
        messages: list[dict[str, Any]],
        item: Any,
    ) -> None:
        call_id = _first_present_value(item, ("tool_call_id", "call_id", "id"))
        output = _first_present_value(item, ("output", "content", "text"))
        messages.append(
            {
                "role": "tool",
                "tool_call_id": str(call_id or ""),
                "content": self._tool_output_content(output),
            }
        )

    @staticmethod
    def _looks_like_canonical_item(value: Any) -> bool:
        return _get_value(value, "type") in (
            "message",
            "tool_call",
            "tool_result",
            "function_call",
            "function_call_output",
        )

    @staticmethod
    def _normalize_tool_schema(tool: ToolSpec | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(tool, ToolSpec):
            return {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.parameters),
                },
            }

        raw_tool = dict(tool)
        function = raw_tool.get("function")
        if raw_tool.get("type") == "function" and isinstance(function, Mapping):
            return {
                "type": "function",
                "function": {
                    "name": str(function.get("name", "")),
                    "description": str(function.get("description", "")),
                    "parameters": dict(function.get("parameters") or {}),
                },
            }

        if raw_tool.get("type") == "function" and "name" in raw_tool:
            return {
                "type": "function",
                "function": {
                    "name": str(raw_tool.get("name", "")),
                    "description": str(raw_tool.get("description", "")),
                    "parameters": dict(raw_tool.get("parameters") or {}),
                },
            }

        return raw_tool

    @staticmethod
    def _normalize_message_tool_call(tool_call: Any) -> dict[str, Any]:
        function = _get_value(tool_call, "function", {})
        name = _first_present_value(tool_call, ("name",))
        if name is None:
            name = _get_value(function, "name", "")

        arguments = _first_present_value(tool_call, ("arguments",))
        if arguments is None:
            arguments = _get_value(function, "arguments")

        return {
            "id": str(
                _first_present_value(tool_call, ("tool_call_id", "call_id", "id"))
                or ""
            ),
            "type": "function",
            "function": {
                "name": str(name or ""),
                "arguments": _arguments_to_json(arguments),
            },
        }

    @staticmethod
    def _message_content(content: Any) -> Any:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            normalized_parts: list[dict[str, Any]] = []
            for item in content:
                normalized_part = _normalize_chat_content_part(item)
                if normalized_part is None:
                    continue
                normalized_parts.append(normalized_part)
            return normalized_parts
        if isinstance(content, Mapping):
            normalized_part = _normalize_chat_content_part(content)
            if normalized_part is not None:
                return [normalized_part]
        return _json_dumps(content)

    @staticmethod
    def _tool_output_content(output: Any) -> str:
        if output is None:
            return ""
        if isinstance(output, str):
            return output
        return _json_dumps(output)

    @staticmethod
    def _accumulate_tool_call_delta(
        tool_call_parts: dict[int, dict[str, Any]],
        tool_call_delta: Any,
        fallback_index: int,
    ) -> None:
        index = _get_value(tool_call_delta, "index", fallback_index)
        if index is None:
            index = fallback_index
        index = int(index)

        accumulator = tool_call_parts.setdefault(
            index,
            {"id": "", "name": "", "arguments": []},
        )

        call_id = _get_value(tool_call_delta, "id")
        if call_id:
            accumulator["id"] = str(call_id)

        function = _get_value(tool_call_delta, "function", {})
        name_delta = _get_value(function, "name")
        if name_delta:
            accumulator["name"] = f"{accumulator['name']}{name_delta}"

        arguments_delta = _get_value(function, "arguments")
        if arguments_delta:
            accumulator["arguments"].append(str(arguments_delta))

    def _flush_tool_calls(
        self,
        tool_call_parts: dict[int, dict[str, Any]],
        emitted_tool_call_indexes: set[int],
        *,
        provider_raw: Any,
    ) -> Iterable[ToolCallReadyEvent]:
        for index in sorted(tool_call_parts):
            if index in emitted_tool_call_indexes:
                continue

            parts = tool_call_parts[index]
            raw_arguments = "".join(parts["arguments"])
            emitted_tool_call_indexes.add(index)
            yield ToolCallReadyEvent(
                name=str(parts["name"]),
                arguments=self._parse_tool_arguments(raw_arguments),
                call_id=str(parts["id"] or "") or None,
                raw_arguments=raw_arguments,
                index=index,
                provider_raw=provider_raw,
            )

    @staticmethod
    def _parse_tool_arguments(raw_arguments: str) -> Mapping[str, Any]:
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return {}

        if isinstance(arguments, Mapping):
            return dict(arguments)
        return {}

    @staticmethod
    def _done_canonical_items(
        *,
        assistant_message: Mapping[str, Any],
        tool_calls: Sequence[ToolCallReadyEvent],
    ) -> tuple[CanonicalItem, ...]:
        payload = dict(assistant_message)
        metadata = {
            "tool_calls": [
                {
                    "name": tool_call.name,
                    "call_id": tool_call.call_id or "",
                    "arguments": dict(tool_call.arguments),
                    "raw_arguments": tool_call.raw_arguments or "",
                }
                for tool_call in tool_calls
            ]
        }
        return (
            CanonicalItem(
                type="message",
                role="assistant",
                content=payload.get("content"),
                provider_raw=ProviderRaw(
                    provider_id="openai_chat_completions",
                    event_type="assistant_message",
                    payload=payload,
                ),
                metadata=metadata,
            ),
        )


def _get_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _first_present_value(value: Any, keys: Sequence[str]) -> Any:
    for key in keys:
        item = _get_value(value, key)
        if item is not None:
            return item
    return None


def _as_sequence(value: Any) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return (value,)


def _to_mapping(value: Any) -> Mapping[str, Any] | None:
    plain = to_plain_value(value)
    return dict(plain) if isinstance(plain, Mapping) else None


def _accumulate_assistant_delta(
    message: dict[str, Any],
    delta: Any,
) -> None:
    plain_delta = _to_mapping(delta)
    if not plain_delta:
        return

    for key, value in plain_delta.items():
        if value is None or key == "tool_calls":
            continue
        if key == "role":
            message["role"] = str(value)
            continue
        if isinstance(value, str):
            message[key] = f"{message.get(key, '')}{value}"
            continue
        if isinstance(value, list):
            message.setdefault(key, [])
            if isinstance(message[key], list):
                message[key].extend(value)
            continue
        if isinstance(value, Mapping):
            existing = message.get(key)
            message[key] = {
                **(dict(existing) if isinstance(existing, Mapping) else {}),
                **dict(value),
            }
            continue
        message[key] = value


def _finalize_assistant_message(
    message: Mapping[str, Any],
    tool_call_parts: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any]:
    final_message = dict(message)
    final_message["role"] = "assistant"
    if tool_call_parts:
        final_message["tool_calls"] = [
            {
                "id": str(parts.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(parts.get("name") or ""),
                    "arguments": "".join(parts.get("arguments") or ()) or "{}",
                },
            }
            for _, parts in sorted(tool_call_parts.items())
        ]
    if final_message.get("content") == "" and not tool_call_parts:
        final_message["content"] = ""
    return final_message


def _reasoning_delta_text(delta: Any) -> str:
    for key in ("reasoning_content", "reasoning_text", "reasoning"):
        value = _get_value(delta, key)
        if isinstance(value, str) and value:
            return value
    return ""


def _arguments_to_json(arguments: Any) -> str:
    if arguments is None:
        return "{}"
    if isinstance(arguments, str):
        return arguments or "{}"
    return _json_dumps(arguments)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _normalize_chat_content_part(part: Any) -> dict[str, Any] | None:
    if part is None:
        return None
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if not isinstance(part, Mapping):
        return {"type": "text", "text": _json_dumps(part)}

    part_type = str(part.get("type") or "")
    if "text" in part and part_type in {"", "text", "input_text", "output_text"}:
        text = str(part.get("text") or "")
        return {"type": "text", "text": text} if text else None

    if part_type == "image_url" and isinstance(part.get("image_url"), Mapping):
        image_url = dict(part["image_url"])
        url = str(image_url.get("url") or "")
        if not url:
            return None
        normalized: dict[str, Any] = {"type": "image_url", "image_url": {"url": url}}
        detail = image_url.get("detail")
        if detail:
            normalized["image_url"]["detail"] = str(detail)
        return normalized

    if part_type == "input_image":
        url = str(part.get("image_url") or "")
        if not url:
            return None
        normalized: dict[str, Any] = {"type": "image_url", "image_url": {"url": url}}
        detail = part.get("detail")
        if detail:
            normalized["image_url"]["detail"] = str(detail)
        return normalized

    raise ValueError(f"Unsupported Chat Completions content part: {part_type or 'unknown'}")


__all__ = ["ChatCompletionsAdapter"]
