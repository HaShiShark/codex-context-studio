from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.adapters.base import (
    BaseAdapter,
    ProviderRequestContext,
    ToolSpec,
    reasoning_effort_token_budget,
)
from agent_runtime.core.stream_events import (
    AdapterStreamEvent,
    ProviderDoneEvent,
    ReasoningDeltaEvent,
    ReasoningDoneEvent,
    ReasoningStartEvent,
    TextDeltaEvent,
    ToolCallReadyEvent,
)


_PROMPT_BLOCK_LABELS: Mapping[str, str] = {
    "system": "System",
    "developer": "Developer",
    "memory": "Memory",
    "summary": "Summary",
}

_TOOL_CALL_ITEM_TYPES = {"tool_call", "function_call"}
_TOOL_RESULT_ITEM_TYPES = {"tool_result", "function_call_output"}
_DATA_URL_PATTERN = re.compile(r"^data:(?P<mime>[^;,]+);base64,(?P<data>.+)$", re.DOTALL)
_SUPPORTED_IMAGE_MEDIA_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}
_IMAGE_MEDIA_TYPE_ALIASES = {
    "image/jpg": "image/jpeg",
}


@dataclass(slots=True)
class _ToolUseBuffer:
    index: int
    call_id: str = ""
    name: str = ""
    json_chunks: list[str] = field(default_factory=list)
    provider_raw: Any | None = None

    @property
    def raw_arguments(self) -> str:
        return "".join(self.json_chunks) or "{}"


@dataclass(slots=True)
class _ThinkingBlockBuffer:
    index: int
    block_type: str = "thinking"
    thinking_chunks: list[str] = field(default_factory=list)
    signature: str = ""
    provider_raw: Any | None = None

    def to_block(self) -> dict[str, Any]:
        block: dict[str, Any] = {"type": self.block_type}
        if self.block_type == "thinking":
            block["thinking"] = "".join(self.thinking_chunks)
            if self.signature:
                block["signature"] = self.signature
        elif isinstance(self.provider_raw, Mapping):
            block.update(dict(self.provider_raw))
        return block


