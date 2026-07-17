from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal


ADDITIONAL_TOOLS_ITEM_TYPE = "additional_tools"


@dataclass(frozen=True)
class BaseInstructionsLocation:
    transport: Literal["top_level", "lite_developer"]
    text: str
    input_index: int | None = None


def is_responses_lite_input(input_items: Any) -> bool:
    return bool(
        isinstance(input_items, list)
        and input_items
        and isinstance(input_items[0], Mapping)
        and str(input_items[0].get("type") or "").strip() == ADDITIONAL_TOOLS_ITEM_TYPE
    )


def find_base_instructions(body: Mapping[str, Any] | None) -> BaseInstructionsLocation | None:
    if not isinstance(body, Mapping):
        return None

    input_items = body.get("input")
    if is_responses_lite_input(input_items):
        base_index = _lite_base_instructions_index(input_items)
        if base_index is None:
            return None
        text = _message_text(input_items[base_index]).strip()
        if not text:
            return None
        return BaseInstructionsLocation(
            transport="lite_developer",
            text=text,
            input_index=base_index,
        )

    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        return BaseInstructionsLocation(
            transport="top_level",
            text=instructions.strip(),
        )
    return None


def apply_base_instructions_override(
    body: Mapping[str, Any],
    replacement: str | None,
) -> dict[str, Any]:
    next_body = copy.deepcopy(dict(body))
    replacement_text = str(replacement or "").strip()
    if not replacement_text:
        return next_body

    location = find_base_instructions(body)
    if location is None:
        return next_body

    if location.transport == "top_level":
        next_body["instructions"] = replacement_text
        return next_body

    input_items = next_body.get("input")
    if not isinstance(input_items, list) or location.input_index is None:
        return next_body
    provider_item = input_items[location.input_index]
    replacement_item = _message_item_with_text(provider_item, replacement_text)
    if replacement_item is not None:
        input_items[location.input_index] = replacement_item
    return next_body


def responses_lite_prefix_items(input_items: Sequence[Any] | None) -> list[Any]:
    """Return the canonical Lite prefix that Codex prepends before conversation history."""

    if not is_responses_lite_input(input_items):
        return []

    prefix: list[Any] = [copy.deepcopy(input_items[0])]
    for item in input_items[1:]:
        if not isinstance(item, Mapping):
            break
        role = str(item.get("role") or "").strip()
        if role not in {"developer", "system"}:
            break
        prefix.append(copy.deepcopy(item))
    return prefix


def _lite_base_instructions_index(input_items: Sequence[Any]) -> int | None:
    if len(input_items) < 2:
        return None
    candidate = input_items[1]
    if not isinstance(candidate, Mapping):
        return None
    if str(candidate.get("type") or "").strip() != "message":
        return None
    if str(candidate.get("role") or "").strip() != "developer":
        return None
    return 1


def _message_text(item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
        return ""

    parts: list[str] = []
    for part in content:
        if not isinstance(part, Mapping):
            continue
        text = part.get("text") or part.get("input_text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _message_item_with_text(item: Any, replacement: str) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    next_item = copy.deepcopy(dict(item))
    content = next_item.get("content")
    if isinstance(content, str):
        next_item["content"] = replacement
        return next_item
    if not isinstance(content, list):
        return None

    for index, part in enumerate(content):
        if not isinstance(part, Mapping):
            continue
        if "text" not in part and "input_text" not in part:
            continue
        next_part = copy.deepcopy(dict(part))
        if "text" in next_part:
            next_part["text"] = replacement
        else:
            next_part["input_text"] = replacement
        content[index] = next_part
        return next_item
    return None
