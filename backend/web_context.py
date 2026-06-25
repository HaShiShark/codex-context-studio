from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
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

from codex_context import is_conversation_record
from simple_agent.agent import SimpleAgent, ToolEvent, sanitize_text
from simple_agent.config import Settings
from simple_agent.tools import ToolExecution

from backend.web_constants import (
    ATTACHMENTS_DIR,
    ATTACHMENTS_ROUTE,
    CODEX_LOCAL_SESSIONS_DIR,
    CODEX_PAIRED_TOOL_CALL_ITEM_TYPES,
    CODEX_STANDALONE_TOOL_CALL_ITEM_TYPES,
    CODEX_TOOL_CALL_ITEM_TYPES,
    CODEX_TOOL_CALL_TYPES_BY_OUTPUT_TYPE,
    CODEX_TOOL_OUTPUT_ITEM_TYPES,
    CODEX_TOOL_OUTPUT_TYPES_BY_CALL_TYPE,
    CONTEXT_EDITABLE_PROVIDER_ITEM_TYPES,
    CONTEXT_INPUT_MESSAGE_ROLES,
    CONTEXT_INPUT_RECORD_ROLES,
    CONTEXT_REQUEST_DEBUG_FILE,
    CONTEXT_EDIT_MARKERS_FILE,
    ContextWorkbenchToolDefinition,
    REPO_ROOT,
    SessionState,
    STATE_FILE,
    PROXY_STATE_FILE,
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

def attachment_url_path(stored_name: str) -> str:
    return f"{ATTACHMENTS_ROUTE}/{stored_name}"

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

def fallback_blocks_from_text_and_tools(
    role: str,
    text: str,
    tool_events: list[dict[str, object]],
) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    safe_text = sanitize_text(text)

    if safe_text:
        blocks.append(
            {
                "kind": "text",
                "text": safe_text,
            }
        )

    if role == "assistant":
        for tool_event in tool_events:
            blocks.append(
                {
                    "kind": "tool",
                    "tool_event": sanitize_value(tool_event),
                }
            )

    return blocks

def _find_tag(value: str, tag: str) -> int:
    return value.lower().find(tag)

def _safe_emit_split(value: str, tag: str) -> tuple[str, str]:
    lower_value = value.lower()
    max_suffix_length = min(len(value), len(tag) - 1)
    for suffix_length in range(max_suffix_length, 0, -1):
        if tag.startswith(lower_value[-suffix_length:]):
            return value[:-suffix_length], value[-suffix_length:]
    return value, ""

class ThinkTagStreamParser:
    def __init__(
        self,
        *,
        on_text_delta: Callable[[str], None],
        on_reasoning_start: Callable[[], None],
        on_reasoning_delta: Callable[[str], None],
        on_reasoning_done: Callable[[], None],
    ) -> None:
        self.on_text_delta = on_text_delta
        self.on_reasoning_start = on_reasoning_start
        self.on_reasoning_delta = on_reasoning_delta
        self.on_reasoning_done = on_reasoning_done
        self.buffer = ""
        self.in_reasoning = False

    def feed(self, delta: str) -> None:
        safe_delta = sanitize_text(delta)
        if not safe_delta:
            return

        self.buffer = f"{self.buffer}{safe_delta}"
        self._drain()

    def finish(self) -> None:
        if self.buffer:
            if self.in_reasoning:
                self.on_reasoning_delta(self.buffer)
            else:
                self.on_text_delta(self.buffer)
            self.buffer = ""

        if self.in_reasoning:
            self.in_reasoning = False
            self.on_reasoning_done()

    def _drain(self) -> None:
        while self.buffer:
            if self.in_reasoning:
                close_index = _find_tag(self.buffer, "</think>")
                if close_index >= 0:
                    before_close = self.buffer[:close_index]
                    if before_close:
                        self.on_reasoning_delta(before_close)
                    self.buffer = self.buffer[close_index + len("</think>") :]
                    self.in_reasoning = False
                    self.on_reasoning_done()
                    continue

                emit_text, retained = _safe_emit_split(self.buffer, "</think>")
                if emit_text:
                    self.on_reasoning_delta(emit_text)
                self.buffer = retained
                return

            open_index = _find_tag(self.buffer, "<think>")
            if open_index >= 0:
                before_open = self.buffer[:open_index]
                if before_open:
                    self.on_text_delta(before_open)
                self.buffer = self.buffer[open_index + len("<think>") :]
                self.in_reasoning = True
                self.on_reasoning_start()
                continue

            emit_text, retained = _safe_emit_split(self.buffer, "<think>")
            if emit_text:
                self.on_text_delta(emit_text)
            self.buffer = retained
            return

def blocks_from_text_and_tools(
    role: str,
    text: str,
    tool_events: list[dict[str, object]],
) -> list[dict[str, object]]:
    if role != "assistant":
        return fallback_blocks_from_text_and_tools(role, text, tool_events)

    blocks: list[dict[str, object]] = []
    active_reasoning_index: int | None = None

    def append_text_delta(delta: str) -> None:
        safe_delta = sanitize_text(delta)
        if not safe_delta:
            return
        if blocks and blocks[-1].get("kind") == "text":
            blocks[-1]["text"] = sanitize_text(f"{blocks[-1].get('text', '')}{safe_delta}")
            return
        blocks.append({"kind": "text", "text": safe_delta})

    def start_reasoning() -> None:
        nonlocal active_reasoning_index
        if active_reasoning_index is not None:
            return
        blocks.append({"kind": "reasoning", "text": "", "status": "streaming"})
        active_reasoning_index = len(blocks) - 1

    def append_reasoning_delta(delta: str) -> None:
        nonlocal active_reasoning_index
        safe_delta = sanitize_text(delta)
        if not safe_delta:
            return
        if active_reasoning_index is None:
            start_reasoning()
        if active_reasoning_index is None:
            return
        block = blocks[active_reasoning_index]
        block["text"] = sanitize_text(f"{block.get('text', '')}{safe_delta}")

    def finish_reasoning() -> None:
        nonlocal active_reasoning_index
        if active_reasoning_index is None:
            return
        blocks[active_reasoning_index]["status"] = "completed"
        active_reasoning_index = None

    parser = ThinkTagStreamParser(
        on_text_delta=append_text_delta,
        on_reasoning_start=start_reasoning,
        on_reasoning_delta=append_reasoning_delta,
        on_reasoning_done=finish_reasoning,
    )
    parser.feed(text)
    parser.finish()

    for tool_event in tool_events:
        blocks.append(
            {
                "kind": "tool",
                "tool_event": sanitize_value(tool_event),
            }
        )

    return blocks

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

        item_type = sanitize_text(item.get("type") or "").strip()
        if item_type == "message":
            role = sanitize_text(item.get("role") or "").strip()
            if role not in CONTEXT_INPUT_MESSAGE_ROLES:
                continue

            content = item.get("content")
            if isinstance(content, list):
                safe_content = sanitize_value(content)
            else:
                safe_content = sanitize_text(content or "")

            normalized.append(
                {
                    "type": "message",
                    "role": role,
                    "content": safe_content,
                }
            )
            continue

        if item_type in {"compaction", "compaction_summary"}:
            normalized.append(sanitize_value(item))
            continue

        if item_type == "function_call":
            call_id = sanitize_text(item.get("call_id") or "").strip()
            name = sanitize_text(item.get("name") or "").strip()
            if not call_id or not name:
                continue

            normalized.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": sanitize_text(item.get("arguments") or "{}") or "{}",
                }
            )
            continue

        if item_type == "function_call_output":
            call_id = sanitize_text(item.get("call_id") or "").strip()
            if not call_id:
                continue

            normalized.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": sanitize_value(item.get("output") if "output" in item else ""),
                }
            )

        elif item_type:
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

def message_blocks_have_reasoning(blocks: list[dict[str, object]]) -> bool:
    return any(sanitize_text(block.get("kind") or "").strip() == "reasoning" for block in blocks)

