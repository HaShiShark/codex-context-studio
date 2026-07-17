from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
try:
    import tiktoken
except ImportError:
    tiktoken = None

_TOKEN_ENCODING: Any | None = None
_TOKEN_ENCODING_LOAD_FAILED = False

from simple_agent.agent import SimpleAgent, ToolEvent, sanitize_text
from simple_agent.codex_tool_registry import ToolExecution

from backend.web_constants import (
    ATTACHMENTS_DIR,
    ATTACHMENTS_ROUTE,
    CODEX_LOCAL_SESSIONS_DIR,
    CODEX_COMPACTION_ITEM_TYPES,
    CODEX_ITEM_DISPLAY_HINTS_BY_ITEM_TYPE,
    CODEX_PAIRED_TOOL_CALL_ITEM_TYPES,
    CODEX_STANDALONE_TOOL_CALL_ITEM_TYPES,
    CODEX_TOOL_CALL_ITEM_TYPES,
    CODEX_TOOL_OUTPUT_ITEM_TYPES,
    CODEX_TOOL_OUTPUT_TYPES_BY_CALL_TYPE,
    CONTEXT_INPUT_MESSAGE_ROLES,
    CONTEXT_INPUT_RECORD_ROLES,
    CONTEXT_REQUEST_DEBUG_FILE,
    CONTEXT_EDIT_MARKERS_FILE,
    ContextWorkbenchToolDefinition,
    REPO_ROOT,
    SessionState,
    STATE_FILE,
    STATE_DIR,
)
from backend.transcript_codec import (
    input_items_to_transcript as core_input_items_to_transcript,
    transcript_to_input_items as core_transcript_to_input_items,
)
from backend.node_locking import (
    is_node_locked,
    normalize_node_locks,
    transcript_node_id,
)

def sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {
            sanitize_value(key): sanitize_value(item)
            for key, item in value.items()
        }
    return value

def is_relative_to_path(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents

def resolve_attachment_file_path(relative_path: str) -> Path | None:
    safe_relative_path = sanitize_text(relative_path or "").replace("\\", "/").lstrip("/")
    if not safe_relative_path:
        return None

    route_prefix = f"{ATTACHMENTS_ROUTE}/"
    if safe_relative_path.startswith(route_prefix):
        attachment_name = safe_relative_path.removeprefix(route_prefix).strip("/")
        if not attachment_name or "/" in attachment_name:
            return None

        attachments_root = ATTACHMENTS_DIR.resolve()
        candidate = (ATTACHMENTS_DIR / attachment_name).resolve()
        return candidate if is_relative_to_path(candidate, attachments_root) else None

    repo_root = REPO_ROOT.resolve()
    candidate = (REPO_ROOT / safe_relative_path).resolve()
    return candidate if is_relative_to_path(candidate, repo_root) else None

def normalize_message_blocks(raw_blocks: Any) -> list[dict[str, object]]:
    if not isinstance(raw_blocks, list):
        return []

    normalized: list[dict[str, object]] = []
    for item in raw_blocks:
        if not isinstance(item, dict):
            continue

        kind = sanitize_text(item.get("kind") or "").strip()
        if kind == "text":
            text = sanitize_text(item.get("text") or "")
            if not text:
                continue
            normalized.append(
                {
                    "kind": "text",
                    "text": text,
                }
            )
            continue

        if kind == "reasoning":
            text = sanitize_text(item.get("text") or "")
            status = sanitize_text(item.get("status") or "").strip() or "completed"
            if not text and status != "streaming":
                continue
            normalized.append(
                {
                    "kind": "reasoning",
                    "text": text,
                    "status": "streaming" if status == "streaming" else "completed",
                }
            )
            continue

        if kind == "tool" and isinstance(item.get("tool_event"), dict):
            normalized.append(
                {
                    "kind": "tool",
                    "tool_event": sanitize_value(item.get("tool_event")),
                }
            )

    return normalized

def extract_tool_events_from_blocks(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    tool_events: list[dict[str, object]] = []
    for block in blocks:
        if sanitize_text(block.get("kind") or "").strip() != "tool":
            continue
        tool_event = block.get("tool_event")
        if isinstance(tool_event, dict):
            tool_events.append(sanitize_value(tool_event))
    return tool_events

def append_tool_provider_items(
    provider_items: list[dict[str, Any]],
    *,
    tool_event: dict[str, object],
    record_index: int,
    tool_index: int,
) -> None:
    safe_tool_event = sanitize_value(tool_event)
    tool_name = sanitize_text(safe_tool_event.get("name") or "").strip() or f"tool_{tool_index}"
    call_id = f"stored_{record_index}_{tool_index}"
    arguments_value = safe_tool_event.get("arguments")

    if isinstance(arguments_value, str):
        arguments_text = sanitize_text(arguments_value) or "{}"
    else:
        arguments_text = json.dumps(sanitize_value(arguments_value), ensure_ascii=False)

    tool_output = (
        sanitize_text(safe_tool_event.get("raw_output") or "")
        or sanitize_text(safe_tool_event.get("display_result") or "")
        or sanitize_text(safe_tool_event.get("output_preview") or "")
    )

    provider_items.append(
        {
            "type": "function_call",
            "call_id": call_id,
            "name": tool_name,
            "arguments": arguments_text or "{}",
        }
    )
    provider_items.append(
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": tool_output,
        }
    )

def flush_assistant_text_buffer(
    provider_items: list[dict[str, Any]],
    text_buffer: list[str],
) -> None:
    if not text_buffer:
        return

    provider_items.append(
        SimpleAgent._message(
            "assistant",
            "".join(text_buffer),
        )
    )
    text_buffer.clear()

def normalize_provider_items(raw_items: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue

        normalized_item = sanitize_value(item)
        if isinstance(normalized_item, dict):
            normalized.append(normalized_item)

    return normalized

def build_provider_items_for_record(
    *,
    role: str,
    text: str,
    attachments: list[dict[str, object]],
    tool_events: list[dict[str, object]],
    blocks: list[dict[str, object]],
    record_index: int,
) -> list[dict[str, Any]]:
    safe_role = sanitize_text(role).strip()
    if safe_role in {"system", "developer", "user"}:
        return [
            SimpleAgent._message(
                safe_role,
                sanitize_text(text),
                attachments=attachment_inputs_from_records(attachments) if safe_role == "user" else None,
            )
        ]

    if safe_role != "assistant":
        return []

    effective_tool_events = tool_events or extract_tool_events_from_blocks(blocks)
    provider_items: list[dict[str, Any]] = []
    text_buffer: list[str] = []
    saw_tool = False
    next_tool_index = 1

    for block in blocks:
        kind = sanitize_text(block.get("kind") or "").strip()
        if kind == "text":
            block_text = sanitize_text(block.get("text") or "")
            if block_text:
                text_buffer.append(block_text)
            continue

        if kind != "tool":
            continue

        saw_tool = True
        flush_assistant_text_buffer(provider_items, text_buffer)

        raw_tool_event = block.get("tool_event")
        if isinstance(raw_tool_event, dict):
            append_tool_provider_items(
                provider_items,
                tool_event=raw_tool_event,
                record_index=record_index,
                tool_index=next_tool_index,
            )
            next_tool_index += 1
            continue

        if next_tool_index - 1 < len(effective_tool_events):
            append_tool_provider_items(
                provider_items,
                tool_event=effective_tool_events[next_tool_index - 1],
                record_index=record_index,
                tool_index=next_tool_index,
            )
            next_tool_index += 1

    while next_tool_index - 1 < len(effective_tool_events):
        saw_tool = True
        append_tool_provider_items(
            provider_items,
            tool_event=effective_tool_events[next_tool_index - 1],
            record_index=record_index,
            tool_index=next_tool_index,
        )
        next_tool_index += 1

    flush_assistant_text_buffer(provider_items, text_buffer)

    if not provider_items:
        provider_items.append(
            SimpleAgent._message(
                "assistant",
                sanitize_text(text),
            )
        )
    elif provider_items[-1].get("type") != "message":
        fallback_text = sanitize_text(text or "")
        provider_items.append(
            SimpleAgent._message(
                "assistant",
                fallback_text,
            )
        )
    elif saw_tool:
        last_item_content = provider_items[-1].get("content")
        if not sanitize_text(last_item_content or "").strip():
            provider_items[-1] = SimpleAgent._message(
                "assistant",
                sanitize_text(text or ""),
            )

    return normalize_provider_items(provider_items)

def message_blocks_to_text(blocks: list[dict[str, object]]) -> str:
    text_parts: list[str] = []
    for block in blocks:
        if sanitize_text(block.get("kind") or "").strip() != "text":
            continue
        text = sanitize_text(block.get("text") or "")
        if text:
            text_parts.append(text)

    return "".join(text_parts)

def is_core_transcript_node(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("items"), list)
        and "providerItems" not in value
    )

def normalize_transcript_node(raw_node: dict[str, Any], fallback_index: int) -> dict[str, object] | None:
    raw_items = raw_node.get("items")
    if not isinstance(raw_items, list):
        return None

    items: list[dict[str, object]] = []
    for item_index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict) or "providerItem" not in raw_item:
            continue
        provider_item = sanitize_value(raw_item.get("providerItem"))
        if not isinstance(provider_item, dict):
            continue
        input_index = raw_item.get("inputIndex")
        node_item: dict[str, object] = {
            "kind": sanitize_text(raw_item.get("kind") or provider_item.get("type") or "unknown").strip()
            or "unknown",
            "providerItem": provider_item,
        }
        if type(input_index) is int:
            node_item["inputIndex"] = input_index
        else:
            node_item["inputIndex"] = item_index
        items.append(node_item)

    if not items:
        return None

    source_map = raw_node.get("source_map")
    return {
        "id": sanitize_text(raw_node.get("id") or f"node_{fallback_index}").strip() or f"node_{fallback_index}",
        "role": sanitize_text(raw_node.get("role") or "unknown").strip() or "unknown",
        "items": items,
        "source_map": sanitize_value(source_map) if isinstance(source_map, dict) else {},
    }

