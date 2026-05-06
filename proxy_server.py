from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import http.client
import io
import json
import os
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HOST = os.environ.get("HASH_CONTEXT_PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("HASH_CONTEXT_PROXY_PORT", "8787"))
OPENAI_UPSTREAM_BASE_URL = os.environ.get(
    "HASH_CONTEXT_OPENAI_UPSTREAM_BASE_URL",
    os.environ.get("HASH_CONTEXT_UPSTREAM_BASE_URL", "https://api.openai.com/v1"),
)
CHATGPT_UPSTREAM_BASE_URL = os.environ.get(
    "HASH_CONTEXT_CHATGPT_UPSTREAM_BASE_URL",
    "https://chatgpt.com/backend-api/codex",
)
DATA_DIR = Path(os.environ.get("HASH_CONTEXT_PROXY_DATA_DIR", Path(__file__).parent / "data"))
STATE_PATH = DATA_DIR / "proxy_state.json"
LOG_PATH = DATA_DIR / "proxy.log"
CODEX_PROXY_PROVIDER_ID = "codex-proxy"
CODEX_PROXY_BASE_URL = f"http://{HOST}:{PORT}/v1"
INTERNAL_CONTEXT_HEADER = "x-hash-context-internal"
INTERNAL_CONTEXT_VALUE = "context-workbench"
LOCAL_COMPACT_PROMPT_PREFIX = "You are performing a CONTEXT CHECKPOINT COMPACTION."
LOCAL_COMPACT_SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary of its thinking process. "
    "You also have access to the state of the tools that were used by that language model. "
    "Use this to build on the work that has already been done and avoid duplicating work. "
    "Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:"
)
_UPSTREAM_AUTH_LOCK = threading.Lock()
_UPSTREAM_AUTH_HEADERS: dict[str, str] = {}
CODEX_AUTH_PATH = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def proxy_log(message: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text("", encoding="utf-8") if not LOG_PATH.exists() else None
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{utc_timestamp()} {message}\n")
    except Exception:
        pass


def compact_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = (
                    item.get("text")
                    or item.get("input_text")
                    or item.get("output_text")
                    or item.get("summary_text")
                    or item.get("reasoning_text")
                )
                if text:
                    parts.append(str(text))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(value, dict):
        text = (
            value.get("text")
            or value.get("input_text")
            or value.get("output_text")
            or value.get("summary_text")
            or value.get("reasoning_text")
        )
        if text:
            return str(text)
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)
    return str(value)


def read_message_text(item: dict[str, Any]) -> str:
    if "content" in item:
        return compact_text(item.get("content"))
    return compact_text(item.get("text"))


def role_message_text_from_items(items: list[dict[str, Any]], role: str) -> str:
    parts = [
        read_message_text(item)
        for item in items
        if item.get("type") == "message" and item.get("role") == role
    ]
    return "\n\n".join(part for part in parts if part)


def provider_message(role: str, text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": role,
        "content": text,
    }


ASSISTANT_ITEM_TYPES = {
    "reasoning",
    "local_shell_call",
    "function_call",
    "tool_search_call",
    "function_call_output",
    "mcp_tool_call_output",
    "local_shell_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "tool_search_output",
    "web_search_call",
    "image_generation_call",
    "compaction",
    "compaction_summary",
}

TOOL_CALL_ITEM_TYPES = {
    "function_call",
    "custom_tool_call",
    "local_shell_call",
    "tool_search_call",
    "web_search_call",
    "image_generation_call",
}

TOOL_OUTPUT_ITEM_TYPES = {
    "function_call_output",
    "custom_tool_call_output",
    "mcp_tool_call_output",
    "tool_search_output",
    "local_shell_call_output",
}

REQUIRED_TOOL_OUTPUT_TYPES_BY_CALL_TYPE = {
    "function_call": {"function_call_output", "mcp_tool_call_output"},
    "local_shell_call": {"function_call_output", "local_shell_call_output"},
    "custom_tool_call": {"custom_tool_call_output"},
    "tool_search_call": {"tool_search_output"},
}

REQUIRED_TOOL_CALL_TYPES_BY_OUTPUT_TYPE: dict[str, set[str]] = {}
for _call_type, _output_types in REQUIRED_TOOL_OUTPUT_TYPES_BY_CALL_TYPE.items():
    for _output_type in _output_types:
        REQUIRED_TOOL_CALL_TYPES_BY_OUTPUT_TYPE.setdefault(_output_type, set()).add(_call_type)


def transcript_record(role: str, text: str, provider_items: list[dict[str, Any]]) -> dict[str, Any]:
    safe_provider_items = provider_items_with_record_text(role, text, provider_items)
    tool_events = tool_events_from_provider_items(safe_provider_items)
    blocks = blocks_from_provider_items(role, text, safe_provider_items, tool_events)
    return {
        "role": role,
        "text": text,
        "attachments": [],
        "toolEvents": tool_events,
        "blocks": blocks,
        "providerItems": safe_provider_items,
        "pending": role == "assistant" and not text,
    }


def provider_items_with_record_text(role: str, text: str, provider_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = [copy.deepcopy(item) for item in provider_items if isinstance(item, dict)]
    if not text or role != "assistant":
        return items
    if assistant_text_from_items(items):
        return items
    for item in items:
        if item.get("type") == "message" and item.get("role") == "assistant":
            item["content"] = [{"type": "output_text", "text": text}]
            return items
    return [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}, *items]


