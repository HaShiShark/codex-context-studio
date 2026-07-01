"""Pure data helpers for Codex local compact handling.

This module implements only the local compact path described in
``docs/proxy-design.md``.  It has no HTTP, persistence, or frontend knowledge.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .codex_input_cursor import fingerprint_provider_item
from .transcript_codec import input_items_to_transcript, transcript_to_input_items


TURN_METADATA_KEY = "x-codex-turn-metadata"
COMPACT_REQUEST_KIND = "compaction"
COMPACT_TRIGGERS = frozenset({"auto", "manual"})
DEFAULT_COMPACT_TRIGGER = "manual"

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
Be concise but complete; err on the side of including information that would prevent duplicate work or repeated mistakes. Write in a way that enables immediate resumption of the task.
Wrap your summary in <summary></summary> tags."""

CUSTOM_LOCAL_COMPACT_PROMPTS = (
    MANUAL_LOCAL_COMPACT_PROMPT,
    AUTO_LOCAL_COMPACT_PROMPT,
)


@dataclass(frozen=True)
class CompactTurnMetadata:
    """Parsed compact request metadata from ``body.client_metadata``."""

    trigger: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class PromptReplacementResult:
    """Result of replacing Codex's original local compact prompt."""

    new_transcript: list[dict[str, Any]]
    replaced: bool
    replacement_prompt: str
    compact_kind: str


@dataclass(frozen=True)
class CompactSuccessResult:
    """Simulated transcript/cursor state after a successful local compact."""

    new_transcript: list[dict[str, Any]]
    new_cursor: list[Any]
    compact_kind: str
    retained_user_count: int
    summary_text: str

    @property
    def new_items(self) -> list[Any]:
        """Alias used by the design doc for the rebuilt cursor items."""

        return self.new_cursor


class CompactController:
    """Facade for the pure compact data operations."""

    @staticmethod
    def parse_turn_metadata(body: Mapping[str, Any] | None) -> CompactTurnMetadata | None:
        return parse_compact_turn_metadata(body)

    @staticmethod
    def is_compact_request(body: Mapping[str, Any] | None) -> bool:
        return is_compact_request(body)

    @staticmethod
    def replacement_prompt(compact_kind: str) -> str:
        return replacement_local_compact_prompt(compact_kind)

    @staticmethod
    def replace_last_compact_prompt(
        transcript: Sequence[Mapping[str, Any]],
        compact_kind: str,
    ) -> PromptReplacementResult:
        return replace_last_local_compact_prompt(transcript, compact_kind)

    @staticmethod
    def on_compact_success(
        transcript: Sequence[Mapping[str, Any]],
        response_items: Any,
        compact_kind: str,
        *,
        max_user_tokens: int = LOCAL_COMPACT_USER_MESSAGE_MAX_TOKENS,
    ) -> CompactSuccessResult:
        return compact_success_from_response_items(
            transcript,
            response_items,
            compact_kind,
            max_user_tokens=max_user_tokens,
        )


def replace_compact_prompt(state: Any) -> PromptReplacementResult:
    """Module-level adapter used by ``proxy_core``'s automatic import path."""

    compact_kind = _state_compact_kind(state)
    result = replace_last_local_compact_prompt(state.transcript, compact_kind)
    state.transcript = result.new_transcript
    state.compact_error = None
    return result


def on_compact_success(
    state: Any,
    response_items: Any,
    *,
    text: str = "",
) -> CompactSuccessResult:
    """Module-level adapter that writes simulated compact state back to ProxyState."""

    compact_kind = _state_compact_kind(state)
    summary_source = response_items if _has_response_items(response_items) else text
    result = compact_success_from_response_items(
        state.transcript,
        summary_source,
        compact_kind,
    )
    state.transcript = result.new_transcript
    state.codex_input_cursor = copy.deepcopy(result.new_cursor)
    state.compact_pending = False
    state.compact_kind = ""
    state.compact_error = None
    return result