def transcript_node_provider_items(node: dict[str, object]) -> list[dict[str, Any]]:
    raw_items = node.get("items")
    if not isinstance(raw_items, list):
        return []
    provider_items: list[dict[str, Any]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        provider_item = raw_item.get("providerItem")
        if isinstance(provider_item, dict):
            provider_items.append(sanitize_value(provider_item))
    return provider_items

def context_records_to_input_items(raw_records: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_records, list):
        return []

    input_items: list[dict[str, Any]] = []
    for record_index, item in enumerate(raw_records):
        if not isinstance(item, dict):
            continue
        if is_core_transcript_node(item):
            input_items.extend(core_transcript_to_input_items([item]))
            continue

        role = sanitize_text(item.get("role") or "").strip()
        provider_items = normalize_provider_items(item.get("providerItems"))
        if provider_items:
            input_items.extend(provider_items)
            continue
        if role in CONTEXT_INPUT_RECORD_ROLES:
            input_items.extend(
                build_provider_items_for_record(
                    role=role,
                    text=sanitize_text(item.get("text") or ""),
                    attachments=normalize_attachment_records(item.get("attachments")),
                    tool_events=sanitize_value(item.get("toolEvents"))
                    if isinstance(item.get("toolEvents"), list)
                    else [],
                    blocks=normalize_message_blocks(item.get("blocks")),
                    record_index=record_index,
                )
            )
    return input_items

def normalize_transcript(raw_records: Any) -> list[dict[str, object]]:
    if not isinstance(raw_records, list):
        return []

    if all(is_core_transcript_node(item) for item in raw_records):
        nodes: list[dict[str, object]] = []
        for index, raw_node in enumerate(raw_records):
            node = normalize_transcript_node(raw_node, index)
            if node is not None:
                nodes.append(node)
        return nodes

    return core_input_items_to_transcript(context_records_to_input_items(raw_records))

def reindex_transcript_input_indexes(nodes: list[dict[str, object]]) -> list[dict[str, object]]:
    reindexed_nodes = sanitize_value(nodes)
    next_input_index = 0
    for node in reindexed_nodes:
        if not isinstance(node, dict):
            continue
        source_map = node.get("source_map")
        if isinstance(source_map, dict):
            source_map.clear()
        items = node.get("items")
        if not isinstance(items, list):
            continue
        for node_item in items:
            if not isinstance(node_item, dict):
                continue
            node_item["inputIndex"] = next_input_index
            next_input_index += 1
    return reindexed_nodes

def normalize_context_records(raw_records: Any) -> list[dict[str, object]]:
    core_nodes = normalize_transcript(raw_records)
    records: list[dict[str, object]] = []
    for record_index, item in enumerate(core_nodes):
        if not isinstance(item, dict):
            continue
        role = sanitize_text(item.get("role") or "").strip()
        normalized_provider_items = transcript_node_provider_items(item)
        core_items = [
            sanitize_value(core_item)
            for core_item in normalized_provider_items
            if isinstance(core_item, dict)
        ]
        if role in CONTEXT_INPUT_RECORD_ROLES and core_items:
            recovered_record = compile_record_from_provider_items(
                {
                    "role": role,
                    "attachments": [],
                },
                core_items,
            )
            safe_text = sanitize_text(recovered_record.get("text") or "") if isinstance(recovered_record, dict) else ""
            safe_tool_events = (
                sanitize_value(recovered_record.get("toolEvents"))
                if isinstance(recovered_record, dict) and isinstance(recovered_record.get("toolEvents"), list)
                else []
            )
            blocks = normalize_message_blocks(recovered_record.get("blocks")) if isinstance(recovered_record, dict) else []
            provider_items = normalize_provider_items(recovered_record.get("providerItems")) if isinstance(recovered_record, dict) else core_items
        elif role == "subagent":
            safe_text = subagent_text_from_provider_items(core_items)
            safe_tool_events = []
            blocks = [{"kind": "text", "text": safe_text}] if safe_text.strip() else []
            provider_items = core_items
        else:
            safe_text = ""
            safe_tool_events = []
            blocks = []
            provider_items = core_items
        records.append(
            {
                "id": sanitize_text(item.get("id") or "").strip(),
                "role": role,
                "text": safe_text,
                "attachments": [],
                "toolEvents": safe_tool_events,
                "blocks": blocks,
                "providerItems": provider_items,
            }
        )
    return records

def serialize_tool_event(event: ToolEvent) -> dict[str, object]:
    return {
        "name": event.name,
        "arguments": event.arguments,
        "output_preview": event.output_preview,
        "raw_output": event.raw_output,
        "display_title": event.display_title,
        "display_detail": event.display_detail,
        "display_result": event.display_result,
        "status": event.status,
    }

def debug_request_item_summary(item: Any, index: int) -> dict[str, object]:
    item_json = json.dumps(sanitize_value(item), ensure_ascii=False)
    summary: dict[str, object] = {
        "index": index,
        "json_chars": len(item_json),
    }
    if not isinstance(item, dict):
        summary["type"] = type(item).__name__
        return summary

    item_type = sanitize_text(item.get("type") or "").strip()
    summary["type"] = item_type or "unknown"
    if item_type == "message":
        summary["role"] = sanitize_text(item.get("role") or "").strip()
        text = extract_text_from_provider_message_content(item.get("content"))
        summary["text_chars"] = len(text)
        summary["preview"] = block_text_preview(text, limit=120)
        return summary

    if item_type == "function_call":
        summary["name"] = sanitize_text(item.get("name") or "").strip()
        summary["call_id"] = sanitize_text(item.get("call_id") or "").strip()
        arguments_text = sanitize_text(item.get("arguments") or "")
        summary["arguments_chars"] = len(arguments_text)
        try:
            parsed_arguments = json.loads(arguments_text) if arguments_text.strip() else {}
        except json.JSONDecodeError:
            parsed_arguments = {}
        if isinstance(parsed_arguments, dict):
            argument_summary: dict[str, object] = {}
            for key in [
                "node_numbers",
                "item_number",
                "item_numbers",
                "item_refs",
                "title",
                "style",
                "reason",
            ]:
                if key in parsed_arguments:
                    argument_summary[key] = sanitize_value(parsed_arguments.get(key))

            for text_key in ["summary_markdown", "compressed_content"]:
                if text_key in parsed_arguments:
                    argument_summary[f"{text_key}_chars"] = len(sanitize_text(parsed_arguments.get(text_key) or ""))

            selector = parsed_arguments.get("selector")
            if isinstance(selector, dict):
                argument_summary["selector_keys"] = sorted(str(key) for key in selector.keys())

            operation = parsed_arguments.get("operation")
            if isinstance(operation, dict):
                argument_summary["operation_type"] = sanitize_text(operation.get("type") or "").strip()

            if argument_summary:
                summary["arguments_summary"] = argument_summary
        return summary

    if item_type == "function_call_output":
        output = sanitize_text(item.get("output") or "")
        summary["call_id"] = sanitize_text(item.get("call_id") or "").strip()
        summary["output_chars"] = len(output)
        summary["preview"] = block_text_preview(output, limit=120)
        return summary

    return summary

def write_context_request_debug(
    *,
    session_id: str,
    request_model: str,
    round_count: int,
    request: dict[str, Any],
    note: str,
) -> None:
    try:
        input_items = request.get("input")
        tools = request.get("tools")
        input_list = input_items if isinstance(input_items, list) else []
        tool_list = tools if isinstance(tools, list) else []
        payload = {
            "created_at": utc_timestamp(),
            "pid": os.getpid(),
            "state_file": str(STATE_FILE),
            "session_id": session_id,
            "model": request_model,
            "round_count": round_count,
            "note": note,
            "request_json_chars": len(json.dumps(sanitize_value(request), ensure_ascii=False)),
            "input_count": len(input_list),
            "input_json_chars": len(json.dumps(sanitize_value(input_list), ensure_ascii=False)),
            "tools_count": len(tool_list),
            "tools_json_chars": len(json.dumps(sanitize_value(tool_list), ensure_ascii=False)),
            "items": [
                debug_request_item_summary(item, index)
                for index, item in enumerate(input_list)
            ],
        }
        CONTEXT_REQUEST_DEBUG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with CONTEXT_REQUEST_DEBUG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        return

def context_record_node_id(record: dict[str, object]) -> str:
    return transcript_node_id(record)


def is_context_record_locked(record: dict[str, object], node_locks: dict[str, bool] | None = None) -> bool:
    return is_node_locked(record, node_locks)


def context_node_locked_indexes(
    transcript: list[dict[str, object]],
    node_locks: dict[str, bool] | None = None,
) -> set[int]:
    return {
        index
        for index, record in enumerate(transcript)
        if isinstance(record, dict) and is_context_record_locked(record, node_locks)
    }


def editable_context_node_entries(
    transcript: list[dict[str, object]],
    node_locks: dict[str, bool] | None = None,
) -> list[dict[str, object]]:
    locked_indexes = context_node_locked_indexes(transcript, node_locks)
    entries: list[dict[str, object]] = []
    node_number = 0
    for index, record in enumerate(transcript):
        if index in locked_indexes:
            continue
        node_number += 1
        entries.append(
            {
                "record": record,
                "raw_index": index,
                "node_number": node_number,
            }
        )
    return entries

def editable_context_node_count(
    transcript: list[dict[str, object]],
    node_locks: dict[str, bool] | None = None,
) -> int:
    return len(editable_context_node_entries(transcript, node_locks))

def selected_display_node_numbers(
    transcript: list[dict[str, object]],
    selected_indexes: list[int],
    node_locks: dict[str, bool] | None = None,
) -> list[int]:
    selected_index_set = set(selected_indexes)
    return [
        int(entry["node_number"])
        for entry in editable_context_node_entries(transcript, node_locks)
        if int(entry["raw_index"]) in selected_index_set
    ]

def normalize_selected_node_indexes(raw_indexes: Any, transcript_length: int) -> list[int]:
    if not isinstance(raw_indexes, list):
        return []

    selected_indexes: list[int] = []
    for raw_item in raw_indexes:
        try:
            index = int(raw_item)
        except (TypeError, ValueError):
            continue

        if 0 <= index < transcript_length and index not in selected_indexes:
            selected_indexes.append(index)

    return selected_indexes

def block_text_preview(text: str, limit: int = 280) -> str:
    compact = " ".join(sanitize_text(text).split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: max(0, limit - 3)]}..."

def collapsed_context_map_preview(text: str, limit: int = 72) -> str:
    compact = " ".join(sanitize_text(text).split())
    if not compact:
        return ""

    sentence_match = re.search(r"[。！？!?\.]", compact)
    if sentence_match:
        end_index = sentence_match.end()
        preview = compact[:end_index].strip()
    else:
        preview = compact[:limit].strip()

    was_shortened = len(preview) < len(compact)
    if len(preview) > limit:
        preview = preview[: max(0, limit - 3)].rstrip()
        was_shortened = True

    if was_shortened and not preview.endswith("..."):
        preview = f"{preview.rstrip()}..."

    return preview