def blocks_from_provider_items(
    role: str,
    text: str,
    provider_items: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if role != "assistant":
        return [{"kind": "text", "text": text}] if text else []
    if not text and not tool_events:
        return [{"kind": "thinking"}]

    events_by_call_id = {
        str(event.get("call_id") or ""): event
        for event in tool_events
        if isinstance(event, dict) and event.get("call_id")
    }
    blocks: list[dict[str, Any]] = []
    used_call_ids: set[str] = set()
    for item in provider_items:
        item_type = item.get("type")
        if item_type == "message" and item.get("role") == "assistant":
            item_text = read_message_text(item)
            if item_text:
                blocks.append({"kind": "text", "text": item_text})
        elif item_type in TOOL_CALL_ITEM_TYPES:
            call_id = str(item.get("call_id") or item.get("id") or "")
            event = events_by_call_id.get(call_id)
            if event is not None:
                blocks.append({"kind": "tool", "tool_event": event})
                used_call_ids.add(call_id)
        elif item_type == "reasoning":
            reasoning_text = reasoning_text_from_item(item)
            if reasoning_text:
                blocks.append({"kind": "reasoning", "text": reasoning_text, "status": "completed"})

    for event in tool_events:
        call_id = str(event.get("call_id") or "")
        if call_id and call_id in used_call_ids:
            continue
        blocks.append({"kind": "tool", "tool_event": event})

    if text and not any(block.get("kind") == "text" for block in blocks):
        blocks.insert(0, {"kind": "text", "text": text})
    return blocks


def tool_events_from_provider_items(provider_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    consumed_output_indexes: set[int] = set()
    output_indexes_by_call_id: dict[str, list[int]] = {}
    for index, item in enumerate(provider_items):
        item_type = str(item.get("type") or "")
        if item_type not in TOOL_OUTPUT_ITEM_TYPES:
            continue
        call_id = str(item.get("call_id") or "").strip()
        if call_id:
            output_indexes_by_call_id.setdefault(call_id, []).append(index)

    for index, item in enumerate(provider_items):
        item_type = str(item.get("type") or "")
        if item_type in REQUIRED_TOOL_OUTPUT_TYPES_BY_CALL_TYPE:
            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            event = tool_call_event_from_item(item)
            allowed_output_types = REQUIRED_TOOL_OUTPUT_TYPES_BY_CALL_TYPE.get(item_type, set())
            for output_index in output_indexes_by_call_id.get(call_id, []):
                if output_index in consumed_output_indexes:
                    continue
                output_item = provider_items[output_index]
                if str(output_item.get("type") or "") not in allowed_output_types:
                    continue
                output = output_text_from_item(output_item)
                event["raw_output"] = output
                event["output_preview"] = output[:500]
                event["display_result"] = output[:500]
                event["status"] = status_from_tool_output(output, str(event.get("status") or "completed"))
                consumed_output_indexes.add(output_index)
                break
            events.append(event)
        elif item_type in {"web_search_call", "image_generation_call"}:
            events.append(tool_call_event_from_item(item))
        elif item_type in TOOL_OUTPUT_ITEM_TYPES and index not in consumed_output_indexes:
            call_id = str(item.get("call_id") or "").strip()
            output = output_text_from_item(item)
            events.append(
                {
                    "name": str(item.get("name") or item_type or "tool_output"),
                    "arguments": "",
                    "call_id": call_id,
                    "output_preview": output[:500],
                    "raw_output": output,
                    "display_title": str(item.get("name") or item_type or "tool_output"),
                    "display_detail": "",
                    "display_result": output[:500],
                    "status": status_from_tool_output(output),
                }
            )
    return events


def tool_call_event_from_item(item: dict[str, Any]) -> dict[str, Any]:
    item_type = str(item.get("type") or "")
    call_id = str(item.get("call_id") or item.get("id") or "")
    name = str(item.get("name") or item_type or "tool_call")
    arguments = item.get("arguments")
    if arguments is None:
        arguments = item.get("input")
    if arguments is None:
        arguments = item.get("action")
    if arguments is None and item_type == "tool_search_call":
        arguments = item.get("arguments")
    if arguments is None:
        arguments = ""

    result = ""
    if item_type == "image_generation_call":
        result = compact_text(item.get("result"))

    return {
        "name": name,
        "arguments": compact_text(arguments),
        "output_preview": result[:500],
        "raw_output": result,
        "display_title": display_title_for_tool_item(item),
        "display_detail": display_detail_for_tool_item(item),
        "display_result": result[:500],
        "status": str(item.get("status") or "completed"),
        "call_id": call_id,
    }


def display_title_for_tool_item(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "")
    if item_type == "web_search_call":
        return "web_search"
    if item_type == "image_generation_call":
        return "image_generation"
    if item_type == "tool_search_call":
        return "tool_search"
    if item_type == "local_shell_call":
        return "local_shell"
    return str(item.get("name") or item_type or "tool_call")


def output_text_from_item(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "")
    if item_type == "tool_search_output":
        return compact_text(item.get("tools"))
    if item_type == "mcp_tool_call_output":
        return compact_text(item.get("output"))
    return compact_text(item.get("output"))


def display_detail_for_tool_item(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "")
    name = str(item.get("name") or "")
    arguments = item.get("arguments")
    if isinstance(arguments, str):
        try:
            parsed_arguments = json.loads(arguments)
        except json.JSONDecodeError:
            parsed_arguments = None
    else:
        parsed_arguments = arguments

    if name in {"shell_command", "exec_command"} and isinstance(parsed_arguments, dict):
        command = parsed_arguments.get("command")
        if isinstance(command, list):
            return " ".join(str(part) for part in command)
        if command is not None:
            return compact_text(command)
    if name == "write_stdin" and isinstance(parsed_arguments, dict):
        return compact_text(parsed_arguments.get("stdin") or parsed_arguments.get("input") or arguments)
    if item_type == "local_shell_call":
        action = item.get("action")
        if isinstance(action, dict):
            command = action.get("command")
            if isinstance(command, list):
                return " ".join(str(part) for part in command)
            if command is not None:
                return compact_text(command)
        return compact_text(action)
    return item_type or name or "tool call"


def status_from_tool_output(output: str, fallback: str = "completed") -> str:
    lines = compact_text(output).splitlines()
    first_line = lines[0] if lines else ""
    if first_line.lower().startswith("exit code:"):
        raw_code = first_line.split(":", 1)[1].strip().split(maxsplit=1)[0]
        try:
            return "completed" if int(raw_code) == 0 else "error"
        except ValueError:
            return fallback
    return fallback


def reasoning_text_from_item(item: dict[str, Any]) -> str:
    parts: list[str] = []
    summary = item.get("summary")
    if isinstance(summary, list):
        parts.extend(compact_text(entry) for entry in summary if entry is not None)
    elif summary:
        parts.append(compact_text(summary))
    content = item.get("content")
    if isinstance(content, list):
        parts.extend(compact_text(entry) for entry in content if entry is not None)
    elif content:
        parts.append(compact_text(content))
    return "\n".join(part for part in parts if part)


def visible_text_from_compaction_item(item: dict[str, Any]) -> str:
    parts: list[str] = []

    def append_visible(value: Any) -> None:
        if isinstance(value, str):
            text = compact_text(value)
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


def visible_text_from_context_item(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "")
    if item_type in {"compaction", "compaction_summary"}:
        return visible_text_from_compaction_item(item)
    if item_type == "message":
        return read_message_text(item)
    if item_type == "reasoning":
        return reasoning_text_from_item(item)
    return compact_text(item)


def input_items_to_transcript(input_items: Any) -> list[dict[str, Any]]:
    if isinstance(input_items, str):
        return [transcript_record("user", input_items, [provider_message("user", input_items)])]
    if not isinstance(input_items, list):
        return []

    records: list[dict[str, Any]] = []
    assistant_items: list[dict[str, Any]] = []
    assistant_text_parts: list[str] = []

    def flush_assistant() -> None:
        nonlocal assistant_items, assistant_text_parts
        if not assistant_items:
            return
        text = "\n".join(part for part in assistant_text_parts if part)
        records.append(transcript_record("assistant", text, assistant_items))
        assistant_items = []
        assistant_text_parts = []

    for raw_item in input_items:
        if not isinstance(raw_item, dict):
            continue
        item = copy.deepcopy(raw_item)
        item_type = str(item.get("type") or "")
        role = str(item.get("role") or "")

        if item_type == "message" and role in {"system", "developer", "user"}:
            flush_assistant()
            text = read_message_text(item)
            records.append(transcript_record(role, text, [item]))
            continue

        if item_type == "message" and role == "assistant":
            text = read_message_text(item)
            assistant_items.append(item)
            if text:
                assistant_text_parts.append(text)
            continue

        if item_type in {"compaction", "compaction_summary"} and assistant_items:
            assistant_items.append(item)
            continue

        if item_type in {"compaction", "compaction_summary"}:
            flush_assistant()
            records.append(transcript_record("compaction", visible_text_from_compaction_item(item), [item]))
            continue

        if item_type in ASSISTANT_ITEM_TYPES:
            assistant_items.append(item)
            continue

        flush_assistant()
        records.append(transcript_record("context", visible_text_from_context_item(item), [item]))

    flush_assistant()
    return records


def transcript_to_input_items(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    input_items: list[dict[str, Any]] = []
    for record in transcript:
        if not isinstance(record, dict):
            continue
        record_role = str(record.get("role") or "").strip()
        role = record_role if record_role in {"system", "developer", "user", "assistant"} else ""
        text = compact_text(record.get("text"))
        provider_items = record.get("providerItems")
        if isinstance(provider_items, list) and provider_items and not role:
            input_items.extend(copy.deepcopy(item) for item in provider_items if isinstance(item, dict))
        elif isinstance(provider_items, list) and provider_items:
            input_items.extend(compile_provider_items(role, text, provider_items))
        elif role:
            input_items.append(provider_message(role, text))
    return input_items


def input_items_contain_tool_output(input_items: Any) -> bool:
    if not isinstance(input_items, list):
        return False
    return any(
        isinstance(item, dict) and str(item.get("type") or "") in TOOL_OUTPUT_ITEM_TYPES
        for item in input_items
    )


def input_items_end_with_tool_output(input_items: Any) -> bool:
    if not isinstance(input_items, list):
        return False
    for item in reversed(input_items):
        if not isinstance(item, dict):
            continue
        return str(item.get("type") or "") in TOOL_OUTPUT_ITEM_TYPES
    return False


def drop_unpaired_tool_items(input_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output_counts_by_call: dict[tuple[str, str], int] = {}
    for item in input_items:
        item_type = str(item.get("type") or "")
        output_call_types = REQUIRED_TOOL_CALL_TYPES_BY_OUTPUT_TYPE.get(item_type)
        if not output_call_types:
            continue
        call_id = str(item.get("call_id") or "").strip()
        if not call_id:
            continue
        for output_call_type in output_call_types:
            key = (output_call_type, call_id)
            output_counts_by_call[key] = output_counts_by_call.get(key, 0) + 1

    kept_call_counts: dict[tuple[str, str], int] = {}
    for item in input_items:
        item_type = str(item.get("type") or "")
        if item_type not in REQUIRED_TOOL_OUTPUT_TYPES_BY_CALL_TYPE:
            continue
        call_id = str(item.get("call_id") or item.get("id") or "").strip()
        if not call_id:
            continue
        key = (item_type, call_id)
        available_outputs = output_counts_by_call.get(key, 0)
        if available_outputs <= kept_call_counts.get(key, 0):
            continue
        kept_call_counts[key] = kept_call_counts.get(key, 0) + 1

    emitted_call_counts: dict[tuple[str, str], int] = {}
    used_output_counts: dict[tuple[str, str], int] = {}
    sanitized_items: list[dict[str, Any]] = []
    for item in input_items:
        item_type = str(item.get("type") or "")
        if item_type in REQUIRED_TOOL_OUTPUT_TYPES_BY_CALL_TYPE:
            call_id = str(item.get("call_id") or item.get("id") or "").strip()
            if not call_id:
                continue
            key = (item_type, call_id)
            if kept_call_counts.get(key, 0) <= emitted_call_counts.get(key, 0):
                continue
            emitted_call_counts[key] = emitted_call_counts.get(key, 0) + 1
            sanitized_items.append(item)
            continue

        output_call_types = REQUIRED_TOOL_CALL_TYPES_BY_OUTPUT_TYPE.get(item_type)
        if output_call_types:
            call_id = str(item.get("call_id") or "").strip()
            if not call_id:
                continue
            key = next(
                (
                    (output_call_type, call_id)
                    for output_call_type in output_call_types
                    if emitted_call_counts.get((output_call_type, call_id), 0)
                    > used_output_counts.get((output_call_type, call_id), 0)
                ),
                None,
            )
            if key is None:
                continue
            used_output_counts[key] = used_output_counts.get(key, 0) + 1
            sanitized_items.append(item)
            continue

        sanitized_items.append(item)
    return sanitized_items


def compile_provider_items(role: str, text: str, provider_items: list[Any]) -> list[dict[str, Any]]:
    items = [copy.deepcopy(item) for item in provider_items if isinstance(item, dict)]
    if not items:
        return [provider_message(role, text)]

    existing_text = role_message_text_from_items(items, role)
    if existing_text.strip() == compact_text(text).strip():
        return items

    message_indexes = [
        index
        for index, item in enumerate(items)
        if item.get("type") == "message" and item.get("role") == role
    ]
    structural_items = [
        item
        for item in items
        if not (item.get("type") == "message" and item.get("role") == role)
    ]
    if not structural_items:
        return [provider_message(role, text)]

    if message_indexes:
        first_index = message_indexes[0]
        items[first_index]["content"] = text
        for duplicate_index in reversed(message_indexes[1:]):
            del items[duplicate_index]
        return items

    if text:
        return [provider_message(role, text), *items]
    return items


def session_id_for_request(body: dict[str, Any], headers: dict[str, str]) -> str:
    for key in ("x-hash-context-session-id", "x-codex-conversation-id", "x-codex-session-id"):
        value = headers.get(key)
        if value:
            return sanitize_id(value)
    metadata_session_id = session_id_from_codex_metadata(headers)
    if metadata_session_id:
        return sanitize_id(metadata_session_id)
    prompt_cache_key = body.get("prompt_cache_key")
    if isinstance(prompt_cache_key, str) and prompt_cache_key.strip():
        return sanitize_id(prompt_cache_key)
    metadata = body.get("client_metadata")
    if isinstance(metadata, dict):
        for value in metadata.values():
            if isinstance(value, str) and value.strip():
                return sanitize_id(value)
    digest = hashlib.sha1(json.dumps(body.get("input", []), sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return f"session-{digest[:16]}"


def session_id_for_compact_request(body: dict[str, Any], headers: dict[str, str], active_session_id: str) -> str:
    for key in ("x-hash-context-session-id", "x-codex-conversation-id", "x-codex-session-id"):
        value = headers.get(key)
        if value:
            return sanitize_id(value)
    metadata_session_id = session_id_from_codex_metadata(headers)
    if metadata_session_id:
        return sanitize_id(metadata_session_id)
    if active_session_id:
        return active_session_id
    return session_id_for_request(body, headers)


def session_id_from_codex_metadata(headers: dict[str, str]) -> str:
    for key in ("x-codex-turn-metadata", "x-codex-turn-state"):
        raw_value = headers.get(key)
        if not raw_value:
            continue
        try:
            parsed = json.loads(raw_value)
        except (TypeError, json.JSONDecodeError):
            continue
        session_id = find_session_id_in_value(parsed)
        if session_id:
            return session_id
    return ""


def find_session_id_in_value(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("session_id", "conversation_id", "thread_id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for key, nested_value in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in ("session", "conversation", "thread")):
                found = find_session_id_in_value(nested_value)
                if found:
                    return found
        for nested_value in value.values():
            found = find_session_id_in_value(nested_value)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_session_id_in_value(item)
            if found:
                return found
    return ""


def sanitize_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in value.strip())
    return cleaned[:120] or uuid.uuid4().hex


def response_headers_for_upstream(headers: dict[str, str]) -> dict[str, str]:
    hop_by_hop = {
        "host",
        "content-length",
        "content-encoding",
        "connection",
        "accept-encoding",
        "transfer-encoding",
        INTERNAL_CONTEXT_HEADER,
    }
    canonical_names = {
        "accept": "Accept",
        "authorization": "Authorization",
        "chatgpt-account-id": "ChatGPT-Account-ID",
        "content-type": "Content-Type",
        "cookie": "Cookie",
        "openai-beta": "OpenAI-Beta",
        "originator": "originator",
        "user-agent": "User-Agent",
        "x-client-request-id": "x-client-request-id",
        "x-codex-beta-features": "x-codex-beta-features",
        "x-codex-installation-id": "x-codex-installation-id",
        "x-codex-parent-thread-id": "x-codex-parent-thread-id",
        "x-codex-turn-metadata": "x-codex-turn-metadata",
        "x-codex-turn-state": "x-codex-turn-state",
        "x-codex-window-id": "x-codex-window-id",
        "x-openai-subagent": "x-openai-subagent",
        "x-responsesapi-include-timing-metrics": "x-responsesapi-include-timing-metrics",
    }
    next_headers: dict[str, str] = {}
    for key, value in headers.items():
        lower_key = key.lower()
        if lower_key in hop_by_hop:
            continue
        next_headers[canonical_names.get(lower_key, key)] = value

    # Codex sends these through reqwest, but be explicit here because the
    # proxy reserializes the JSON body before forwarding it.
    next_headers["Content-Type"] = "application/json"
    next_headers["Accept"] = "text/event-stream"
    next_headers.setdefault("User-Agent", "codex_cli_rs")
    return next_headers


def _has_usable_auth(headers: dict[str, str]) -> bool:
    authorization = str(headers.get("Authorization") or headers.get("authorization") or "").strip()
    if authorization and authorization.lower() not in {"bearer not-needed", "bearer dummy", "bearer fake"}:
        return True
    return bool(
        str(headers.get("ChatGPT-Account-ID") or headers.get("chatgpt-account-id") or "").strip()
        or str(headers.get("Cookie") or headers.get("cookie") or "").strip()
    )


def remember_upstream_auth(headers: dict[str, str]) -> None:
    candidate = response_headers_for_upstream(headers)
    if not _has_usable_auth(candidate):
        return
    cached = {
        key: value
        for key, value in candidate.items()
        if key.lower()
        not in {
            "accept",
            "content-type",
            "content-length",
            "host",
            "connection",
            "transfer-encoding",
            INTERNAL_CONTEXT_HEADER,
        }
    }
    with _UPSTREAM_AUTH_LOCK:
        _UPSTREAM_AUTH_HEADERS.clear()
        _UPSTREAM_AUTH_HEADERS.update(cached)


def preload_codex_subscription_auth() -> bool:
    try:
        raw = json.loads(CODEX_AUTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    tokens = raw.get("tokens") if isinstance(raw, dict) else None
    if not isinstance(tokens, dict):
        return False

    access_token = str(tokens.get("access_token") or "").strip()
    account_id = str(tokens.get("account_id") or "").strip()
    if not access_token or not account_id:
        return False

    headers = {
        "Authorization": f"Bearer {access_token}",
        "ChatGPT-Account-ID": account_id,
        "User-Agent": "codex_cli_rs",
        "originator": "codex-tui",
    }
    remember_upstream_auth(headers)
    return bool(cached_upstream_auth_headers())


def preload_openai_api_auth() -> bool:
    api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return False
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "codex_cli_rs",
    }
    remember_upstream_auth(headers)
    return bool(cached_upstream_auth_headers())


def cached_upstream_auth_headers() -> dict[str, str]:
    with _UPSTREAM_AUTH_LOCK:
        return dict(_UPSTREAM_AUTH_HEADERS)


def apply_cached_upstream_auth(headers: dict[str, str]) -> dict[str, str]:
    next_headers = dict(headers)
    cached = cached_upstream_auth_headers()
    if not cached:
        return next_headers

    if not _has_usable_auth(next_headers):
        for key, value in cached.items():
            if key.lower() == "accept":
                continue
            next_headers[key] = value
        return next_headers

    return next_headers


def upstream_headers_for_request(headers: dict[str, str], *, accept: str) -> dict[str, str]:
    remember_upstream_auth(headers)
    next_headers = apply_cached_upstream_auth(response_headers_for_upstream(headers))
    next_headers["Accept"] = accept
    next_headers["Content-Type"] = "application/json"
    return next_headers


def json_headers_for_upstream(headers: dict[str, str]) -> dict[str, str]:
    return upstream_headers_for_request(headers, accept="application/json")


def safe_headers_for_log(headers: dict[str, str]) -> dict[str, str]:
    redacted = {"authorization", "cookie", "set-cookie"}
    return {
        key: ("<redacted>" if key.lower() in redacted else value)
        for key, value in headers.items()
        if key.lower() not in {"host", "content-length"}
    }


def decode_request_body(raw_body: bytes, content_encoding: str | None) -> bytes:
    encoding = (content_encoding or "").strip().lower()
    if not encoding or encoding == "identity":
        return raw_body
    if encoding in {"gzip", "x-gzip"}:
        return gzip.decompress(raw_body)
    if encoding == "br":
        import brotli

        return brotli.decompress(raw_body)
    if encoding in {"zstd", "zstandard"}:
        import zstandard

        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw_body)) as reader:
            return reader.read()
    raise ValueError(f"unsupported request content-encoding: {content_encoding}")


def parse_json_request_body(raw_body: bytes, content_encoding: str | None) -> Any:
    decoded = decode_request_body(raw_body, content_encoding)
    return json.loads(decoded.decode("utf-8"))


def upstream_base_url_for_request(headers: dict[str, str]) -> str:
    effective_headers = apply_cached_upstream_auth(headers)
    lowered = {key.lower(): value for key, value in effective_headers.items()}
    if lowered.get("chatgpt-account-id"):
        return CHATGPT_UPSTREAM_BASE_URL
    return OPENAI_UPSTREAM_BASE_URL


def has_effective_chatgpt_auth(headers: dict[str, str]) -> bool:
    effective_headers = apply_cached_upstream_auth(headers)
    lowered = {key.lower(): value for key, value in effective_headers.items()}
    return bool(str(lowered.get("chatgpt-account-id") or "").strip())


def has_effective_upstream_auth(headers: dict[str, str]) -> bool:
    return _has_usable_auth(apply_cached_upstream_auth(headers))


def is_internal_context_request(headers: dict[str, str], body: dict[str, Any] | None = None) -> bool:
    if str(headers.get(INTERNAL_CONTEXT_HEADER) or "").strip() == INTERNAL_CONTEXT_VALUE:
        return True
    for metadata_key in ("metadata", "client_metadata"):
        metadata = (body or {}).get(metadata_key) if isinstance(body, dict) else None
        if not isinstance(metadata, dict):
            continue
        if str(metadata.get("hash_context_internal") or "").strip() == INTERNAL_CONTEXT_VALUE:
            return True
    return False


def normalize_model_record(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        model_id = item.strip()
        if not model_id:
            return None
        return {"id": model_id, "object": "model", "owned_by": "codex"}

    if not isinstance(item, dict):
        return None

    model_id = str(item.get("id") or item.get("name") or item.get("slug") or "").strip()
    if not model_id:
        return None

    normalized = copy.deepcopy(item)
    normalized["id"] = model_id.removeprefix("models/")
    normalized.setdefault("object", "model")
    normalized.setdefault("owned_by", str(item.get("owned_by") or item.get("provider") or "codex"))
    return normalized


def normalize_models_payload(payload: Any) -> dict[str, Any] | None:
    raw_models: Any = None
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            raw_models = payload.get("data")
        elif isinstance(payload.get("models"), list):
            raw_models = payload.get("models")
        elif isinstance(payload.get("items"), list):
            raw_models = payload.get("items")
        elif isinstance(payload.get("model_slugs"), list):
            raw_models = payload.get("model_slugs")
    elif isinstance(payload, list):
        raw_models = payload

    if not isinstance(raw_models, list):
        return None

    normalized_models: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in raw_models:
        record = normalize_model_record(item)
        if record is None:
            continue
        model_id = str(record.get("id") or "").strip()
        if not model_id or model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        normalized_models.append(record)

    return {"object": "list", "data": normalized_models}


def normalize_models_response_body(response_body: bytes) -> bytes:
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return response_body

    normalized = normalize_models_payload(payload)
    if normalized is None:
        return response_body
    return json.dumps(normalized, ensure_ascii=False).encode("utf-8")


def fallback_models_response_body() -> bytes:
    configured = os.environ.get("HASH_CONTEXT_FALLBACK_MODELS", "")
    model_ids = [
        item.strip()
        for item in configured.split(",")
        if item.strip()
    ] or [
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex",
        "gpt-5.2",
    ]
    payload = {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "owned_by": "codex",
            }
            for model_id in model_ids
        ],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def is_context_edit_notice_text(text: str) -> bool:
    compact = " ".join(str(text or "").split()).strip()
    return compact.startswith("Hash Context: context has been edited")


def remove_context_edit_notice_prefix(text: str) -> str:
    raw_text = str(text or "")
    if not is_context_edit_notice_text(raw_text):
        return raw_text
    lines = raw_text.splitlines()
    if not lines:
        return ""
    return "\n".join(lines[1:]).lstrip()


def strip_context_edit_notice_records(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    next_transcript: list[dict[str, Any]] = []
    for record in transcript:
        if str(record.get("role") or "").strip() != "assistant":
            next_transcript.append(record)
            continue

        text = compact_text(record.get("text") or "")
        if not is_context_edit_notice_text(text):
            next_transcript.append(record)
            continue

        cleaned_text = remove_context_edit_notice_prefix(str(record.get("text") or "")).strip()
        if not cleaned_text:
            continue

        next_record = copy.deepcopy(record)
        next_record["text"] = cleaned_text
        next_record["providerItems"] = [provider_message("assistant", cleaned_text)]
        next_record["blocks"] = [{"kind": "text", "text": cleaned_text}]
        next_transcript.append(next_record)
    return next_transcript


def is_local_compact_prompt_text(text: str) -> bool:
    return " ".join(str(text or "").split()).startswith(LOCAL_COMPACT_PROMPT_PREFIX)


def is_local_compact_summary_text(text: str) -> bool:
    return str(text or "").startswith(f"{LOCAL_COMPACT_SUMMARY_PREFIX}\n\n")


def is_contextual_user_text(text: str) -> bool:
    trimmed = str(text or "").lstrip()
    lowered = trimmed.lower()
    return (
        trimmed.startswith("# AGENTS.md instructions for ")
        or lowered.startswith("<environment_context>")
        or lowered.startswith("<skills>")
        or lowered.startswith("<user_shell_command>")
        or lowered.startswith("<turn_aborted>")
        or lowered.startswith("<subagent_notification>")
    )


def is_initial_context_prefix_record(record: dict[str, Any]) -> bool:
    role = str(record.get("role") or "").strip()
    if role in {"system", "developer"}:
        return True
    if role != "user":
        return False
    return is_contextual_user_text(compact_text(record.get("text")))


def split_initial_context_prefix(
    transcript: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records = clean_transcript(transcript)
    index = 0
    while index < len(records) and is_initial_context_prefix_record(records[index]):
        index += 1
    return copy.deepcopy(records[:index]), copy.deepcopy(records[index:])


def strip_initial_context_prefix_records(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _prefix, body = split_initial_context_prefix(transcript)
    return body


def with_fresh_initial_context_prefix(
    source_transcript: list[dict[str, Any]],
    transcript: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prefix, _source_body = split_initial_context_prefix(source_transcript)
    _stale_prefix, body = split_initial_context_prefix(transcript)
    return clean_transcript([*prefix, *body])


def local_compact_source_from_transcript(transcript: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    records = clean_transcript(transcript)
    if not records:
        return None
    last_record = records[-1]
    if str(last_record.get("role") or "") != "user":
        return None
    if not is_local_compact_prompt_text(compact_text(last_record.get("text"))):
        return None
    return records[:-1]


def local_compacted_transcript(source_transcript: list[dict[str, Any]], assistant_summary_text: str) -> list[dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    for record in clean_transcript(source_transcript):
        if str(record.get("role") or "") != "user":
            continue
        text = compact_text(record.get("text"))
        if (
            not text
            or is_contextual_user_text(text)
            or is_local_compact_prompt_text(text)
            or is_local_compact_summary_text(text)
        ):
            continue
        retained.append(copy.deepcopy(record))

    summary_text = f"{LOCAL_COMPACT_SUMMARY_PREFIX}\n\n{assistant_summary_text or ''}"
    retained.append(transcript_record("user", summary_text, [provider_message("user", summary_text)]))
    return clean_transcript(retained)


@dataclass
class ProxySession:
    id: str
    title: str
    status: str = "mirror"
    transcript: list[dict[str, Any]] = field(default_factory=list)
    edited_transcript: list[dict[str, Any]] | None = None
    pending_transcript: list[dict[str, Any]] | None = None
    local_compact_source_transcript: list[dict[str, Any]] | None = None
    request_log: list[dict[str, Any]] = field(default_factory=list)
    response_items: list[dict[str, Any]] = field(default_factory=list)
    last_error: str = ""
    created_at: str = field(default_factory=utc_timestamp)
    updated_at: str = field(default_factory=utc_timestamp)

    def visible_transcript(self) -> list[dict[str, Any]]:
        if self.pending_transcript is not None:
            return self.pending_transcript
        if self.edited_transcript is not None:
            return self.edited_transcript
        return self.transcript

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "transcript": self.visible_transcript(),
            "raw_transcript": self.transcript,
            "edited_transcript": self.edited_transcript,
            "pending_transcript": self.pending_transcript,
            "has_override": self.edited_transcript is not None,
            "is_running": self.status in {"running", "compacting"},
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def should_expose(self) -> bool:
        return not self.id.startswith("session-")


class ProxyStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.sessions: dict[str, ProxySession] = {}
        self.active_session_id = ""
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        self.active_session_id = str(data.get("active_session_id") or "")
        for raw in data.get("sessions", []):
            if not isinstance(raw, dict):
                continue
            session = ProxySession(
                id=str(raw.get("id") or uuid.uuid4().hex),
                title=str(raw.get("title") or "Codex Session"),
                status=str(raw.get("status") or "mirror"),
                transcript=clean_transcript(raw.get("transcript")),
                edited_transcript=clean_transcript(raw.get("edited_transcript"))
                if isinstance(raw.get("edited_transcript"), list)
                else None,
                pending_transcript=clean_transcript(raw.get("pending_transcript"))
                if isinstance(raw.get("pending_transcript"), list)
                else None,
                request_log=raw.get("request_log") if isinstance(raw.get("request_log"), list) else [],
                response_items=raw.get("response_items") if isinstance(raw.get("response_items"), list) else [],
                last_error=str(raw.get("last_error") or ""),
                created_at=str(raw.get("created_at") or utc_timestamp()),
                updated_at=str(raw.get("updated_at") or utc_timestamp()),
            )
            self.sessions[session.id] = session

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "active_session_id": self.active_session_id,
            "sessions": [
                {
                    "id": session.id,
                    "title": session.title,
                    "status": session.status,
                    "transcript": session.transcript,
                    "edited_transcript": session.edited_transcript,
                    "pending_transcript": session.pending_transcript,
                    "last_error": session.last_error,
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                    "request_log": session.request_log[-20:],
                    "response_items": session.response_items[-100:],
                }
                for session in self.sessions.values()
            ],
        }
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def begin_request(self, session_id: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[ProxySession, dict[str, Any]]:
        request_body = copy.deepcopy(body)
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                session = ProxySession(id=session_id, title=f"Codex {session_id[:8]}")
                self.sessions[session_id] = session
            self.active_session_id = session_id
            source_transcript = strip_context_edit_notice_records(
                input_items_to_transcript(body.get("input"))
            )
            if session.edited_transcript is not None:
                merged_body_transcript = merge_override_transcript(
                    strip_initial_context_prefix_records(
                        strip_context_edit_notice_records(session.edited_transcript)
                    ),
                    strip_initial_context_prefix_records(
                        strip_context_edit_notice_records(session.transcript)
                    ),
                    strip_initial_context_prefix_records(source_transcript),
                )
                forwarded_transcript = with_fresh_initial_context_prefix(
                    source_transcript,
                    merged_body_transcript,
                )
                if input_items_end_with_tool_output(body.get("input")):
                    session.pending_transcript = with_running_assistant(source_transcript)
                    session.status = "running"
                    session.last_error = ""
                    session.updated_at = utc_timestamp()
                    session.request_log.append(
                        {
                            "created_at": session.updated_at,
                            "kind": "tool_output_passthrough",
                            "headers": {key: value for key, value in headers.items() if key.lower().startswith("x-")},
                            "body": body,
                            "forwarded_body": request_body,
                        }
                    )
                    session.request_log = session.request_log[-20:]
                    self.save()
                    return session, request_body
                compact_source = local_compact_source_from_transcript(forwarded_transcript)
                if compact_source is not None:
                    session.local_compact_source_transcript = compact_source
                    session.pending_transcript = None
                    request_body["input"] = drop_unpaired_tool_items(transcript_to_input_items(forwarded_transcript))
                    request_body.pop("previous_response_id", None)
                    session.status = "compacting"
                    session.last_error = ""
                    session.updated_at = utc_timestamp()
                    session.request_log.append(
                        {
                            "created_at": session.updated_at,
                            "kind": "local_compact",
                            "headers": {key: value for key, value in headers.items() if key.lower().startswith("x-")},
                            "body": body,
                            "forwarded_body": request_body,
                        }
                    )
                    session.request_log = session.request_log[-20:]
                    self.save()
                    return session, request_body
                session.edited_transcript = forwarded_transcript
                session.pending_transcript = with_running_assistant(forwarded_transcript)
                request_body["input"] = drop_unpaired_tool_items(transcript_to_input_items(forwarded_transcript))
                request_body.pop("previous_response_id", None)
                session.status = "running"
            else:
                compact_source = local_compact_source_from_transcript(source_transcript)
                if compact_source is not None:
                    session.local_compact_source_transcript = compact_source
                    session.transcript = compact_source
                    session.pending_transcript = None
                    session.status = "compacting"
                    session.last_error = ""
                    session.updated_at = utc_timestamp()
                    session.request_log.append(
                        {
                            "created_at": session.updated_at,
                            "kind": "local_compact",
                            "headers": {key: value for key, value in headers.items() if key.lower().startswith("x-")},
                            "body": body,
                            "forwarded_body": request_body,
                        }
                    )
                    session.request_log = session.request_log[-20:]
                    self.save()
                    return session, request_body
                session.transcript = source_transcript
                session.pending_transcript = None
                session.status = "running"
                session.transcript = with_running_assistant(source_transcript)
            session.last_error = ""
            session.updated_at = utc_timestamp()
            session.request_log.append(
                {
                    "created_at": session.updated_at,
                    "kind": "mirror_passthrough" if session.edited_transcript is None else "override_rewrite",
                    "headers": {key: value for key, value in headers.items() if key.lower().startswith("x-")},
                    "body": body,
                    "forwarded_body": request_body,
                }
            )
            session.request_log = session.request_log[-20:]
            self.save()
            return session, request_body

    def begin_compact(self, session_id: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[ProxySession, dict[str, Any]]:
        request_body = copy.deepcopy(body)
        with self.lock:
            source_transcript = strip_context_edit_notice_records(
                input_items_to_transcript(body.get("input"))
            )
            session = self.sessions.get(session_id)
            if session is None:
                session = ProxySession(id=session_id, title=f"Codex {session_id[:8]}")
                session.transcript = clean_transcript(source_transcript)
                self.sessions[session_id] = session
            self.active_session_id = session_id
            session.local_compact_source_transcript = None

            if session.edited_transcript is not None:
                compact_body_transcript = strip_initial_context_prefix_records(
                    strip_context_edit_notice_records(session.edited_transcript)
                )
                compact_transcript = with_fresh_initial_context_prefix(
                    source_transcript,
                    compact_body_transcript,
                )
                request_body["input"] = drop_unpaired_tool_items(
                    transcript_to_input_items(compact_transcript)
                )
                request_body.pop("previous_response_id", None)
            session.status = "compacting"
            session.pending_transcript = None
            session.last_error = ""
            session.updated_at = utc_timestamp()
            session.request_log.append(
                {
                    "created_at": session.updated_at,
                    "kind": "compact",
                    "headers": {key: value for key, value in headers.items() if key.lower().startswith("x-")},
                    "body": body,
                    "forwarded_body": request_body,
                }
            )
            session.request_log = session.request_log[-20:]
            self.save()
            return session, request_body

    def complete_compact(self, session_id: str, output_items: list[dict[str, Any]]) -> None:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                return
            compacted_transcript = input_items_to_transcript(output_items)
            session.transcript = compacted_transcript
            if session.edited_transcript is not None:
                session.edited_transcript = copy.deepcopy(compacted_transcript)
                session.status = "override"
            else:
                session.status = "mirror"
            session.pending_transcript = None
            session.response_items.extend(output_items)
            session.response_items = session.response_items[-100:]
            session.updated_at = utc_timestamp()
            self.save()

    def complete_response(self, session_id: str, items: list[dict[str, Any]], text: str) -> None:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                return
            if session.local_compact_source_transcript is not None:
                assistant_items = items or [{"type": "message", "role": "assistant", "content": text}]
                assistant_text = text or assistant_text_from_items(assistant_items)
                compacted_transcript = local_compacted_transcript(
                    session.local_compact_source_transcript,
                    assistant_text,
                )
                session.transcript = compacted_transcript
                if session.edited_transcript is not None:
                    session.edited_transcript = copy.deepcopy(compacted_transcript)
                    session.status = "override"
                else:
                    session.status = "mirror"
                session.local_compact_source_transcript = None
                session.pending_transcript = None
                session.response_items.extend(items)
                session.response_items = session.response_items[-100:]
                session.updated_at = utc_timestamp()
                self.save()
                return
            if session.edited_transcript is None:
                base = [record for record in session.transcript if not is_running_assistant(record)]
                assistant_items = items or [{"type": "message", "role": "assistant", "content": text}]
                assistant_text = text or assistant_text_from_items(assistant_items)
                base = append_assistant_response_record(base, assistant_text, assistant_items)
                session.transcript = base
                session.status = "mirror"
            else:
                base = clean_transcript(session.pending_transcript or session.edited_transcript)
                assistant_items = items or [{"type": "message", "role": "assistant", "content": text}]
                assistant_text = text or assistant_text_from_items(assistant_items)
                base = append_assistant_response_record(base, assistant_text, assistant_items)
                session.edited_transcript = base
                session.status = "override"
            session.pending_transcript = None
            session.response_items.extend(items)
            session.response_items = session.response_items[-100:]
            session.updated_at = utc_timestamp()
            self.save()

    def fail_response(self, session_id: str, message: str) -> None:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                return
            session.status = "error"
            session.last_error = message
            session.local_compact_source_transcript = None
            session.pending_transcript = None
            session.updated_at = utc_timestamp()
            self.save()

    def list_sessions(self) -> dict[str, Any]:
        with self.lock:
            sessions = [
                session
                for session in sorted(self.sessions.values(), key=lambda item: item.updated_at, reverse=True)
                if session.should_expose()
            ]
            active_session_id = self.active_session_id
            if active_session_id not in {session.id for session in sessions}:
                active_session_id = sessions[0].id if sessions else ""
            return {
                "active_session_id": active_session_id,
                "sessions": [session.to_payload() for session in sessions],
            }

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            session = self.sessions.get(session_id)
            return session.to_payload() if session else None

    def override(self, session_id: str, transcript: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                session = ProxySession(id=session_id, title=f"Codex {session_id[:8]}")
                self.sessions[session_id] = session
            previous_visible = clean_transcript(session.visible_transcript())
            next_transcript = clean_transcript(transcript)
            mirror_transcript = clean_transcript(session.transcript)
            if next_transcript == mirror_transcript:
                session.edited_transcript = None
                session.status = "mirror"
            else:
                session.edited_transcript = copy.deepcopy(next_transcript)
                session.status = "override"
            session.pending_transcript = None
            session.updated_at = utc_timestamp()
            self.active_session_id = session_id
            self.save()
            payload = session.to_payload()
            payload["changed"] = previous_visible != clean_transcript(session.visible_transcript())
            return payload

    def reset(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            previous_visible = clean_transcript(session.visible_transcript())
            session.edited_transcript = None
            session.pending_transcript = None
            session.status = "mirror"
            session.updated_at = utc_timestamp()
            self.save()
            payload = session.to_payload()
            payload["changed"] = previous_visible != clean_transcript(session.visible_transcript())
            return payload


def is_running_assistant(record: dict[str, Any]) -> bool:
    if record.get("role") != "assistant":
        return False
    provider_items = record.get("providerItems")
    if isinstance(provider_items, list) and any(
        isinstance(item, dict) and str(item.get("type") or "") in ASSISTANT_ITEM_TYPES
        for item in provider_items
    ):
        return False
    tool_events = record.get("toolEvents")
    if isinstance(tool_events, list) and tool_events:
        return False
    blocks = record.get("blocks")
    return not record.get("text") and isinstance(blocks, list) and any(
        isinstance(block, dict) and block.get("kind") == "thinking" for block in blocks
    )


def assistant_text_from_items(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in items:
        if item.get("type") == "message" and item.get("role") == "assistant":
            text = read_message_text(item)
            if text:
                parts.append(text)
    return "\n".join(parts)


def clean_transcript(transcript: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not isinstance(transcript, list):
        return []
    records = [copy.deepcopy(record) for record in transcript if isinstance(record, dict) and not is_running_assistant(record)]
    return coalesce_adjacent_assistant_records(records)


def coalesce_adjacent_assistant_records(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for record in transcript:
        if record.get("role") == "assistant" and merged and merged[-1].get("role") == "assistant":
            provider_items = record.get("providerItems")
            assistant_items = provider_items if isinstance(provider_items, list) else []
            merged = append_assistant_response_record(merged, compact_text(record.get("text")), assistant_items)
        else:
            merged.append(copy.deepcopy(record))
    return merged


def append_assistant_response_record(
    transcript: list[dict[str, Any]],
    assistant_text: str,
    assistant_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not assistant_text and not assistant_items:
        return transcript

    next_transcript = copy.deepcopy(transcript)
    response_items = provider_items_with_record_text("assistant", assistant_text, assistant_items)
    if next_transcript and next_transcript[-1].get("role") == "assistant":
        previous = next_transcript[-1]
        previous_items = previous.get("providerItems")
        combined_items: list[dict[str, Any]] = []
        if isinstance(previous_items, list):
            combined_items.extend(copy.deepcopy(item) for item in previous_items if isinstance(item, dict))
        combined_items.extend(copy.deepcopy(item) for item in response_items if isinstance(item, dict))
        combined_text = combine_assistant_texts(compact_text(previous.get("text")), assistant_text)
        next_transcript[-1] = transcript_record("assistant", combined_text, combined_items)
        return next_transcript

    next_transcript.append(transcript_record("assistant", assistant_text, response_items))
    return next_transcript


def combine_assistant_texts(previous_text: str, next_text: str) -> str:
    previous = compact_text(previous_text).strip()
    current = compact_text(next_text).strip()
    if not previous:
        return current
    if not current or current == previous:
        return previous
    if previous.endswith(current):
        return previous
    return f"{previous}\n\n{current}"


def transcript_record_signature(record: dict[str, Any]) -> tuple[str, str, str]:
    provider_items = record.get("providerItems")
    provider_json = ""
    if isinstance(provider_items, list) and provider_items:
        provider_json = json.dumps(provider_items, sort_keys=True, ensure_ascii=False, default=str)
    return (str(record.get("role") or ""), compact_text(record.get("text")), provider_json)


def common_prefix_len(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> int:
    count = 0
    for left_record, right_record in zip(left, right):
        if transcript_record_signature(left_record) != transcript_record_signature(right_record):
            break
        count += 1
    return count


def append_non_duplicate(base: list[dict[str, Any]], tail: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = copy.deepcopy(base)
    for record in tail:
        if merged and transcript_record_signature(merged[-1]) == transcript_record_signature(record):
            continue
        merged.append(copy.deepcopy(record))
    return merged


def latest_user_turn_tail(source_transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source = clean_transcript(source_transcript)
    if not source or str(source[-1].get("role") or "") != "user":
        return []

    start = len(source) - 1
    while start > 0 and is_initial_context_prefix_record(source[start - 1]):
        start -= 1
    return copy.deepcopy(source[start:])


def merge_override_transcript(
    edited_transcript: list[dict[str, Any]],
    mirrored_transcript: list[dict[str, Any]],
    source_transcript: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    base = clean_transcript(edited_transcript)
    mirror = clean_transcript(mirrored_transcript)
    source = clean_transcript(source_transcript)
    if not source:
        return base

    if mirror:
        mirror_prefix = common_prefix_len(source, mirror)
        if mirror_prefix >= len(mirror):
            return append_non_duplicate(base, source[mirror_prefix:])

    base_prefix = common_prefix_len(source, base)
    if base and base_prefix >= len(base):
        return append_non_duplicate(base, source[base_prefix:])

    latest_tail = latest_user_turn_tail(source)
    if latest_tail:
        return append_non_duplicate(base, latest_tail)
    return base


def with_running_assistant(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    next_transcript = clean_transcript(transcript)
    if next_transcript and next_transcript[-1].get("role") != "assistant":
        next_transcript.append(
            transcript_record("assistant", "", [{"type": "message", "role": "assistant", "content": ""}])
        )
    return next_transcript


STORE = ProxyStore(STATE_PATH)


class Handler(BaseHTTPRequestHandler):
    server_version = "HashContextProxy/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[proxy] {self.address_string()} - {format % args}")

    def do_OPTIONS(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        proxy_log(
            "incoming options "
            f"path={parsed.path} "
            f"access_control_request_method={self.headers.get('access-control-request-method', '')}"
        )
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        proxy_log(
            "incoming get "
            f"path={parsed.path} "
            f"upgrade={self.headers.get('upgrade', '')} "
            f"accept={self.headers.get('accept', '')} "
            f"user_agent={self.headers.get('user-agent', '')}"
        )
        if parsed.path == "/api/proxy/sessions":
            self._send_json(STORE.list_sessions())
            return
        if parsed.path.startswith("/api/proxy/sessions/"):
            session_id = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
            session = STORE.get_session(session_id)
            if session is None:
                self._send_json({"error": "session not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(session)
            return
        if parsed.path == "/v1/models":
            self._handle_models()
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _handle_models(self) -> None:
        headers = {key.lower(): value for key, value in self.headers.items()}
        upstream_base_url = upstream_base_url_for_request(headers)
        upstream = urllib.parse.urlparse(upstream_base_url.rstrip("/") + "/models")
        proxy_log(f"models upstream={upstream.geturl()}")
        connection_cls = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
        conn = connection_cls(upstream.hostname, upstream.port, timeout=60)
        path = upstream.path or "/v1/models"
        if upstream.query:
            path = f"{path}?{upstream.query}"

        try:
            upstream_headers = json_headers_for_upstream(headers)
            proxy_log(
                "models upstream headers "
                f"{json.dumps(safe_headers_for_log(upstream_headers), ensure_ascii=False, sort_keys=True)}"
            )
            conn.request("GET", path, headers=upstream_headers)
            upstream_response = conn.getresponse()
            response_body = upstream_response.read()
            proxy_log(
                "models upstream status "
                f"status={upstream_response.status} reason={upstream_response.reason} bytes={len(response_body)}"
            )

            status = upstream_response.status
            body = normalize_models_response_body(response_body) if status < 400 else response_body
            if status >= 400 and has_effective_chatgpt_auth(headers):
                proxy_log("models upstream failed for ChatGPT auth; serving local fallback model list")
                status = HTTPStatus.OK
                body = fallback_models_response_body()

            self.send_response(status)
            self._send_cors_headers()
            for key, value in upstream_response.getheaders():
                if key.lower() in {"transfer-encoding", "connection", "content-encoding", "content-length", "content-type"}:
                    continue
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            proxy_log(f"models error error={type(exc).__name__}: {exc}")
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        finally:
            conn.close()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        proxy_log(
            "incoming post "
            f"path={parsed.path} "
            f"content_type={self.headers.get('content-type', '')} "
            f"content_encoding={self.headers.get('content-encoding', '')} "
            f"content_length={self.headers.get('content-length', '')} "
            f"expect={self.headers.get('expect', '')}"
        )
        if parsed.path == "/v1/responses":
            self._handle_responses()
            return
        if parsed.path == "/v1/responses/compact":
            self._handle_compact()
            return
        if parsed.path.endswith("/override") and parsed.path.startswith("/api/proxy/sessions/"):
            session_id = urllib.parse.unquote(parsed.path.split("/api/proxy/sessions/", 1)[1].rsplit("/", 1)[0])
            payload = self._read_json()
            transcript = payload.get("transcript")
            if not isinstance(transcript, list):
                self._send_json({"error": "transcript must be a list"}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(STORE.override(session_id, transcript))
            return
        if parsed.path.endswith("/reset") and parsed.path.startswith("/api/proxy/sessions/"):
            session_id = urllib.parse.unquote(parsed.path.split("/api/proxy/sessions/", 1)[1].rsplit("/", 1)[0])
            try:
                self._send_json(STORE.reset(session_id))
            except KeyError:
                self._send_json({"error": "session not found"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _handle_responses(self) -> None:
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        proxy_log(f"responses body bytes={len(raw_body)} prefix={raw_body[:80]!r}")
        try:
            body = parse_json_request_body(raw_body, self.headers.get("content-encoding"))
            if not isinstance(body, dict):
                raise json.JSONDecodeError("request body must be an object", "", 0)
        except json.JSONDecodeError:
            self._send_json({"error": "request body must be JSON"}, HTTPStatus.BAD_REQUEST)
            return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return

        headers = {key.lower(): value for key, value in self.headers.items()}
        is_internal_context = is_internal_context_request(headers, body)
        if is_internal_context and not has_effective_upstream_auth(headers):
            proxy_log("internal context request missing cached upstream auth")
            self._send_json(
                {
                    "error": {
                        "message": (
                            "Codex auth has not been captured by the local proxy yet. "
                            "Send one normal Codex message through this proxy first, then retry the context workbench."
                        ),
                        "type": "hash_context_auth_unavailable",
                        "code": "codex_auth_not_captured",
                    }
                },
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if is_internal_context:
            session_id = INTERNAL_CONTEXT_VALUE
            forwarded_body = copy.deepcopy(body)
        else:
            session_id = session_id_for_request(body, headers)
            _session, forwarded_body = STORE.begin_request(session_id, body, headers)
        upstream_base_url = upstream_base_url_for_request(headers)
        upstream = urllib.parse.urlparse(upstream_base_url.rstrip("/") + "/responses")
        effective_headers = apply_cached_upstream_auth(headers)
        effective_lowered = {key.lower(): value for key, value in effective_headers.items()}
        auth_kind = "chatgpt" if effective_lowered.get("chatgpt-account-id") else "api-key-or-bearer"
        proxy_log(
            f"request session={session_id} auth={auth_kind} "
            f"internal={is_internal_context} upstream={upstream.geturl()}"
        )
        connection_cls = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
        conn = connection_cls(upstream.hostname, upstream.port, timeout=120)
        path = upstream.path or "/v1/responses"
        if upstream.query:
            path = f"{path}?{upstream.query}"

        try:
            payload = json.dumps(forwarded_body, ensure_ascii=False).encode("utf-8")
            upstream_headers = upstream_headers_for_request(headers, accept="text/event-stream")
            proxy_log(
                f"upstream headers session={session_id} "
                f"{json.dumps(safe_headers_for_log(upstream_headers), ensure_ascii=False, sort_keys=True)}"
            )
            conn.request("POST", path, body=payload, headers=upstream_headers)
            upstream_response = conn.getresponse()
            proxy_log(f"upstream status session={session_id} status={upstream_response.status} reason={upstream_response.reason}")
            self.send_response(upstream_response.status)
            self._send_cors_headers()
            for key, value in upstream_response.getheaders():
                if key.lower() in {"transfer-encoding", "connection", "content-encoding", "content-length"}:
                    continue
                self.send_header(key, value)
            self.end_headers()

            response_items: list[dict[str, Any]] = []
            text_parts: list[str] = []
            buffer = ""
            error_preview = bytearray()
            while True:
                chunk = upstream_response.read(4096)
                if not chunk:
                    break
                if upstream_response.status >= 400 and len(error_preview) < 4000:
                    error_preview.extend(chunk[: 4000 - len(error_preview)])
                self.wfile.write(chunk)
                self.wfile.flush()
                buffer += chunk.decode("utf-8", errors="ignore")
                buffer = parse_sse_buffer(buffer, response_items, text_parts)

            if upstream_response.status >= 400:
                preview = error_preview.decode("utf-8", errors="replace")
                proxy_log(f"upstream error session={session_id} body={preview[:1000]!r}")
                if not is_internal_context:
                    STORE.fail_response(session_id, preview[:1000] or upstream_response.reason)
            else:
                if not is_internal_context:
                    STORE.complete_response(session_id, response_items, "".join(text_parts))
        except Exception as exc:
            proxy_log(f"error session={session_id} error={type(exc).__name__}: {exc}")
            if not is_internal_context:
                STORE.fail_response(session_id, str(exc))
            if not self.wfile.closed:
                try:
                    self._send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
                except Exception:
                    pass
        finally:
            conn.close()

    def _handle_compact(self) -> None:
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        proxy_log(f"compact body bytes={len(raw_body)} prefix={raw_body[:80]!r}")
        try:
            body = parse_json_request_body(raw_body, self.headers.get("content-encoding"))
            if not isinstance(body, dict):
                raise json.JSONDecodeError("request body must be an object", "", 0)
        except json.JSONDecodeError:
            self._send_json({"error": "request body must be JSON object"}, HTTPStatus.BAD_REQUEST)
            return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return

        headers = {key.lower(): value for key, value in self.headers.items()}
        session_id = session_id_for_compact_request(body, headers, STORE.active_session_id)
        _session, forwarded_body = STORE.begin_compact(session_id, body, headers)
        upstream_base_url = upstream_base_url_for_request(headers)
        upstream = urllib.parse.urlparse(upstream_base_url.rstrip("/") + "/responses/compact")
        auth_kind = "chatgpt" if headers.get("chatgpt-account-id") else "api-key-or-bearer"
        proxy_log(f"compact session={session_id} auth={auth_kind} upstream={upstream.geturl()}")
        connection_cls = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
        conn = connection_cls(upstream.hostname, upstream.port, timeout=180)
        path = upstream.path or "/v1/responses/compact"
        if upstream.query:
            path = f"{path}?{upstream.query}"

        try:
            payload = json.dumps(forwarded_body, ensure_ascii=False).encode("utf-8")
            upstream_headers = json_headers_for_upstream(headers)
            proxy_log(
                f"compact upstream headers session={session_id} "
                f"{json.dumps(safe_headers_for_log(upstream_headers), ensure_ascii=False, sort_keys=True)}"
            )
            conn.request("POST", path, body=payload, headers=upstream_headers)
            upstream_response = conn.getresponse()
            response_body = upstream_response.read()
            proxy_log(
                f"compact upstream status session={session_id} "
                f"status={upstream_response.status} reason={upstream_response.reason} bytes={len(response_body)}"
            )

            self.send_response(upstream_response.status)
            self._send_cors_headers()
            for key, value in upstream_response.getheaders():
                if key.lower() in {"transfer-encoding", "connection", "content-encoding"}:
                    continue
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response_body)

            if upstream_response.status >= 400:
                preview = response_body.decode("utf-8", errors="replace")
                proxy_log(f"compact upstream error session={session_id} body={preview[:1000]!r}")
                STORE.fail_response(session_id, preview[:1000] or upstream_response.reason)
                return

            try:
                parsed_body = json.loads(response_body.decode("utf-8"))
            except json.JSONDecodeError as exc:
                STORE.fail_response(session_id, f"compact response was not JSON: {exc}")
                return
            output = parsed_body.get("output") if isinstance(parsed_body, dict) else None
            if not isinstance(output, list):
                STORE.fail_response(session_id, "compact response did not include output list")
                return
            output_items = [copy.deepcopy(item) for item in output if isinstance(item, dict)]
            STORE.complete_compact(session_id, output_items)
        except Exception as exc:
            proxy_log(f"compact error session={session_id} error={type(exc).__name__}: {exc}")
            STORE.fail_response(session_id, str(exc))
            if not self.wfile.closed:
                try:
                    self._send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
                except Exception:
                    pass
        finally:
            conn.close()

    def _read_json(self) -> dict[str, Any]:
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        if not raw_body:
            return {}
        data = parse_json_request_body(raw_body, self.headers.get("content-encoding"))
        return data if isinstance(data, dict) else {}

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type, x-hash-context-session-id")

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def parse_sse_buffer(buffer: str, response_items: list[dict[str, Any]], text_parts: list[str]) -> str:
    def replace_or_append_item(next_item: dict[str, Any]) -> None:
        item_id = str(next_item.get("id") or "").strip()
        call_id = str(next_item.get("call_id") or "").strip()
        for index, existing in enumerate(response_items):
            if not isinstance(existing, dict):
                continue
            existing_id = str(existing.get("id") or "").strip()
            existing_call_id = str(existing.get("call_id") or "").strip()
            if (item_id and existing_id == item_id) or (call_id and existing_call_id == call_id):
                response_items[index] = next_item
                return
        response_items.append(next_item)

    def item_for_delta(event: dict[str, Any]) -> dict[str, Any] | None:
        item_id = str(event.get("item_id") or "").strip()
        output_index = event.get("output_index")
        if item_id:
            for item in response_items:
                if isinstance(item, dict) and str(item.get("id") or "").strip() == item_id:
                    return item
        if isinstance(output_index, int) and 0 <= output_index < len(response_items):
            item = response_items[output_index]
            return item if isinstance(item, dict) else None
        for item in reversed(response_items):
            if isinstance(item, dict) and str(item.get("type") or "") == "function_call":
                return item
        return None

    while "\n\n" in buffer:
        block, buffer = buffer.split("\n\n", 1)
        data_lines = [line[5:].strip() for line in block.splitlines() if line.startswith("data:")]
        if not data_lines:
            continue
        raw = "\n".join(data_lines)
        if raw == "[DONE]":
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type in {"response.output_text.delta", "response.refusal.delta"}:
            delta = event.get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)
        if event_type == "response.output_item.added" and isinstance(event.get("item"), dict):
            replace_or_append_item(event["item"])
        if event_type == "response.output_item.done" and isinstance(event.get("item"), dict):
            replace_or_append_item(event["item"])
        if event_type == "response.function_call_arguments.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                target_item = item_for_delta(event)
                if target_item is not None and str(target_item.get("type") or "") == "function_call":
                    target_item["arguments"] = f"{target_item.get('arguments') or ''}{delta}"
        response = event.get("response")
        if event_type == "response.completed" and isinstance(response, dict):
            output = response.get("output")
            if isinstance(output, list):
                completed_items: list[dict[str, Any]] = []
                for item in output:
                    if isinstance(item, dict):
                        completed_items.append(item)
                if completed_items:
                    response_items[:] = completed_items
    return buffer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if preload_codex_subscription_auth():
        proxy_log("preloaded Codex subscription auth from local auth.json")
    elif preload_openai_api_auth():
        proxy_log("preloaded OpenAI API auth from OPENAI_API_KEY")
    else:
        proxy_log("local Codex auth was not available at proxy startup")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Hash Context proxy listening on http://{args.host}:{args.port}")
    print(f"OpenAI API upstream: {OPENAI_UPSTREAM_BASE_URL.rstrip()}/responses")
    print(f"ChatGPT upstream: {CHATGPT_UPSTREAM_BASE_URL.rstrip()}/responses")
    server.serve_forever()


if __name__ == "__main__":
    main()