def normalize_transcript(raw_records: Any) -> list[dict[str, object]]:
    if not isinstance(raw_records, list):
        return []

    records: list[dict[str, object]] = []
    for record_index, item in enumerate(raw_records):
        if not isinstance(item, dict):
            continue
        role = sanitize_text(item.get("role") or "").strip()
        if role not in CONTEXT_INPUT_RECORD_ROLES:
            continue
        tool_events = item.get("toolEvents")
        attachments = item.get("attachments")
        normalized_attachments = normalize_attachment_records(attachments)
        normalized_provider_items = normalize_provider_items(item.get("providerItems"))
        recovered_record = (
            compile_record_from_provider_items(
                {
                    "role": role,
                    "attachments": normalized_attachments,
                },
                normalized_provider_items,
            )
            if normalized_provider_items
            else None
        )

        if isinstance(recovered_record, dict):
            safe_text = sanitize_text(recovered_record.get("text") or "")
            safe_tool_events = (
                sanitize_value(recovered_record.get("toolEvents"))
                if isinstance(recovered_record.get("toolEvents"), list)
                else []
            )
            blocks = normalize_message_blocks(recovered_record.get("blocks"))
            provider_items = normalize_provider_items(recovered_record.get("providerItems"))
        else:
            safe_text = sanitize_text(item.get("text") or "")
            safe_tool_events = sanitize_value(tool_events) if isinstance(tool_events, list) else []
            blocks = normalize_message_blocks(item.get("blocks"))
            if not blocks:
                blocks = blocks_from_text_and_tools(
                    role,
                    safe_text,
                    safe_tool_events,
                )
            if role == "assistant" and not safe_tool_events:
                safe_tool_events = extract_tool_events_from_blocks(blocks)
            if not safe_text:
                safe_text = message_blocks_to_text(blocks)
            provider_items = build_provider_items_for_record(
                role=role,
                text=safe_text,
                attachments=normalized_attachments,
                tool_events=safe_tool_events,
                blocks=blocks,
                record_index=record_index,
            )
        records.append(
            {
                "role": role,
                "text": safe_text,
                "attachments": normalized_attachments,
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

def settings_payload(settings: Settings) -> dict[str, object]:
    return settings.public_payload()

def estimate_provider_item_token_count(item: dict[str, Any]) -> int:
    item_type = sanitize_text(item.get("type") or "").strip()
    if item_type == "message":
        return estimate_token_count(extract_text_from_provider_message_content(item.get("content")))

    if item_type == "function_call":
        source = "\n".join(
            part
            for part in [
                sanitize_text(item.get("name") or ""),
                sanitize_text(item.get("arguments") or ""),
            ]
            if part.strip()
        )
        return estimate_token_count(source)

    if item_type == "function_call_output":
        return estimate_token_count(sanitize_text(item.get("output") or ""))

    return 0

def estimate_tool_schema_token_count(schema: dict[str, Any]) -> int:
    parts = [
        sanitize_text(schema.get("name") or ""),
        sanitize_text(schema.get("description") or ""),
    ]
    parameters = schema.get("parameters")
    if isinstance(parameters, dict):
        parts.append(json.dumps(sanitize_value(parameters), ensure_ascii=False))
    elif parameters is not None:
        parameter_text = sanitize_text(parameters)
        if parameter_text.strip():
            parts.append(parameter_text)

    return estimate_token_count("\n".join(part for part in parts if part.strip()))

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
                "node_indexes",
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

def provider_items_tool_token_count(items: list[dict[str, Any]]) -> int:
    total = 0
    for item in items:
        if sanitize_text(item.get("type") or "").strip() not in {"function_call", "function_call_output"}:
            continue
        total += estimate_provider_item_token_count(item)
    return total

def is_environment_context_record(record: dict[str, object]) -> bool:
    if sanitize_text(record.get("role") or "").strip() != "user":
        return False
    return sanitize_text(record.get("text") or "").lstrip().lower().startswith("<environment_context>")

def internal_context_prefix_indexes(transcript: list[dict[str, object]]) -> set[int]:
    internal_indexes: set[int] = set()
    environment_index: int | None = None

    for index, record in enumerate(transcript):
        role = sanitize_text(record.get("role") or "").strip()
        if role in {"system", "developer"}:
            internal_indexes.add(index)
            continue
        if environment_index is None and is_environment_context_record(record):
            environment_index = index

    if environment_index is not None:
        internal_indexes.add(environment_index)

    return internal_indexes

def editable_context_node_entries(transcript: list[dict[str, object]]) -> list[dict[str, object]]:
    internal_indexes = internal_context_prefix_indexes(transcript)
    entries: list[dict[str, object]] = []
    node_number = 0
    for index, record in enumerate(transcript):
        if index in internal_indexes:
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

def editable_context_node_count(transcript: list[dict[str, object]]) -> int:
    return len(editable_context_node_entries(transcript))

def selected_display_node_numbers(
    transcript: list[dict[str, object]],
    selected_indexes: list[int],
) -> list[int]:
    selected_index_set = set(selected_indexes)
    return [
        int(entry["node_number"])
        for entry in editable_context_node_entries(transcript)
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

def normalize_node_numbers(raw_numbers: Any, max_node_number: int) -> list[int]:
    if not isinstance(raw_numbers, list):
        return []

    normalized: list[int] = []
    for raw_item in raw_numbers:
        try:
            node_number = int(raw_item)
        except (TypeError, ValueError):
            continue

        if 1 <= node_number <= max_node_number and node_number not in normalized:
            normalized.append(node_number)

    return normalized

def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def append_jsonl_state_event(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(sanitize_value(payload), ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")

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

def unique_int_list(values: Any) -> list[int]:
    if not isinstance(values, list):
        return []

    unique_values: list[int] = []
    for raw_value in values:
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            continue
        if value not in unique_values:
            unique_values.append(value)
    return unique_values

def unique_text_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []

    unique_values: list[str] = []
    for raw_value in values:
        value = sanitize_text(raw_value or "").strip()
        if value and value not in unique_values:
            unique_values.append(value)
    return unique_values

def operation_changed_nodes(operation: dict[str, object]) -> list[int]:
    explicit_nodes = unique_int_list(operation.get("changed_nodes"))
    if explicit_nodes:
        return explicit_nodes

    target_nodes = unique_int_list(operation.get("target_node_numbers"))
    if target_nodes:
        return target_nodes

    target_items = operation.get("target_items")
    if isinstance(target_items, list):
        item_nodes: list[int] = []
        for item in target_items:
            if not isinstance(item, dict):
                continue
            try:
                node_number = int(item.get("node_number") or 0)
            except (TypeError, ValueError):
                continue
            if node_number > 0 and node_number not in item_nodes:
                item_nodes.append(node_number)
        if item_nodes:
            return item_nodes

    return []

def normalize_change_type(raw_value: Any) -> str:
    value = sanitize_text(raw_value or "").strip().lower()
    if value in {"delete", "replace", "compress", "mixed", "update"}:
        return value
    if value.startswith("delete"):
        return "delete"
    if value.startswith("replace"):
        return "replace"
    if value.startswith("compress"):
        return "compress"
    return "update"

def operation_change_type(operation: dict[str, object]) -> str:
    return normalize_change_type(
        operation.get("change_type")
        or operation.get("operation_type")
        or operation.get("type")
        or "update"
    )

def summarize_change_type(change_types: list[str]) -> str:
    normalized = [normalize_change_type(item) for item in change_types if sanitize_text(item).strip()]
    unique_types = [item for item in normalized if item]
    if not unique_types:
        return "update"
    if len(set(unique_types)) == 1:
        return unique_types[0]
    return "mixed"

def summarize_changed_nodes_from_operations(operations: list[dict[str, object]]) -> list[int]:
    changed_nodes: list[int] = []
    for operation in operations:
        for node_number in operation_changed_nodes(operation):
            if node_number not in changed_nodes:
                changed_nodes.append(node_number)
    return changed_nodes

def fallback_context_revision_summary(label: str, operations: list[dict[str, object]]) -> str:
    safe_label = sanitize_text(label).strip() or "Context update"
    if not operations:
        return safe_label

    if len(operations) == 1:
        operation = operations[0]
        operation_type = sanitize_text(operation.get("operation_type") or "").strip()
        target_nodes = unique_int_list(operation.get("target_node_numbers") or operation.get("changed_nodes"))
        node_text = f"节点 #{format_node_ranges(target_nodes)}" if target_nodes else "当前上下文"
        target_items = operation.get("target_items")
        first_item = target_items[0] if isinstance(target_items, list) and target_items else {}
        item_number = int(first_item.get("item_number") or 0) if isinstance(first_item, dict) else 0
        item_text = f"{node_text} 的第 {item_number} 个条目" if item_number else node_text

        if operation_type == "compress_nodes":
            return f"把{node_text}压缩成了更短的摘要，尽量保留主要信息。"
        if operation_type == "delete_nodes":
            return f"删除了{node_text}，让当前上下文更紧凑。"
        if operation_type == "delete_item":
            return f"删除了{item_text}，去掉了不再需要的上下文内容。"
        if operation_type == "compress_item":
            return f"压缩了{item_text}，保留原有条目类型的同时缩短了内容。"
        if operation_type == "replace_item":
            return f"改写了{item_text}，把它换成了更合适的新内容。"

    changed_nodes = summarize_changed_nodes_from_operations(operations)
    if changed_nodes:
        return f"这一轮集中更新了节点 #{format_node_ranges(changed_nodes)} 的内容，并把它们整理成了新的上下文版本。"
    return safe_label

def find_active_context_revision_id(revisions: list[dict[str, object]]) -> str | None:
    for revision in revisions:
        revision_id = sanitize_text(revision.get("id") or "").strip()
        if revision_id and bool(revision.get("is_active")):
            return revision_id
    return None

def mark_active_context_revision(revisions: list[dict[str, object]], revision_id: str | None) -> None:
    safe_revision_id = sanitize_text(revision_id or "").strip()
    for revision in revisions:
        current_id = sanitize_text(revision.get("id") or "").strip()
        revision["is_active"] = bool(safe_revision_id and current_id == safe_revision_id)

def coerce_context_revision_number(raw_value: Any, fallback: int, *, minimum: int = 0) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = int(fallback)
    return max(minimum, value)

def has_initial_context_revision(revisions: list[dict[str, object]]) -> bool:
    return any(
        coerce_context_revision_number(revision.get("revision_number"), 1) == 0
        for revision in revisions
    )

def next_context_revision_number(revisions: list[dict[str, object]]) -> int:
    numbers = [
        coerce_context_revision_number(revision.get("revision_number"), 0)
        for revision in revisions
    ]
    return max([number for number in numbers if number > 0], default=0) + 1

def ensure_initial_context_revision(session: SessionState) -> None:
    if has_initial_context_revision(session.context_revisions):
        return
    if session.context_revisions:
        return

    session.context_revisions.append(
        build_context_revision_entry(
            transcript=normalize_transcript(session.transcript),
            context_workbench_history=normalize_context_chat_history(session.context_workbench_history),
            revision_label="初始版本",
            revision_summary="还没有进行压缩、删除或替换时的完整上下文。",
            operations=[],
            revision_number=0,
        )
    )

def sync_active_context_revision_snapshot(session: SessionState) -> None:
    active_revision_id = find_active_context_revision_id(session.context_revisions)
    if not active_revision_id:
        return

    safe_snapshot = sanitize_value(normalize_transcript(session.transcript))
    safe_context_workbench_history = sanitize_value(
        normalize_context_chat_history(session.context_workbench_history)
    )
    for revision in reversed(session.context_revisions):
        current_id = sanitize_text(revision.get("id") or "").strip()
        if current_id != active_revision_id:
            continue
        revision["snapshot"] = safe_snapshot
        revision["context_workbench_history_snapshot"] = safe_context_workbench_history
        revision["node_count"] = editable_context_node_count(normalize_transcript(session.transcript))
        return

def build_context_revision_entry(
    *,
    transcript: list[dict[str, object]],
    context_workbench_history: list[dict[str, str]],
    revision_label: str,
    revision_summary: str,
    operations: list[dict[str, object]],
    revision_number: int,
) -> dict[str, object]:
    sanitized_operations = [
        sanitize_value(operation)
        for operation in operations
        if isinstance(operation, dict)
    ]
    changed_nodes = summarize_changed_nodes_from_operations(sanitized_operations)
    change_types = [
        operation_change_type(operation)
        for operation in sanitized_operations
    ]
    label = sanitize_text(revision_label).strip() or "Context update"
    summary = sanitize_text(revision_summary).strip() or fallback_context_revision_summary(label, sanitized_operations)
    return {
        "id": uuid.uuid4().hex,
        "label": label,
        "summary": summary,
        "created_at": utc_timestamp(),
        "revision_number": coerce_context_revision_number(revision_number, 1),
        "change_type": summarize_change_type(change_types),
        "change_types": unique_text_list(change_types),
        "changed_nodes": changed_nodes,
        "operations": sanitized_operations,
        "node_count": editable_context_node_count(normalize_transcript(transcript)),
        "snapshot": sanitize_value(transcript),
        "context_workbench_history_snapshot": sanitize_value(
            normalize_context_chat_history(context_workbench_history)
        ),
        "is_active": True,
    }

def normalize_context_revision_entries(raw_entries: Any) -> list[dict[str, object]]:
    if not isinstance(raw_entries, list):
        return []

    normalized: list[dict[str, object]] = []
    for index, item in enumerate(raw_entries, start=1):
        if not isinstance(item, dict):
            continue

        revision_id = sanitize_text(item.get("id") or "").strip()
        label = sanitize_text(item.get("label") or "").strip()
        created_at = sanitize_text(item.get("created_at") or "").strip() or utc_timestamp()
        snapshot = normalize_transcript(item.get("snapshot"))
        context_workbench_history_snapshot = normalize_context_chat_history(
            item.get("context_workbench_history_snapshot")
        )
        operations = sanitize_value(item.get("operations")) if isinstance(item.get("operations"), list) else []
        if not revision_id or not label:
            continue

        changed_nodes = unique_int_list(item.get("changed_nodes")) or summarize_changed_nodes_from_operations(operations)
        change_types = unique_text_list(item.get("change_types"))
        if not change_types:
            change_types = [operation_change_type(operation) for operation in operations if isinstance(operation, dict)]
        change_type = normalize_change_type(item.get("change_type") or summarize_change_type(change_types))

        summary = sanitize_text(item.get("summary") or "").strip()
        if not summary or summary == label:
            summary = fallback_context_revision_summary(label, operations)

        normalized.append(
            {
                "id": revision_id,
                "label": label,
                "summary": summary,
                "created_at": created_at,
                "revision_number": coerce_context_revision_number(
                    item.get("revision_number"),
                    index,
                ),
                "change_type": change_type,
                "change_types": unique_text_list(change_types) or [change_type],
                "changed_nodes": changed_nodes,
                "operations": operations,
                "node_count": len(snapshot),
                "snapshot": sanitize_value(snapshot),
                "context_workbench_history_snapshot": sanitize_value(context_workbench_history_snapshot),
                "is_active": bool(item.get("is_active")),
            }
        )

    if normalized and not any(bool(revision.get("is_active")) for revision in normalized):
        normalized[-1]["is_active"] = True

    for revision_number, revision in enumerate(normalized, start=1):
        revision["revision_number"] = coerce_context_revision_number(
            revision.get("revision_number"),
            revision_number,
        )

    return normalized

def context_revision_summaries(revisions: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "id": sanitize_text(revision.get("id") or "").strip(),
            "label": sanitize_text(revision.get("label") or "").strip() or "Revision",
            "summary": (
                lambda label, summary, operations: (
                    fallback_context_revision_summary(label, operations)
                    if not summary or summary == label
                    else summary
                )
            )(
                sanitize_text(revision.get("label") or "").strip() or "Revision",
                sanitize_text(revision.get("summary") or "").strip(),
                sanitize_value(revision.get("operations")) if isinstance(revision.get("operations"), list) else [],
            ),
            "created_at": sanitize_text(revision.get("created_at") or "").strip() or utc_timestamp(),
            "revision_number": coerce_context_revision_number(revision.get("revision_number"), 0),
            "change_type": normalize_change_type(revision.get("change_type") or "update"),
            "change_types": unique_text_list(revision.get("change_types")) or [
                normalize_change_type(revision.get("change_type") or "update")
            ],
            "changed_nodes": unique_int_list(revision.get("changed_nodes")),
            "is_active": bool(revision.get("is_active")),
            "operation_count": len(revision.get("operations") or []),
            "node_count": int(revision.get("node_count") or 0),
        }
        for revision in reversed(revisions)
        if sanitize_text(revision.get("id") or "").strip()
    ]

def context_pending_restore_payload(raw_restore: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(raw_restore, dict):
        return None

    target_revision_id = sanitize_text(raw_restore.get("target_revision_id") or "").strip()
    if not target_revision_id:
        return None

    return {
        "target_revision_id": target_revision_id,
        "target_label": sanitize_text(raw_restore.get("target_label") or "").strip() or "Revision",
        "created_at": sanitize_text(raw_restore.get("created_at") or "").strip() or utc_timestamp(),
        "undo_active_revision_id": sanitize_text(raw_restore.get("undo_active_revision_id") or "").strip(),
        "can_undo": True,
    }

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
    revision_number: int,
    node_count: int,
) -> None:
    safe_session_id = sanitize_text(session_id).strip()
    if not safe_session_id:
        return
    markers = load_context_edit_markers()
    markers[safe_session_id] = {
        "session_id": safe_session_id,
        "summary": sanitize_text(summary).strip() or "Context has been edited.",
        "revision_number": revision_number,
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
    return sanitize_value(marker) if isinstance(marker, dict) else None

def context_record_preview(record: dict[str, object], *, limit: int = 140) -> str:
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

    if attachments:
        attachment_names = ", ".join(
            sanitize_text(item.get("name") or "").strip()
            for item in attachments
            if sanitize_text(item.get("name") or "").strip()
        )
        if attachment_names:
            return f"Attachments: {attachment_names}"

    return "[empty]"

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
    if role == "assistant":
        preview = collapsed_context_map_preview(preview) or "[empty]"
    tool_usage = record_tool_usage(record)
    provider_items = normalize_provider_items(record.get("providerItems"))
    token_estimate = estimate_token_count(record_context_weight_source(record))
    tool_token_estimate = estimate_token_count(record_context_tool_weight_source(record))
    return {
        "node_number": node_number,
        "role": role,
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

def context_workbench_suggestions_payload(session: SessionState) -> dict[str, object]:
    nodes: list[dict[str, object]] = []
    transcript = normalize_transcript(session.transcript)
    internal_indexes = internal_context_prefix_indexes(transcript)
    display_number_by_raw_index = {
        int(entry["raw_index"]): int(entry["node_number"])
        for entry in editable_context_node_entries(transcript)
    }
    stats_total_token_count = 0
    stats_tool_token_count = 0

    for index, record in enumerate(transcript):
        node_number = display_number_by_raw_index.get(index, index + 1)
        overview = context_record_overview(record, node_number=node_number)
        token_count = int(overview.get("token_estimate") or 0)
        tool_token_count = int(overview.get("tool_token_estimate") or 0)
        stats_total_token_count += token_count
        stats_tool_token_count += tool_token_count
        if index in internal_indexes:
            continue
        nodes.append(
            {
                "node_index": index,
                "node_number": node_number,
                "role": sanitize_text(overview.get("role") or "").strip() or "assistant",
                "token_count": token_count,
                "tool_token_count": tool_token_count,
                "preview": sanitize_text(overview.get("preview") or "").strip(),
            }
        )

    nodes.sort(
        key=lambda item: (
            -int(item.get("token_count") or 0),
            int(item.get("node_number") or 0),
        )
    )

    return {
        "stats": {
            "total_token_count": stats_total_token_count,
            "tool_token_count": stats_tool_token_count,
        },
        "nodes": sanitize_value(nodes),
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

def replace_provider_message_text(content: Any, replacement_text: str) -> str | list[dict[str, Any]]:
    safe_text = sanitize_text(replacement_text)
    if isinstance(content, list):
        rewritten: list[dict[str, Any]] = []
        text_item_type = "input_text"
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = sanitize_text(item.get("type") or "").strip()
            if item_type in {"input_text", "output_text", "text"} or "text" in item:
                if item_type == "output_text":
                    text_item_type = "output_text"
                continue
            rewritten.append(sanitize_value(item))

        if safe_text:
            rewritten.insert(
                0,
                {
                    "type": text_item_type,
                    "text": safe_text,
                },
            )
        return rewritten

    return safe_text

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
    if item_type == "local_shell_call":
        return "local_shell"
    if item_type == "tool_search_call":
        return "tool_search"
    if item_type == "web_search_call":
        return "web_search"
    if item_type == "image_generation_call":
        return "image_generation"
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

def provider_item_detail(item: dict[str, Any], item_number: int) -> dict[str, object]:
    item_type = provider_item_type(item) or "unknown"
    detail: dict[str, object] = {
        "item_number": item_number,
        "item_label": f"item #{item_number}",
        "item_type": item_type,
        "type": item_type,
        "provider_item_ref": f"provider_items[{item_number - 1}]",
        "delete_supported": True,
        "replace_supported": item_type in CONTEXT_EDITABLE_PROVIDER_ITEM_TYPES,
        "compress_supported": item_type in {"message", "function_call", "custom_tool_call", *CODEX_TOOL_OUTPUT_ITEM_TYPES},
    }

    if item_type == "message":
        content = item.get("content")
        detail["role"] = sanitize_text(item.get("role") or "").strip() or "assistant"
        text = extract_text_from_provider_message_content(content)
        detail["text_preview"] = block_text_preview(text, limit=220)
        detail["editable_text_ref"] = f"provider_items[{item_number - 1}].content"
        preview_source = (
            json.dumps(sanitize_value(content), ensure_ascii=False)
            if isinstance(content, list)
            else sanitize_text(content or "")
        )
        detail["preview"] = block_text_preview(preview_source, limit=180)
        return detail

    if item_type in CODEX_TOOL_CALL_ITEM_TYPES:
        detail["name"] = tool_display_title_from_provider_item(item)
        detail["call_id"] = provider_item_call_id(item)
        arguments = provider_payload_text(tool_call_arguments_value(item))
        detail["arguments_preview"] = block_text_preview(arguments, limit=220)
        detail["editable_text_ref"] = (
            f"provider_items[{item_number - 1}].input"
            if item_type == "custom_tool_call"
            else f"provider_items[{item_number - 1}].arguments"
        )
        detail["preview"] = block_text_preview(arguments, limit=180)
        return detail

    if item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES:
        detail["name"] = sanitize_text(item.get("name") or "").strip()
        detail["call_id"] = provider_item_call_id(item)
        output = tool_output_text_from_provider_item(item)
        detail["output_preview"] = block_text_preview(output, limit=220)
        detail["editable_text_ref"] = (
            f"provider_items[{item_number - 1}].tools"
            if item_type == "tool_search_output"
            else f"provider_items[{item_number - 1}].output"
        )
        detail["preview"] = block_text_preview(output, limit=180)
        return detail

    if item_type in {"compaction", "compaction_summary"}:
        encoded_content = sanitize_text(item.get("encrypted_content") or "")
        detail["encoded_content_preview"] = block_text_preview(encoded_content, limit=220)
        detail["preview"] = block_text_preview(encoded_content, limit=180)
        return detail

    return detail

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

        if item_type in {"compaction", "compaction_summary"}:
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

        if item_type in {"compaction", "compaction_summary"}:
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
    transcript = normalize_transcript(session.transcript)
    safe_selected_indexes = normalize_selected_node_indexes(selected_indexes or [], len(transcript))
    selected_numbers = selected_display_node_numbers(transcript, safe_selected_indexes)
    editable_entries = editable_context_node_entries(transcript)
    lines = [
        "# 当前主 Codex 上下文快照",
        f"- 会话标题：{session.title}",
        f"- 会话类型：{session.scope}",
        f"- 当前节点数：{len(editable_entries)}",
        f"- 当前选中节点：{format_node_ranges(selected_numbers) or '未单独选中，默认面向全局'}",
        "- 这一轮里所有 Node # 都以这份快照为准。",
        "- 系统/开发者指令和默认环境说明属于内部前缀，不在本快照中展示，也不能被选择或编辑。",
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
    return any(
        sanitize_text(record.get("role") or "").strip() in {"system", "developer"}
        for record in records
    )

def transcript_has_conversation_records(records: list[dict[str, Any]]) -> bool:
    return any(is_conversation_record(record) for record in records)

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

def proxy_state_sqlite_file() -> Path:
    return PROXY_STATE_FILE.with_suffix(".sqlite3")

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

def latest_proxy_sessions_from_sqlite() -> tuple[str, list[dict[str, Any]]] | None:
    sqlite_file = proxy_state_sqlite_file()
    if not sqlite_file.exists():
        return None
    try:
        with closing(sqlite3.connect(sqlite_file)) as conn:
            active_row = conn.execute("SELECT value FROM meta WHERE key = 'active_session_id'").fetchone()
            active_session_id = sanitize_text(active_row[0] if active_row else "").strip()
            rows = conn.execute(
                """
                SELECT id, updated_at, session_dir
                FROM sessions
                ORDER BY updated_at DESC
                """
            ).fetchall()
    except sqlite3.Error:
        return None

    sessions: list[dict[str, Any]] = []
    sqlite_parent = sqlite_file.parent
    for session_id, updated_at, session_dir in rows:
        safe_session_id = sanitize_text(session_id or "").strip()
        if not safe_session_id:
            continue
        request_log: list[dict[str, Any]] = []
        safe_session_dir = sanitize_text(session_dir or "").strip()
        if safe_session_dir:
            raw_log = read_jsonl_state_file(sqlite_parent / safe_session_dir / "request_log.jsonl", [])
            if isinstance(raw_log, list):
                request_log = [item for item in raw_log if isinstance(item, dict)]
        sessions.append(
            {
                "id": safe_session_id,
                "updated_at": sanitize_text(updated_at or ""),
                "request_log": request_log,
            }
        )
    return active_session_id, sessions

def latest_proxy_instruction_prefix_records() -> list[dict[str, Any]]:
    sqlite_payload = latest_proxy_sessions_from_sqlite()
    if sqlite_payload is None:
        return []
    active_session_id, sessions = sqlite_payload

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

def paired_tool_item_indexes(provider_items: list[dict[str, Any]], item_index: int) -> list[int]:
    if item_index < 0 or item_index >= len(provider_items):
        return []
    item = provider_items[item_index]
    item_type = provider_item_type(item)
    call_id = provider_item_call_id(item)
    if not call_id:
        return [item_index]

    if item_type in CODEX_PAIRED_TOOL_CALL_ITEM_TYPES:
        paired = [item_index]
        allowed_output_types = CODEX_TOOL_OUTPUT_TYPES_BY_CALL_TYPE.get(item_type, set())
        for index, candidate in enumerate(provider_items):
            if index == item_index or provider_item_call_id(candidate) != call_id:
                continue
            if provider_item_type(candidate) in allowed_output_types:
                paired.append(index)
        return sorted(set(paired))

    if item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES:
        paired = [item_index]
        allowed_call_types = CODEX_TOOL_CALL_TYPES_BY_OUTPUT_TYPE.get(item_type, set())
        for index, candidate in enumerate(provider_items):
            if index == item_index or provider_item_call_id(candidate) != call_id:
                continue
            if provider_item_type(candidate) in allowed_call_types:
                paired.append(index)
        return sorted(set(paired))

    return [item_index]

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

def validate_context_replacement_identity(original_item: dict[str, Any], replacement_item: dict[str, Any]) -> None:
    original_type = provider_item_type(original_item)
    replacement_type = provider_item_type(replacement_item)
    if not replacement_type:
        raise ValueError("replacement_item.type is required")
    if original_type and replacement_type != original_type:
        raise ValueError(
            f"replacement_item must keep item type {original_type!r}; use node compression/deletion for structural rewrites"
        )

    if original_type == "message":
        original_role = sanitize_text(original_item.get("role") or "").strip()
        replacement_role = sanitize_text(replacement_item.get("role") or "").strip()
        if original_role and replacement_role != original_role:
            raise ValueError(f"replacement message must keep role {original_role!r}")

    if original_type in CODEX_TOOL_CALL_ITEM_TYPES or original_type in CODEX_TOOL_OUTPUT_ITEM_TYPES:
        original_call_id = provider_item_call_id(original_item)
        replacement_call_id = provider_item_call_id(replacement_item)
        if original_call_id and replacement_call_id != original_call_id:
            raise ValueError(f"replacement tool item must keep call_id {original_call_id!r}")

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
    def __init__(self, transcript: list[dict[str, object]], selected_indexes: list[int]) -> None:
        normalized_transcript = normalize_transcript(transcript)
        safe_selected = normalize_selected_node_indexes(selected_indexes, len(normalized_transcript))
        self.selected_node_numbers = selected_display_node_numbers(normalized_transcript, safe_selected)
        internal_indexes = internal_context_prefix_indexes(normalized_transcript)
        editable_numbers_by_raw_index = {
            int(entry["raw_index"]): int(entry["node_number"])
            for entry in editable_context_node_entries(normalized_transcript)
        }
        self.nodes: list[ContextWorkbenchDraftNode] = []
        for raw_index, record in enumerate(normalized_transcript):
            node_number = editable_numbers_by_raw_index.get(raw_index)
            is_internal = raw_index in internal_indexes
            label = f"Node #{node_number}" if node_number is not None else "Internal Prefix"
            self.nodes.append(
                ContextWorkbenchDraftNode(
                    order=float(raw_index + 1),
                    label=label,
                    record=sanitize_value(record),
                    active=True,
                    source_node_number=node_number,
                    source_index=raw_index,
                    kind="internal" if is_internal else "existing",
                    status="locked" if is_internal else "active",
                    editable=not is_internal,
                )
            )
        self.operations: list[dict[str, object]] = []
        self._draft_counter = 0
        self._revision_summary = ""
        self._working_version = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.operations)

    def _record_operation(self, operation: dict[str, object]) -> None:
        self._working_version += 1
        operation["working_version"] = self._working_version
        self.operations.append(operation)
        self._revision_summary = ""

    def set_revision_summary(self, summary: str) -> dict[str, object]:
        if not self.operations:
            raise ValueError("no working snapshot edits exist yet")

        safe_summary = re.sub(r"\s+", " ", sanitize_text(summary)).strip()
        if not safe_summary:
            raise ValueError("summary is required")
        if len(safe_summary) > 220:
            safe_summary = f"{safe_summary[:219].rstrip()}…"

        self._revision_summary = safe_summary
        return {
            "payload_kind": "revision_summary",
            "saved": True,
            "summary": safe_summary,
            "change_count": len(self.operations),
            "working_version": self._working_version,
        }

    def _fallback_revision_summary(self) -> str:
        if not self.operations:
            return "这次更新了当前上下文。"
        return fallback_context_revision_summary("Context update", self.operations)

    def revision_summary(self) -> str:
        return self._revision_summary or self._fallback_revision_summary()

    def active_nodes(self) -> list[ContextWorkbenchDraftNode]:
        return [
            node
            for node in sorted(self.nodes, key=lambda item: item.order)
            if node.active and node.editable
        ]

    def committed_nodes(self) -> list[ContextWorkbenchDraftNode]:
        return [node for node in sorted(self.nodes, key=lambda item: item.order) if node.active]

    def max_node_number(self) -> int:
        return max((node.source_node_number or 0) for node in self.nodes) if self.nodes else 0

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

    def _node_search_text(self, node: ContextWorkbenchDraftNode) -> str:
        overview = context_record_overview(
            node.record,
            node_number=node.source_node_number or 1,
            selected=(node.source_node_number or 0) in self.selected_node_numbers,
        )
        parts = [
            node.label,
            sanitize_text(overview.get("role") or ""),
            sanitize_text(overview.get("preview") or ""),
            sanitize_text(overview.get("full_text") or ""),
            format_tool_usage(sanitize_value(overview.get("tool_usage"))),
            record_context_weight_source(node.record),
        ]
        return "\n".join(part for part in parts if sanitize_text(part).strip())

    def _candidate_score(self, node: ContextWorkbenchDraftNode, target_hint: str) -> int:
        safe_hint = sanitize_text(target_hint).strip()
        overview = self._overview_for_node(node)
        if not safe_hint:
            return int(overview.get("token_estimate") or 0) + int(overview.get("tool_count") or 0) * 120

        hint_text = safe_hint.lower()
        haystack = self._node_search_text(node).lower()
        score = 0

        if sanitize_text(node.label).strip().lower() in hint_text:
            score += 400

        for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", hint_text):
            if len(token) <= 1:
                continue
            if token in haystack:
                score += 120

        if any(keyword in hint_text for keyword in ["latest", "recent", "last", "最近", "最后"]):
            score += int(node.source_node_number or 0) * 10

        if any(keyword in hint_text for keyword in ["tool", "tools", "工具", "调用"]):
            score += int(overview.get("tool_count") or 0) * 160

        if any(keyword in hint_text for keyword in ["long", "heavy", "verbose", "冗长", "很重", "最长"]):
            score += int(overview.get("token_estimate") or 0)

        if any(keyword in hint_text for keyword in ["user", "用户"]):
            score += 160 if sanitize_text(overview.get("role") or "") == "user" else 0

        if any(keyword in hint_text for keyword in ["assistant", "助手"]):
            score += 160 if sanitize_text(overview.get("role") or "") == "assistant" else 0

        return score

    def suggest_target_nodes(self, target_hint: str = "", *, limit: int = 4) -> list[dict[str, object]]:
        candidates = [
            (self._candidate_score(node, target_hint), node)
            for node in self.active_nodes()
        ]
        candidates.sort(
            key=lambda item: (
                -item[0],
                -int(self._overview_for_node(item[1]).get("token_estimate") or 0),
                -(item[1].source_node_number or 0),
            )
        )
        ranked_nodes = [node for score, node in candidates if score > 0][: max(1, limit)]
        if not ranked_nodes and not target_hint:
            ranked_nodes = self.active_nodes()[: max(1, limit)]
        return self.overview_items(ranked_nodes)

    def _resolve_target_nodes_from_hint(
        self,
        target_hint: str,
        *,
        include_inactive: bool = False,
    ) -> list[ContextWorkbenchDraftNode]:
        searchable_nodes = self.nodes if include_inactive else self.active_nodes()
        ranked = [
            (self._candidate_score(node, target_hint), node)
            for node in searchable_nodes
        ]
        ranked = [item for item in ranked if item[0] > 0]
        ranked.sort(
            key=lambda item: (
                -item[0],
                -int(self._overview_for_node(item[1]).get("token_estimate") or 0),
                -(item[1].source_node_number or 0),
            )
        )
        if not ranked:
            return []

        best_score = ranked[0][0]
        second_score = ranked[1][0] if len(ranked) > 1 else -1
        if len(ranked) == 1 or best_score >= second_score + 120:
            return [ranked[0][1]]
        return []

    def resolve_target_nodes(
        self,
        arguments: dict[str, Any],
        *,
        allow_selected: bool = True,
        allow_all_active: bool = False,
        include_inactive: bool = False,
    ) -> list[ContextWorkbenchDraftNode]:
        explicit_numbers = normalize_node_numbers(arguments.get("node_numbers"), self.max_node_number())
        if explicit_numbers:
            return self._nodes_by_number(explicit_numbers, include_inactive=include_inactive)

        legacy_indexes = normalize_selected_node_indexes(arguments.get("node_indexes"), self.max_node_number())
        if legacy_indexes:
            return self._nodes_by_number([index + 1 for index in legacy_indexes], include_inactive=include_inactive)

        del allow_selected

        if allow_all_active:
            return self.active_nodes()

        return []

    def _overview_for_node(self, node: ContextWorkbenchDraftNode) -> dict[str, object]:
        display_number = node.source_node_number or 1
        overview = context_record_overview(
            node.record,
            node_number=display_number,
            selected=(node.source_node_number or 0) in self.selected_node_numbers,
        )
        overview["payload_kind"] = "node_overview"
        overview["node_number"] = node.source_node_number
        overview["label"] = node.label
        overview["status"] = node.status
        overview["node_kind"] = node.kind
        overview["active"] = node.active
        return overview

    def current_overview_items(self) -> list[dict[str, object]]:
        return [self._overview_for_node(node) for node in self.active_nodes()]

    def compact_overview_for_node(self, node: ContextWorkbenchDraftNode) -> dict[str, object]:
        overview = self._overview_for_node(node)
        overview.pop("full_text", None)
        return overview

    def compact_overview_items(self, nodes: list[ContextWorkbenchDraftNode]) -> list[dict[str, object]]:
        return [self.compact_overview_for_node(node) for node in nodes]

    def final_snapshot_payload(self) -> dict[str, object]:
        active_nodes = self.active_nodes()
        inactive_nodes = [node for node in sorted(self.nodes, key=lambda item: item.order) if not node.active]
        compressed_replacements: dict[int, str] = {}
        for operation in self.operations:
            if sanitize_text(operation.get("operation_type") or "").strip() != "compress_nodes":
                continue
            created_label = sanitize_text(operation.get("created_label") or "").strip()
            if not created_label:
                continue
            for node_number in unique_int_list(operation.get("compressed_node_numbers") or operation.get("target_node_numbers")):
                compressed_replacements[node_number] = created_label

        active_overviews = self.compact_overview_items(active_nodes)
        inactive_overviews: list[dict[str, object]] = []
        for node in inactive_nodes:
            item: dict[str, object] = {
                "node_number": node.source_node_number,
                "label": node.label,
                "status": node.status,
                "node_kind": node.kind,
                "active": node.active,
            }
            if node.status == "compressed" and node.source_node_number in compressed_replacements:
                item["replaced_by"] = compressed_replacements[node.source_node_number]
            inactive_overviews.append(item)

        return {
            "payload_kind": "final_working_snapshot",
            "working_version": self._working_version,
            "active_node_count": len(active_nodes),
            "inactive_node_count": len(inactive_nodes),
            "total_token_estimate": sum(int(item.get("token_estimate") or 0) for item in active_overviews),
            "tool_token_estimate": sum(int(item.get("tool_token_estimate") or 0) for item in active_overviews),
            "selected_node_numbers": list(self.selected_node_numbers),
            "active_nodes": active_overviews,
            "inactive_nodes": inactive_overviews,
            "operations": sanitize_value(self.operations),
        }

    def overview_items(self, nodes: list[ContextWorkbenchDraftNode]) -> list[dict[str, object]]:
        return [self._overview_for_node(node) for node in nodes]

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

    def mutation_node_details(self, nodes: list[ContextWorkbenchDraftNode]) -> list[dict[str, object]]:
        details: list[dict[str, object]] = []
        for node in nodes:
            provider_items = self._provider_items_for_node(node)
            overview = self._overview_for_node(node)
            details.append(
                {
                    "payload_kind": "node_mutation_detail",
                    "node_number": node.source_node_number,
                    "label": node.label,
                    "status": node.status,
                    "active": node.active,
                    "node_kind": node.kind,
                    "overview": overview,
                    "item_count": len(provider_items),
                    "full_detail_note": (
                        "Mutation results intentionally omit full provider_items and per-item detail to avoid repeating large node content. "
                        "For simple delete/replace/compress steps, do not re-open node details just to verify; use the mutation delta. "
                        "Only call get_context_node_details again when the next edit requires exact updated provider_items from the current working snapshot."
                    ),
                }
            )
        return details

    def _next_draft_label(self) -> str:
        self._draft_counter += 1
        return f"Draft Node {letter_index(self._draft_counter)}"

    def _set_node_record(self, node: ContextWorkbenchDraftNode, record: dict[str, object], *, status: str = "updated") -> None:
        normalized_record = normalize_transcript([record])
        if not normalized_record:
            raise ValueError("record could not be normalized after mutation")
        node.record = normalized_record[0]
        if node.kind == "existing":
            node.status = status

    def _provider_items_for_node(self, node: ContextWorkbenchDraftNode) -> list[dict[str, Any]]:
        return normalize_provider_items(node.record.get("providerItems"))

    def _resolve_item_detail(self, node: ContextWorkbenchDraftNode, item_number: int) -> dict[str, object]:
        provider_items = self._provider_items_for_node(node)
        if item_number < 1 or item_number > len(provider_items):
            raise ValueError(f"item #{item_number} does not exist in {node.label}")
        item = provider_items[item_number - 1]
        return provider_item_detail(item, item_number)

    def _item_ref(self, node: ContextWorkbenchDraftNode, item_number: int) -> str:
        return context_node_item_ref(int(node.source_node_number or 0), item_number)

    def _parse_item_ref(self, raw_ref: Any) -> tuple[int, int] | None:
        safe_ref = sanitize_text(raw_ref or "").strip().lower()
        if not safe_ref:
            return None

        match = re.search(r"node\D*(\d+)\D+item\D*(\d+)", safe_ref)
        if match is None:
            match = re.fullmatch(r"(\d+)\s*[:/]\s*(\d+)", safe_ref)
        if match is None:
            return None

        try:
            node_number = int(match.group(1))
            item_number = int(match.group(2))
        except (TypeError, ValueError):
            return None
        if node_number <= 0 or item_number <= 0:
            return None
        return node_number, item_number

    def _item_text_source(self, item: dict[str, Any]) -> str:
        item_type = provider_item_type(item)
        if item_type == "message":
            return extract_text_from_provider_message_content(item.get("content"))
        if item_type == "reasoning":
            return provider_payload_text(item.get("summary") or item.get("content") or item.get("text"))
        if item_type in {"compaction", "compaction_summary"}:
            return visible_text_from_compaction_provider_item(item)
        if item_type in CODEX_TOOL_CALL_ITEM_TYPES:
            return provider_payload_text(tool_call_arguments_value(item))
        if item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES or item_type == "image_generation_call":
            return tool_output_text_from_provider_item(item)
        return provider_payload_text(item)

    def _can_replace_item_content(self, item: dict[str, Any]) -> bool:
        return provider_item_type(item) in {
            "message",
            "function_call",
            "custom_tool_call",
            "function_call_output",
            "custom_tool_call_output",
            "local_shell_call_output",
            "mcp_tool_call_output",
            "tool_search_output",
        }

    def _replace_item_content(self, item: dict[str, Any], replacement_text: str) -> dict[str, Any]:
        item_type = provider_item_type(item)
        replacement_item = sanitize_value(item)
        safe_content = sanitize_text(replacement_text)

        if item_type == "message":
            replacement_item["content"] = replace_provider_message_text(item.get("content"), safe_content)
            return replacement_item
        if item_type == "function_call":
            replacement_item["arguments"] = safe_content
            return replacement_item
        if item_type == "custom_tool_call":
            replacement_item["input"] = safe_content
            return replacement_item
        if item_type in {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}:
            replacement_item["output"] = safe_content
            return replacement_item
        if item_type == "mcp_tool_call_output":
            replacement_item["output"] = {
                "content": [{"type": "text", "text": safe_content}],
                "structured_content": None,
                "is_error": False,
                "meta": None,
            }
            return replacement_item
        if item_type == "tool_search_output":
            replacement_item["tools"] = [{"summary": safe_content}]
            return replacement_item

        raise ValueError(f"{item_type or 'unknown'} items do not support batch content replacement")

    def _light_item_entry(
        self,
        node: ContextWorkbenchDraftNode,
        provider_items: list[dict[str, Any]],
        item_index: int,
    ) -> dict[str, object]:
        item = provider_items[item_index]
        item_number = item_index + 1
        item_type = provider_item_type(item) or "unknown"
        text_source = self._item_text_source(item)
        paired_indexes = paired_tool_item_indexes(provider_items, item_index)
        detail = provider_item_detail(item, item_number)
        entry: dict[str, object] = {
            "node_number": node.source_node_number,
            "node_label": node.label,
            "item_number": item_number,
            "item_ref": self._item_ref(node, item_number),
            "item_type": item_type,
            "type": item_type,
            "role": sanitize_text(item.get("role") or "").strip(),
            "name": tool_display_title_from_provider_item(item)
            if item_type in CODEX_TOOL_CALL_ITEM_TYPES or item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES
            else "",
            "call_id": provider_item_call_id(item),
            "token_estimate": estimate_token_count(text_source),
            "text_chars": len(text_source),
            "preview": block_text_preview(text_source, limit=160),
            "display_detail": tool_display_detail_from_provider_item(item)
            if item_type in CODEX_TOOL_CALL_ITEM_TYPES
            else "",
            "paired_item_numbers": [index + 1 for index in paired_indexes],
            "is_tool_call": item_type in CODEX_TOOL_CALL_ITEM_TYPES,
            "is_tool_output": item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES,
            "delete_supported": True,
            "replace_content_supported": self._can_replace_item_content(item),
        }
        for key in ("text_preview", "arguments_preview", "output_preview", "encoded_content_preview"):
            if key in detail:
                entry[key] = detail[key]
        return entry

    def _selector_item_refs(self, selector: dict[str, Any]) -> set[tuple[int, int]]:
        raw_refs = selector.get("item_refs")
        if not isinstance(raw_refs, list):
            return set()
        refs: set[tuple[int, int]] = set()
        for raw_ref in raw_refs:
            parsed_ref = self._parse_item_ref(raw_ref)
            if parsed_ref is not None:
                refs.add(parsed_ref)
        return refs

    def _selector_nodes(self, selector: dict[str, Any], item_refs: set[tuple[int, int]]) -> list[ContextWorkbenchDraftNode]:
        explicit_numbers = normalize_node_numbers(selector.get("node_numbers"), self.max_node_number())
        if explicit_numbers:
            return self._nodes_by_number(explicit_numbers)

        if item_refs:
            return self._nodes_by_number(sorted({node_number for node_number, _item_number in item_refs}))

        target_hint = sanitize_text(selector.get("target_hint") or "").strip()
        if target_hint:
            return self.resolve_target_nodes(
                {"target_hint": target_hint},
                allow_selected=False,
                allow_all_active=False,
            )

        if bool(selector.get("selected_only")) and self.selected_node_numbers:
            return self._nodes_by_number(self.selected_node_numbers)

        return self.active_nodes()

    def _selector_item_numbers(self, selector: dict[str, Any]) -> set[int]:
        raw_numbers = selector.get("item_numbers")
        if not isinstance(raw_numbers, list):
            return set()

        numbers: set[int] = set()
        for raw_number in raw_numbers:
            try:
                item_number = int(raw_number)
            except (TypeError, ValueError):
                continue
            if item_number > 0:
                numbers.add(item_number)
        return numbers

    def _selector_text_list(self, raw_value: Any) -> set[str]:
        if not isinstance(raw_value, list):
            return set()
        return {
            sanitize_text(item).strip()
            for item in raw_value
            if sanitize_text(item).strip()
        }

    def _match_context_items(
        self,
        selector: dict[str, Any],
        *,
        limit: int | None = None,
    ) -> list[tuple[ContextWorkbenchDraftNode, int, dict[str, object]]]:
        safe_selector = selector if isinstance(selector, dict) else {}
        item_refs = self._selector_item_refs(safe_selector)
        nodes = self._selector_nodes(safe_selector, item_refs)
        item_numbers = self._selector_item_numbers(safe_selector)
        item_types = self._selector_text_list(safe_selector.get("item_types"))
        roles = self._selector_text_list(safe_selector.get("roles"))
        text_contains = sanitize_text(safe_selector.get("text_contains") or "").strip().lower()
        try:
            min_token_estimate = int(safe_selector.get("min_token_estimate") or 0)
        except (TypeError, ValueError):
            min_token_estimate = 0

        tool_type_filters: set[str] = set()
        if bool(safe_selector.get("tool_output_only")):
            tool_type_filters.update(CODEX_TOOL_OUTPUT_ITEM_TYPES)
        if bool(safe_selector.get("tool_call_only")):
            tool_type_filters.update(CODEX_TOOL_CALL_ITEM_TYPES)

        matches: list[tuple[ContextWorkbenchDraftNode, int, dict[str, object]]] = []
        for node in nodes:
            provider_items = self._provider_items_for_node(node)
            for item_index, item in enumerate(provider_items):
                item_number = item_index + 1
                if item_refs and (int(node.source_node_number or 0), item_number) not in item_refs:
                    continue
                if item_numbers and item_number not in item_numbers:
                    continue

                item_type = provider_item_type(item) or "unknown"
                if item_types and item_type not in item_types:
                    continue
                if tool_type_filters and item_type not in tool_type_filters:
                    continue

                role = sanitize_text(item.get("role") or "").strip()
                if roles and role not in roles:
                    continue

                text_source = self._item_text_source(item)
                if text_contains and text_contains not in text_source.lower():
                    continue

                entry = self._light_item_entry(node, provider_items, item_index)
                if min_token_estimate > 0 and int(entry.get("token_estimate") or 0) < min_token_estimate:
                    continue

                matches.append((node, item_index, entry))
                if limit is not None and len(matches) >= limit:
                    return matches

        return matches

    def find_context_items(self, selector: dict[str, Any]) -> dict[str, object]:
        safe_selector = selector if isinstance(selector, dict) else {}
        try:
            max_results = int(safe_selector.get("max_results") or 120)
        except (TypeError, ValueError):
            max_results = 120
        max_results = max(1, min(max_results, 500))

        all_matches = self._match_context_items(safe_selector)
        visible_matches = all_matches[:max_results]
        items = [entry for _node, _index, entry in visible_matches]
        total_tokens = sum(int(entry.get("token_estimate") or 0) for _node, _index, entry in all_matches)
        return {
            "payload_kind": "context_item_list",
            "matched_count": len(all_matches),
            "returned_count": len(items),
            "truncated": len(all_matches) > len(items),
            "total_token_estimate": total_tokens,
            "items": items,
            "selector": sanitize_value(safe_selector),
            "note": (
                "This is a lightweight item inventory. It intentionally contains previews and metadata only, not full item content."
            ),
        }

    def _compact_batch_mutation_result(
        self,
        *,
        summary: str,
        change_type: str,
        matched_count: int,
        changed_items: list[dict[str, object]],
        changed_nodes: list[int],
        dry_run: bool,
        selector: dict[str, Any],
        operation: dict[str, Any],
        before_tokens: int,
        after_tokens: int,
    ) -> dict[str, object]:
        visible_changed_items = changed_items[:80]
        return {
            "payload_kind": "batch_mutation_result",
            "summary": summary,
            "change_type": normalize_change_type(change_type),
            "dry_run": dry_run,
            "matched_count": matched_count,
            "changed_count": len(changed_items),
            "changed_nodes": unique_int_list(changed_nodes),
            "changed_items": visible_changed_items,
            "omitted_changed_items": max(0, len(changed_items) - len(visible_changed_items)),
            "token_delta_estimate": {
                "before": max(0, before_tokens),
                "after": max(0, after_tokens),
                "saved": before_tokens - after_tokens,
            },
            "working_overview": self.current_overview_items(),
            "selector": sanitize_value(selector),
            "operation": sanitize_value(operation),
            "note": (
                "Mutation results are compact by design. They omit full provider_items and old full content; call get_context_node_details only if the next edit needs exact current items."
            ),
        }

    def edit_context_items(
        self,
        *,
        selector: dict[str, Any],
        operation: dict[str, Any],
        reason: str,
        dry_run: bool = False,
    ) -> dict[str, object]:
        safe_selector = selector if isinstance(selector, dict) else {}
        safe_operation = operation if isinstance(operation, dict) else {}
        operation_type = sanitize_text(safe_operation.get("type") or "").strip()
        if operation_type not in {"replace_content", "compress_content", "delete"}:
            raise ValueError("operation.type must be replace_content, compress_content, or delete")

        matches = self._match_context_items(safe_selector)
        if not matches:
            return self._compact_batch_mutation_result(
                summary="No matching context items found.",
                change_type=operation_type,
                matched_count=0,
                changed_items=[],
                changed_nodes=[],
                dry_run=dry_run,
                selector=safe_selector,
                operation=safe_operation,
                before_tokens=0,
                after_tokens=0,
            )

        working_by_node: dict[int, tuple[ContextWorkbenchDraftNode, list[dict[str, Any]]]] = {}
        before_tokens = 0
        after_tokens = 0
        changed_items: list[dict[str, object]] = []

        if operation_type == "delete":
            remove_indexes_by_node: dict[int, set[int]] = {}
            for node, item_index, entry in matches:
                node_key = id(node)
                provider_items = self._provider_items_for_node(node)
                remove_indexes_by_node.setdefault(node_key, set()).update(
                    paired_tool_item_indexes(provider_items, item_index)
                )
                working_by_node.setdefault(node_key, (node, sanitize_value(provider_items)))

            for node_key, remove_indexes in remove_indexes_by_node.items():
                node, provider_items = working_by_node[node_key]
                removed_indexes = sorted(index for index in remove_indexes if 0 <= index < len(provider_items))
                for remove_index in sorted(removed_indexes, reverse=True):
                    removed_entry = self._light_item_entry(node, provider_items, remove_index)
                    before_tokens += int(removed_entry.get("token_estimate") or 0)
                    changed_items.append(
                        {
                            "node_number": removed_entry.get("node_number"),
                            "item_number": removed_entry.get("item_number"),
                            "item_ref": removed_entry.get("item_ref"),
                            "item_type": removed_entry.get("item_type"),
                            "call_id": removed_entry.get("call_id"),
                            "change": "delete",
                            "before_preview": removed_entry.get("preview"),
                        }
                    )
                    del provider_items[remove_index]
                validate_context_provider_items(provider_items)
        else:
            if "content" not in safe_operation:
                raise ValueError("operation.content is required for replace_content and compress_content")
            replacement_content = sanitize_text(safe_operation.get("content") or "")
            for node, item_index, entry in matches:
                node_key = id(node)
                if node_key not in working_by_node:
                    working_by_node[node_key] = (node, self._provider_items_for_node(node))
                working_node, provider_items = working_by_node[node_key]
                original_item = provider_items[item_index]
                replacement_item = self._replace_item_content(original_item, replacement_content)
                validate_context_replacement_identity(original_item, replacement_item)
                before_tokens += int(entry.get("token_estimate") or 0)
                after_tokens += estimate_token_count(self._item_text_source(replacement_item))
                provider_items[item_index] = replacement_item
                changed_items.append(
                    {
                        "node_number": entry.get("node_number"),
                        "item_number": entry.get("item_number"),
                        "item_ref": entry.get("item_ref"),
                        "item_type": entry.get("item_type"),
                        "call_id": entry.get("call_id"),
                        "change": "compress" if operation_type == "compress_content" else "replace",
                        "before_preview": entry.get("preview"),
                        "after_preview": block_text_preview(replacement_content, limit=160),
                    }
                )
                working_by_node[node_key] = (working_node, provider_items)

            for _node_key, (_node, provider_items) in working_by_node.items():
                validate_context_provider_items(provider_items)

        changed_nodes = [
            node.source_node_number
            for node, _provider_items in working_by_node.values()
            if node.source_node_number is not None
        ]
        changed_node_numbers = unique_int_list(changed_nodes)
        change_type = "delete" if operation_type == "delete" else (
            "compress" if operation_type == "compress_content" else "replace"
        )
        summary_action = {
            "delete": "Delete",
            "replace": "Replace content in",
            "compress": "Compress content in",
        }.get(change_type, "Update")
        summary = f"{summary_action} {len(changed_items)} context item(s)"
        if changed_node_numbers:
            summary = f"{summary} across Node #{format_node_ranges(changed_node_numbers)}"

        if not dry_run:
            for _node_key, (node, provider_items) in working_by_node.items():
                self._set_node_record(node, compile_record_from_provider_items(node.record, provider_items))
            self._record_operation(
                {
                    "operation_type": "edit_context_items",
                    "change_type": change_type,
                    "label": summary,
                    "summary": summary,
                    "changed_nodes": changed_node_numbers,
                    "target_node_numbers": changed_node_numbers,
                    "target_items": [
                        {
                            "node_number": item.get("node_number"),
                            "item_number": item.get("item_number"),
                            "item_type": item.get("item_type"),
                            "call_id": item.get("call_id"),
                            "change": item.get("change"),
                        }
                        for item in changed_items
                    ],
                    "selector": sanitize_value(safe_selector),
                    "operation": sanitize_value(safe_operation),
                    "reason": sanitize_text(reason).strip(),
                }
            )

        return self._compact_batch_mutation_result(
            summary=summary if not dry_run else f"Dry run: {summary}",
            change_type=change_type,
            matched_count=len(matches),
            changed_items=changed_items,
            changed_nodes=changed_node_numbers,
            dry_run=dry_run,
            selector=safe_selector,
            operation=safe_operation,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
        )

    def _build_mutation_result(
        self,
        *,
        summary: str,
        change_type: str,
        changed_nodes: list[int],
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        changed_node_details = self.mutation_node_details(
            self._nodes_by_number(changed_nodes, include_inactive=True)
        )
        active_nodes = self.active_nodes()
        payload: dict[str, object] = {
            "payload_kind": "mutation_delta",
            "summary": summary,
            "change_type": normalize_change_type(change_type),
            "working_version": self._working_version,
            "changed_nodes": unique_int_list(changed_nodes),
            "active_node_count": len(active_nodes),
            "inactive_node_count": len([node for node in self.nodes if not node.active]),
            "changed_node_details": changed_node_details,
        }
        if extra:
            payload.update(sanitize_value(extra))
        return payload

    def delete_nodes(self, nodes: list[ContextWorkbenchDraftNode], *, reason: str) -> dict[str, object]:
        active_nodes = [node for node in nodes if node.active]
        if not active_nodes:
            raise ValueError("No active nodes were resolved for deletion.")

        deleted_numbers = [
            node.source_node_number
            for node in active_nodes
            if node.source_node_number is not None
        ]
        for node in active_nodes:
            node.active = False
            node.status = "deleted"

        summary = f"Delete nodes #{format_node_ranges(deleted_numbers)}"
        self._record_operation(
            {
                "operation_type": "delete_nodes",
                "change_type": "delete",
                "label": summary,
                "summary": summary,
                "changed_nodes": deleted_numbers,
                "target_node_numbers": deleted_numbers,
                "reason": sanitize_text(reason),
            }
        )
        return self._build_mutation_result(
            summary=summary,
            change_type="delete",
            changed_nodes=deleted_numbers,
            extra={
                "deleted_node_numbers": deleted_numbers,
            },
        )

    def compress_nodes(
        self,
        nodes: list[ContextWorkbenchDraftNode],
        *,
        summary_markdown: str,
        style: str,
        title: str,
    ) -> dict[str, object]:
        active_nodes = [node for node in nodes if node.active]
        if not active_nodes:
            raise ValueError("No active nodes were resolved for compression.")

        safe_summary = sanitize_text(summary_markdown).strip()
        if not safe_summary:
            raise ValueError("summary_markdown is required")

        target_numbers = [
            node.source_node_number
            for node in active_nodes
            if node.source_node_number is not None
        ]
        for node in active_nodes:
            node.active = False
            node.status = "compressed"

        label = self._next_draft_label()
        heading = sanitize_text(title).strip()
        summary_text = safe_summary if not heading else f"### {heading}\n\n{safe_summary}"
        created_node = ContextWorkbenchDraftNode(
            order=min(node.order for node in active_nodes) + 0.01,
            label=label,
            record={
                "role": "user",
                "text": summary_text,
                "attachments": [],
                "toolEvents": [],
                "blocks": [{"kind": "text", "text": summary_text}],
                "providerItems": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": summary_text,
                    }
                ],
            },
            active=True,
            source_node_number=None,
            kind="draft",
            status="created",
        )
        self.nodes.append(created_node)

        summary = f"Compress nodes #{format_node_ranges(target_numbers)}"
        self._record_operation(
            {
                "operation_type": "compress_nodes",
                "change_type": "compress",
                "label": summary,
                "summary": summary,
                "changed_nodes": target_numbers,
                "target_node_numbers": target_numbers,
                "style": sanitize_text(style).strip(),
                "created_label": label,
            }
        )
        return self._build_mutation_result(
            summary=summary,
            change_type="compress",
            changed_nodes=target_numbers,
            extra={
                "compressed_node_numbers": target_numbers,
                "created_label": label,
                "created_node": self.compact_overview_for_node(created_node),
            },
        )

    def delete_items(self, node: ContextWorkbenchDraftNode, *, item_numbers: list[int], reason: str) -> dict[str, object]:
        provider_items = self._provider_items_for_node(node)
        if not item_numbers:
            raise ValueError("at least one item_number is required")

        resolved_items = []
        removed_indexes: list[int] = []
        for item_number in sorted(set(item_numbers)):
            resolved_items.append(self._resolve_item_detail(node, item_number))
            removed_indexes.extend(paired_tool_item_indexes(provider_items, item_number - 1))
        removed_indexes = sorted(set(removed_indexes))
        for remove_index in sorted(removed_indexes, reverse=True):
            del provider_items[remove_index]
        validate_context_provider_items(provider_items)
        self._set_node_record(node, compile_record_from_provider_items(node.record, provider_items))

        changed_nodes = [node.source_node_number] if node.source_node_number is not None else []
        paired_suffix = " pair" if len(removed_indexes) > 1 else ""
        requested_label = format_node_ranges(sorted(set(item_numbers)))
        summary = f"Delete {node.label} item #{requested_label}{paired_suffix}"
        self._record_operation(
            {
                "operation_type": "delete_items",
                "change_type": "delete",
                "label": summary,
                "summary": summary,
                "changed_nodes": changed_nodes,
                "target_node_numbers": changed_nodes,
                "target_items": [
                    {
                        "node_number": node.source_node_number,
                        "item_number": sanitize_value(item.get("item_number")),
                        "item_type": sanitize_text(item.get("item_type") or ""),
                        "paired_item_numbers": [index + 1 for index in removed_indexes],
                    }
                    for item in resolved_items
                ],
                "reason": sanitize_text(reason).strip(),
            }
        )
        return self._build_mutation_result(
            summary=summary,
            change_type="delete",
            changed_nodes=changed_nodes,
            extra={
                "deleted_items": [
                    {
                        "node_number": node.source_node_number,
                        "item_number": sanitize_value(item.get("item_number")),
                        "paired_item_numbers": [index + 1 for index in removed_indexes],
                        "item": item,
                    }
                    for item in resolved_items
                ],
            },
        )

    def delete_item(self, node: ContextWorkbenchDraftNode, *, item_number: int, reason: str) -> dict[str, object]:
        return self.delete_items(node, item_numbers=[item_number], reason=reason)

    def replace_item(
        self,
        node: ContextWorkbenchDraftNode,
        *,
        item_number: int,
        replacement_item: dict[str, Any],
        reason: str,
        change_type: str = "replace",
    ) -> dict[str, object]:
        provider_items = self._provider_items_for_node(node)
        original_item = self._resolve_item_detail(node, item_number)
        original_provider_item = provider_items[item_number - 1]
        normalized_replacement = normalize_provider_items([replacement_item])
        if len(normalized_replacement) != 1:
            raise ValueError("replacement_item must normalize into exactly one content item")
        validate_context_replacement_identity(original_provider_item, normalized_replacement[0])
        provider_items[item_number - 1] = normalized_replacement[0]
        validate_context_provider_items(provider_items)
        self._set_node_record(node, compile_record_from_provider_items(node.record, provider_items))

        changed_nodes = [node.source_node_number] if node.source_node_number is not None else []
        summary_prefix = "Compress" if normalize_change_type(change_type) == "compress" else "Replace"
        summary = f"{summary_prefix} {node.label} item #{item_number}"
        self._record_operation(
            {
                "operation_type": "compress_item"
                if normalize_change_type(change_type) == "compress"
                else "replace_item",
                "change_type": normalize_change_type(change_type),
                "label": summary,
                "summary": summary,
                "changed_nodes": changed_nodes,
                "target_node_numbers": changed_nodes,
                "target_items": [
                    {
                        "node_number": node.source_node_number,
                        "item_number": item_number,
                        "item_type": sanitize_text(original_item.get("item_type") or ""),
                    }
                ],
                "replacement_item": sanitize_value(normalized_replacement[0]),
                "reason": sanitize_text(reason).strip(),
            }
        )
        return self._build_mutation_result(
            summary=summary,
            change_type=change_type,
            changed_nodes=changed_nodes,
            extra={
                "replaced_items": [
                    {
                        "node_number": node.source_node_number,
                        "item_number": item_number,
                        "before": original_item,
                        "after": provider_item_detail(normalized_replacement[0], item_number),
                    }
                ],
            },
        )

    def compress_item(
        self,
        node: ContextWorkbenchDraftNode,
        *,
        item_number: int,
        compressed_content: str,
        style: str,
    ) -> dict[str, object]:
        provider_items = self._provider_items_for_node(node)
        if item_number < 1 or item_number > len(provider_items):
            raise ValueError(f"item #{item_number} does not exist in {node.label}")

        original_item = provider_items[item_number - 1]
        item_type = sanitize_text(original_item.get("type") or "").strip()
        safe_content = sanitize_text(compressed_content).strip()
        if not safe_content:
            raise ValueError("compressed_content is required")

        replacement_item = sanitize_value(original_item)
        if item_type == "message":
            replacement_item["content"] = replace_provider_message_text(original_item.get("content"), safe_content)
        elif item_type == "function_call":
            replacement_item["arguments"] = safe_content
        elif item_type == "custom_tool_call":
            replacement_item["input"] = safe_content
        elif item_type in {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}:
            replacement_item["output"] = safe_content
        elif item_type == "mcp_tool_call_output":
            replacement_item["output"] = {
                "content": [{"type": "text", "text": safe_content}],
                "structured_content": None,
                "is_error": False,
                "meta": None,
            }
        elif item_type == "tool_search_output":
            replacement_item["tools"] = [{"summary": safe_content}]
        else:
            raise ValueError(f"{node.label} item #{item_number} cannot be compressed")

        return self.replace_item(
            node,
            item_number=item_number,
            replacement_item=replacement_item,
            reason=sanitize_text(style).strip(),
            change_type="compress",
        )

    def committed_transcript(self) -> list[dict[str, object]]:
        return normalize_transcript([node.record for node in self.committed_nodes()])

    def revision_label(self) -> str:
        if not self.operations:
            return "Context update"
        if len(self.operations) == 1:
            return sanitize_text(self.operations[0].get("summary") or self.operations[0].get("label") or "").strip() or "Context update"
        first_label = sanitize_text(
            self.operations[0].get("summary") or self.operations[0].get("label") or ""
        ).strip() or "Context update"
        return f"{first_label} + {len(self.operations) - 1} more"

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
            self._revision_summary = summary
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

    def build_draft_snapshot_text(self, session_title: str, session_scope: str) -> str:
        active = [n for n in sorted(self.nodes, key=lambda n: n.order) if n.active and n.editable]
        selected_numbers = self.selected_node_numbers
        lines = [
            "# 当前主 Codex 上下文快照（已更新）",
            f"- 会话标题：{session_title}",
            f"- 会话类型：{session_scope}",
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
        session_scope: str = "",
    ) -> None:
        self.draft = draft
        self._session_title = session_title
        self._session_scope = session_scope
        self._tools = {
            definition.name: definition
            for definition in [
                self._build_get_nodes_tool(),
                self._build_write_nodes_tool(),
                self._build_write_items_tool(),
            ]
        }

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_schema() for tool in self._tools.values()]

    @classmethod
    def tool_catalog(cls) -> list[dict[str, str]]:
        return [
            {
                "id": "get_nodes",
                "label": "Get Nodes",
                "description": "Expand one or more nodes into full structured item details.",
                "status": "available",
            },
            {
                "id": "write_nodes",
                "label": "Write Nodes",
                "description": "Delete and/or insert nodes in the working snapshot.",
                "status": "available",
            },
            {
                "id": "write_items",
                "label": "Write Items",
                "description": "Delete and/or insert items within a single node.",
                "status": "available",
            },
        ]

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
            status="available", handler=handler,
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
            result = self.draft.apply_write_nodes(delete_numbers, inserts)
            new_snapshot = self.draft.build_draft_snapshot_text(self._session_title, self._session_scope)
            return ToolExecution(
                output_text=json.dumps({"result": result, "updated_snapshot": new_snapshot}, ensure_ascii=False),
                display_title="Write Nodes",
                display_detail=sanitize_text(result.get("summary") or ""),
                display_result=sanitize_text(result.get("summary") or "Nodes updated."),
            )
        return ContextWorkbenchToolDefinition(
            name="write_nodes", label="Write Nodes",
            description=(
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
                },
                "additionalProperties": False,
            },
            status="available", handler=handler,
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
            status="available", handler=handler,
        )


def normalize_context_chat_history(raw_history: Any) -> list[dict[str, str]]:
    if not isinstance(raw_history, list):
        return []

    history: list[dict[str, str]] = []
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        role = sanitize_text(item.get("role") or "").strip()
        if role not in {"user", "assistant"}:
            continue
        content = sanitize_text(item.get("content") or "").strip()
        if not content:
            continue
        history.append(
            {
                "role": role,
                "content": content,
            }
        )
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
    ordered = [default_model, *(configured_models or []), "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.2"]
    unique_models: list[str] = []
    for model in ordered:
        safe_model = sanitize_text(model).strip()
        if safe_model and safe_model not in unique_models:
            unique_models.append(safe_model)
    return unique_models

def active_provider_models(settings: Settings) -> list[str]:
    return settings.active_provider_model_ids()