def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def get_token_encoding() -> Any | None:
    global _TOKEN_ENCODING, _TOKEN_ENCODING_LOAD_FAILED

    if _TOKEN_ENCODING is not None:
        return _TOKEN_ENCODING
    if _TOKEN_ENCODING_LOAD_FAILED or tiktoken is None:
        return None

    try:
        _TOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _TOKEN_ENCODING_LOAD_FAILED = True
        return None

    return _TOKEN_ENCODING

@dataclass(slots=True)
class _TokenCacheEntry:
    value: int

_TOKEN_CACHE: dict[int, _TokenCacheEntry] = {}
_TOKEN_CACHE_MAX = 4096

def _token_cache_key(text: str) -> int:
    return hash(text)

def estimate_token_count(text: str) -> int:
    safe_text = sanitize_text(text)
    if not safe_text.strip():
        return 0

    cache_key = _token_cache_key(safe_text)
    cached = _TOKEN_CACHE.get(cache_key)
    if cached is not None:
        return cached.value

    encoding = get_token_encoding()
    if encoding is not None:
        try:
            result = len(encoding.encode(safe_text))
        except Exception:
            result = _estimate_token_count_fallback(safe_text)
    else:
        result = _estimate_token_count_fallback(safe_text)

    if len(_TOKEN_CACHE) >= _TOKEN_CACHE_MAX:
        _TOKEN_CACHE.clear()
    _TOKEN_CACHE[cache_key] = _TokenCacheEntry(result)
    return result

def _estimate_token_count_fallback(text: str) -> int:
    compact = text.strip()
    ascii_tokens = re.findall(r"[A-Za-z0-9_]+", compact)
    non_ascii_chars = [char for char in compact if not char.isspace() and not char.isascii()]
    return max(1, len(ascii_tokens) + len(non_ascii_chars))

def load_context_edit_markers() -> dict[str, dict[str, object]]:
    try:
        raw = json.loads(CONTEXT_EDIT_MARKERS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        sanitize_text(session_id).strip(): sanitize_value(marker)
        for session_id, marker in raw.items()
        if sanitize_text(session_id).strip() and isinstance(marker, dict)
    }