class ClaudeAdapter(BaseAdapter[dict[str, Any]]):
    """Anthropic Messages API request and stream translation."""

    provider_name = "claude"

    def __init__(self, client: Any) -> None:
        self.client = client

    def build_request(self, context: ProviderRequestContext) -> dict[str, Any]:
        if context.model is None:
            raise ValueError("Claude request requires a model")

        request = {
            "model": context.model,
            "system": _compile_system_prompt(context.prompt_blocks),
            "messages": _compile_messages(
                context.transcript,
                context.current_turn,
            ),
            "stream": True,
            "max_tokens": context.provider_config.get("max_tokens", 4096),
        }
        thinking = _thinking_config(context.provider_config, context.reasoning_effort)
        if thinking:
            request["thinking"] = thinking
            budget = _get_value(thinking, "budget_tokens")
            if isinstance(budget, int) and request["max_tokens"] <= budget:
                request["max_tokens"] = budget + 1024

        if "temperature" in context.provider_config and not thinking:
            request["temperature"] = context.provider_config["temperature"]
        if "top_p" in context.provider_config:
            request["top_p"] = context.provider_config["top_p"]
        tools = [_normalize_tool_schema(tool) for tool in context.tools]
        if tools:
            request["tools"] = tools
        return request

    def stream_response(
        self,
        request: dict[str, Any],
        context: ProviderRequestContext | None = None,
    ) -> Iterable[AdapterStreamEvent]:
        del context

        stream = self.client.messages.stream(**dict(request))
        if hasattr(stream, "__enter__"):
            with stream as active_stream:
                yield from self._stream_events(active_stream)
            return

        yield from self._stream_events(stream)

    def _stream_events(self, stream: Iterable[Any]) -> Iterable[AdapterStreamEvent]:
        output_chunks: list[str] = []
        text_blocks: dict[int, list[str]] = {}
        thinking_buffers: dict[int, _ThinkingBlockBuffer] = {}
        tool_buffers: dict[int, _ToolUseBuffer] = {}
        emitted_tool_indexes: set[int] = set()
        canonical_items: list[dict[str, Any]] = []
        finish_reason: str | None = None
        usage: Mapping[str, Any] | None = None
        done_emitted = False
        active_reasoning_indexes: set[int] = set()

        for event in stream:
            event_type = _get_value(event, "type", "")

            if event_type == "content_block_start":
                index = _event_index(event)
                content_block = _get_value(event, "content_block")
                content_block_type = _get_value(content_block, "type")
                if content_block_type == "thinking":
                    thinking_buffers[index] = _ThinkingBlockBuffer(
                        index=index,
                        block_type="thinking",
                        provider_raw=content_block,
                    )
                    active_reasoning_indexes.add(index)
                    yield ReasoningStartEvent(provider_raw=event)
                    initial_thinking = _coerce_text(
                        _get_value(content_block, "thinking", "")
                    )
                    if initial_thinking:
                        thinking_buffers[index].thinking_chunks.append(initial_thinking)
                        yield ReasoningDeltaEvent(
                            delta=initial_thinking,
                            provider_raw=event,
                        )
                elif content_block_type == "redacted_thinking":
                    thinking_buffers[index] = _ThinkingBlockBuffer(
                        index=index,
                        block_type="redacted_thinking",
                        provider_raw=content_block,
                    )
                elif content_block_type == "text":
                    initial_text = _coerce_text(_get_value(content_block, "text", ""))
                    if initial_text:
                        text_blocks.setdefault(index, []).append(initial_text)
                elif content_block_type == "tool_use":
                    tool_buffers[index] = _ToolUseBuffer(
                        index=index,
                        call_id=_coerce_text(_get_value(content_block, "id", "")),
                        name=_coerce_text(_get_value(content_block, "name", "")),
                        provider_raw=content_block,
                    )
                    initial_input = _get_value(content_block, "input")
                    if isinstance(initial_input, Mapping) and initial_input:
                        tool_buffers[index].json_chunks.append(
                            json.dumps(dict(initial_input), ensure_ascii=False)
                        )

            elif event_type == "content_block_delta":
                index = _event_index(event)
                delta = _get_value(event, "delta")
                delta_type = _get_value(delta, "type", "")

                if delta_type == "text_delta":
                    text_delta = _coerce_text(_get_value(delta, "text", ""))
                    if text_delta:
                        output_chunks.append(text_delta)
                        text_blocks.setdefault(index, []).append(text_delta)
                        yield TextDeltaEvent(delta=text_delta, provider_raw=event)
                elif delta_type == "thinking_delta":
                    thinking_delta = _coerce_text(_get_value(delta, "thinking", ""))
                    if thinking_delta:
                        thinking_buffers.setdefault(
                            index,
                            _ThinkingBlockBuffer(index=index),
                        ).thinking_chunks.append(thinking_delta)
                        if index not in active_reasoning_indexes:
                            active_reasoning_indexes.add(index)
                            yield ReasoningStartEvent(provider_raw=event)
                        yield ReasoningDeltaEvent(
                            delta=thinking_delta,
                            provider_raw=event,
                        )
                elif delta_type == "signature_delta":
                    signature = _coerce_text(_get_value(delta, "signature", ""))
                    if signature:
                        thinking_buffers.setdefault(
                            index,
                            _ThinkingBlockBuffer(index=index),
                        ).signature += signature
                elif delta_type == "input_json_delta":
                    buffer = tool_buffers.setdefault(index, _ToolUseBuffer(index=index))
                    buffer.json_chunks.append(
                        _coerce_text(_get_value(delta, "partial_json", ""))
                    )

            elif event_type == "content_block_stop":
                index = _event_index(event)
                if index in active_reasoning_indexes:
                    active_reasoning_indexes.remove(index)
                    yield ReasoningDoneEvent(provider_raw=event)
                if index in tool_buffers and index not in emitted_tool_indexes:
                    tool_event = _tool_call_ready_event(tool_buffers[index], event)
                    emitted_tool_indexes.add(index)
                    yield tool_event

            elif event_type == "message_delta":
                delta = _get_value(event, "delta")
                finish_reason = _coerce_optional_text(
                    _get_value(delta, "stop_reason", finish_reason)
                )
                usage = _coerce_optional_mapping(_get_value(event, "usage", usage))

            elif event_type == "message_stop":
                for tool_event in _remaining_tool_events(
                    tool_buffers,
                    emitted_tool_indexes,
                    provider_raw=event,
                ):
                    yield tool_event

                canonical_items = _canonical_assistant_items(
                    text_blocks=text_blocks,
                    thinking_buffers=thinking_buffers,
                    tool_buffers=tool_buffers,
                    output_text="".join(output_chunks),
                )

                yield ProviderDoneEvent(
                    output_text="".join(output_chunks),
                    finish_reason=finish_reason,
                    usage=usage,
                    canonical_items=tuple(canonical_items),
                    provider_raw=event,
                )
                done_emitted = True

            elif event_type == "text_delta":
                text_delta = _coerce_text(_get_value(event, "text", ""))
                if text_delta:
                    output_chunks.append(text_delta)
                    yield TextDeltaEvent(delta=text_delta, provider_raw=event)

            elif event_type == "thinking_delta":
                thinking_delta = _coerce_text(_get_value(event, "thinking", ""))
                if thinking_delta:
                    if -1 not in active_reasoning_indexes:
                        active_reasoning_indexes.add(-1)
                        yield ReasoningStartEvent(provider_raw=event)
                    yield ReasoningDeltaEvent(delta=thinking_delta, provider_raw=event)

            elif event_type == "input_json_delta":
                index = _event_index(event)
                buffer = tool_buffers.setdefault(index, _ToolUseBuffer(index=index))
                buffer.json_chunks.append(
                    _coerce_text(_get_value(event, "partial_json", ""))
                )

        if not done_emitted:
            for _ in tuple(active_reasoning_indexes):
                yield ReasoningDoneEvent(provider_raw=None)
            active_reasoning_indexes.clear()
            for tool_event in _remaining_tool_events(
                tool_buffers,
                emitted_tool_indexes,
                provider_raw=None,
            ):
                yield tool_event

            canonical_items = _canonical_assistant_items(
                text_blocks=text_blocks,
                thinking_buffers=thinking_buffers,
                tool_buffers=tool_buffers,
                output_text="".join(output_chunks),
            )

            yield ProviderDoneEvent(
                output_text="".join(output_chunks),
                finish_reason=finish_reason,
                usage=usage,
                canonical_items=tuple(canonical_items),
            )


