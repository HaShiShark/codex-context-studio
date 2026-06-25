from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import http.client
import io
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from codex_context import (
    conversation_record_count,
    is_context_control_command_text,
    is_contextual_user_text,
)



HOST = os.environ.get("HASH_CONTEXT_PROXY_HOST", os.environ.get("HASH_CONTEXT_HOST", "localhost"))
PORT = int(os.environ.get("HASH_CONTEXT_PROXY_PORT", "8787"))
OPENAI_UPSTREAM_BASE_URL = os.environ.get(
    "HASH_CONTEXT_OPENAI_UPSTREAM_BASE_URL",
    os.environ.get("HASH_CONTEXT_UPSTREAM_BASE_URL", "https://api.openai.com/v1"),
)
CHATGPT_UPSTREAM_BASE_URL = os.environ.get(
    "HASH_CONTEXT_CHATGPT_UPSTREAM_BASE_URL",
    "https://chatgpt.com/backend-api/codex",
)
FORCE_UPSTREAM_BASE_URL = os.environ.get("HASH_CONTEXT_FORCE_UPSTREAM_BASE_URL", "").strip()
FORCE_UPSTREAM_API_KEY = os.environ.get("HASH_CONTEXT_FORCE_UPSTREAM_API_KEY", "").strip()
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("HASH_CONTEXT_PROXY_DATA_DIR", REPO_ROOT / "data"))
STATE_PATH = DATA_DIR / "proxy_state.json"
LOG_PATH = DATA_DIR / "proxy.log"
CODEX_PROXY_PROVIDER_ID = "codex-proxy"
CODEX_PROXY_BASE_URL = f"http://{HOST}:{PORT}/v1"
INTERNAL_CONTEXT_HEADER = "x-hash-context-internal"
INTERNAL_CONTEXT_VALUE = "context-workbench"
CONTEXT_CONTROL_NOTICE_TEXT = "Hash Context: opened workbench."
CONTROL_PORT = int(os.environ.get("HASH_CONTEXT_CONTROL_PORT", "8790"))
LOCAL_COMPACT_PROMPT_PREFIX = "You are performing a CONTEXT CHECKPOINT COMPACTION."
LOCAL_COMPACT_SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary of its thinking process. "
    "You also have access to the state of the tools that were used by that language model. "
    "Use this to build on the work that has already been done and avoid duplicating work. "
    "Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:"
)
LOCAL_COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000
MANUAL_LOCAL_COMPACT_PROMPT = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly.

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests or really old requests that were already completed without confirming with the user first.
                       If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure there's no drift in task interpretation.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]
   - [...]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Summary of the changes made to this file, if any]
      - [Important Code Snippet]
   - [File Name 2]
      - [Important Code Snippet]
   - [...]

4. Errors and fixes:
    - [Detailed description of error 1]:
      - [How you fixed the error]
      - [User feedback on the error if any]
    - [...]

5. Problem Solving:
   [Description of solved problems and ongoing troubleshooting]

6. All user messages:
    - [Detailed non tool use user message]
    - [...]

7. Pending Tasks:
   - [Task 1]
   - [Task 2]
   - [...]

8. Current Work:
   [Precise description of current work]

9. Optional Next Step:
   [Optional Next step to take]

</summary>
</example>

Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response.

There may be additional summarization instructions provided in the included context. If so, remember to follow these instructions when creating the above summary. Examples of instructions include:
<example>
## Compact Instructions
When summarizing the conversation focus on typescript code changes and also remember the mistakes you made and how you fixed them.
</example>