def save_context_edit_markers(markers: dict[str, dict[str, object]]) -> None:
    CONTEXT_EDIT_MARKERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONTEXT_EDIT_MARKERS_FILE.write_text(
        json.dumps(sanitize_value(markers), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def write_context_edit_marker(
    session_id: str,
    *,
    summary: str,
    edit_version: int,
    node_count: int,
) -> None:
    safe_session_id = sanitize_text(session_id).strip()
    if not safe_session_id:
        return
    markers = load_context_edit_markers()
    markers[safe_session_id] = {
        "session_id": safe_session_id,
        "summary": sanitize_text(summary).strip() or "Context has been edited.",
        "edit_version": edit_version,
        "node_count": max(0, int(node_count or 0)),
        "created_at": utc_timestamp(),
    }
    save_context_edit_markers(markers)

def consume_context_edit_marker(session_id: str) -> dict[str, object] | None:
    safe_session_id = sanitize_text(session_id).strip()
    if not safe_session_id:
        return None
    markers = load_context_edit_markers()
    marker = markers.pop(safe_session_id, None)
    if marker is not None:
        save_context_edit_markers(markers)
    if not isinstance(marker, dict):
        return None
    return {
        "session_id": sanitize_text(marker.get("session_id") or safe_session_id).strip() or safe_session_id,
        "summary": sanitize_text(marker.get("summary") or "Context has been edited.").strip()
        or "Context has been edited.",
        "edit_version": int(marker.get("edit_version") or 0),
        "node_count": max(0, int(marker.get("node_count") or 0)),
        "created_at": sanitize_text(marker.get("created_at") or ""),
    }

def context_record_preview(record: dict[str, object], *, limit: int = 140) -> str:
    role = sanitize_text(record.get("role") or "").strip()
    blocks = normalize_message_blocks(record.get("blocks"))
    attachments = normalize_attachment_records(record.get("attachments"))
    text = sanitize_text(record.get("text") or "")

    if blocks:
        for block in blocks:
            kind = sanitize_text(block.get("kind") or "").strip()
            if kind == "text":
                preview = block_text_preview(block.get("text") or "", limit=limit)
                if preview:
                    return preview
                continue

            if kind != "tool":
                continue

            tool_event = block.get("tool_event")
            if not isinstance(tool_event, dict):
                continue
            tool_name = sanitize_text(tool_event.get("name") or tool_event.get("display_title") or "").strip() or "tool"
            tool_detail = block_text_preview(tool_event.get("display_detail") or "", limit=max(40, min(limit, 88)))
            if tool_detail:
                return f"{tool_name}: {tool_detail}"
            return tool_name

    if text:
        return block_text_preview(text, limit=limit)

    if role == "subagent":
        subagent_text = subagent_text_from_provider_items(normalize_provider_items(record.get("providerItems")))
        if subagent_text.strip():
            return block_text_preview(subagent_text, limit=limit)

    if attachments:
        attachment_names = ", ".join(
            sanitize_text(item.get("name") or "").strip()
            for item in attachments
            if sanitize_text(item.get("name") or "").strip()
        )
        if attachment_names:
            return f"Attachments: {attachment_names}"

    return "[empty]"

def subagent_author_from_provider_items(provider_items: list[dict[str, Any]]) -> str:
    for item in provider_items:
        if provider_item_type(item) != "agent_message":
            continue
        author = sanitize_text(item.get("author") or "").strip()
        if author:
            return author
    return ""

def subagent_text_from_provider_items(provider_items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in provider_items:
        if provider_item_type(item) != "agent_message":
            continue
        text = extract_text_from_provider_agent_message_content(item.get("content"))
        if text.strip():
            parts.append(text)
    return "\n\n".join(parts)

def record_tool_usage(record: dict[str, object]) -> list[dict[str, object]]:
    tool_events = sanitize_value(record.get("toolEvents")) if isinstance(record.get("toolEvents"), list) else []
    if not tool_events:
        tool_events = extract_tool_events_from_blocks(normalize_message_blocks(record.get("blocks")))

    counts: dict[str, int] = {}
    for tool_event in tool_events:
        if not isinstance(tool_event, dict):
            continue
        tool_name = sanitize_text(tool_event.get("name") or tool_event.get("display_title") or "").strip() or "tool"
        counts[tool_name] = counts.get(tool_name, 0) + 1

    return [
        {"name": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]

def format_tool_usage(tool_usage: list[dict[str, object]]) -> str:
    if not tool_usage:
        return "none"

    return ", ".join(
        f"{sanitize_text(item.get('name') or '').strip() or 'tool'} x{int(item.get('count') or 0)}"
        for item in tool_usage
    )

def format_token_count(token_estimate: int) -> str:
    safe_value = max(0, int(token_estimate or 0))
    if safe_value >= 1000:
        return f"{safe_value / 1000:.1f}k"
    return str(safe_value)

def record_context_tool_weight_source(record: dict[str, object]) -> str:
    parts: list[str] = []
    for block in normalize_message_blocks(record.get("blocks")):
        kind = sanitize_text(block.get("kind") or "").strip()
        if kind != "tool":
            continue

        tool_event = block.get("tool_event")
        if not isinstance(tool_event, dict):
            continue

        tool_parts = [
            sanitize_text(tool_event.get("display_title") or "").strip(),
            sanitize_text(tool_event.get("display_detail") or "").strip(),
            sanitize_text(tool_event.get("output_preview") or "").strip(),
            sanitize_text(tool_event.get("display_result") or "").strip(),
            sanitize_text(tool_event.get("raw_output") or "").strip(),
        ]
        joined = "\n".join(part for part in tool_parts if part)
        if joined:
            parts.append(joined)

    return "\n\n".join(parts)

def record_context_weight_source(record: dict[str, object]) -> str:
    parts: list[str] = []
    for block in normalize_message_blocks(record.get("blocks")):
        kind = sanitize_text(block.get("kind") or "").strip()
        if kind == "text":
            text = sanitize_text(block.get("text") or "")
            if text.strip():
                parts.append(text)
            continue

        if kind in {"reasoning", "thinking"}:
            continue

        tool_event = block.get("tool_event")
        if not isinstance(tool_event, dict):
            continue

        tool_source = record_context_tool_weight_source({"blocks": [block]})
        if tool_source:
            parts.append(tool_source)

    if not parts:
        text = sanitize_text(record.get("text") or "")
        if text.strip():
            parts.append(text)

    raw_attachments = record.get("attachments")
    attachments = raw_attachments if isinstance(raw_attachments, list) else []
    attachment_names = "\n".join(
        sanitize_text(attachment.get("name") or "").strip()
        for attachment in attachments
        if isinstance(attachment, dict) and sanitize_text(attachment.get("name") or "").strip()
    )
    if attachment_names:
        parts.append(attachment_names)

    return "\n\n".join(part for part in parts if part.strip())

def context_record_overview(record: dict[str, object], *, node_number: int, selected: bool = False) -> dict[str, object]:
    role = sanitize_text(record.get("role") or "").strip() or "unknown"
    preview = context_record_preview(record)
    if role in {"assistant", "subagent"}:
        preview = collapsed_context_map_preview(preview) or "[empty]"
    tool_usage = record_tool_usage(record)
    provider_items = normalize_provider_items(record.get("providerItems"))
    subagent_author = subagent_author_from_provider_items(provider_items) if role == "subagent" else ""
    token_estimate = estimate_token_count(record_context_weight_source(record))
    tool_token_estimate = estimate_token_count(record_context_tool_weight_source(record))
    return {
        "node_number": node_number,
        "role": role,
        "subagent_author": subagent_author,
        "selected": selected,
        "preview": preview,
        "token_estimate": token_estimate,
        "tool_token_estimate": tool_token_estimate,
        "tool_usage": tool_usage,
        "tool_count": sum(int(item.get("count") or 0) for item in tool_usage),
        "item_count": len(provider_items),
        "item_types": [
            sanitize_text(item.get("type") or "").strip() or "unknown"
            for item in provider_items
        ],
        "full_text": sanitize_text(record.get("text") or "") if role != "assistant" else "",
    }

def context_review_transcript_stats(transcript: list[dict[str, object]]) -> dict[str, object]:
    records = normalize_context_records(normalize_transcript(transcript))
    token_count = 0
    tool_token_count = 0
    for record in records:
        token_count += estimate_token_count(record_context_weight_source(record))
        tool_token_count += estimate_token_count(record_context_tool_weight_source(record))
    return {
        "node_count": len(records),
        "token_count": token_count,
        "tool_token_count": tool_token_count,
    }

def extract_text_from_provider_message_content(content: Any) -> str:
    if isinstance(content, str):
        return sanitize_text(content)

    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            text = sanitize_text(item)
            if text:
                parts.append(text)
            continue

        if not isinstance(item, dict):
            continue

        text = sanitize_text(item.get("text") or item.get("content") or "")
        if text:
            parts.append(text)

    return "".join(parts)

def extract_text_from_provider_agent_message_content(content: Any) -> str:
    if not isinstance(content, list):
        return provider_payload_text(content)

    parts: list[str] = []
    encrypted = False
    for item in content:
        if isinstance(item, str):
            text = sanitize_text(item)
            if text:
                parts.append(text)
            continue

        if not isinstance(item, dict):
            continue

        item_type = sanitize_text(item.get("type") or "").strip()
        if item_type == "encrypted_content":
            encrypted = True
            continue
        text = sanitize_text(item.get("text") or item.get("content") or "")
        if text:
            parts.append(text)

    if parts:
        return "\n".join(parts)
    return "[encrypted subagent message]" if encrypted else ""

def provider_item_type(item: dict[str, Any] | None) -> str:
    return sanitize_text((item or {}).get("type") or "").strip()

def provider_item_call_id(item: dict[str, Any] | None) -> str:
    safe_item = item or {}
    return sanitize_text(safe_item.get("call_id") or safe_item.get("id") or "").strip()

def provider_payload_text(value: Any) -> str:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        parts: list[str] = []
        for entry in value:
            if isinstance(entry, str):
                text = sanitize_text(entry)
            elif isinstance(entry, dict):
                text = sanitize_text(entry.get("text") or entry.get("content") or entry.get("summary") or "")
            else:
                text = ""
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts)
    if isinstance(value, dict):
        for key in ("text", "summary"):
            text = sanitize_text(value.get(key) or "")
            if text:
                return text
        content = value.get("content")
        if isinstance(content, list):
            text = provider_payload_text(content)
            if text:
                return text
        if isinstance(content, str):
            text = sanitize_text(content)
            if text:
                return text
    if value is None:
        return ""
    return json.dumps(sanitize_value(value), ensure_ascii=False)

def provider_jsonish_value(value: Any) -> Any:
    if isinstance(value, str):
        safe_text = sanitize_text(value)
        if not safe_text.strip():
            return ""
        try:
            return sanitize_value(json.loads(safe_text))
        except json.JSONDecodeError:
            return safe_text
    return sanitize_value(value)

def web_search_action_display_detail(action: Any) -> str:
    if not isinstance(action, dict):
        return block_text_preview(provider_payload_text(action), limit=160)

    action_type = sanitize_text(action.get("type") or "").strip()
    if action_type == "search":
        query = sanitize_text(action.get("query") or "").strip()
        if query:
            return query
        queries = action.get("queries")
        if isinstance(queries, list):
            query_parts = [sanitize_text(query).strip() for query in queries]
            return ", ".join(query for query in query_parts if query)
    if action_type == "open_page":
        return sanitize_text(action.get("url") or "").strip()
    if action_type == "find_in_page":
        pattern = sanitize_text(action.get("pattern") or "").strip()
        url = sanitize_text(action.get("url") or "").strip()
        if pattern and url:
            return f"{pattern} in {url}"
        return pattern or url

    fallback = action.get("query") or action.get("url") or action_type or action
    return block_text_preview(provider_payload_text(fallback), limit=160)

def tool_call_arguments_value(item: dict[str, Any] | None) -> Any:
    if not isinstance(item, dict):
        return ""
    item_type = provider_item_type(item)
    if item_type == "function_call":
        return provider_jsonish_value(item.get("arguments") or "{}")
    if item_type == "custom_tool_call":
        return provider_jsonish_value(item.get("input") or "")
    if item_type in {"local_shell_call", "web_search_call"}:
        return sanitize_value(item.get("action"))
    if item_type == "tool_search_call":
        return provider_jsonish_value(item.get("arguments"))
    if item_type == "image_generation_call":
        return sanitize_text(item.get("revised_prompt") or "")
    return provider_jsonish_value(item.get("arguments") or item.get("input") or item.get("action"))

def tool_output_text_from_provider_item(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    item_type = provider_item_type(item)
    if item_type == "tool_search_output":
        return provider_payload_text(item.get("tools"))
    if item_type == "image_generation_call":
        return provider_payload_text(item.get("result"))
    if item_type == "web_search_call":
        return ""
    return provider_payload_text(item.get("output"))

def tool_display_title_from_provider_item(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return "tool"
    item_type = provider_item_type(item)
    if item_type in {"function_call", "custom_tool_call"}:
        return sanitize_text(item.get("name") or "").strip() or "tool"
    display_hint = CODEX_ITEM_DISPLAY_HINTS_BY_ITEM_TYPE.get(item_type, {})
    if display_hint.get("title"):
        return display_hint["title"]
    if item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES:
        return sanitize_text(item.get("name") or item_type or "tool_output").strip()
    return item_type or "tool"

def tool_display_detail_from_provider_item(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    item_type = provider_item_type(item)
    call_name = sanitize_text(item.get("name") or "").strip()
    arguments_value = tool_call_arguments_value(item)

    if call_name in {"shell_command", "exec_command"} and isinstance(arguments_value, dict):
        command = arguments_value.get("command")
        if isinstance(command, list):
            return " ".join(sanitize_text(part) for part in command)
        if command is not None:
            return sanitize_text(command)
    if call_name == "write_stdin" and isinstance(arguments_value, dict):
        return sanitize_text(arguments_value.get("stdin") or arguments_value.get("input") or "")
    if item_type == "local_shell_call":
        action = item.get("action")
        if isinstance(action, dict):
            command = action.get("command")
            if isinstance(command, list):
                return " ".join(sanitize_text(part) for part in command)
            if command is not None:
                return sanitize_text(command)
        return block_text_preview(provider_payload_text(action), limit=160)
    if item_type == "web_search_call":
        return web_search_action_display_detail(item.get("action"))
    if item_type == "image_generation_call":
        return block_text_preview(sanitize_text(item.get("revised_prompt") or ""), limit=160)

    detail_text = provider_payload_text(arguments_value)
    return block_text_preview(detail_text, limit=160) if detail_text.strip() not in {"", "{}", "[]"} else ""

def visible_text_from_compaction_provider_item(item: dict[str, Any]) -> str:
    parts: list[str] = []

    def append_visible(value: Any) -> None:
        if isinstance(value, str):
            text = sanitize_text(value).strip()
            if text:
                parts.append(text)
            return
        if isinstance(value, list):
            for entry in value:
                append_visible(entry)
            return
        if isinstance(value, dict):
            for key in ("text", "summary", "content"):
                if key in value:
                    append_visible(value.get(key))

    for key in ("summary", "content", "text"):
        append_visible(item.get(key))
    if not parts:
        append_visible(item.get("encrypted_content"))
    return "\n".join(part for part in parts if part)

def tool_status_from_output(output_text: str, fallback: str = "completed") -> str:
    lines = sanitize_text(output_text).splitlines()
    first_line = lines[0] if lines else ""
    if first_line.lower().startswith("exit code:"):
        raw_code = first_line.split(":", 1)[1].strip().split(maxsplit=1)[0]
        try:
            return "completed" if int(raw_code) == 0 else "error"
        except ValueError:
            return fallback
    return fallback

def build_tool_event_from_provider_items(
    tool_call_item: dict[str, Any] | None,
    tool_output_item: dict[str, Any] | None,
) -> dict[str, object]:
    identity_item = tool_call_item or tool_output_item or {}
    call_name = tool_display_title_from_provider_item(identity_item)
    output_text = tool_output_text_from_provider_item(tool_output_item or tool_call_item)
    fallback_status = sanitize_text((identity_item or {}).get("status") or "").strip() or "completed"
    has_output = tool_output_item is not None or provider_item_type(tool_call_item) in CODEX_STANDALONE_TOOL_CALL_ITEM_TYPES
    return {
        "name": call_name,
        "arguments": tool_call_arguments_value(tool_call_item),
        "call_id": provider_item_call_id(identity_item),
        "output_preview": block_text_preview(output_text, limit=180) if output_text else "",
        "raw_output": output_text,
        "display_title": call_name,
        "display_detail": tool_display_detail_from_provider_item(tool_call_item),
        "display_result": block_text_preview(output_text, limit=180) if output_text else "",
        "status": tool_status_from_output(output_text, fallback_status) if has_output else "pending",
    }

def context_node_item_ref(node_number: int, item_number: int) -> str:
    return f"node:{node_number}:item:{item_number}"

def context_block_ref(block_number: int, field: str = "") -> str:
    suffix = f".{field}" if field else ""
    return f"block:{block_number}{suffix}"

def context_model_block_base(block_number: int, kind: str) -> dict[str, object]:
    return {
        "block_number": block_number,
        "block_ref": context_block_ref(block_number),
        "kind": kind,
    }

def append_context_model_text_block(
    blocks: list[dict[str, object]],
    *,
    node_number: int,
    item: dict[str, Any],
    item_number: int,
    kind: str,
    content: str,
) -> None:
    block_number = len(blocks) + 1
    safe_content = sanitize_text(content)
    block = context_model_block_base(block_number, kind)
    block.update(
        {
            "content_ref": context_block_ref(block_number, "content"),
            "content": safe_content,
            "item_number": item_number,
            "item_ref": context_node_item_ref(node_number, item_number),
            "item_type": provider_item_type(item) or "unknown",
            "token_estimate": estimate_token_count(safe_content),
            "text_chars": len(safe_content),
        }
    )
    role = sanitize_text(item.get("role") or "").strip()
    if role:
        block["role"] = role
    blocks.append(block)

def context_model_blocks_from_provider_items(
    provider_items: list[dict[str, Any]],
    *,
    node_number: int,
) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    consumed_output_indexes: set[int] = set()
    output_indexes_by_call_id: dict[str, list[int]] = {}

    for index, item in enumerate(provider_items):
        if provider_item_type(item) not in CODEX_TOOL_OUTPUT_ITEM_TYPES:
            continue
        call_id = provider_item_call_id(item)
        if call_id:
            output_indexes_by_call_id.setdefault(call_id, []).append(index)

    for index, item in enumerate(provider_items):
        item_type = provider_item_type(item)
        item_number = index + 1
        if item_type == "message":
            append_context_model_text_block(
                blocks,
                node_number=node_number,
                item=item,
                item_number=item_number,
                kind="text",
                content=extract_text_from_provider_message_content(item.get("content")),
            )
            continue

        if item_type in CODEX_COMPACTION_ITEM_TYPES:
            append_context_model_text_block(
                blocks,
                node_number=node_number,
                item=item,
                item_number=item_number,
                kind="compaction",
                content=visible_text_from_compaction_provider_item(item),
            )
            continue

        if item_type == "reasoning":
            append_context_model_text_block(
                blocks,
                node_number=node_number,
                item=item,
                item_number=item_number,
                kind="reasoning",
                content=provider_payload_text(item.get("summary") or item.get("content") or item.get("text")),
            )
            continue

        if item_type in CODEX_TOOL_CALL_ITEM_TYPES:
            output_item: dict[str, Any] | None = None
            output_index: int | None = None
            allowed_output_types = CODEX_TOOL_OUTPUT_TYPES_BY_CALL_TYPE.get(item_type, set())
            call_id = provider_item_call_id(item)
            for candidate_index in output_indexes_by_call_id.get(call_id, []):
                if candidate_index in consumed_output_indexes:
                    continue
                candidate_output = provider_items[candidate_index]
                if allowed_output_types and provider_item_type(candidate_output) not in allowed_output_types:
                    continue
                output_item = candidate_output
                output_index = candidate_index
                consumed_output_indexes.add(candidate_index)
                break

            block_number = len(blocks) + 1
            arguments = provider_payload_text(tool_call_arguments_value(item))
            output = tool_output_text_from_provider_item(output_item or item)
            block = context_model_block_base(block_number, "tool")
            block.update(
                {
                    "name": tool_display_title_from_provider_item(item),
                    "tool_type": item_type,
                    "call_id": call_id,
                    "call_item_number": item_number,
                    "call_item_ref": context_node_item_ref(node_number, item_number),
                    "arguments_ref": context_block_ref(block_number, "arguments"),
                    "arguments": arguments,
                    "argument_token_estimate": estimate_token_count(arguments),
                    "argument_chars": len(arguments),
                }
            )
            if output_item is not None and output_index is not None:
                output_item_number = output_index + 1
                block.update(
                    {
                        "output_item_number": output_item_number,
                        "output_item_ref": context_node_item_ref(node_number, output_item_number),
                        "output_item_type": provider_item_type(output_item) or "unknown",
                        "output_ref": context_block_ref(block_number, "output"),
                        "output": output,
                        "output_token_estimate": estimate_token_count(output),
                        "output_chars": len(output),
                    }
                )
            elif item_type in CODEX_STANDALONE_TOOL_CALL_ITEM_TYPES and output:
                block.update(
                    {
                        "output_ref": context_block_ref(block_number, "output"),
                        "output": output,
                        "output_token_estimate": estimate_token_count(output),
                        "output_chars": len(output),
                    }
                )
            blocks.append(block)
            continue

        if item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES and index not in consumed_output_indexes:
            output = tool_output_text_from_provider_item(item)
            block_number = len(blocks) + 1
            block = context_model_block_base(block_number, "tool_output")
            block.update(
                {
                    "call_id": provider_item_call_id(item),
                    "output_item_number": item_number,
                    "output_item_ref": context_node_item_ref(node_number, item_number),
                    "output_item_type": item_type,
                    "output_ref": context_block_ref(block_number, "output"),
                    "output": output,
                    "output_token_estimate": estimate_token_count(output),
                    "output_chars": len(output),
                }
            )
            blocks.append(block)

    return blocks

def compile_record_from_provider_items(
    original_record: dict[str, object],
    provider_items: list[dict[str, Any]],
) -> dict[str, object]:
    normalized_provider_items = normalize_provider_items(provider_items)
    role = sanitize_text(original_record.get("role") or "").strip() or "assistant"
    attachments = normalize_attachment_records(original_record.get("attachments"))

    blocks: list[dict[str, object]] = []
    tool_events: list[dict[str, object]] = []
    consumed_output_indexes: set[int] = set()
    output_indexes_by_call_id: dict[str, list[int]] = {}

    for index, item in enumerate(normalized_provider_items):
        if provider_item_type(item) not in CODEX_TOOL_OUTPUT_ITEM_TYPES:
            continue
        call_id = provider_item_call_id(item)
        if not call_id:
            continue
        output_indexes_by_call_id.setdefault(call_id, []).append(index)

    for index, item in enumerate(normalized_provider_items):
        item_type = provider_item_type(item)
        if item_type == "message":
            message_text = extract_text_from_provider_message_content(item.get("content"))
            if message_text:
                blocks.append(
                    {
                        "kind": "text",
                        "text": message_text,
                    }
                )
            continue

        if item_type in CODEX_COMPACTION_ITEM_TYPES:
            visible_text = visible_text_from_compaction_provider_item(item)
            if visible_text:
                blocks.append(
                    {
                        "kind": "text",
                        "text": visible_text,
                    }
                )
            continue

        if item_type == "reasoning":
            reasoning_text = provider_payload_text(item.get("summary") or item.get("content") or item.get("text"))
            if reasoning_text:
                blocks.append(
                    {
                        "kind": "reasoning",
                        "text": reasoning_text,
                        "status": "completed",
                    }
                )
            continue

        if item_type in CODEX_PAIRED_TOOL_CALL_ITEM_TYPES:
            call_id = provider_item_call_id(item)
            allowed_output_types = CODEX_TOOL_OUTPUT_TYPES_BY_CALL_TYPE.get(item_type, set())
            output_item = None
            for output_index in output_indexes_by_call_id.get(call_id, []):
                if output_index in consumed_output_indexes:
                    continue
                candidate_output = normalized_provider_items[output_index]
                if allowed_output_types and provider_item_type(candidate_output) not in allowed_output_types:
                    continue
                output_item = candidate_output
                consumed_output_indexes.add(output_index)
                break

            tool_event = build_tool_event_from_provider_items(item, output_item)
            tool_events.append(tool_event)
            blocks.append(
                {
                    "kind": "tool",
                    "tool_event": tool_event,
                }
            )
            continue

        if item_type in CODEX_STANDALONE_TOOL_CALL_ITEM_TYPES:
            tool_event = build_tool_event_from_provider_items(item, None)
            tool_events.append(tool_event)
            blocks.append(
                {
                    "kind": "tool",
                    "tool_event": tool_event,
                }
            )
            continue

        if item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES and index not in consumed_output_indexes:
            tool_event = build_tool_event_from_provider_items(None, item)
            tool_events.append(tool_event)
            blocks.append(
                {
                    "kind": "tool",
                    "tool_event": tool_event,
                }
            )

    return {
        "role": role,
        "text": message_blocks_to_text(blocks),
        "attachments": sanitize_value(attachments),
        "toolEvents": sanitize_value(tool_events),
        "blocks": sanitize_value(blocks),
        "providerItems": sanitize_value(normalized_provider_items),
    }

def context_record_details_payload(record: dict[str, object], *, node_number: int) -> dict[str, object]:
    overview = context_record_overview(record, node_number=node_number)
    provider_items = normalize_provider_items(record.get("providerItems"))
    model_blocks = context_model_blocks_from_provider_items(provider_items, node_number=node_number)
    return {
        "node_number": node_number,
        "role": overview["role"],
        "token_estimate": overview["token_estimate"],
        "tool_token_estimate": overview["tool_token_estimate"],
        "tool_usage": overview["tool_usage"],
        "preview": overview["preview"],
        "block_count": len(model_blocks),
        "content_source": "blocks",
        "attachments": sanitize_value(normalize_attachment_records(record.get("attachments"))),
        "blocks": model_blocks,
    }

def build_context_workspace_snapshot(
    session: SessionState,
    *,
    selected_indexes: list[int] | None = None,
) -> str:
    transcript = normalize_context_records(session.transcript)
    node_locks = normalize_node_locks(getattr(session, "node_locks", {}))
    safe_selected_indexes = normalize_selected_node_indexes(selected_indexes or [], len(transcript))
    selected_numbers = selected_display_node_numbers(transcript, safe_selected_indexes, node_locks)
    editable_entries = editable_context_node_entries(transcript, node_locks)
    lines = [
        "# 当前主 Codex 上下文快照",
        f"- 会话标题：{session.title}",
        f"- 当前节点数：{len(editable_entries)}",
        f"- 当前选中节点：{format_node_ranges(selected_numbers) or '未单独选中，默认面向全局'}",
        "- 这一轮里所有 Node # 都以这份快照为准。",
        "- 已锁定节点不在本快照中展示，也不能被选择或编辑。",
        "- developer 节点默认锁定；如果已解锁，它会像普通节点一样出现在本快照中。",
        "- 非 assistant 节点直接给全文，assistant 节点默认只给首句预览，预览后面的内容你不可见。",
        "- 压缩 assistant 节点前必须先调用 get_nodes 获取完整节点内容；不要用首句预览编写压缩摘要。",
        "- 如果你需要精细编辑 content item，先用明确的 Node # 调用 get_nodes，再根据返回的 item # 用 write_items 操作。",
        "",
        "## 节点概览",
    ]

    for entry in editable_entries:
        node_number = int(entry["node_number"])
        raw_index = int(entry["raw_index"])
        record = sanitize_value(entry["record"])
        overview = context_record_overview(
            record,
            node_number=node_number,
            selected=raw_index in safe_selected_indexes,
        )
        marker = " | selected" if overview["selected"] else ""
        token_label = format_token_count(int(overview["token_estimate"] or 0))
        tool_token_estimate = int(overview.get("tool_token_estimate") or 0)
        tool_token_label = (
            f" | tool {format_token_count(tool_token_estimate)} tokens"
            if tool_token_estimate > 0
            else ""
        )
        role = sanitize_text(overview["role"] or "").strip() or "unknown"
        if role != "assistant":
            node_text = sanitize_text(overview["full_text"] or "").strip() or "[empty]"
            lines.append(f"- Node #{node_number} | {role}{marker} | {token_label} tokens")
            lines.append("  content:")
            for content_line in node_text.splitlines() or ["[empty]"]:
                lines.append(f"    {content_line}")
            continue

        lines.append(
            f"- Node #{node_number} | {role}{marker} | {token_label} tokens{tool_token_label} | {format_tool_usage(overview['tool_usage'])} | {int(overview['item_count'] or 0)} items"
        )
        lines.append(f"  preview: {sanitize_text(overview['preview'] or '') or '[empty]'}")

    return "\n".join(lines).strip()

def find_codex_local_session_file(session_id: str) -> Path | None:
    safe_session_id = sanitize_text(session_id or "").strip()
    if not safe_session_id or not CODEX_LOCAL_SESSIONS_DIR.exists():
        return None

    try:
        matches = [
            path
            for path in CODEX_LOCAL_SESSIONS_DIR.rglob(f"*{safe_session_id}.jsonl")
            if path.is_file()
        ]
    except OSError:
        return None

    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)

def codex_message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return sanitize_text(content)
    if not isinstance(content, list):
        return sanitize_text(content)

    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text") or item.get("input_text") or item.get("output_text")
            if text:
                parts.append(sanitize_text(text))
        elif item is not None:
            parts.append(sanitize_text(item))
    return "\n".join(part for part in parts if part).strip()

def transcript_has_instruction_prefix(records: list[dict[str, Any]]) -> bool:
    transcript = normalize_transcript(records)
    return any(
        sanitize_text(record.get("role") or "").strip() in {"system", "developer"}
        for record in transcript
    )

def transcript_has_conversation_records(records: list[dict[str, Any]]) -> bool:
    transcript = normalize_transcript(records)
    return any(
        sanitize_text(record.get("role") or "").strip() in {"user", "assistant"}
        for record in transcript
    )

def provider_message_record(item: dict[str, Any]) -> dict[str, Any] | None:
    role = sanitize_text(item.get("role") or "").strip()
    if role not in CONTEXT_INPUT_MESSAGE_ROLES:
        return None

    text = codex_message_content_text(item.get("content")).strip()
    return {
        "role": role,
        "text": text,
        "attachments": [],
        "toolEvents": [],
        "blocks": [{"kind": "text", "text": text}] if text else [],
        "providerItems": [sanitize_value(item)],
    }

def read_jsonl_state_file(path: Path, default: Any) -> Any:
    state = sanitize_value(default)
    if not path.exists():
        return state
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return state
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = sanitize_text(event.get("type") or "").strip()
        if event_type == "clear":
            state = None
        elif event_type == "set":
            state = sanitize_value(event.get("records") if isinstance(event.get("records"), list) else [])
        elif event_type == "append":
            records = event.get("records")
            if isinstance(state, list) and isinstance(records, list):
                state = [*state, *sanitize_value(records)]
            elif isinstance(records, list):
                state = sanitize_value(records)
    return sanitize_value(default) if state is None and default is not None else state

def latest_proxy_instruction_prefix_records() -> list[dict[str, Any]]:
    try:
        from backend.proxy_session_storage import ProxySessionStorage
    except ImportError:
        return []

    storage = ProxySessionStorage(STATE_DIR)
    index = storage.load_index()
    active_session_id = sanitize_text(index.get("active_session_id") or "").strip()
    sessions = [item for item in index.get("sessions", []) if isinstance(item, dict)]

    sessions.sort(
        key=lambda session: (
            sanitize_text(session.get("id") or "").strip() == active_session_id,
            sanitize_text(session.get("updated_at") or ""),
        ),
        reverse=True,
    )

    for session in sessions:
        latest_prefix = session.get("latest_instruction_prefix")
        if isinstance(latest_prefix, list):
            normalized_prefix = normalize_transcript(latest_prefix)
            if normalized_prefix:
                return normalized_prefix

        request_log = session.get("request_log")
        if not isinstance(request_log, list):
            continue
        for entry in reversed(request_log):
            if not isinstance(entry, dict):
                continue
            body = entry.get("forwarded_body") if isinstance(entry.get("forwarded_body"), dict) else entry.get("body")
            if not isinstance(body, dict):
                continue
            input_items = body.get("input")
            if not isinstance(input_items, list):
                continue

            prefix: list[dict[str, Any]] = []
            for raw_item in input_items:
                if not isinstance(raw_item, dict):
                    continue
                item_type = sanitize_text(raw_item.get("type") or "").strip()
                role = sanitize_text(raw_item.get("role") or "").strip()
                if item_type == "message" and role in {"system", "developer"}:
                    record = provider_message_record(raw_item)
                    if record is not None:
                        prefix.append(record)
                    continue
                if prefix:
                    break

            if prefix:
                return normalize_transcript(prefix)

    return []

def codex_local_session_transcript(session_id: str) -> list[dict[str, Any]]:
    session_file = find_codex_local_session_file(session_id)
    if session_file is None:
        return []

    records: list[dict[str, Any]] = []
    try:
        lines = session_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        role = sanitize_text(payload.get("role") or "").strip()
        if role not in {"system", "developer", "user", "assistant"}:
            continue
        text = codex_message_content_text(payload.get("content")).strip()
        if not text:
            continue
        records.append(
            {
                "role": role,
                "text": text,
                "attachments": [],
                "toolEvents": [],
                "blocks": [{"kind": "text", "text": text}],
                "providerItems": [{"type": "message", "role": role, "content": text}],
            }
        )

    if not records:
        return []

    if not transcript_has_instruction_prefix(records):
        records = [*latest_proxy_instruction_prefix_records(), *records]

    return normalize_transcript(records)

def format_node_ranges(node_numbers: list[int]) -> str:
    if not node_numbers:
        return ""

    ordered = sorted(set(node_numbers))
    segments: list[str] = []
    range_start = ordered[0]
    previous = ordered[0]
    for current in ordered[1:]:
        if current == previous + 1:
            previous = current
            continue
        segments.append(f"{range_start}" if range_start == previous else f"{range_start}-{previous}")
        range_start = current
        previous = current
    segments.append(f"{range_start}" if range_start == previous else f"{range_start}-{previous}")
    return ", ".join(segments)

def tool_output_type_matches_call_type(output_type: str, call_type: str) -> bool:
    return output_type in CODEX_TOOL_OUTPUT_TYPES_BY_CALL_TYPE.get(call_type, set())

def validate_context_provider_items(provider_items: list[dict[str, Any]]) -> None:
    calls_by_id: dict[str, list[tuple[int, str]]] = {}
    outputs_by_id: dict[str, list[tuple[int, str]]] = {}

    for index, item in enumerate(provider_items):
        item_type = provider_item_type(item)
        call_id = provider_item_call_id(item)
        if item_type in CODEX_PAIRED_TOOL_CALL_ITEM_TYPES:
            if not call_id:
                raise ValueError(f"tool call item #{index + 1} is missing call_id")
            calls_by_id.setdefault(call_id, []).append((index, item_type))
        elif item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES:
            if not call_id:
                raise ValueError(f"tool output item #{index + 1} is missing call_id")
            outputs_by_id.setdefault(call_id, []).append((index, item_type))

    for call_id, calls in calls_by_id.items():
        outputs = outputs_by_id.get(call_id, [])
        for call_index, call_type in calls:
            if not any(tool_output_type_matches_call_type(output_type, call_type) for _, output_type in outputs):
                raise ValueError(
                    f"tool call item #{call_index + 1} ({call_type}, call_id={call_id}) has no matching output item"
                )

    for call_id, outputs in outputs_by_id.items():
        calls = calls_by_id.get(call_id, [])
        for output_index, output_type in outputs:
            if not any(tool_output_type_matches_call_type(output_type, call_type) for _, call_type in calls):
                raise ValueError(
                    f"tool output item #{output_index + 1} ({output_type}, call_id={call_id}) has no matching call item"
                )

def letter_index(value: int) -> str:
    result = ""
    current = max(1, value)
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        result = f"{chr(65 + remainder)}{result}"
    return result

@dataclass(slots=True)
class ContextWorkbenchDraftNode:
    order: float
    label: str
    record: dict[str, object]
    active: bool
    source_node_number: int | None = None
    source_index: int | None = None
    kind: str = "existing"
    status: str = "active"
    editable: bool = True

class ContextWorkbenchDraft:
    def __init__(
        self,
        transcript: list[dict[str, object]],
        selected_indexes: list[int],
        node_locks: dict[str, bool] | None = None,
        node_lock_revision: int = 0,
    ) -> None:
        normalized_transcript = normalize_transcript(transcript)
        display_records = normalize_context_records(normalized_transcript)
        self.node_locks = normalize_node_locks(node_locks or {})
        self.node_lock_revision = max(0, int(node_lock_revision or 0))
        safe_selected = normalize_selected_node_indexes(selected_indexes, len(display_records))
        self.selected_node_numbers = selected_display_node_numbers(display_records, safe_selected, self.node_locks)
        locked_indexes = context_node_locked_indexes(display_records, self.node_locks)
        editable_numbers_by_raw_index = {
            int(entry["raw_index"]): int(entry["node_number"])
            for entry in editable_context_node_entries(display_records, self.node_locks)
        }
        self.nodes: list[ContextWorkbenchDraftNode] = []
        for raw_index, record in enumerate(display_records):
            node_number = editable_numbers_by_raw_index.get(raw_index)
            is_locked = raw_index in locked_indexes
            label = f"Node #{node_number}" if node_number is not None else "Locked Node"
            self.nodes.append(
                ContextWorkbenchDraftNode(
                    order=float(raw_index + 1),
                    label=label,
                    record=sanitize_value(record),
                    active=True,
                    source_node_number=node_number,
                    source_index=raw_index,
                    kind="locked" if is_locked else "existing",
                    status="locked" if is_locked else "active",
                    editable=not is_locked,
                )
        )
        self.operations: list[dict[str, object]] = []
        self._draft_counter = 0
        self._working_version = 0

    def _record_operation(self, operation: dict[str, object]) -> None:
        self._working_version += 1
        operation["working_version"] = self._working_version
        self.operations.append(operation)

    @property
    def has_changes(self) -> bool:
        return bool(self.operations)

    def committed_nodes(self) -> list[ContextWorkbenchDraftNode]:
        return [node for node in sorted(self.nodes, key=lambda item: item.order) if node.active]

    def _nodes_by_number(self, node_numbers: list[int], *, include_inactive: bool = False) -> list[ContextWorkbenchDraftNode]:
        targets: list[ContextWorkbenchDraftNode] = []
        for node_number in node_numbers:
            node = next(
                (
                    item
                    for item in self.nodes
                    if item.source_node_number == node_number and (include_inactive or item.active)
                ),
                None,
            )
            if node is not None:
                targets.append(node)
        return targets

    def node_details(self, nodes: list[ContextWorkbenchDraftNode]) -> list[dict[str, object]]:
        details: list[dict[str, object]] = []
        for node in nodes:
            detail = context_record_details_payload(node.record, node_number=node.source_node_number or 1)
            detail["payload_kind"] = "node_detail"
            detail["node_number"] = node.source_node_number
            detail["label"] = node.label
            detail["status"] = node.status
            detail["active"] = node.active
            detail["node_kind"] = node.kind
            details.append(detail)
        return details
    def _next_draft_label(self) -> str:
        self._draft_counter += 1
        return f"Draft Node {letter_index(self._draft_counter)}"

    def _set_node_record(self, node: ContextWorkbenchDraftNode, record: dict[str, object], *, status: str = "updated") -> None:
        normalized_record = normalize_context_records([record])
        if not normalized_record:
            raise ValueError("record could not be normalized after mutation")
        node.record = normalized_record[0]
        if node.kind == "existing":
            node.status = status

    def _provider_items_for_node(self, node: ContextWorkbenchDraftNode) -> list[dict[str, Any]]:
        provider_items = normalize_provider_items(node.record.get("providerItems"))
        if provider_items:
            return provider_items
        if not isinstance(node.record, dict):
            return []
        node_transcript = normalize_transcript([node.record])
        return transcript_node_provider_items(node_transcript[0]) if node_transcript else []

    def committed_transcript(self) -> list[dict[str, object]]:
        core_nodes: list[dict[str, object]] = []
        for node in self.committed_nodes():
            if isinstance(node.record, dict) and "items" in node.record:
                core_nodes.extend(normalize_transcript([node.record]))
                continue
            provider_items = self._provider_items_for_node(node)
            if not provider_items:
                continue
            next_nodes = core_input_items_to_transcript(provider_items)
            existing_id = context_record_node_id(node.record)
            if existing_id and len(next_nodes) == 1 and isinstance(next_nodes[0], dict):
                next_nodes[0]["id"] = existing_id
            core_nodes.extend(next_nodes)
        return reindex_transcript_input_indexes(core_nodes)

    def _make_insert_provider_item(self, node: ContextWorkbenchDraftNode, ins: dict[str, Any]) -> dict[str, Any]:
        content = sanitize_text(ins.get("content") or "").strip()
        role = sanitize_text(node.record.get("role") or "user").strip() or "user"
        return {"type": "message", "role": role, "content": content}

    def apply_write_nodes(
        self,
        delete_numbers: list[int],
        inserts: list[dict[str, Any]],
    ) -> dict[str, object]:
        safe_deletes = sorted(set(n for n in delete_numbers if isinstance(n, int) and n > 0))
        nodes_to_delete = self._nodes_by_number(safe_deletes)
        active_deletes = [n for n in nodes_to_delete if n.active]

        anchor_order_counts: dict[float, int] = {}
        created_nodes: list[ContextWorkbenchDraftNode] = []
        for ins in inserts:
            try:
                after_int = int(ins.get("after") or 0)
            except (TypeError, ValueError):
                after_int = 0
            if after_int <= 0:
                anchor_order = 0.0
            else:
                anchor = next((n for n in self.nodes if n.source_node_number == after_int), None)
                anchor_order = anchor.order if anchor else float(after_int)
            anchor_order_counts[anchor_order] = anchor_order_counts.get(anchor_order, 0) + 1
            offset = 0.001 * anchor_order_counts[anchor_order]

            role_raw = sanitize_text(ins.get("role") or "user").strip()
            role = role_raw if role_raw in {"user", "assistant", "developer"} else "user"
            content = sanitize_text(ins.get("content") or "").strip()
            label = self._next_draft_label()
            created = ContextWorkbenchDraftNode(
                order=anchor_order + offset,
                label=label,
                record={
                    "role": role,
                    "text": content,
                    "attachments": [],
                    "toolEvents": [],
                    "blocks": [{"kind": "text", "text": content}],
                    "providerItems": [{"type": "message", "role": role, "content": content}],
                },
                active=True,
                source_node_number=None,
                kind="draft",
                status="created",
            )
            self.nodes.append(created)
            created_nodes.append(created)

        deleted_numbers: list[int] = []
        for node in active_deletes:
            node.active = False
            node.status = "deleted"
            if node.source_node_number is not None:
                deleted_numbers.append(node.source_node_number)

        summary_parts = []
        if deleted_numbers:
            summary_parts.append(f"Delete #{format_node_ranges(deleted_numbers)}")
        if created_nodes:
            summary_parts.append(f"Insert {len(created_nodes)} node(s)")
        summary = ", ".join(summary_parts) or "No changes"

        if deleted_numbers or created_nodes:
            self._record_operation({
                "operation_type": "write_nodes",
                "change_type": "compress" if created_nodes else "delete",
                "label": summary,
                "summary": summary,
                "changed_nodes": deleted_numbers,
                "target_node_numbers": deleted_numbers,
            })
        return {"summary": summary, "deleted": deleted_numbers, "inserted": len(created_nodes)}

    def apply_write_items(
        self,
        node_number: int,
        delete_item_numbers: list[int],
        inserts: list[dict[str, Any]],
    ) -> dict[str, object]:
        nodes = self._nodes_by_number([node_number])
        if not nodes:
            raise ValueError(f"Node #{node_number} not found")
        node = nodes[0]
        provider_items = list(self._provider_items_for_node(node))

        safe_deletes = sorted(set(n for n in delete_item_numbers if 1 <= n <= len(provider_items)))
        delete_set = set(safe_deletes)

        inserts_by_after: dict[int, list[dict[str, Any]]] = {}
        for ins in inserts:
            try:
                after = int(ins.get("after") or 0)
            except (TypeError, ValueError):
                after = 0
            inserts_by_after.setdefault(after, []).append(ins)

        new_items: list[dict[str, Any]] = []
        for stub in inserts_by_after.get(0, []):
            new_items.append(self._make_insert_provider_item(node, stub))
        for i, item in enumerate(provider_items):
            item_number = i + 1
            if item_number not in delete_set:
                new_items.append(item)
            for stub in inserts_by_after.get(item_number, []):
                new_items.append(self._make_insert_provider_item(node, stub))

        validate_context_provider_items(new_items)
        self._set_node_record(node, compile_record_from_provider_items(node.record, new_items))

        changed_nodes = [node.source_node_number] if node.source_node_number is not None else []
        summary = f"Edit items in Node #{node_number} (del:{len(safe_deletes)}, ins:{len(inserts)})"
        self._record_operation({
            "operation_type": "write_items",
            "change_type": "compress" if inserts else "delete",
            "label": summary,
            "summary": summary,
            "changed_nodes": changed_nodes,
            "target_node_numbers": changed_nodes,
        })
        return {"applied": True, "node": node_number, "items_deleted": len(safe_deletes), "items_inserted": len(inserts)}

    def build_draft_snapshot_text(self, session_title: str) -> str:
        active = [n for n in sorted(self.nodes, key=lambda n: n.order) if n.active and n.editable]
        selected_numbers = self.selected_node_numbers
        lines = [
            "# 当前主 Codex 上下文快照（已更新）",
            f"- 会话标题：{session_title}",
            f"- 当前节点数：{len(active)}",
            "",
            "## 节点概览",
        ]
        for seq, node in enumerate(active, 1):
            display_num = node.source_node_number or seq
            overview = context_record_overview(node.record, node_number=display_num, selected=display_num in selected_numbers)
            role = sanitize_text(overview.get("role") or "").strip() or "unknown"
            token_label = format_token_count(int(overview.get("token_estimate") or 0))
            kind_mark = " [new]" if node.kind == "draft" else ""
            if role != "assistant":
                text = sanitize_text(overview.get("full_text") or "").strip() or "[empty]"
                lines.append(f"- Node #{seq}{kind_mark} | {role} | {token_label} tokens")
                lines.append("  content:")
                for line in text.splitlines() or ["[empty]"]:
                    lines.append(f"    {line}")
            else:
                tool_est = int(overview.get("tool_token_estimate") or 0)
                tool_label = f" | tool {format_token_count(tool_est)} tokens" if tool_est > 0 else ""
                lines.append(
                    f"- Node #{seq}{kind_mark} | {role} | {token_label} tokens{tool_label}"
                    f" | {format_tool_usage(overview.get('tool_usage', {}))} | {int(overview.get('item_count') or 0)} items"
                )
                lines.append(f"  preview: {sanitize_text(overview.get('preview') or '') or '[empty]'}")
        return "\n".join(lines).strip()

class ContextWorkbenchToolRegistry:
    def __init__(
        self,
        draft: ContextWorkbenchDraft,
        session_title: str = "",
        *,
        review_mode: bool = False,
    ) -> None:
        self.draft = draft
        self._session_title = session_title
        self.review_mode = review_mode
        self.review_rationale = ""
        definitions = (
            [self._build_write_nodes_tool()]
            if review_mode
            else [
                self._build_get_nodes_tool(),
                self._build_write_nodes_tool(),
                self._build_write_items_tool(),
            ]
        )
        self._tools = {
            definition.name: definition
            for definition in definitions
        }

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_schema() for tool in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolExecution:
        tool = self._tools.get(name)
        if tool is None:
            return ToolExecution(
                output_text=json.dumps({"error": f"unknown workbench tool: {name}"}, ensure_ascii=False),
                display_title=name,
                display_detail="unknown context workbench tool",
                display_result="The requested context workbench tool does not exist.",
                status="error",
            )

        try:
            return tool.handler(arguments)
        except Exception as exc:  # noqa: BLE001
            return ToolExecution(
                output_text=json.dumps({"error": str(exc), "tool": name}, ensure_ascii=False),
                display_title=tool.label,
                display_detail="context workbench tool failed",
                display_result=sanitize_text(str(exc) or "The context workbench tool failed."),
                status="error",
            )

    def _build_get_nodes_tool(self) -> "ContextWorkbenchToolDefinition":
        def handler(arguments):
            raw_numbers = arguments.get("node_numbers")
            if not isinstance(raw_numbers, list) or not raw_numbers:
                return ToolExecution(
                    output_text='{"error":"node_numbers is required"}',
                    display_title="Get Nodes", display_detail="missing node_numbers",
                    display_result="node_numbers is required.", status="error",
                )
            node_numbers = [int(n) for n in raw_numbers if isinstance(n, (int, float))]
            nodes = self.draft._nodes_by_number(node_numbers)
            if not nodes:
                return ToolExecution(
                    output_text='{"error":"no matching nodes found"}',
                    display_title="Get Nodes", display_detail="no nodes found",
                    display_result="No matching nodes found in the current snapshot.", status="error",
                )
            details = self.draft.node_details(nodes)
            label = ", ".join(f"Node #{n}" for n in node_numbers)
            return ToolExecution(
                output_text=json.dumps({"nodes": details}, ensure_ascii=False),
                display_title="Get Nodes", display_detail=label,
                display_result=f"Returned details for {label}.",
            )
        return ContextWorkbenchToolDefinition(
            name="get_nodes", label="Get Nodes",
            description="Expand one or more nodes into full structured item details. Only needed for assistant nodes — non-assistant full text is already in the snapshot.",
            parameters={
                "type": "object",
                "properties": {
                    "node_numbers": {
                        "type": "array", "items": {"type": "integer"},
                        "description": "1-based Node # values from the current snapshot.",
                    },
                },
                "required": ["node_numbers"], "additionalProperties": False,
            },
            handler=handler,
        )

    def _build_write_nodes_tool(self) -> "ContextWorkbenchToolDefinition":
        def handler(arguments):
            raw_delete = arguments.get("delete") or []
            raw_inserts = arguments.get("inserts") or []
            delete_numbers = [int(n) for n in raw_delete if isinstance(n, (int, float))]
            inserts = [i for i in raw_inserts if isinstance(i, dict)]
            if not delete_numbers and not inserts:
                return ToolExecution(
                    output_text='{"error":"provide delete and/or inserts"}',
                    display_title="Write Nodes", display_detail="nothing to do",
                    display_result="Provide delete and/or inserts.", status="error",
                )
            if self.review_mode:
                self.review_rationale = sanitize_text(arguments.get("review_rationale") or "").strip()
                if not self.review_rationale:
                    return ToolExecution(
                        output_text='{"error":"review_rationale is required"}',
                        display_title="Write Nodes", display_detail="missing review rationale",
                        display_result="review_rationale is required for context review.", status="error",
                    )
            result = self.draft.apply_write_nodes(delete_numbers, inserts)
            new_snapshot = self.draft.build_draft_snapshot_text(self._session_title)
            return ToolExecution(
                output_text=json.dumps({"result": result, "updated_snapshot": new_snapshot}, ensure_ascii=False),
                display_title="Write Nodes",
                display_detail=sanitize_text(result.get("summary") or ""),
                display_result=sanitize_text(result.get("summary") or "Nodes updated."),
            )
        return ContextWorkbenchToolDefinition(
            name="write_nodes", label="Write Nodes",
            description=(
                "Submit the complete selective-compression edit in one call. "
                "All node numbers reference the initial transcript for this turn."
                if self.review_mode
                else
                "Delete and/or insert nodes in the working snapshot. "
                "All node numbers reference the initial snapshot for this turn. "
                "Returns updated_snapshot — use it to confirm the result to the user."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "delete": {
                        "type": "array", "items": {"type": "integer"},
                        "description": "Node numbers to delete (initial snapshot). Can be non-contiguous.",
                    },
                    "inserts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "after": {"type": "integer", "description": "Anchor node number (initial snapshot). Insert goes after this position. Use 0 to insert before all nodes. Valid even if the anchor node is also deleted."},
                                "role": {"type": "string", "description": "user | assistant | developer. Defaults to user."},
                                "content": {"type": "string", "description": "Markdown content for the new node."},
                            },
                            "required": ["after", "content"], "additionalProperties": False,
                        },
                        "description": "Nodes to insert. Each is independent of deletions; after references the initial snapshot.",
                    },
                    **(
                        {
                            "review_rationale": {
                                "type": "string",
                                "description": "User-facing proposal rationale: why this consolidation is useful, why it should not affect the current task, what important information remains, and any material risk. Use proposal language. Never mention node numbers, token counts, tools, or draft mechanics.",
                            }
                        }
                        if self.review_mode
                        else {}
                    ),
                },
                "required": ["review_rationale"] if self.review_mode else [],
                "additionalProperties": False,
            },
            handler=handler,
        )

    def _build_write_items_tool(self) -> "ContextWorkbenchToolDefinition":
        def handler(arguments):
            try:
                node_number = int(arguments.get("node_number") or 0)
            except (TypeError, ValueError):
                node_number = 0
            if node_number <= 0:
                return ToolExecution(
                    output_text='{"error":"node_number is required"}',
                    display_title="Write Items", display_detail="missing node_number",
                    display_result="node_number is required.", status="error",
                )
            raw_delete = arguments.get("delete") or []
            raw_inserts = arguments.get("inserts") or []
            delete_item_numbers = [int(n) for n in raw_delete if isinstance(n, (int, float))]
            inserts = [i for i in raw_inserts if isinstance(i, dict)]
            result = self.draft.apply_write_items(node_number, delete_item_numbers, inserts)
            deleted = result["items_deleted"]
            inserted = result["items_inserted"]
            return ToolExecution(
                output_text=json.dumps(result, ensure_ascii=False),
                display_title="Write Items", display_detail=f"Node #{node_number}",
                display_result=f"Node #{node_number}: deleted {deleted}, inserted {inserted}.",
            )
        return ContextWorkbenchToolDefinition(
            name="write_items", label="Write Items",
            description="Delete and/or insert items within a single node. Use get_nodes first to see item numbers.",
            parameters={
                "type": "object",
                "properties": {
                    "node_number": {"type": "integer", "description": "The node to edit."},
                    "delete": {
                        "type": "array", "items": {"type": "integer"},
                        "description": "Item numbers to delete (1-based, from original item list).",
                    },
                    "inserts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "after": {"type": "integer", "description": "Insert after this item number (original). Use 0 to insert before all items."},
                                "content": {"type": "string"},
                                "kind": {"type": "string", "description": "Optional: text | tool. Auto-detected if omitted."},
                            },
                            "required": ["after", "content"], "additionalProperties": False,
                        },
                    },
                },
                "required": ["node_number"], "additionalProperties": False,
            },
            handler=handler,
        )