def _compile_system_prompt(prompt_blocks: Sequence[Any]) -> str:
    sections: list[str] = []
    for block in prompt_blocks:
        kind = _coerce_text(_get_value(block, "kind", "system"))
        label = _PROMPT_BLOCK_LABELS.get(kind, kind.title() or "System")
        text = _coerce_text(_get_value(block, "text", "")).strip()
        if text:
            sections.append(f"[{label}]\n{text}")
    return "\n\n".join(sections)


def _compile_messages(
    transcript: Sequence[Any],
    current_turn: Sequence[Any],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for record_or_item in (*transcript, *current_turn):
        for item in _canonical_items_from_record(record_or_item):
            _append_canonical_item(messages, item)
    return messages


def _canonical_items_from_record(record_or_item: Any) -> Iterable[Any]:
    item_type = _get_value(record_or_item, "type")
    if item_type in {"message", *_TOOL_CALL_ITEM_TYPES, *_TOOL_RESULT_ITEM_TYPES}:
        yield record_or_item
        return

    canonical_items = _get_value(record_or_item, "canonical_items")
    if canonical_items:
        yield from canonical_items
        return

    role = _get_value(record_or_item, "role")
    text = _get_value(record_or_item, "text", "")
    if role in {"user", "assistant"} and text is not None:
        yield {"type": "message", "role": role, "content": text}


def _append_canonical_item(messages: list[dict[str, Any]], item: Any) -> None:
    item_type = _coerce_text(_get_value(item, "type"))

    if item_type == "message":
        role = _coerce_text(_get_value(item, "role", ""))
        if role not in {"user", "assistant"}:
            raise ValueError(f"Claude messages only support user/assistant, got {role!r}")
        _append_blocks(messages, role, _content_to_blocks(_get_value(item, "content")))
        return

    if item_type in _TOOL_CALL_ITEM_TYPES:
        _append_blocks(
            messages,
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": _coerce_text(_get_value(item, "call_id", "")),
                    "name": _coerce_text(_get_value(item, "name", "")),
                    "input": _coerce_json_object(_get_value(item, "arguments")),
                }
            ],
        )
        return

    if item_type in _TOOL_RESULT_ITEM_TYPES:
        call_id = _coerce_text(_get_value(item, "call_id", ""))
        output = _get_value(item, "output")
        if output is None:
            output = _get_value(item, "content", "")
        _append_blocks(
            messages,
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": _stringify_tool_result(output),
                }
            ],
        )
        return

    raise ValueError(f"Unsupported canonical item type for Claude: {item_type!r}")


