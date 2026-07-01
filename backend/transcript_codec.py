from __future__ import annotations

import copy
import hashlib
import json
import uuid
from typing import Any, Sequence, TypedDict

from .codex_item_registry import CODEX_ITEM_REGISTRY
from .codex_input_cursor import canonical_provider_item_for_request


class NodeItem(TypedDict):
    kind: str
    providerItem: dict[str, Any]
    inputIndex: int


class TranscriptNode(TypedDict):
    id: str
    role: str
    items: list[NodeItem]
    source_map: dict[str, str]


NON_DICT_PROVIDER_ITEM_MARKER = "__hash_context_non_dict_provider_item__"

_MESSAGE_TYPE = "message"
_AGENT_MESSAGE_TYPES = CODEX_ITEM_REGISTRY.subagent_message_item_types
_REASONING_TYPES = {"reasoning"}
_TOOL_CALL_TYPES = CODEX_ITEM_REGISTRY.tool_call_item_types
_TOOL_OUTPUT_TYPES = CODEX_ITEM_REGISTRY.tool_output_item_types
_COMPACTION_TYPES = CODEX_ITEM_REGISTRY.compaction_item_types
_ADDITIONAL_TOOLS_TYPES = CODEX_ITEM_REGISTRY.developer_context_item_types


def input_items_to_transcript(input_items: Sequence[Any]) -> list[TranscriptNode]:
    """Group provider input items into transcript nodes without dropping items."""

    transcript: list[TranscriptNode] = []
    current_assistant: TranscriptNode | None = None

    for input_index, raw_item in enumerate(input_items):
        node_item = _node_item(raw_item, input_index)
        current_assistant = _append_node_item(transcript, current_assistant, node_item)

    return transcript


def append_input_items(
    transcript: list[TranscriptNode],
    input_items: Sequence[Any],
) -> list[TranscriptNode] | None:
    """Append provider input items to an existing transcript in place."""

    if not isinstance(transcript, list):
        return None

    current_assistant = _current_assistant_from_transcript(transcript)
    next_input_index = _next_input_index(transcript)

    for offset, raw_item in enumerate(input_items):
        node_item = _node_item(raw_item, next_input_index + offset)
        current_assistant = _append_node_item(transcript, current_assistant, node_item)

    return transcript


def transcript_to_input_items(transcript: Sequence[TranscriptNode]) -> list[Any]:
    """Rebuild provider input items from transcript nodes."""

    node_items: list[NodeItem] = []
    for node in transcript:
        items = node.get("items", [])
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                node_items.append(item)

    if node_items and all(isinstance(item.get("inputIndex"), int) for item in node_items):
        node_items = sorted(node_items, key=lambda item: item["inputIndex"])

    return [_unwrap_provider_item(item.get("providerItem")) for item in node_items]


def _node_item(raw_item: Any, input_index: int) -> NodeItem:
    provider_item = _wrap_provider_item(raw_item)
    return {
        "kind": _item_kind(provider_item),
        "providerItem": provider_item,
        "inputIndex": input_index,
    }


def _wrap_provider_item(raw_item: Any) -> dict[str, Any]:
    if isinstance(raw_item, dict):
        return copy.deepcopy(raw_item)
    return {
        NON_DICT_PROVIDER_ITEM_MARKER: True,
        "value": copy.deepcopy(raw_item),
    }


def _unwrap_provider_item(provider_item: Any) -> Any:
    if (
        isinstance(provider_item, dict)
        and provider_item.get(NON_DICT_PROVIDER_ITEM_MARKER) is True
        and set(provider_item.keys()) == {NON_DICT_PROVIDER_ITEM_MARKER, "value"}
    ):
        return copy.deepcopy(provider_item.get("value"))
    return copy.deepcopy(provider_item)


def _append_new_node(transcript: list[TranscriptNode], role: str, item: NodeItem) -> TranscriptNode:
    node = _new_node(role)
    transcript.append(node)
    _append_item(node, item)
    return node


def _new_node(role: str) -> TranscriptNode:
    node: TranscriptNode = {
        "id": f"node_{uuid.uuid4().hex}",
        "role": role,
        "items": [],
        "source_map": {},
    }
    return node