def parse_compact_turn_metadata(body: Mapping[str, Any] | None) -> CompactTurnMetadata | None:
    """Parse Codex compact turn metadata from the request body."""

    if not isinstance(body, Mapping):
        return None
    client_metadata = body.get("client_metadata")
    if not isinstance(client_metadata, Mapping):
        return None

    raw_value = client_metadata.get(TURN_METADATA_KEY)
    metadata = _parse_metadata_value(raw_value)
    if not isinstance(metadata, dict):
        return None
    if metadata.get("request_kind") != COMPACT_REQUEST_KIND:
        return None

    trigger = str(metadata.get("trigger") or "").strip().lower()
    if trigger not in COMPACT_TRIGGERS:
        trigger = DEFAULT_COMPACT_TRIGGER
    return CompactTurnMetadata(trigger=trigger, raw=copy.deepcopy(metadata))


def is_compact_request(body: Mapping[str, Any] | None) -> bool:
    """Return true when body metadata declares a local compaction request."""

    return parse_compact_turn_metadata(body) is not None


def replacement_local_compact_prompt(compact_kind: str) -> str:
    """Return the custom local compact prompt for ``manual`` or ``auto``."""

    return AUTO_LOCAL_COMPACT_PROMPT if compact_kind == "auto" else MANUAL_LOCAL_COMPACT_PROMPT


def replace_last_local_compact_prompt(
    transcript: Sequence[Mapping[str, Any]],
    compact_kind: str,
) -> PromptReplacementResult:
    """Replace the last user message if it is Codex's original compact prompt.

    The returned transcript is a deep copy.  The provider message shape is kept
    intact as far as possible; only the text field is changed.
    """

    next_transcript = copy.deepcopy(list(transcript))
    replacement_prompt = replacement_local_compact_prompt(compact_kind)

    target = _find_last_user_message(next_transcript)
    if target is None:
        return PromptReplacementResult(
            new_transcript=next_transcript,
            replaced=False,
            replacement_prompt=replacement_prompt,
            compact_kind=compact_kind,
        )

    node, item_index, node_item, provider_item = target
    if not is_codex_original_local_compact_prompt_text(read_message_text(provider_item)):
        return PromptReplacementResult(
            new_transcript=next_transcript,
            replaced=False,
            replacement_prompt=replacement_prompt,
            compact_kind=compact_kind,
        )

    old_fingerprint = fingerprint_provider_item(provider_item)
    next_provider_item = message_item_with_text(provider_item, replacement_prompt)
    node_item["providerItem"] = next_provider_item
    node_item["kind"] = str(next_provider_item.get("type") or node_item.get("kind") or "message")
    _refresh_node_source_map(node, item_index, old_fingerprint, next_provider_item)

    return PromptReplacementResult(
        new_transcript=next_transcript,
        replaced=True,
        replacement_prompt=replacement_prompt,
        compact_kind=compact_kind,
    )


def compact_success_from_response_items(
    transcript: Sequence[Mapping[str, Any]],
    response_items: Any,
    compact_kind: str,
    *,
    max_user_tokens: int = LOCAL_COMPACT_USER_MESSAGE_MAX_TOKENS,
) -> CompactSuccessResult:
    """Build the simulated compact transcript/cursor from provider response items."""

    return compact_success_from_summary(
        transcript,
        summary_text_from_response_items(response_items),
        compact_kind,
        max_user_tokens=max_user_tokens,
    )


