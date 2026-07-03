"""Cursor diff utilities for Codex provider input items.

The cursor is an auxiliary sequence of provider items that have already been
absorbed into transcript.  This module deliberately knows only how to compare
provider item sequences.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


_MESSAGE_FIELDS = ("type", "role", "content", "phase", "internal_chat_message_metadata_passthrough")
_AGENT_MESSAGE_FIELDS = ("type", "author", "recipient", "content", "internal_chat_message_metadata_passthrough")
_REASONING_FIELDS = ("type", "summary", "content", "encrypted_content", "internal_chat_message_metadata_passthrough")
_COMPACTION_TYPES = {"compaction", "compaction_summary"}
_SIMPLE_RESPONSE_ITEM_FIELDS: dict[str, tuple[str, ...]] = {
    "function_call": ("type", "name", "namespace", "arguments", "call_id", "internal_chat_message_metadata_passthrough"),
    "function_call_output": ("type", "call_id", "output", "internal_chat_message_metadata_passthrough"),
    "custom_tool_call": ("type", "status", "call_id", "name", "input", "internal_chat_message_metadata_passthrough"),
    "custom_tool_call_output": ("type", "call_id", "name", "output", "internal_chat_message_metadata_passthrough"),
    "local_shell_call": ("type", "call_id", "status", "action", "internal_chat_message_metadata_passthrough"),
    "tool_search_call": ("type", "call_id", "status", "execution", "arguments", "internal_chat_message_metadata_passthrough"),
    "tool_search_output": ("type", "call_id", "status", "execution", "tools", "internal_chat_message_metadata_passthrough"),
    "web_search_call": ("type", "status", "action", "internal_chat_message_metadata_passthrough"),
    "image_generation_call": ("type", "status", "revised_prompt", "result", "internal_chat_message_metadata_passthrough"),
    "context_compaction": ("type", "encrypted_content", "internal_chat_message_metadata_passthrough"),
    "additional_tools": ("type", "role", "tools"),
}


def response_item_to_request_item(item: Any, *, include_id: bool = False) -> Any:
    """Project an upstream response item into the shape Codex resends as input."""

    if not isinstance(item, Mapping):
        return normalize_provider_item(item)

    item_type = str(item.get("type") or "").strip()
    if item_type == "message":
        result = _pick(item, *_MESSAGE_FIELDS, include_id=include_id)
        if "content" in result:
            result["content"] = _canonical_message_content(result["content"], include_id=include_id)
        return result
    if item_type == "agent_message":
        result = _pick(item, *_AGENT_MESSAGE_FIELDS, include_id=include_id)
        if "content" in result:
            result["content"] = _canonical_agent_message_content(result["content"], include_id=include_id)
        return result
    if item_type == "reasoning":
        return _canonical_reasoning_item(item, include_id=include_id)

    if item_type in _COMPACTION_TYPES:
        result = _pick(item, "type", "encrypted_content", "internal_chat_message_metadata_passthrough", include_id=include_id)
        result["type"] = "compaction"
        return result

    fields = _SIMPLE_RESPONSE_ITEM_FIELDS.get(item_type)
    if fields is not None:
        return _pick(item, *fields, include_id=include_id)

    return normalize_provider_item(item)


def response_items_to_request_items(items: Sequence[Any], *, include_ids: bool = False) -> list[Any]:
    return [response_item_to_request_item(item, include_id=include_ids) for item in items]


def _pick(item: Mapping[str, Any], *keys: str, include_id: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if include_id and "id" in item:
        result["id"] = normalize_provider_item(item["id"])
    for key in keys:
        if key in item and key != "id":
            result[key] = normalize_provider_item(item[key])
    return result


def _canonical_reasoning_item(item: Mapping[str, Any], *, include_id: bool) -> dict[str, Any]:
    result = _pick(item, *_REASONING_FIELDS, include_id=include_id)
    if result.get("internal_chat_message_metadata_passthrough") is None:
        result.pop("internal_chat_message_metadata_passthrough", None)
    content = result.get("content")
    if isinstance(content, list) and not _should_serialize_reasoning_content(content):
        del result["content"]
    return result


def _should_serialize_reasoning_content(content: list[Any]) -> bool:
    return any(isinstance(part, Mapping) and part.get("type") == "reasoning_text" for part in content)


def _canonical_message_content(content: Any, *, include_id: bool) -> Any:
    if not isinstance(content, list):
        return normalize_provider_item(content)

    canonical: list[Any] = []
    for part in content:
        if not isinstance(part, Mapping):
            canonical.append(normalize_provider_item(part))
            continue
        part_type = str(part.get("type") or "").strip()
        if part_type in {"input_text", "output_text"}:
            canonical.append(_pick(part, "type", "text", include_id=include_id))
        elif part_type == "input_image":
            canonical.append(_pick(part, "type", "image_url", "detail", include_id=include_id))
        else:
            canonical.append(normalize_provider_item(part))
    return canonical


def _canonical_agent_message_content(content: Any, *, include_id: bool) -> Any:
    if not isinstance(content, list):
        return normalize_provider_item(content)

    canonical: list[Any] = []
    for part in content:
        if not isinstance(part, Mapping):
            canonical.append(normalize_provider_item(part))
            continue
        part_type = str(part.get("type") or "").strip()
        if part_type == "input_text":
            canonical.append(_pick(part, "type", "text", include_id=include_id))
        elif part_type == "encrypted_content":
            canonical.append(_pick(part, "type", "encrypted_content", include_id=include_id))
        else:
            canonical.append(normalize_provider_item(part))
    return canonical


@dataclass(frozen=True)
class CursorDiff:
    """The suffix delta between the current cursor and a new input array."""

    prefix_len: int
    pop: list[Any]
    append: list[Any]

    def __iter__(self):
        yield self.prefix_len
        yield self.pop
        yield self.append


def normalize_provider_item(value: Any) -> Any:
    """Return a request-safe deep normalization of provider values.

    This function deliberately preserves nested ``id`` keys. Tool schemas can
    contain semantic fields named ``id`` (for example
    ``parameters.properties.id``), and deleting them corrupts the request body.
    Dynamic response item ids are ignored only by ``_pick`` at known item
    boundaries and by the fingerprint-only normalization below.
    """

    if isinstance(value, Mapping):
        return {
            key: normalize_provider_item(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [normalize_provider_item(child) for child in value]
    if isinstance(value, tuple):
        return [normalize_provider_item(child) for child in value]
    return value


def fingerprint_provider_item(item: Any) -> str:
    """Hash a provider item without suppressing protocol fields."""

    normalized = normalize_provider_item(item)
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def provider_items_equal(left: Any, right: Any) -> bool:
    """Compare two provider items by exact normalized fingerprint."""

    return fingerprint_provider_item(left) == fingerprint_provider_item(right)


def longest_common_prefix_len(cursor: Sequence[Any], new_input: Sequence[Any]) -> int:
    """Return the normalized-fingerprint common prefix length."""

    prefix_len = 0
    max_len = min(len(cursor), len(new_input))
    while prefix_len < max_len:
        if not provider_items_equal(cursor[prefix_len], new_input[prefix_len]):
            break
        prefix_len += 1
    return prefix_len


def longest_common_prefix(cursor: Sequence[Any], new_input: Sequence[Any]) -> int:
    """Alias kept close to the design doc naming."""

    return longest_common_prefix_len(cursor, new_input)


def compute_diff(cursor: Sequence[Any], new_input: Sequence[Any]) -> CursorDiff:
    """Compute the suffix pop/append delta from cursor to new input."""

    prefix_len = longest_common_prefix_len(cursor, new_input)
    return CursorDiff(
        prefix_len=prefix_len,
        pop=list(cursor[prefix_len:]),
        append=list(new_input[prefix_len:]),
    )


normalize_for_fingerprint = normalize_provider_item
fingerprint_item = fingerprint_provider_item