def normalize_context_chat_history(raw_history: Any) -> list[dict[str, object]]:
    if not isinstance(raw_history, list):
        return []

    history: list[dict[str, object]] = []
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        role = sanitize_text(item.get("role") or "").strip()
        if role not in {"user", "assistant"}:
            continue
        blocks = normalize_message_blocks(item.get("blocks"))
        tool_events = (
            sanitize_value(item.get("toolEvents"))
            if isinstance(item.get("toolEvents"), list)
            else extract_tool_events_from_blocks(blocks)
        )
        if not isinstance(tool_events, list):
            tool_events = []
        content = sanitize_text(item.get("content") or "").strip() or message_blocks_to_text(blocks).strip()
        if not content and not blocks and not tool_events:
            continue
        record: dict[str, object] = {
            "role": role,
            "content": content,
        }
        if role == "assistant":
            if tool_events:
                record["toolEvents"] = tool_events
            if blocks:
                record["blocks"] = blocks
        history.append(record)
    return history

def normalize_attachment_records(raw_attachments: Any) -> list[dict[str, object]]:
    if not isinstance(raw_attachments, list):
        return []

    normalized: list[dict[str, object]] = []
    for item in raw_attachments:
        if not isinstance(item, dict):
            continue

        name = sanitize_text(item.get("name") or "").strip()
        relative_path = sanitize_text(item.get("relative_path") or "").strip()
        mime_type = sanitize_text(item.get("mime_type") or "").strip()
        kind = sanitize_text(item.get("kind") or "").strip() or "file"
        attachment_id = sanitize_text(item.get("id") or "").strip()

        if not name or not relative_path:
            continue

        size_bytes = item.get("size_bytes")
        if not isinstance(size_bytes, int):
            try:
                size_bytes = int(size_bytes)
            except (TypeError, ValueError):
                size_bytes = 0

        normalized.append(
            {
                "id": attachment_id or uuid.uuid4().hex,
                "name": name,
                "mime_type": mime_type or "application/octet-stream",
                "kind": "image" if kind == "image" else "file",
                "size_bytes": max(0, size_bytes),
                "relative_path": relative_path,
                "url": f"/{relative_path}",
            }
        )

    return normalized