<example>
# Summary instructions
When you are using compact - please focus on test output and code changes. Include file reads verbatim.
</example>"""
AUTO_LOCAL_COMPACT_PROMPT = """You have been working on the task described above but have not yet completed it. Write a continuation summary that will allow you (or another instance of yourself) to resume work efficiently in a future context window where the conversation history will be replaced with this summary. Your summary should be structured, concise, and actionable. Include:
1. Task Overview
The user's core request and success criteria
Any clarifications or constraints they specified
2. Current State
What has been completed so far
Files created, modified, or analyzed (with paths if relevant)
Key outputs or artifacts produced
3. Important Discoveries
Technical constraints or requirements uncovered
Decisions made and their rationale
Errors encountered and how they were resolved
What approaches were tried that didn't work (and why)
4. Next Steps
Specific actions needed to complete the task
Any blockers or open questions to resolve
Priority order if multiple steps remain
5. Context to Preserve
User preferences or style requirements
Domain-specific details that aren't obvious
Any promises made to the user
Be concise but complete—err on the side of including information that would prevent duplicate work or repeated mistakes. Write in a way that enables immediate resumption of the task.
Wrap your summary in <summary></summary> tags."""
CUSTOM_LOCAL_COMPACT_PROMPTS = (MANUAL_LOCAL_COMPACT_PROMPT, AUTO_LOCAL_COMPACT_PROMPT)
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


def web_search_action_display_detail(action: Any) -> str:
    if not isinstance(action, dict):
        return compact_text(action)

    action_type = str(action.get("type") or "").strip()
    if action_type == "search":
        query = compact_text(action.get("query")).strip()
        if query:
            return query
        queries = action.get("queries")
        if isinstance(queries, list):
            query_parts = [compact_text(query).strip() for query in queries]
            return ", ".join(query for query in query_parts if query)
    if action_type == "open_page":
        return compact_text(action.get("url")).strip()
    if action_type == "find_in_page":
        pattern = compact_text(action.get("pattern")).strip()
        url = compact_text(action.get("url")).strip()
        if pattern and url:
            return f"{pattern} in {url}"
        return pattern or url

    fallback = action.get("query") or action.get("url") or action_type or action
    return compact_text(fallback).strip()


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
    if item_type == "web_search_call":
        return web_search_action_display_detail(item.get("action"))
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


def context_control_command_from_input(input_items: Any) -> str:
    transcript = input_items_to_transcript(input_items)
    for record in reversed(transcript):
        if str(record.get("role") or "").strip() != "user":
            continue
        text = compact_text(record.get("text") or "").strip()
        return text if is_context_control_command_text(text) else ""
    return ""


CODEX_UI_TITLE_GENERATION_MARKERS = (
    "you are a helpful assistant. you will be presented with a user prompt",
    "provide a short title for a task that will be created from that prompt",
    "generate a concise ui title",
    "fill the structured title field with plain text",
    "user prompt:",
)
HASH_CONTEXT_TITLE_PROMPT_MARKER = "请根据下面这条新对话的第一条用户消息，生成一个对话标题。"
HASH_CONTEXT_TITLE_INSTRUCTION_MARKERS = (
    "你只负责给一段新对话起标题。",
    "标题要短、具体、自然，优先使用用户的语言。",
    "最多 18 个中文字符或 8 个英文单词。",
)


def normalize_detection_text(value: str) -> str:
    return " ".join(value.lower().split())


def is_title_generation_request(body: dict[str, Any]) -> bool:
    transcript = input_items_to_transcript(body.get("input") if isinstance(body, dict) else None)
    user_texts = [
        compact_text(record.get("text") or "")
        for record in transcript
        if str(record.get("role") or "").strip() == "user"
    ]
    if not user_texts:
        return False

    for text in user_texts:
        normalized = normalize_detection_text(text)
        if all(marker in normalized for marker in CODEX_UI_TITLE_GENERATION_MARKERS):
            return True

    instructions = compact_text(body.get("instructions") if isinstance(body, dict) else "")
    if any(HASH_CONTEXT_TITLE_PROMPT_MARKER in text for text in user_texts) and all(
        marker in instructions for marker in HASH_CONTEXT_TITLE_INSTRUCTION_MARKERS
    ):
        return True

    return False


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


CODEX_SESSION_HEADER_NAMES = {
    "session-id",
    "session_id",
    "thread-id",
    "thread_id",
    "x-client-request-id",
    "x-codex-beta-features",
    "x-codex-installation-id",
    "x-codex-parent-thread-id",
    "x-codex-turn-metadata",
    "x-codex-turn-state",
    "x-codex-window-id",
    "x-openai-subagent",
}


def codex_session_headers_from_request(headers: dict[str, str]) -> dict[str, str]:
    return {
        key.lower(): str(value)
        for key, value in headers.items()
        if key.lower() in CODEX_SESSION_HEADER_NAMES and str(value).strip()
    }


def fallback_codex_session_headers(session_id: str) -> dict[str, str]:
    safe_session_id = sanitize_id(session_id)
    if not safe_session_id:
        return {}
    return {
        "session-id": safe_session_id,
        "session_id": safe_session_id,
        "thread-id": safe_session_id,
        "thread_id": safe_session_id,
        "x-client-request-id": safe_session_id,
        "x-codex-window-id": f"{safe_session_id}:0",
        "x-codex-turn-metadata": json.dumps(
            {
                "session_id": safe_session_id,
                "thread_id": safe_session_id,
                "thread_source": "hash_context_workbench",
                "turn_id": f"hash-context-{safe_session_id}",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def merge_codex_session_headers(
    headers: dict[str, str],
    session_headers: dict[str, str],
    *,
    session_id: str = "",
) -> dict[str, str]:
    merged = dict(headers)
    effective_session_headers = dict(session_headers)
    for key, value in fallback_codex_session_headers(session_id).items():
        effective_session_headers.setdefault(key, value)
    lowered_existing = {key.lower() for key in merged}
    for key, value in effective_session_headers.items():
        lower_key = key.lower()
        if lower_key in lowered_existing or not str(value).strip():
            continue
        merged[lower_key] = str(value)
    return merged


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
        "x-hash-context-session-id",
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


def preload_force_upstream_auth() -> bool:
    if not FORCE_UPSTREAM_API_KEY:
        return False
    headers = {
        "Authorization": f"Bearer {FORCE_UPSTREAM_API_KEY}",
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
    if FORCE_UPSTREAM_BASE_URL:
        return FORCE_UPSTREAM_BASE_URL
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


def open_context_workbench(session_id: str) -> tuple[bool, str]:
    path = "/show"
    if session_id:
        path = f"{path}?session_id={urllib.parse.quote(session_id, safe='')}"
    conn = http.client.HTTPConnection(os.environ.get("HASH_CONTEXT_HOST", "localhost"), CONTROL_PORT, timeout=2)
    try:
        conn.request("POST", path, body=b"", headers={"Content-Length": "0"})
        response = conn.getresponse()
        preview = response.read(1000).decode("utf-8", errors="replace")
        if 200 <= response.status < 300:
            return True, ""
        return False, f"window-control returned {response.status}: {preview}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


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

    ctx = _extract_context_window(item)
    if ctx is not None:
        normalized["context_window"] = ctx

    return normalized


def _extract_context_window(item: dict[str, Any]) -> int | None:
    if isinstance(item.get("context_window"), (int, float)):
        return int(item["context_window"])
    if isinstance(item.get("max_context_length"), (int, float)):
        return int(item["max_context_length"])
    if isinstance(item.get("max_context_window"), (int, float)):
        return int(item["max_context_window"])
    meta = item.get("meta") or item.get("metadata")
    if isinstance(meta, dict):
        for key in ("context_window", "max_context_length", "max_context_window"):
            if isinstance(meta.get(key), (int, float)):
                return int(meta[key])
    return None


DEFAULT_MODEL_CONTEXT_WINDOW = 200000


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
        if "context_window" not in record or not isinstance(record.get("context_window"), (int, float)):
            record["context_window"] = DEFAULT_MODEL_CONTEXT_WINDOW
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
                "context_window": DEFAULT_MODEL_CONTEXT_WINDOW,
            }
            for model_id in model_ids
        ],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def is_context_edit_notice_text(text: str) -> bool:
    compact = " ".join(str(text or "").split()).strip()
    return compact.startswith("Hash Context: context has been edited")


def is_context_control_notice_text(text: str) -> bool:
    compact = " ".join(str(text or "").split()).strip()
    return compact == CONTEXT_CONTROL_NOTICE_TEXT


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
        role = str(record.get("role") or "").strip()
        text = compact_text(record.get("text") or "")
        if role == "user" and is_context_control_command_text(text):
            continue

        if role != "assistant":
            next_transcript.append(record)
            continue

        if is_context_control_notice_text(text):
            continue

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
    normalized = " ".join(str(text or "").split())
    return normalized.startswith(LOCAL_COMPACT_PROMPT_PREFIX) or str(text or "") in CUSTOM_LOCAL_COMPACT_PROMPTS


def is_local_compact_summary_text(text: str) -> bool:
    return str(text or "").startswith(f"{LOCAL_COMPACT_SUMMARY_PREFIX}\n\n")


def is_local_compact_summary_record(record: dict[str, Any]) -> bool:
    return (
        str(record.get("role") or "") == "user"
        and is_local_compact_summary_text(compact_text(record.get("text")))
    )


def local_compact_summary_text(record: dict[str, Any]) -> str:
    return compact_text(record.get("text")) if is_local_compact_summary_record(record) else ""


def dedupe_local_compact_summary_records(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary_indexes = [
        index
        for index, record in enumerate(transcript)
        if is_local_compact_summary_record(record)
    ]
    if len(summary_indexes) <= 1:
        return copy.deepcopy(transcript)

    keep_index = summary_indexes[-1]
    return [
        copy.deepcopy(record)
        for index, record in enumerate(transcript)
        if not is_local_compact_summary_record(record) or index == keep_index
    ]


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


def should_replace_transcript_from_control_intercept(
    existing_transcript: list[dict[str, Any]],
    candidate_transcript: list[dict[str, Any]],
) -> bool:
    return conversation_record_count(candidate_transcript) >= conversation_record_count(existing_transcript)


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


def request_turn_metadata(headers: dict[str, str]) -> str:
    return str(headers.get("x-codex-turn-metadata") or headers.get("X-Codex-Turn-Metadata") or "")


def assistant_record_ends_with_tool_activity(record: dict[str, Any]) -> bool:
    if str(record.get("role") or "") != "assistant":
        return False
    provider_items = record.get("providerItems")
    if not isinstance(provider_items, list):
        return False
    for item in reversed(provider_items):
        if not isinstance(item, dict):
            continue
        return str(item.get("type") or "") in TOOL_CALL_ITEM_TYPES | TOOL_OUTPUT_ITEM_TYPES
    return False


def is_same_turn_local_compact(
    current_turn_metadata: str,
    previous_turn_metadata: str,
) -> bool:
    return bool(current_turn_metadata and previous_turn_metadata and current_turn_metadata == previous_turn_metadata)


def is_auto_local_compact_source(
    source_transcript: list[dict[str, Any]],
    current_turn_metadata: str = "",
    previous_turn_metadata: str = "",
) -> bool:
    if is_same_turn_local_compact(current_turn_metadata, previous_turn_metadata):
        return True
    source = clean_transcript(source_transcript)
    if not source:
        return False
    return assistant_record_ends_with_tool_activity(source[-1])


def replacement_local_compact_prompt(
    source_transcript: list[dict[str, Any]],
    current_turn_metadata: str = "",
    previous_turn_metadata: str = "",
) -> str:
    if is_auto_local_compact_source(source_transcript, current_turn_metadata, previous_turn_metadata):
        return AUTO_LOCAL_COMPACT_PROMPT
    return MANUAL_LOCAL_COMPACT_PROMPT


def replace_last_local_compact_prompt(
    transcript: list[dict[str, Any]],
    replacement_prompt: str,
) -> list[dict[str, Any]]:
    records = clean_transcript(transcript)
    if not records:
        return records
    last_record = records[-1]
    if str(last_record.get("role") or "") != "user":
        return records
    if not is_local_compact_prompt_text(compact_text(last_record.get("text"))):
        return records
    records[-1] = transcript_record("user", replacement_prompt, [provider_message("user", replacement_prompt)])
    return clean_transcript(records)


def message_item_with_text(item: dict[str, Any], text: str) -> dict[str, Any]:
    next_item = copy.deepcopy(item)
    content = next_item.get("content")
    if isinstance(content, str):
        next_item["content"] = text
    elif isinstance(content, list):
        replaced = False
        next_content: list[Any] = []
        for content_item in content:
            if isinstance(content_item, dict) and not replaced and (
                "text" in content_item
                or content_item.get("type") in {"input_text", "output_text"}
            ):
                next_content.append({**content_item, "text": text})
                replaced = True
            else:
                next_content.append(copy.deepcopy(content_item))
        next_item["content"] = next_content if replaced else [{"type": "input_text", "text": text}]
    elif isinstance(content, dict):
        if "text" in content or content.get("type") in {"input_text", "output_text"}:
            next_item["content"] = {**content, "text": text}
        else:
            next_item["content"] = {"type": "input_text", "text": text}
    else:
        next_item["content"] = text
    if "text" in next_item:
        next_item["text"] = text
    return next_item


def replace_last_local_compact_prompt_input(input_items: Any, replacement_prompt: str) -> Any:
    if isinstance(input_items, str):
        return replacement_prompt if is_local_compact_prompt_text(input_items) else input_items
    if not isinstance(input_items, list):
        return input_items
    replaced_items = [copy.deepcopy(item) for item in input_items]
    for index in range(len(replaced_items) - 1, -1, -1):
        item = replaced_items[index]
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message" or item.get("role") != "user":
            return input_items
        if not is_local_compact_prompt_text(read_message_text(item)):
            return input_items
        replaced_items[index] = message_item_with_text(item, replacement_prompt)
        return replaced_items
    return input_items


def local_compact_approx_token_count(text: str) -> int:
    return max(1, (len(str(text or "")) + 3) // 4)


def truncate_text_to_approx_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    return str(text or "")[: max_tokens * 4]


def local_compacted_transcript(source_transcript: list[dict[str, Any]], assistant_summary_text: str) -> list[dict[str, Any]]:
    user_messages: list[str] = []
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
        user_messages.append(text)

    selected_messages: list[str] = []
    remaining_tokens = LOCAL_COMPACT_USER_MESSAGE_MAX_TOKENS
    for message in reversed(user_messages):
        if remaining_tokens <= 0:
            break
        tokens = local_compact_approx_token_count(message)
        if tokens <= remaining_tokens:
            selected_messages.append(message)
            remaining_tokens = max(0, remaining_tokens - tokens)
        else:
            selected_messages.append(truncate_text_to_approx_tokens(message, remaining_tokens))
            break
    selected_messages.reverse()

    retained = [
        transcript_record("user", message, [provider_message("user", message)])
        for message in selected_messages
        if message
    ]
    summary_text = f"{LOCAL_COMPACT_SUMMARY_PREFIX}\n\n{assistant_summary_text or ''}"
    retained.append(transcript_record("user", summary_text, [provider_message("user", summary_text)]))
    return clean_transcript(retained)


USAGE_EVENT_LIMIT = 500


def usage_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(float(value.strip())))
        except ValueError:
            return 0
    return 0


def usage_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str):
        try:
            return max(0.0, float(value.strip()))
        except ValueError:
            return None
    return None


def first_usage_int(record: dict[str, Any], *keys: str) -> int:
    for key in keys:
        if key in record:
            value = usage_int(record.get(key))
            if value:
                return value
    return 0


GPT55_INPUT_USD_PER_MILLION = 1.25
GPT55_CACHED_INPUT_USD_PER_MILLION = 0.125
GPT55_OUTPUT_USD_PER_MILLION = 10.0


def estimate_gpt55_cost_usd(input_tokens: int, cached_input_tokens: int, output_tokens: int) -> float:
    cached_tokens = min(max(0, cached_input_tokens), max(0, input_tokens))
    non_cached_tokens = max(0, input_tokens - cached_tokens)
    return (
        (non_cached_tokens * GPT55_INPUT_USD_PER_MILLION)
        + (cached_tokens * GPT55_CACHED_INPUT_USD_PER_MILLION)
        + (max(0, output_tokens) * GPT55_OUTPUT_USD_PER_MILLION)
    ) / 1_000_000


def empty_usage_bucket() -> dict[str, Any]:
    return {
        "request_count": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "non_cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "known_cost_usd": 0,
        "unknown_cost_request_count": 0,
        "cache_hit_rate": 0,
    }


def normalize_usage_payload(raw_usage: Any) -> dict[str, Any] | None:
    if not isinstance(raw_usage, dict):
        return None

    input_details = raw_usage.get("input_tokens_details") or raw_usage.get("prompt_tokens_details")
    if not isinstance(input_details, dict):
        input_details = {}
    output_details = raw_usage.get("output_tokens_details") or raw_usage.get("completion_tokens_details")
    if not isinstance(output_details, dict):
        output_details = {}

    input_tokens = first_usage_int(raw_usage, "input_tokens", "prompt_tokens")
    output_tokens = first_usage_int(raw_usage, "output_tokens", "completion_tokens")
    cached_input_tokens = first_usage_int(
        raw_usage,
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cached_tokens",
    ) or first_usage_int(
        input_details,
        "cached_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
    )
    reasoning_tokens = first_usage_int(
        raw_usage,
        "reasoning_tokens",
        "reasoning_output_tokens",
    ) or first_usage_int(
        output_details,
        "reasoning_tokens",
        "reasoning_output_tokens",
    )
    total_tokens = first_usage_int(raw_usage, "total_tokens") or input_tokens + output_tokens
    if not any((input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, total_tokens)):
        return None

    bucket = empty_usage_bucket()
    bucket.update(
        {
            "input_tokens": input_tokens,
            "cached_input_tokens": min(cached_input_tokens, input_tokens) if input_tokens else cached_input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": total_tokens,
            "unknown_cost_request_count": 0,
        }
    )
    bucket["non_cached_input_tokens"] = max(0, bucket["input_tokens"] - bucket["cached_input_tokens"])
    bucket["known_cost_usd"] = estimate_gpt55_cost_usd(
        bucket["input_tokens"],
        bucket["cached_input_tokens"],
        bucket["output_tokens"],
    )
    bucket["cache_hit_rate"] = (
        bucket["cached_input_tokens"] / bucket["input_tokens"] if bucket["input_tokens"] else 0
    )
    return bucket


def usage_event(kind: str, model: str, raw_usage: Any) -> dict[str, Any] | None:
    usage = normalize_usage_payload(raw_usage)
    if usage is None:
        return None
    return {
        "created_at": utc_timestamp(),
        "kind": kind,
        "model": model.strip() or "unknown",
        "usage": usage,
    }


def add_usage_to_bucket(bucket: dict[str, Any], usage: dict[str, Any], created_at: str) -> None:
    bucket["request_count"] += 1
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "non_cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    ):
        bucket[key] += usage_int(usage.get(key))
    bucket["known_cost_usd"] += estimate_gpt55_cost_usd(
        usage_int(usage.get("input_tokens")),
        usage_int(usage.get("cached_input_tokens")),
        usage_int(usage.get("output_tokens")),
    )
    bucket["unknown_cost_request_count"] = 0
    bucket["cache_hit_rate"] = (
        bucket["cached_input_tokens"] / bucket["input_tokens"] if bucket["input_tokens"] else 0
    )
    if created_at and (not bucket.get("latest_at") or created_at > str(bucket.get("latest_at") or "")):
        bucket["latest_at"] = created_at


def usage_summary_from_events(session_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    summary = empty_usage_bucket()
    summary["session_id"] = session_id
    by_kind: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}

    for event in events:
        if not isinstance(event, dict):
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        created_at = str(event.get("created_at") or "")
        kind = str(event.get("kind") or "unknown").strip() or "unknown"
        model = str(event.get("model") or "unknown").strip() or "unknown"
        add_usage_to_bucket(summary, usage, created_at)
        kind_bucket = by_kind.setdefault(kind, empty_usage_bucket())
        add_usage_to_bucket(kind_bucket, usage, created_at)
        model_bucket = by_model.setdefault(model, empty_usage_bucket())
        add_usage_to_bucket(model_bucket, usage, created_at)

    summary["by_kind"] = by_kind
    summary["by_model"] = by_model
    return summary


def usage_summary_with_event(session_id: str, current_summary: dict[str, Any] | None, event: dict[str, Any]) -> dict[str, Any]:
    summary = copy.deepcopy(current_summary) if isinstance(current_summary, dict) else usage_summary_from_events(session_id, [])
    summary["session_id"] = session_id
    usage = event.get("usage") if isinstance(event, dict) else None
    if not isinstance(usage, dict):
        return summary
    created_at = str(event.get("created_at") or "")
    kind = str(event.get("kind") or "unknown").strip() or "unknown"
    model = str(event.get("model") or "unknown").strip() or "unknown"
    by_kind = summary.setdefault("by_kind", {})
    if not isinstance(by_kind, dict):
        by_kind = {}
        summary["by_kind"] = by_kind
    by_model = summary.setdefault("by_model", {})
    if not isinstance(by_model, dict):
        by_model = {}
        summary["by_model"] = by_model
    add_usage_to_bucket(summary, usage, created_at)
    kind_bucket = by_kind.setdefault(kind, empty_usage_bucket())
    add_usage_to_bucket(kind_bucket, usage, created_at)
    model_bucket = by_model.setdefault(model, empty_usage_bucket())
    add_usage_to_bucket(model_bucket, usage, created_at)
    return summary


def json_dumps_compact(value: Any) -> str:
    return json.dumps(sanitize_json_value(value), ensure_ascii=False, separators=(",", ":"))


def json_loads_value(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return copy.deepcopy(default)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return copy.deepcopy(default)


def sanitize_json_value(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def safe_session_path_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return safe or uuid.uuid4().hex


def session_date_parts(created_at: str) -> tuple[str, str, str]:
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(created_at or ""))
    if match:
        return match.group(1), match.group(2), match.group(3)
    fallback = utc_timestamp()
    return fallback[:4], fallback[5:7], fallback[8:10]


def append_jsonl_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json_dumps_compact(payload))
        handle.write("\n")


def load_jsonl_state(path: Path, default: Any) -> Any:
    state = copy.deepcopy(default)
    if not path.exists():
        return state
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return state
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "")
            if event_type == "clear":
                state = None
            elif event_type == "set":
                state = copy.deepcopy(event.get("records", []))
            elif event_type == "append":
                records = event.get("records")
                if isinstance(state, list) and isinstance(records, list):
                    state = [*state, *copy.deepcopy(records)]
                elif isinstance(records, list):
                    state = copy.deepcopy(records)
            elif event_type == "replace_from":
                records = event.get("records")
                index = event.get("index")
                if isinstance(records, list):
                    try:
                        safe_index = int(index)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(state, list):
                        state = []
                    safe_index = max(0, min(safe_index, len(state)))
                    state = [*state[:safe_index], *copy.deepcopy(records)]
    return copy.deepcopy(default) if state is None and default is not None else state


@dataclass
class ProxySession:
    id: str
    title: str
    status: str = "mirror"
    transcript: list[dict[str, Any]] = field(default_factory=list)
    override_base_transcript: list[dict[str, Any]] | None = None
    edited_transcript: list[dict[str, Any]] | None = None
    pending_transcript: list[dict[str, Any]] | None = None
    local_compact_source_transcript: list[dict[str, Any]] | None = None
    request_log: list[dict[str, Any]] = field(default_factory=list)
    response_items: list[dict[str, Any]] = field(default_factory=list)
    usage_events: list[dict[str, Any]] = field(default_factory=list)
    usage_summary_cache: dict[str, Any] | None = None
    last_codex_session_headers: dict[str, str] = field(default_factory=dict)
    last_turn_metadata_header: str = ""
    last_error: str = ""
    created_at: str = field(default_factory=utc_timestamp)
    updated_at: str = field(default_factory=utc_timestamp)
    payloads_loaded: bool = True

    def visible_transcript(self) -> list[dict[str, Any]]:
        if self.pending_transcript is not None:
            return self.pending_transcript
        if self.edited_transcript is not None:
            return self.edited_transcript
        return self.transcript

    def usage_summary(self) -> dict[str, Any]:
        if isinstance(self.usage_summary_cache, dict):
            summary = copy.deepcopy(self.usage_summary_cache)
            summary["session_id"] = self.id
            summary.setdefault("by_kind", {})
            summary.setdefault("by_model", {})
            return summary
        return usage_summary_from_events(self.id, self.usage_events)

    def metadata_payload(self) -> dict[str, Any]:
        has_override = self.edited_transcript is not None if self.payloads_loaded else self.status == "override"
        active_context_source = "raw"
        if self.payloads_loaded:
            active_context_source = (
                "pending"
                if self.pending_transcript is not None
                else "committed"
                if self.edited_transcript is not None
                else "raw"
            )
        elif self.status in {"running", "compacting"}:
            active_context_source = "pending"
        elif self.status == "override":
            active_context_source = "committed"
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "active_context_source": active_context_source,
            "has_override": has_override,
            "is_running": self.status in {"running", "compacting"},
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "usage_summary": self.usage_summary(),
        }

    def to_payload(self) -> dict[str, Any]:
        visible_transcript = self.visible_transcript()
        return {
            **self.metadata_payload(),
            "transcript": visible_transcript,
            "active_transcript": visible_transcript,
            "raw_transcript": self.transcript,
            "edited_transcript": self.edited_transcript,
            "pending_transcript": self.pending_transcript,
        }

    def should_expose(self) -> bool:
        return not self.id.startswith("session-")


class ProxyStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sqlite_path = path.with_suffix(".sqlite3")
        self.lock = threading.Lock()
        self.sessions: dict[str, ProxySession] = {}
        self.active_session_id = ""
        self._persisted_payloads: dict[str, dict[str, list[dict[str, Any]] | None]] = {}
        self._init_db()
        self.load()

    @contextmanager
    def _connect(self):
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.sqlite_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    session_dir TEXT NOT NULL DEFAULT '',
                    last_codex_session_headers_json TEXT NOT NULL DEFAULT '{}',
                    last_turn_metadata_header TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_events (
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, position)
                );
                CREATE TABLE IF NOT EXISTS usage_summaries (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                    summary_json TEXT NOT NULL
                );
                """
            )
            self._ensure_column(conn, "sessions", "session_dir", "TEXT NOT NULL DEFAULT ''")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _sessions_root(self) -> Path:
        return self.sqlite_path.parent / "sessions"

    def _session_dir(self, session: ProxySession) -> Path:
        year, month, day = session_date_parts(session.created_at)
        return self._sessions_root() / year / month / day / safe_session_path_part(session.id)

    def _state_file(self, session: ProxySession, kind: str) -> Path:
        return self._session_dir(session) / f"{kind}.jsonl"

    def _v2_state_file(self, session: ProxySession, kind: str) -> Path:
        if kind == "pending":
            return self._session_dir(session) / "pending" / "active.jsonl"
        return self._session_dir(session) / "branches" / f"{kind}.jsonl"

    def _storage_manifest_file(self, session: ProxySession) -> Path:
        return self._session_dir(session) / "storage.json"

    def _write_session_storage_manifest(self, session: ProxySession) -> None:
        path = self._storage_manifest_file(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json_dumps_compact(
                {
                    "version": 2,
                    "updated_at": utc_timestamp(),
                    "layout": {
                        "raw": "branches/raw.jsonl",
                        "edited": "branches/edited.jsonl",
                        "override_base": "branches/override_base.jsonl",
                        "pending": "pending/active.jsonl",
                    },
                }
            ),
            encoding="utf-8",
        )

    def _load_required_payload_file(self, session: ProxySession, kind: str, default: list[dict[str, Any]]) -> list[dict[str, Any]]:
        v2_path = self._v2_state_file(session, kind)
        loaded = load_jsonl_state(v2_path, default)
        return loaded if isinstance(loaded, list) else copy.deepcopy(default)

    def _load_optional_payload_file(self, session: ProxySession, kind: str) -> list[dict[str, Any]] | None:
        v2_path = self._v2_state_file(session, kind)
        if v2_path.exists():
            loaded = load_jsonl_state(v2_path, None)
            return loaded if isinstance(loaded, list) else None
        return None

    def _load_session_payloads_from_files(self, session: ProxySession) -> dict[str, list[dict[str, Any]] | None]:
        return {
            "transcript": self._load_required_payload_file(session, "raw", []),
            "override_base_transcript": self._load_optional_payload_file(session, "override_base"),
            "edited_transcript": self._load_optional_payload_file(session, "edited"),
            "pending_transcript": self._load_optional_payload_file(session, "pending"),
        }

    def _append_state_delta(
        self,
        session: ProxySession,
        kind: str,
        previous: list[dict[str, Any]] | None,
        current: list[dict[str, Any]] | None,
    ) -> None:
        path = self._v2_state_file(session, kind)
        previous_safe = copy.deepcopy(previous)
        current_safe = copy.deepcopy(current)
        if previous_safe == current_safe:
            return
        if current_safe is None:
            if previous_safe is not None:
                append_jsonl_line(path, {"type": "clear", "created_at": utc_timestamp()})
            return
        if previous_safe is None:
            if current_safe:
                append_jsonl_line(
                    path,
                    {"type": "append", "created_at": utc_timestamp(), "records": sanitize_json_value(current_safe)},
                )
            else:
                append_jsonl_line(path, {"type": "clear", "created_at": utc_timestamp()})
            return
        if len(current_safe) >= len(previous_safe) and current_safe[: len(previous_safe)] == previous_safe:
            appended = current_safe[len(previous_safe) :]
            if appended:
                append_jsonl_line(
                    path,
                    {"type": "append", "created_at": utc_timestamp(), "records": sanitize_json_value(appended)},
                )
            return

        common_prefix = 0
        for previous_record, current_record in zip(previous_safe, current_safe):
            if previous_record != current_record:
                break
            common_prefix += 1
        if common_prefix > 0:
            append_jsonl_line(
                path,
                {
                    "type": "replace_from",
                    "created_at": utc_timestamp(),
                    "index": common_prefix,
                    "records": sanitize_json_value(current_safe[common_prefix:]),
                },
            )
            return
        append_jsonl_line(
            path,
            {"type": "set", "created_at": utc_timestamp(), "records": sanitize_json_value(current_safe)},
        )

    def _append_list_delta(
        self,
        path: Path,
        previous: list[dict[str, Any]],
        current: list[dict[str, Any]],
    ) -> None:
        previous_safe = copy.deepcopy(previous)
        current_safe = copy.deepcopy(current)
        if previous_safe == current_safe:
            return
        if len(current_safe) >= len(previous_safe) and current_safe[: len(previous_safe)] == previous_safe:
            appended = current_safe[len(previous_safe) :]
            if appended:
                append_jsonl_line(
                    path,
                    {"type": "append", "created_at": utc_timestamp(), "records": sanitize_json_value(appended)},
                )
            return
        append_jsonl_line(
            path,
            {"type": "set", "created_at": utc_timestamp(), "records": sanitize_json_value(current_safe)},
        )

    def _persist_session_log_files(self, session: ProxySession) -> None:
        req_log_path = self._session_dir(session) / "request_log.jsonl"
        resp_items_path = self._session_dir(session) / "response_items.jsonl"
        req_log_path.parent.mkdir(parents=True, exist_ok=True)
        slim_log = [
            {"kind": e.get("kind"), "created_at": e.get("created_at"), "headers": e.get("headers")}
            for e in session.request_log[-20:]
        ]
        req_log_path.write_text(
            json_dumps_compact(
                {"type": "set", "created_at": utc_timestamp(), "records": sanitize_json_value(slim_log)}
            )
            + "\n",
            encoding="utf-8",
        )
        resp_items_path.write_text(
            json_dumps_compact(
                {
                    "type": "set",
                    "created_at": utc_timestamp(),
                    "records": sanitize_json_value(session.response_items[-100:]),
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def _persist_session_payload_files(self, session: ProxySession) -> None:
        previous = self._persisted_payloads.get(session.id, {})
        current = {
            "transcript": clean_transcript(session.transcript),
            "override_base_transcript": (
                clean_transcript(session.override_base_transcript)
                if session.override_base_transcript is not None
                else None
            ),
            "edited_transcript": (
                clean_transcript(session.edited_transcript)
                if session.edited_transcript is not None
                else None
            ),
            "pending_transcript": (
                clean_transcript(session.pending_transcript)
                if session.pending_transcript is not None
                else None
            ),
        }
        for key, kind in (
            ("transcript", "raw"),
            ("override_base_transcript", "override_base"),
            ("edited_transcript", "edited"),
            ("pending_transcript", "pending"),
        ):
            self._append_state_delta(session, kind, previous.get(key), current[key])
        self._write_session_storage_manifest(session)
        self._persisted_payloads[session.id] = copy.deepcopy(current)

    def _session_from_row(
        self,
        session_row: sqlite3.Row | tuple[Any, ...],
        usage_events: list[dict[str, Any]],
        usage_summary_cache: dict[str, Any] | None,
    ) -> ProxySession:
        (
            session_id,
            title,
            status,
            _session_dir,
            headers_json,
            last_turn_metadata_header,
            last_error,
            created_at,
            updated_at,
        ) = session_row
        session = ProxySession(
            id=str(session_id),
            title=str(title or "Codex Session"),
            status=str(status or "mirror"),
            request_log=[],
            response_items=[],
            usage_events=usage_events,
            usage_summary_cache=usage_summary_cache,
            last_codex_session_headers=json_loads_value(str(headers_json or "{}"), {}),
            last_turn_metadata_header=str(last_turn_metadata_header or ""),
            last_error=str(last_error or ""),
            created_at=str(created_at or utc_timestamp()),
            updated_at=str(updated_at or utc_timestamp()),
            payloads_loaded=False,
        )
        session.request_log = []
        session.response_items = []
        return session

    def _ensure_session_payloads_loaded(self, session: ProxySession) -> None:
        if session.payloads_loaded:
            return
        payloads = self._load_session_payloads_from_files(session)
        session.transcript = clean_transcript(payloads["transcript"])
        session.override_base_transcript = (
            clean_transcript(payloads["override_base_transcript"])
            if payloads["override_base_transcript"] is not None
            else None
        )
        session.edited_transcript = (
            clean_transcript(payloads["edited_transcript"])
            if payloads["edited_transcript"] is not None
            else None
        )
        session.pending_transcript = (
            clean_transcript(payloads["pending_transcript"])
            if payloads["pending_transcript"] is not None
            else None
        )
        if session.edited_transcript is None and session.pending_transcript is None and session.status in {"override", "running", "compacting"}:
            session.status = "mirror"
            session.local_compact_source_transcript = None
        self._persisted_payloads[session.id] = {
            "transcript": copy.deepcopy(session.transcript),
            "override_base_transcript": copy.deepcopy(session.override_base_transcript) if session.override_base_transcript is not None else None,
            "edited_transcript": copy.deepcopy(session.edited_transcript) if session.edited_transcript is not None else None,
            "pending_transcript": copy.deepcopy(session.pending_transcript) if session.pending_transcript is not None else None,
        }
        session.payloads_loaded = True

    def load(self) -> None:
        with self._connect() as conn:
            active = conn.execute("SELECT value FROM meta WHERE key = 'active_session_id'").fetchone()
            self.active_session_id = str(active[0]) if active else ""
            rows = conn.execute(
                """
                SELECT id, title, status, session_dir, last_codex_session_headers_json, last_turn_metadata_header,
                       last_error, created_at, updated_at
                FROM sessions
                """
            ).fetchall()
            for row in rows:
                usage_events = [
                    item
                    for item in (
                        json_loads_value(event_row[0], {})
                        for event_row in conn.execute(
                            """
                            SELECT event_json FROM usage_events
                            WHERE session_id = ?
                            ORDER BY position
                            """,
                            (row[0],),
                        ).fetchall()
                    )
                    if isinstance(item, dict)
                ]
                summary_row = conn.execute(
                    "SELECT summary_json FROM usage_summaries WHERE session_id = ?",
                    (row[0],),
                ).fetchone()
                usage_summary_cache = (
                    json_loads_value(summary_row[0], None)
                    if summary_row is not None
                    else usage_summary_from_events(str(row[0]), usage_events)
                )
                session = self._session_from_row(
                    row,
                    usage_events,
                    usage_summary_cache if isinstance(usage_summary_cache, dict) else None,
                )
                self.sessions[session.id] = session

    def _save_session_to_db(self, conn: sqlite3.Connection, session: ProxySession) -> None:
        conn.execute(
            """
            INSERT INTO sessions (
                id, title, status, session_dir, last_codex_session_headers_json, last_turn_metadata_header,
                last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                status = excluded.status,
                session_dir = excluded.session_dir,
                last_codex_session_headers_json = excluded.last_codex_session_headers_json,
                last_turn_metadata_header = excluded.last_turn_metadata_header,
                last_error = excluded.last_error,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            """,
            (
                session.id,
                session.title,
                session.status,
                self._session_dir(session).relative_to(self.sqlite_path.parent).as_posix(),
                json_dumps_compact(session.last_codex_session_headers),
                session.last_turn_metadata_header,
                session.last_error,
                session.created_at,
                session.updated_at,
            ),
        )
        self._persist_session_payload_files(session)
        self._persist_session_log_files(session)
        self._save_usage_to_db(conn, session)

    def _save_usage_to_db(self, conn: sqlite3.Connection, session: ProxySession) -> None:
        conn.execute("DELETE FROM usage_events WHERE session_id = ?", (session.id,))
        for index, event in enumerate(session.usage_events[-USAGE_EVENT_LIMIT:]):
            conn.execute(
                "INSERT INTO usage_events (session_id, position, event_json) VALUES (?, ?, ?)",
                (session.id, index, json_dumps_compact(event)),
            )
        summary = session.usage_summary()
        session.usage_summary_cache = summary
        conn.execute(
            """
            INSERT INTO usage_summaries (session_id, summary_json) VALUES (?, ?)
            ON CONFLICT(session_id) DO UPDATE SET summary_json = excluded.summary_json
            """,
            (session.id, json_dumps_compact(summary)),
        )

    def _append_usage_event_to_db(
        self,
        conn: sqlite3.Connection,
        session: ProxySession,
        event: dict[str, Any],
    ) -> None:
        next_position = conn.execute(
            "SELECT COALESCE(MAX(position) + 1, 0) FROM usage_events WHERE session_id = ?",
            (session.id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO usage_events (session_id, position, event_json) VALUES (?, ?, ?)",
            (session.id, int(next_position or 0), json_dumps_compact(event)),
        )
        conn.execute(
            """
            DELETE FROM usage_events
            WHERE session_id = ?
              AND position NOT IN (
                  SELECT position FROM usage_events
                  WHERE session_id = ?
                  ORDER BY position DESC
                  LIMIT ?
              )
            """,
            (session.id, session.id, USAGE_EVENT_LIMIT),
        )
        summary = session.usage_summary()
        session.usage_summary_cache = summary
        conn.execute(
            """
            INSERT INTO usage_summaries (session_id, summary_json) VALUES (?, ?)
            ON CONFLICT(session_id) DO UPDATE SET summary_json = excluded.summary_json
            """,
            (session.id, json_dumps_compact(summary)),
        )

    def _save_session_metadata_to_db(self, conn: sqlite3.Connection, session: ProxySession) -> None:
        conn.execute(
            """
            INSERT INTO sessions (
                id, title, status, session_dir, last_codex_session_headers_json, last_turn_metadata_header,
                last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                status = excluded.status,
                session_dir = excluded.session_dir,
                last_codex_session_headers_json = excluded.last_codex_session_headers_json,
                last_turn_metadata_header = excluded.last_turn_metadata_header,
                last_error = excluded.last_error,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            """,
            (
                session.id,
                session.title,
                session.status,
                self._session_dir(session).relative_to(self.sqlite_path.parent).as_posix(),
                json_dumps_compact(session.last_codex_session_headers),
                session.last_turn_metadata_header,
                session.last_error,
                session.created_at,
                session.updated_at,
            ),
        )

    def save(self, session_ids: str | set[str] | list[str] | tuple[str, ...] | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('active_session_id', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self.active_session_id,),
            )
            if session_ids is None:
                live_ids = set(self.sessions.keys())
                if live_ids:
                    placeholders = ",".join("?" for _ in live_ids)
                    conn.execute(f"DELETE FROM sessions WHERE id NOT IN ({placeholders})", tuple(live_ids))
                else:
                    conn.execute("DELETE FROM sessions")
                target_ids = live_ids
            elif isinstance(session_ids, str):
                target_ids = {session_ids}
            else:
                target_ids = {str(session_id) for session_id in session_ids if str(session_id)}
            for session_id in target_ids:
                session = self.sessions.get(session_id)
                if session is None:
                    continue
                if not session.payloads_loaded:
                    self._save_session_metadata_to_db(conn, session)
                    self._save_usage_to_db(conn, session)
                    continue
                session.request_log = session.request_log[-20:]
                session.response_items = session.response_items[-100:]
                session.usage_events = session.usage_events[-USAGE_EVENT_LIMIT:]
                self._save_session_to_db(conn, session)

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
            current_turn_metadata = request_turn_metadata(headers)
            current_codex_session_headers = codex_session_headers_from_request(headers)
            if current_codex_session_headers:
                session.last_codex_session_headers = current_codex_session_headers
            previous_turn_metadata = session.last_turn_metadata_header
            if not session.payloads_loaded:
                self._ensure_session_payloads_loaded(session)
            if session.edited_transcript is not None:
                source_snapshot = clean_transcript(source_transcript)
                override_base_transcript = (
                    session.override_base_transcript
                    if session.override_base_transcript is not None
                    else session.transcript
                )
                merged_body_transcript = merge_override_transcript(
                    strip_initial_context_prefix_records(
                        strip_context_edit_notice_records(session.edited_transcript)
                    ),
                    strip_initial_context_prefix_records(
                        strip_context_edit_notice_records(override_base_transcript)
                    ),
                    strip_initial_context_prefix_records(source_transcript),
                )
                forwarded_transcript = with_fresh_initial_context_prefix(
                    source_transcript,
                    merged_body_transcript,
                )
                if input_items_end_with_tool_output(body.get("input")):
                    session.transcript = copy.deepcopy(source_snapshot)
                    session.override_base_transcript = copy.deepcopy(source_snapshot)
                    session.edited_transcript = forwarded_transcript
                    session.pending_transcript = with_running_assistant(forwarded_transcript)
                    request_body["input"] = drop_unpaired_tool_items(transcript_to_input_items(forwarded_transcript))
                    request_body.pop("previous_response_id", None)
                    session.status = "running"
                    if current_turn_metadata:
                        session.last_turn_metadata_header = current_turn_metadata
                    session.last_error = ""
                    session.updated_at = utc_timestamp()
                    session.request_log.append(
                        {
                            "created_at": session.updated_at,
                            "kind": "override_tool_output_rewrite",
                            "headers": {key: value for key, value in headers.items() if key.lower().startswith("x-")},
                            "body": body,
                            "forwarded_body": request_body,
                        }
                    )
                    session.request_log = session.request_log[-20:]
                    self.save(session.id)
                    return session, request_body
                compact_source = local_compact_source_from_transcript(forwarded_transcript)
                if compact_source is not None:
                    session.transcript = copy.deepcopy(compact_source)
                    session.override_base_transcript = copy.deepcopy(compact_source)
                    session.local_compact_source_transcript = compact_source
                    session.pending_transcript = None
                    compact_prompt_transcript = replace_last_local_compact_prompt(
                        forwarded_transcript,
                        replacement_local_compact_prompt(
                            compact_source,
                            current_turn_metadata,
                            previous_turn_metadata,
                        ),
                    )
                    request_body["input"] = drop_unpaired_tool_items(transcript_to_input_items(compact_prompt_transcript))
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
                    self.save(session.id)
                    return session, request_body
                session.transcript = copy.deepcopy(source_snapshot)
                session.override_base_transcript = copy.deepcopy(source_snapshot)
                session.edited_transcript = forwarded_transcript
                session.pending_transcript = with_running_assistant(forwarded_transcript)
                request_body["input"] = drop_unpaired_tool_items(transcript_to_input_items(forwarded_transcript))
                request_body.pop("previous_response_id", None)
                session.status = "running"
                session.payloads_loaded = True
                if current_turn_metadata:
                    session.last_turn_metadata_header = current_turn_metadata
            else:
                compact_source = local_compact_source_from_transcript(source_transcript)
                if compact_source is not None:
                    session.override_base_transcript = None
                    session.local_compact_source_transcript = compact_source
                    session.transcript = compact_source
                    session.pending_transcript = None
                    session.payloads_loaded = True
                    request_body["input"] = replace_last_local_compact_prompt_input(
                        body.get("input"),
                        replacement_local_compact_prompt(
                            compact_source,
                            current_turn_metadata,
                            previous_turn_metadata,
                        ),
                    )
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
                    self.save(session.id)
                    return session, request_body
                session.override_base_transcript = None
                session.transcript = source_transcript
                session.pending_transcript = None
                session.status = "running"
                session.transcript = with_running_assistant(source_transcript)
                session.payloads_loaded = True
                if current_turn_metadata:
                    session.last_turn_metadata_header = current_turn_metadata
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
            self.save(session.id)
            return session, request_body

    def codex_session_headers(self, session_id: str) -> dict[str, str]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                return {}
            if session.last_codex_session_headers:
                return dict(session.last_codex_session_headers)
            for entry in reversed(session.request_log):
                if not isinstance(entry, dict):
                    continue
                entry_headers = entry.get("headers")
                if not isinstance(entry_headers, dict):
                    continue
                session_headers = codex_session_headers_from_request(
                    {str(key).lower(): str(value) for key, value in entry_headers.items()}
                )
                if session_headers:
                    return session_headers
            return {}

    def record_control_intercept(self, session_id: str, body: dict[str, Any], headers: dict[str, str], command: str) -> ProxySession:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                session = ProxySession(id=session_id, title=f"Codex {session_id[:8]}")
                self.sessions[session_id] = session
            else:
                self._ensure_session_payloads_loaded(session)
            source_transcript = clean_transcript(
                strip_context_edit_notice_records(input_items_to_transcript(body.get("input")))
            )
            existing_transcript = clean_transcript(session.transcript)
            if should_replace_transcript_from_control_intercept(existing_transcript, source_transcript):
                session.transcript = source_transcript
            session.pending_transcript = None
            session.local_compact_source_transcript = None
            session.status = "override" if session.edited_transcript is not None else "mirror"
            current_codex_session_headers = codex_session_headers_from_request(headers)
            if current_codex_session_headers:
                session.last_codex_session_headers = current_codex_session_headers
            current_turn_metadata = request_turn_metadata(headers)
            if current_turn_metadata:
                session.last_turn_metadata_header = current_turn_metadata
            session.last_error = ""
            session.updated_at = utc_timestamp()
            self.active_session_id = session_id
            session.request_log.append(
                {
                    "created_at": session.updated_at,
                    "kind": "context_control_intercept",
                    "command": command,
                    "headers": {key: value for key, value in headers.items() if key.lower().startswith("x-")},
                    "body": body,
                }
            )
            session.request_log = session.request_log[-20:]
            self.save(session.id)
            return session

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
            elif not session.payloads_loaded:
                self._ensure_session_payloads_loaded(session)
            self.active_session_id = session_id
            session.local_compact_source_transcript = None

            if session.edited_transcript is not None:
                source_snapshot = clean_transcript(source_transcript)
                override_base_transcript = (
                    session.override_base_transcript
                    if session.override_base_transcript is not None
                    else session.transcript
                )
                edited_compact_body_transcript = strip_initial_context_prefix_records(
                    strip_context_edit_notice_records(session.edited_transcript)
                )
                compact_base_transcript = strip_initial_context_prefix_records(
                    strip_context_edit_notice_records(override_base_transcript)
                )
                compact_body_transcript = (
                    merge_override_transcript(
                        edited_compact_body_transcript,
                        compact_base_transcript,
                        strip_initial_context_prefix_records(source_transcript),
                    )
                    if compact_base_transcript
                    else edited_compact_body_transcript
                )
                compact_transcript = with_fresh_initial_context_prefix(
                    source_transcript,
                    compact_body_transcript,
                )
                session.transcript = copy.deepcopy(source_snapshot)
                session.override_base_transcript = copy.deepcopy(source_snapshot)
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
            self.save(session.id)
            return session, request_body

    def complete_compact(self, session_id: str, output_items: list[dict[str, Any]]) -> None:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                return
            compacted_transcript = input_items_to_transcript(output_items)
            session.transcript = compacted_transcript
            if session.edited_transcript is not None:
                session.override_base_transcript = copy.deepcopy(compacted_transcript)
                session.edited_transcript = copy.deepcopy(compacted_transcript)
                session.status = "override"
            else:
                session.override_base_transcript = None
                session.status = "mirror"
            session.pending_transcript = None
            session.payloads_loaded = True
            session.response_items.extend(output_items)
            session.response_items = session.response_items[-100:]
            session.updated_at = utc_timestamp()
            self.save(session.id)

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
                    session.override_base_transcript = copy.deepcopy(compacted_transcript)
                    session.edited_transcript = copy.deepcopy(compacted_transcript)
                    session.status = "override"
                else:
                    session.override_base_transcript = None
                    session.status = "mirror"
                session.local_compact_source_transcript = None
                session.pending_transcript = None
                session.payloads_loaded = True
                session.response_items.extend(items)
                session.response_items = session.response_items[-100:]
                session.updated_at = utc_timestamp()
                self.save(session.id)
                return
            if session.edited_transcript is None:
                base = [record for record in session.transcript if not is_running_assistant(record)]
                assistant_items = items or [{"type": "message", "role": "assistant", "content": text}]
                assistant_text = text or assistant_text_from_items(assistant_items)
                base = append_assistant_response_record(base, assistant_text, assistant_items)
                session.override_base_transcript = None
                session.transcript = base
                session.status = "mirror"
            else:
                base = clean_transcript(session.pending_transcript or session.edited_transcript)
                assistant_items = items or [{"type": "message", "role": "assistant", "content": text}]
                assistant_text = text or assistant_text_from_items(assistant_items)
                base = append_assistant_response_record(base, assistant_text, assistant_items)
                raw_base = clean_transcript(session.override_base_transcript or session.transcript)
                raw_base = append_assistant_response_record(raw_base, assistant_text, assistant_items)
                session.transcript = raw_base
                session.override_base_transcript = copy.deepcopy(raw_base)
                session.edited_transcript = base
                session.status = "override"
            session.pending_transcript = None
            session.payloads_loaded = True
            session.response_items.extend(items)
            session.response_items = session.response_items[-100:]
            session.updated_at = utc_timestamp()
            self.save(session.id)

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
            self.save(session.id)

    def record_usage(self, session_id: str, kind: str, model: str, raw_usage: Any) -> None:
        event = usage_event(kind, model, raw_usage)
        if event is None:
            return
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                session = ProxySession(id=session_id, title=f"Codex {session_id[:8]}")
                self.sessions[session_id] = session
            session.usage_events.append(event)
            session.usage_events = session.usage_events[-USAGE_EVENT_LIMIT:]
            session.usage_summary_cache = usage_summary_with_event(session.id, session.usage_summary_cache, event)
            session.updated_at = utc_timestamp()
            with self._connect() as conn:
                self._save_session_metadata_to_db(conn, session)
                self._append_usage_event_to_db(conn, session, event)

    def all_usage(self) -> dict[str, Any]:
        with self.lock:
            exposed_sessions = [session for session in self.sessions.values() if session.should_expose()]
            sessions: dict[str, dict[str, Any]] = {}
            overall_events: list[dict[str, Any]] = []
            for session in exposed_sessions:
                sessions[session.id] = session.usage_summary()
                overall_events.extend(session.usage_events)
            return {
                "overall": usage_summary_from_events("overall", overall_events),
                "sessions": sessions,
            }

    def session_usage(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                return None
            return {"summary": session.usage_summary()}

    def reset_usage(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            cleared_count = len(session.usage_events)
            session.usage_events = []
            session.usage_summary_cache = usage_summary_from_events(session.id, [])
            session.updated_at = utc_timestamp()
            with self._connect() as conn:
                self._save_session_metadata_to_db(conn, session)
                self._save_usage_to_db(conn, session)
            return {"cleared_count": cleared_count, "summary": session.usage_summary()}

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
                "sessions": [session.metadata_payload() for session in sessions],
            }

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is not None:
                self._ensure_session_payloads_loaded(session)
            return session.to_payload() if session else None

    def override(self, session_id: str, transcript: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                session = ProxySession(id=session_id, title=f"Codex {session_id[:8]}")
                self.sessions[session_id] = session
            else:
                self._ensure_session_payloads_loaded(session)
            previous_visible = clean_transcript(session.visible_transcript())
            next_transcript = clean_transcript(transcript)
            mirror_transcript = clean_transcript(session.transcript)
            if next_transcript == mirror_transcript:
                session.override_base_transcript = None
                session.edited_transcript = None
                session.status = "mirror"
            else:
                session.override_base_transcript = copy.deepcopy(mirror_transcript)
                session.edited_transcript = copy.deepcopy(next_transcript)
                session.status = "override"
            session.pending_transcript = None
            session.updated_at = utc_timestamp()
            self.active_session_id = session_id
            self.save(session.id)
            payload = session.to_payload()
            payload["changed"] = previous_visible != clean_transcript(session.visible_transcript())
            return payload

    def reset(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            self._ensure_session_payloads_loaded(session)
            previous_visible = clean_transcript(session.visible_transcript())
            session.override_base_transcript = None
            session.edited_transcript = None
            session.pending_transcript = None
            session.status = "mirror"
            session.updated_at = utc_timestamp()
            self.save(session.id)
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
    records = coalesce_adjacent_assistant_records(records)
    records = dedupe_local_compact_summary_records(records)
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


def provider_item_signature(item: dict[str, Any]) -> str:
    return json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)


def provider_item_logical_key(item: dict[str, Any]) -> tuple[str, str] | None:
    item_type = str(item.get("type") or "")
    if item_type not in TOOL_CALL_ITEM_TYPES | TOOL_OUTPUT_ITEM_TYPES:
        return None
    call_id = str(item.get("call_id") or item.get("id") or "").strip()
    return (item_type, call_id) if call_id else None


def append_unique_provider_items(
    existing_items: list[dict[str, Any]],
    next_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    combined = copy.deepcopy(existing_items)
    seen = {provider_item_signature(item) for item in combined}
    logical_indexes = {
        logical_key: index
        for index, item in enumerate(combined)
        if (logical_key := provider_item_logical_key(item)) is not None
    }
    for item in next_items:
        next_item = copy.deepcopy(item)
        logical_key = provider_item_logical_key(next_item)
        if logical_key is not None and logical_key in logical_indexes:
            combined[logical_indexes[logical_key]] = next_item
            seen = {provider_item_signature(existing) for existing in combined}
            continue
        signature = provider_item_signature(next_item)
        if signature in seen:
            continue
        seen.add(signature)
        if logical_key is not None:
            logical_indexes[logical_key] = len(combined)
        combined.append(next_item)
    return combined


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
        combined_items = append_unique_provider_items(
            combined_items,
            [copy.deepcopy(item) for item in response_items if isinstance(item, dict)],
        )
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
    clean_tail = clean_transcript(tail)
    if not clean_tail:
        return clean_transcript(merged)

    max_overlap = min(len(merged), len(clean_tail))
    overlap = 0
    for size in range(max_overlap, 0, -1):
        if [transcript_record_signature(record) for record in merged[-size:]] == [
            transcript_record_signature(record)
            for record in clean_tail[:size]
        ]:
            overlap = size
            break

    for record in clean_tail[overlap:]:
        if merged and transcript_record_signature(merged[-1]) == transcript_record_signature(record):
            continue
        summary_text = local_compact_summary_text(record)
        if summary_text and any(local_compact_summary_text(existing) == summary_text for existing in merged):
            continue
        merged.append(copy.deepcopy(record))
    return clean_transcript(merged)


def latest_user_turn_tail(source_transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source = clean_transcript(source_transcript)
    if not source or str(source[-1].get("role") or "") != "user":
        return []

    start = len(source) - 1
    while start > 0 and is_initial_context_prefix_record(source[start - 1]):
        start -= 1
    return copy.deepcopy(source[start:])


def latest_conversation_turn_tail(source_transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source = clean_transcript(source_transcript)
    for index in range(len(source) - 1, -1, -1):
        if str(source[index].get("role") or "") == "user" and not is_initial_context_prefix_record(source[index]):
            start = index
            while start > 0 and is_initial_context_prefix_record(source[start - 1]):
                start -= 1
            return copy.deepcopy(source[start:])
    return []


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
        if (
            mirror_prefix == len(mirror) - 1
            and mirror_prefix < len(source)
            and str(mirror[mirror_prefix].get("role") or "") == "assistant"
            and str(source[mirror_prefix].get("role") or "") == "assistant"
            and not any(str(record.get("role") or "") == "user" for record in source[mirror_prefix + 1 :])
        ):
            return append_non_duplicate(base, source[mirror_prefix:])

    base_prefix = common_prefix_len(source, base)
    if base and base_prefix >= len(base):
        return append_non_duplicate(base, source[base_prefix:])

    latest_tail = latest_user_turn_tail(source) or latest_conversation_turn_tail(source)
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
        if parsed.path == "/api/proxy/health":
            self._send_json({"ok": True})
            return

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
        if parsed.path == "/api/proxy/usage":
            self._send_json(STORE.all_usage())
            return
        if parsed.path.startswith("/api/proxy/sessions/") and parsed.path.endswith("/usage"):
            session_id = urllib.parse.unquote(parsed.path.split("/api/proxy/sessions/", 1)[1].rsplit("/", 1)[0])
            usage = STORE.session_usage(session_id)
            if usage is None:
                self._send_json({"error": "session not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(usage)
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
        if parsed.path.endswith("/usage/reset") and parsed.path.startswith("/api/proxy/sessions/"):
            session_id = urllib.parse.unquote(parsed.path.split("/api/proxy/sessions/", 1)[1].rsplit("/usage/", 1)[0])
            try:
                self._send_json(STORE.reset_usage(session_id))
            except KeyError:
                self._send_json({"error": "session not found"}, HTTPStatus.NOT_FOUND)
            return
        if parsed.path.endswith("/reset") and parsed.path.startswith("/api/proxy/sessions/"):
            session_id = urllib.parse.unquote(parsed.path.split("/api/proxy/sessions/", 1)[1].rsplit("/", 1)[0])
            try:
                self._send_json(STORE.reset(session_id))
            except KeyError:
                self._send_json({"error": "session not found"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _send_sse_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "message")
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"event: {event_type}\n".encode("utf-8"))
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _send_context_control_response(self, body: dict[str, Any], opened: bool, error: str = "") -> None:
        response_id = f"resp_hash_context_{uuid.uuid4().hex}"
        message_id = f"msg_hash_context_{uuid.uuid4().hex}"
        model = str(body.get("model") or "gpt-5.5")
        text = CONTEXT_CONTROL_NOTICE_TEXT if opened else f"Hash Context: workbench unavailable. {error}".strip()
        item = {
            "type": "message",
            "role": "assistant",
            "id": message_id,
            "content": [{"type": "output_text", "text": text}],
        }
        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self._send_sse_event({"type": "response.created", "response": {"id": response_id, "model": model}})
        self._send_sse_event({"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": [{"type": "output_text", "text": ""}]}})
        self._send_sse_event({"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "item_id": message_id, "delta": text})
        self._send_sse_event({"type": "response.output_item.done", "output_index": 0, "item": item})
        self._send_sse_event({"type": "response.completed", "response": {"id": response_id, "model": model, "output": [item]}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

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
        is_title_generation = is_title_generation_request(body)
        capture_proxy_session = not is_internal_context and not is_title_generation
        if capture_proxy_session:
            control_command = context_control_command_from_input(body.get("input"))
            if control_command:
                session_id = session_id_for_request(body, headers)
                STORE.record_control_intercept(session_id, body, headers, control_command)
                opened, error = open_context_workbench(session_id)
                proxy_log(
                    f"context control intercepted session={session_id} command={control_command!r} "
                    f"opened={opened} error={error!r}"
                )
                self._send_context_control_response(body, opened, error)
                return
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
            session_id = session_id_for_request(body, headers)
            headers_for_upstream = merge_codex_session_headers(
                headers,
                STORE.codex_session_headers(session_id),
                session_id=session_id,
            )
            forwarded_body = copy.deepcopy(body)
        elif capture_proxy_session:
            session_id = session_id_for_request(body, headers)
            headers_for_upstream = headers
            _session, forwarded_body = STORE.begin_request(session_id, body, headers)
        else:
            session_id = session_id_for_request(body, headers)
            headers_for_upstream = headers
            forwarded_body = copy.deepcopy(body)
        upstream_base_url = upstream_base_url_for_request(headers_for_upstream)
        upstream = urllib.parse.urlparse(upstream_base_url.rstrip("/") + "/responses")
        effective_headers = apply_cached_upstream_auth(headers_for_upstream)
        effective_lowered = {key.lower(): value for key, value in effective_headers.items()}
        auth_kind = "chatgpt" if effective_lowered.get("chatgpt-account-id") else "api-key-or-bearer"
        proxy_log(
            f"request session={session_id} auth={auth_kind} "
            f"internal={is_internal_context} title_generation={is_title_generation} "
            f"capture={capture_proxy_session} upstream={upstream.geturl()}"
        )
        connection_cls = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
        conn = connection_cls(upstream.hostname, upstream.port, timeout=120)
        path = upstream.path or "/v1/responses"
        if upstream.query:
            path = f"{path}?{upstream.query}"

        try:
            payload = json.dumps(forwarded_body, ensure_ascii=False).encode("utf-8")
            upstream_headers = upstream_headers_for_request(headers_for_upstream, accept="text/event-stream")
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
            completed_responses: list[dict[str, Any]] = []
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
                buffer = parse_sse_buffer(buffer, response_items, text_parts, completed_responses)

            if upstream_response.status >= 400:
                preview = error_preview.decode("utf-8", errors="replace")
                proxy_log(f"upstream error session={session_id} body={preview[:1000]!r}")
                if capture_proxy_session:
                    STORE.fail_response(session_id, preview[:1000] or upstream_response.reason)
            else:
                if is_internal_context or capture_proxy_session:
                    usage_kind = "context_workbench" if is_internal_context else "main"
                    fallback_model = str(forwarded_body.get("model") or body.get("model") or "")
                    for completed_response in completed_responses:
                        STORE.record_usage(
                            session_id,
                            usage_kind,
                            str(completed_response.get("model") or fallback_model),
                            completed_response.get("usage"),
                        )
                if capture_proxy_session:
                    STORE.complete_response(session_id, response_items, "".join(text_parts))
        except Exception as exc:
            proxy_log(f"error session={session_id} error={type(exc).__name__}: {exc}")
            if capture_proxy_session:
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
            raw_usage = parsed_body.get("usage") if isinstance(parsed_body, dict) else None
            if raw_usage is None and isinstance(parsed_body.get("response"), dict):
                raw_usage = parsed_body["response"].get("usage")
            STORE.record_usage(
                session_id,
                "compact",
                str(parsed_body.get("model") or body.get("model") or ""),
                raw_usage,
            )
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
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type, x-hash-context-internal, x-hash-context-session-id")

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def parse_sse_buffer(
    buffer: str,
    response_items: list[dict[str, Any]],
    text_parts: list[str],
    completed_responses: list[dict[str, Any]] | None = None,
) -> str:
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
            if completed_responses is not None:
                completed_responses.append(response)
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
    LOG_PATH.write_text("", encoding="utf-8")
    if preload_force_upstream_auth():
        proxy_log(f"preloaded force upstream auth for {FORCE_UPSTREAM_BASE_URL}")
    elif preload_codex_subscription_auth():
        proxy_log("preloaded Codex subscription auth from local auth.json")
    elif preload_openai_api_auth():
        proxy_log("preloaded OpenAI API auth from OPENAI_API_KEY")
    else:
        proxy_log("local Codex auth was not available at proxy startup")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Hash Context proxy listening on http://{args.host}:{args.port}")
    if FORCE_UPSTREAM_BASE_URL:
        print(f"Force upstream: {FORCE_UPSTREAM_BASE_URL.rstrip('/')}/responses")
    else:
        print(f"OpenAI API upstream: {OPENAI_UPSTREAM_BASE_URL.rstrip()}/responses")
        print(f"ChatGPT upstream: {CHATGPT_UPSTREAM_BASE_URL.rstrip()}/responses")
    server.serve_forever()


if __name__ == "__main__":
    main()