def compact_success_from_summary(
    transcript: Sequence[Mapping[str, Any]],
    assistant_summary_text: str,
    compact_kind: str,
    *,
    max_user_tokens: int = LOCAL_COMPACT_USER_MESSAGE_MAX_TOKENS,
) -> CompactSuccessResult:
    """Build ``[retained user messages + summary user message]`` state."""

    exclude_in_progress_user = compact_kind == "auto"
    retained_user_items = collect_user_message_items(
        transcript,
        exclude_last_user=exclude_in_progress_user,
        max_user_tokens=max_user_tokens,
    )
    summary_text = build_local_compact_summary_text(assistant_summary_text)
    summary_item = provider_message("user", summary_text)
    new_cursor = [*retained_user_items, summary_item]
    new_transcript = input_items_to_transcript(new_cursor)

    return CompactSuccessResult(
        new_transcript=new_transcript,
        new_cursor=copy.deepcopy(new_cursor),
        compact_kind=compact_kind,
        retained_user_count=len(retained_user_items),
        summary_text=summary_text,
    )


def collect_user_message_items(
    transcript: Sequence[Mapping[str, Any]],
    *,
    exclude_last_user: bool = False,
    max_user_tokens: int = LOCAL_COMPACT_USER_MESSAGE_MAX_TOKENS,
) -> list[dict[str, Any]]:
    """Collect user message provider items eligible for simulated compact state."""

    user_items: list[dict[str, Any]] = []
    for item in transcript_to_input_items(transcript):
        if not _is_user_message(item):
            continue
        text = read_message_text(item)
        if not text:
            continue
        if is_local_compact_summary_text(text) or is_local_compact_prompt_text(text):
            continue
        user_items.append(copy.deepcopy(item))

    if exclude_last_user and user_items:
        user_items = user_items[:-1]

    return _select_user_items_by_token_budget(user_items, max_user_tokens)


def summary_text_from_response_items(response_items: Any) -> str:
    """Extract compact summary text from response output items."""

    if isinstance(response_items, str):
        return response_items
    if isinstance(response_items, Mapping):
        if isinstance(response_items.get("output"), Sequence) and not isinstance(response_items.get("output"), (str, bytes)):
            return summary_text_from_response_items(response_items.get("output"))
        return _first_present_text(response_items)
    if not isinstance(response_items, Sequence) or isinstance(response_items, (str, bytes)):
        return compact_text(response_items)

    parts: list[str] = []
    for item in response_items:
        text = _first_present_text(item) if isinstance(item, Mapping) else compact_text(item)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def build_local_compact_summary_text(assistant_summary_text: str) -> str:
    """Return the user-role compact summary message text."""

    return f"{LOCAL_COMPACT_SUMMARY_PREFIX}\n\n{assistant_summary_text or ''}"


def is_codex_original_local_compact_prompt_text(text: str) -> bool:
    """Return true only for Codex's built-in local compact prompt."""

    normalized = " ".join(str(text or "").split())
    return normalized.startswith(LOCAL_COMPACT_PROMPT_PREFIX)


def is_local_compact_prompt_text(text: str) -> bool:
    """Return true for original or proxy-provided local compact prompts."""

    value = str(text or "")
    return is_codex_original_local_compact_prompt_text(value) or value in CUSTOM_LOCAL_COMPACT_PROMPTS


def is_local_compact_summary_text(text: str) -> bool:
    """Return true for user-role local compact summary text."""

    return str(text or "").startswith(LOCAL_COMPACT_SUMMARY_PREFIX)