def _append_blocks(
    messages: list[dict[str, Any]],
    role: str,
    blocks: list[dict[str, Any]],
) -> None:
    if not blocks:
        return

    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
        return

    messages.append({"role": role, "content": blocks})


def _content_to_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []

    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for value in content:
            blocks.extend(_content_to_blocks(value))
        return blocks

    if isinstance(content, Mapping):
        block_type = _coerce_text(content.get("type"))
        if block_type in {"text", "input_text", "output_text"}:
            text = _coerce_text(content.get("text", ""))
            return [{"type": "text", "text": text}] if text else []
        if block_type == "input_image":
            block = _image_block_from_input_image(content)
            return [block] if block else []
        if block_type == "image_url":
            block = _image_block_from_image_url_part(content)
            return [block] if block else []
        if block_type == "image" and isinstance(content.get("source"), Mapping):
            return [dict(content)]
        if block_type in {"thinking", "redacted_thinking", "tool_use", "tool_result"}:
            return [dict(content)]
        return [{"type": "text", "text": json.dumps(content, ensure_ascii=False)}]

    text = _coerce_text(content)
    return [{"type": "text", "text": text}] if text else []


def _image_block_from_input_image(part: Mapping[str, Any]) -> dict[str, Any] | None:
    image_url = _coerce_text(part.get("image_url") or part.get("url") or "").strip()
    return _image_block_from_url(image_url)


def _image_block_from_image_url_part(part: Mapping[str, Any]) -> dict[str, Any] | None:
    image_url = part.get("image_url")
    if isinstance(image_url, Mapping):
        url = _coerce_text(image_url.get("url") or "").strip()
    else:
        url = _coerce_text(image_url or part.get("url") or "").strip()
    return _image_block_from_url(url)


def _image_block_from_url(url: str) -> dict[str, Any] | None:
    if not url:
        return None

    data_url_match = _DATA_URL_PATTERN.match(url)
    if data_url_match:
        media_type = _normalize_image_media_type(data_url_match.group("mime"))
        data = "".join(_coerce_text(data_url_match.group("data")).split())
        if not data:
            return None
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            },
        }

    if url.startswith(("http://", "https://")):
        return {
            "type": "image",
            "source": {
                "type": "url",
                "url": url,
            },
        }

    raise ValueError("Claude image input requires a data URL or an http(s) URL")


def _normalize_image_media_type(media_type: Any) -> str:
    normalized = _coerce_text(media_type).strip().lower()
    normalized = _IMAGE_MEDIA_TYPE_ALIASES.get(normalized, normalized)
    if normalized not in _SUPPORTED_IMAGE_MEDIA_TYPES:
        supported = ", ".join(sorted(_SUPPORTED_IMAGE_MEDIA_TYPES))
        raise ValueError(
            f"Claude image input supports {supported}; got {normalized or 'unknown'}"
        )
    return normalized


