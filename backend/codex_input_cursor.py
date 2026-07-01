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


DYNAMIC_FINGERPRINT_KEYS = frozenset({"id"})


def canonical_provider_item_for_request(item: Any) -> Any:
    """Return the Responses item shape Codex is expected to resend as input."""

    if not isinstance(item, Mapping):
        return normalize_provider_item(item)

    item_type = str(item.get("type") or "").strip()
    if item_type == "message":
        result = _pick(item, "type", "role", "content", "phase", "internal_chat_message_metadata_passthrough")
        if "content" in result:
            result["content"] = _canonical_message_content(result["content"])
        return result
    if item_type == "agent_message":
        result = _pick(item, "type", "author", "recipient", "content", "internal_chat_message_metadata_passthrough")
        if "content" in result:
            result["content"] = _canonical_agent_message_content(result["content"])
        return result
    if item_type == "reasoning":
        return _canonical_reasoning_item(item)
    if item_type == "function_call":
        return _pick(item, "type", "name", "namespace", "arguments", "call_id", "internal_chat_message_metadata_passthrough")
    if item_type == "function_call_output":
        return _pick(item, "type", "call_id", "output", "internal_chat_message_metadata_passthrough")
    if item_type == "custom_tool_call":
        return _pick(item, "type", "status", "call_id", "name", "input", "internal_chat_message_metadata_passthrough")
    if item_type == "custom_tool_call_output":
        return _pick(item, "type", "call_id", "name", "output", "internal_chat_message_metadata_passthrough")
    if item_type == "local_shell_call":
        return _pick(item, "type", "call_id", "status", "action", "internal_chat_message_metadata_passthrough")
    if item_type == "tool_search_call":
        return _pick(item, "type", "call_id", "status", "execution", "arguments", "internal_chat_message_metadata_passthrough")
    if item_type == "tool_search_output":
        return _pick(item, "type", "call_id", "status", "execution", "tools", "internal_chat_message_metadata_passthrough")
    if item_type == "web_search_call":
        return _pick(item, "type", "status", "action", "internal_chat_message_metadata_passthrough")
    if item_type == "image_generation_call":
        return _pick(item, "type", "status", "revised_prompt", "result", "internal_chat_message_metadata_passthrough")
    if item_type in {"compaction", "compaction_summary"}:
        result = _pick(item, "type", "encrypted_content", "internal_chat_message_metadata_passthrough")
        result["type"] = "compaction"
        return result
    if item_type == "context_compaction":
        return _pick(item, "type", "encrypted_content", "internal_chat_message_metadata_passthrough")
    if item_type == "additional_tools":
        return _pick(item, "type", "role", "tools")

    return normalize_provider_item(item)


def canonical_provider_items_for_request(items: Sequence[Any]) -> list[Any]:
    return [canonical_provider_item_for_request(item) for item in items]


def _pick(item: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    return {
        key: normalize_provider_item(item[key])
        for key in keys
        if key in item and key not in DYNAMIC_FINGERPRINT_KEYS
    }


def _drop_none(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if value is not None}


def _canonical_reasoning_item(item: Mapping[str, Any]) -> dict[str, Any]:
    result = _drop_none(
        _pick(item, "type", "summary", "content", "encrypted_content", "internal_chat_message_metadata_passthrough")
    )
    content = result.get("content")
    if isinstance(content, list) and not _should_serialize_reasoning_content(content):
        del result["content"]
    return result


def _should_serialize_reasoning_content(content: list[Any]) -> bool:
    return any(isinstance(part, Mapping) and part.get("type") == "reasoning_text" for part in content)


def _canonical_message_content(content: Any) -> Any:
    if not isinstance(content, list):
        return normalize_provider_item(content)

    canonical: list[Any] = []
    for part in content:
        if not isinstance(part, Mapping):
            canonical.append(normalize_provider_item(part))
            continue
        part_type = str(part.get("type") or "").strip()
        if part_type in {"input_text", "output_text"}:
            canonical.append(_pick(part, "type", "text"))
        elif part_type == "input_image":
            canonical.append(_pick(part, "type", "image_url", "detail"))
        else:
            canonical.append(normalize_provider_item(part))
    return canonical


def _canonical_agent_message_content(content: Any) -> Any:
    if not isinstance(content, list):
        return normalize_provider_item(content)

    canonical: list[Any] = []
    for part in content:
        if not isinstance(part, Mapping):
            canonical.append(normalize_provider_item(part))
            continue
        part_type = str(part.get("type") or "").strip()
        if part_type == "input_text":
            canonical.append(_pick(part, "type", "text"))
        elif part_type == "encrypted_content":
            canonical.append(_pick(part, "type", "encrypted_content"))
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
    """Return a stable semantic projection used for provider item hashing.

    Only exact ``id`` keys are removed.  Semantic identifiers such as
    ``call_id`` or ``file_id`` are preserved, as are opaque reasoning fields
    such as ``encrypted_content``.
    """

    if isinstance(value, Mapping):
        return {
            key: normalize_provider_item(child)
            for key, child in value.items()
            if key not in DYNAMIC_FINGERPRINT_KEYS
        }
    if isinstance(value, list):
        return [normalize_provider_item(child) for child in value]
    if isinstance(value, tuple):
        return [normalize_provider_item(child) for child in value]
    return value


def fingerprint_provider_item(item: Any) -> str:
    """Hash a provider item after semantic normalization."""

    normalized = semantic_provider_item_for_fingerprint(canonical_provider_item_for_request(item))
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def semantic_provider_item_for_fingerprint(value: Any) -> Any:
    if isinstance(value, Mapping):
        item_type = str(value.get("type") or "").strip()
        ignored_keys = {"id"}
        if item_type == "message":
            ignored_keys.add("phase")
        return {
            key: semantic_provider_item_for_fingerprint(child)
            for key, child in value.items()
            if key not in ignored_keys
        }
    if isinstance(value, list):
        return [semantic_provider_item_for_fingerprint(child) for child in value]
    if isinstance(value, tuple):
        return [semantic_provider_item_for_fingerprint(child) for child in value]
    return value


def provider_items_equal(left: Any, right: Any) -> bool:
    """Compare two provider items by normalized fingerprint."""

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