def local_compact_approx_token_count(text: str) -> int:
    """Approximate token count using the existing proxy heuristic."""

    return max(1, (len(str(text or "")) + 3) // 4)


def truncate_text_to_approx_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to the approximate token budget used for retained users."""

    if max_tokens <= 0:
        return ""
    return str(text or "")[: max_tokens * 4]


def provider_message(role: str, text: str) -> dict[str, Any]:
    """Build the provider message shape used for simulated compact state."""

    return {
        "type": "message",
        "role": role,
        "content": [{"type": "input_text", "text": text}],
    }


def read_message_text(item: Mapping[str, Any]) -> str:
    """Read visible text from a provider message item."""

    if "content" in item:
        return compact_text(item.get("content"))
    return compact_text(item.get("text"))


def message_item_with_text(item: Mapping[str, Any], text: str) -> dict[str, Any]:
    """Return a provider message item with its primary text replaced."""

    next_item = copy.deepcopy(dict(item))
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
    elif isinstance(content, Mapping):
        if "text" in content or content.get("type") in {"input_text", "output_text"}:
            next_item["content"] = {**content, "text": text}
        else:
            next_item["content"] = {"type": "input_text", "text": text}
    else:
        next_item["content"] = text

    if "text" in next_item:
        next_item["text"] = text
    return next_item


def compact_text(value: Any) -> str:
    """Return a readable text projection from provider content shapes."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, Mapping):
        for key in ("text", "summary", "content", "output"):
            if key in value:
                text = compact_text(value.get(key))
                if text:
                    return text
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "\n".join(part for part in (compact_text(item) for item in value) if part)
    return str(value)


def _parse_metadata_value(raw_value: Any) -> dict[str, Any] | None:
    if isinstance(raw_value, Mapping):
        return dict(raw_value)
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _state_compact_kind(state: Any) -> str:
    compact_kind = str(getattr(state, "compact_kind", "") or "").strip().lower()
    return compact_kind if compact_kind in COMPACT_TRIGGERS else DEFAULT_COMPACT_TRIGGER


def _has_response_items(response_items: Any) -> bool:
    if response_items is None:
        return False
    if isinstance(response_items, str):
        return bool(response_items)
    if isinstance(response_items, Mapping):
        return bool(response_items)
    if isinstance(response_items, Sequence) and not isinstance(response_items, (str, bytes, bytearray)):
        return bool(response_items)
    return True


def _find_last_user_message(
    transcript: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], int, dict[str, Any], dict[str, Any]] | None:
    for node in reversed(transcript):
        if not isinstance(node, dict):
            continue
        items = node.get("items")
        if not isinstance(items, list):
            continue
        for item_index in range(len(items) - 1, -1, -1):
            node_item = items[item_index]
            if not isinstance(node_item, dict):
                continue
            provider_item = node_item.get("providerItem")
            if _is_user_message(provider_item):
                return node, item_index, node_item, provider_item
    return None


def _is_user_message(item: Any) -> bool:
    return (
        isinstance(item, Mapping)
        and item.get("type") == "message"
        and item.get("role") == "user"
    )


def _refresh_node_source_map(
    node: dict[str, Any],
    item_index: int,
    old_fingerprint: str,
    provider_item: Mapping[str, Any],
) -> None:
    source_map = node.get("source_map")
    if not isinstance(source_map, dict):
        return
    pointer = f"items[{item_index}]"
    if source_map.get(old_fingerprint) == pointer:
        del source_map[old_fingerprint]
    source_map[fingerprint_provider_item(provider_item)] = pointer


def _select_user_items_by_token_budget(
    user_items: Sequence[Mapping[str, Any]],
    max_user_tokens: int,
) -> list[dict[str, Any]]:
    selected_reversed: list[dict[str, Any]] = []
    remaining_tokens = max(0, int(max_user_tokens))

    for item in reversed(user_items):
        if remaining_tokens <= 0:
            break
        text = read_message_text(item)
        tokens = local_compact_approx_token_count(text)
        if tokens <= remaining_tokens:
            selected_reversed.append(copy.deepcopy(dict(item)))
            remaining_tokens -= tokens
            continue

        truncated_text = truncate_text_to_approx_tokens(text, remaining_tokens)
        if truncated_text:
            selected_reversed.append(message_item_with_text(item, truncated_text))
        break

    selected_reversed.reverse()
    return selected_reversed


def _first_present_text(item: Mapping[str, Any]) -> str:
    if item.get("type") == "message":
        return read_message_text(item)
    for key in ("content", "text", "summary", "output"):
        if key in item:
            text = compact_text(item.get(key))
            if text:
                return text
    return ""