def build_attachment_input(name: str, mime_type: str, data_url: str) -> dict[str, Any]:
    safe_name = sanitize_text(name).strip() or "upload"
    safe_mime_type = sanitize_text(mime_type).strip() or "application/octet-stream"
    safe_data_url = sanitize_text(data_url)

    if safe_mime_type.startswith("image/"):
        return {
            "type": "input_image",
            "image_url": safe_data_url,
            "detail": "auto",
        }

    return {
        "type": "input_file",
        "filename": safe_name,
        "file_data": safe_data_url,
    }

def build_attachment_path_note(name: str, mime_type: str, file_path: Path) -> dict[str, str]:
    safe_name = sanitize_text(name).strip() or file_path.name
    safe_mime_type = sanitize_text(mime_type).strip() or "application/octet-stream"
    return {
        "type": "input_text",
        "text": (
            f"Attachment available locally: {safe_name}\n"
            f"MIME type: {safe_mime_type}\n"
            f"Local path for tools: {file_path}"
        ),
    }

def attachment_inputs_from_records(attachments: list[dict[str, object]]) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    for attachment in attachments:
        relative_path = sanitize_text(attachment.get("relative_path") or "").strip()
        name = sanitize_text(attachment.get("name") or "").strip()
        mime_type = sanitize_text(attachment.get("mime_type") or "").strip()
        if not relative_path:
            continue

        file_path = resolve_attachment_file_path(relative_path)
        if file_path is None or not file_path.exists() or not file_path.is_file():
            continue

        raw_bytes = file_path.read_bytes()
        if not raw_bytes:
            continue

        safe_mime_type = mime_type or mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        data_url = f"data:{safe_mime_type};base64,{base64.b64encode(raw_bytes).decode('ascii')}"
        inputs.append(build_attachment_path_note(name or file_path.name, safe_mime_type, file_path))
        inputs.append(build_attachment_input(name or file_path.name, safe_mime_type, data_url))

    return inputs

def model_options(default_model: str, configured_models: list[str] | None = None) -> list[str]:
    ordered = [
        default_model,
        *(configured_models or []),
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.2",
    ]
    unique_models: list[str] = []
    for model in ordered:
        safe_model = sanitize_text(model).strip()
        if safe_model and safe_model not in unique_models:
            unique_models.append(safe_model)
    return unique_models

