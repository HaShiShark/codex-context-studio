from __future__ import annotations

from typing import Any


def normalize_node_locks(raw_locks: Any) -> dict[str, bool]:
    if not isinstance(raw_locks, dict):
        return {}
    locks: dict[str, bool] = {}
    for raw_key, raw_value in raw_locks.items():
        node_id = str(raw_key or "").strip()
        if node_id:
            locks[node_id] = bool(raw_value)
    return locks


def transcript_node_id(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    return str(node.get("id") or "").strip()


def transcript_node_role(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    return str(node.get("role") or "").strip()


def transcript_node_ids(transcript: Any) -> set[str]:
    if not isinstance(transcript, list):
        return set()
    return {
        node_id
        for node_id in (transcript_node_id(node) for node in transcript)
        if node_id
    }


def transcript_nodes_by_id(transcript: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(transcript, list):
        return {}
    nodes: dict[str, dict[str, Any]] = {}
    for node in transcript:
        if not isinstance(node, dict):
            continue
        node_id = transcript_node_id(node)
        if node_id:
            nodes[node_id] = node
    return nodes


def default_node_locked(node: Any) -> bool:
    return transcript_node_role(node) == "developer"


def is_node_locked(node: Any, node_locks: dict[str, bool] | None = None) -> bool:
    locks = normalize_node_locks(node_locks or {})
    node_id = transcript_node_id(node)
    if node_id and node_id in locks:
        return bool(locks[node_id])
    return default_node_locked(node)


def effective_node_lock_overrides(
    node_locks: dict[str, bool] | None,
    transcript: Any,
) -> dict[str, bool]:
    locks = normalize_node_locks(node_locks or {})
    nodes_by_id = transcript_nodes_by_id(transcript)
    if not nodes_by_id:
        return {}

    effective_locks: dict[str, bool] = {}
    for node_id, explicit_locked in locks.items():
        node = nodes_by_id.get(node_id)
        if node is None:
            continue
        if bool(explicit_locked) != default_node_locked(node):
            effective_locks[node_id] = bool(explicit_locked)
    return effective_locks