def _normalize_tool_schema(tool: ToolSpec | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(tool, ToolSpec):
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": dict(tool.parameters),
        }

    tool_mapping = dict(tool)
    if isinstance(tool_mapping.get("function"), Mapping):
        function_schema = dict(tool_mapping["function"])
        return {
            "name": _coerce_text(function_schema.get("name", "")),
            "description": _coerce_text(function_schema.get("description", "")),
            "input_schema": _schema_or_default(function_schema.get("parameters")),
        }

    return {
        "name": _coerce_text(tool_mapping.get("name", "")),
        "description": _coerce_text(tool_mapping.get("description", "")),
        "input_schema": _schema_or_default(
            tool_mapping.get("input_schema", tool_mapping.get("parameters"))
        ),
    }


def _thinking_config(
    provider_config: Mapping[str, Any],
    reasoning_effort: str | None,
) -> dict[str, Any] | None:
    raw_thinking = provider_config.get("thinking")
    if isinstance(raw_thinking, Mapping):
        return dict(raw_thinking)

    budget = provider_config.get("thinking_budget")
    if isinstance(budget, int) and budget > 0:
        return {
            "type": "enabled",
            "budget_tokens": max(1024, budget),
            "display": "summarized",
        }

    effort_budget = reasoning_effort_token_budget(reasoning_effort)
    if effort_budget is None:
        return None
    return {
        "type": "enabled",
        "budget_tokens": effort_budget,
        "display": "summarized",
    }


def _schema_or_default(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {"type": "object", "properties": {}}


def _tool_call_ready_event(
    buffer: _ToolUseBuffer,
    provider_raw: Any | None,
) -> ToolCallReadyEvent:
    raw_arguments = buffer.raw_arguments
    return ToolCallReadyEvent(
        name=buffer.name,
        arguments=_parse_tool_arguments(raw_arguments),
        call_id=buffer.call_id,
        raw_arguments=raw_arguments,
        index=buffer.index,
        provider_raw=provider_raw or buffer.provider_raw,
    )


def _remaining_tool_events(
    tool_buffers: Mapping[int, _ToolUseBuffer],
    emitted_tool_indexes: set[int],
    *,
    provider_raw: Any | None,
) -> Iterable[ToolCallReadyEvent]:
    for index in sorted(tool_buffers):
        if index in emitted_tool_indexes:
            continue
        emitted_tool_indexes.add(index)
        yield _tool_call_ready_event(tool_buffers[index], provider_raw)


def _canonical_assistant_items(
    *,
    text_blocks: Mapping[int, list[str]],
    thinking_buffers: Mapping[int, _ThinkingBlockBuffer],
    tool_buffers: Mapping[int, _ToolUseBuffer],
    output_text: str,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    indexes = sorted({*text_blocks, *thinking_buffers, *tool_buffers})
    for index in indexes:
        if index in thinking_buffers:
            block = thinking_buffers[index].to_block()
            if block.get("type") != "thinking" or block.get("thinking") or block.get("signature"):
                blocks.append(block)
        if index in text_blocks:
            text = "".join(text_blocks[index])
            if text:
                blocks.append({"type": "text", "text": text})
        if index in tool_buffers:
            buffer = tool_buffers[index]
            blocks.append(
                {
                    "type": "tool_use",
                    "id": buffer.call_id,
                    "name": buffer.name,
                    "input": _parse_tool_arguments(buffer.raw_arguments),
                }
            )

    if not blocks and output_text:
        blocks.append({"type": "text", "text": output_text})

    if not blocks:
        return []

    return [{"type": "message", "role": "assistant", "content": blocks}]


def _parse_tool_arguments(raw_arguments: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _coerce_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        return dict(_parse_tool_arguments(value))
    return {}


def _stringify_tool_result(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _event_index(event: Any) -> int:
    index = _get_value(event, "index", 0)
    try:
        return int(index)
    except (TypeError, ValueError):
        return 0


def _get_value(value: Any, key: str, default: Any | None = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return _coerce_text(value)


def _coerce_optional_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    return None


__all__ = ["ClaudeAdapter"]