def _append_node_item(
    transcript: list[TranscriptNode],
    current_assistant: TranscriptNode | None,
    node_item: NodeItem,
) -> TranscriptNode | None:
    provider_item = node_item["providerItem"]
    item_type = _item_type(provider_item)

    if _is_message(provider_item):
        role = _item_role(provider_item)
        if role == "user":
            _append_new_node(transcript, "user", node_item)
            return None
        if role in {"developer", "system"}:
            _append_new_node(transcript, role, node_item)
            return None
        if role == "assistant":
            target = _assistant_node(transcript, current_assistant)
            _append_item(target, node_item)
            return target
        _append_new_node(transcript, role, node_item)
        return None

    if item_type in _AGENT_MESSAGE_TYPES:
        _append_new_node(transcript, "subagent", node_item)
        return None

    if item_type in _REASONING_TYPES or item_type in _TOOL_CALL_TYPES:
        target = _assistant_node(transcript, current_assistant)
        _append_item(target, node_item)
        return target

    if item_type in _TOOL_OUTPUT_TYPES:
        target = _find_assistant_for_tool_output(transcript, provider_item)
        if target is None:
            target = _assistant_node(transcript, current_assistant)
        _append_item(target, node_item)
        return target

    if item_type in _COMPACTION_TYPES:
        _append_new_node(transcript, _item_role(provider_item), node_item)
        return None

    if item_type in _ADDITIONAL_TOOLS_TYPES:
        _append_new_node(transcript, "developer", node_item)
        return None

    _append_new_node(transcript, _item_role(provider_item), node_item)
    return None


def _assistant_node(
    transcript: list[TranscriptNode],
    current_assistant: TranscriptNode | None,
) -> TranscriptNode:
    if current_assistant is not None:
        return current_assistant
    node = _new_node("assistant")
    transcript.append(node)
    return node


def _append_item(node: TranscriptNode, item: NodeItem) -> None:
    item_index = len(node["items"])
    node["items"].append(item)
    node["source_map"][_source_fingerprint(item["providerItem"])] = f"items[{item_index}]"


def _find_assistant_for_tool_output(
    transcript: Sequence[TranscriptNode],
    output_item: dict[str, Any],
) -> TranscriptNode | None:
    call_id = str(output_item.get("call_id") or "").strip()
    latest_assistant: TranscriptNode | None = None

    for node in reversed(transcript):
        if node.get("role") != "assistant":
            continue
        if latest_assistant is None:
            latest_assistant = node
        if call_id and _assistant_has_call_id(node, call_id):
            return node

    return latest_assistant


def _current_assistant_from_transcript(transcript: Sequence[TranscriptNode]) -> TranscriptNode | None:
    last_index: int | None = None
    last_node: TranscriptNode | None = None

    for node in transcript:
        if not isinstance(node, dict):
            continue
        items = node.get("items", [])
        if not isinstance(items, list):
            continue
        for node_item in items:
            if not isinstance(node_item, dict):
                continue
            input_index = node_item.get("inputIndex")
            if type(input_index) is not int:
                continue
            if last_index is None or input_index > last_index:
                last_index = input_index
                last_node = node

    if last_node is not None:
        return last_node if last_node.get("role") == "assistant" else None

    if transcript:
        tail_node = transcript[-1]
        if isinstance(tail_node, dict) and tail_node.get("role") == "assistant":
            return tail_node
    return None


def _next_input_index(transcript: Sequence[TranscriptNode]) -> int:
    max_index = -1
    for node in transcript:
        if not isinstance(node, dict):
            continue
        items = node.get("items", [])
        if not isinstance(items, list):
            continue
        for node_item in items:
            if not isinstance(node_item, dict):
                continue
            input_index = node_item.get("inputIndex")
            if type(input_index) is int:
                max_index = max(max_index, input_index)
    return max_index + 1


def _assistant_has_call_id(node: TranscriptNode, call_id: str) -> bool:
    for node_item in node.get("items", []):
        provider_item = node_item.get("providerItem")
        if not isinstance(provider_item, dict):
            continue
        item_call_id = str(provider_item.get("call_id") or provider_item.get("id") or "").strip()
        if item_call_id == call_id:
            return True
    return False


def _is_message(item: dict[str, Any]) -> bool:
    return _item_type(item) == _MESSAGE_TYPE


def _item_type(item: dict[str, Any]) -> str:
    value = item.get("type")
    return str(value) if value is not None else ""


def _item_kind(item: dict[str, Any]) -> str:
    if item.get(NON_DICT_PROVIDER_ITEM_MARKER) is True:
        return "non_dict"
    item_type = _item_type(item).strip()
    return item_type or "unknown"


def _item_role(item: dict[str, Any]) -> str:
    value = item.get("role")
    role = str(value).strip() if value is not None else ""
    return role or "unknown"


def _source_fingerprint(provider_item: dict[str, Any]) -> str:
    normalized = _normalize_for_source_map(canonical_provider_item_for_request(provider_item))
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_for_source_map(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_for_source_map(inner_value)
            for key, inner_value in value.items()
            if key != "id"
        }
    if isinstance(value, list):
        return [_normalize_for_source_map(item) for item in value]
    return value
