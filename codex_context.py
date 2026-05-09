from __future__ import annotations

from typing import Any

CONTEXT_CONTROL_COMMANDS = {"ctx", "/ctx", "context", "/context"}


def text_value(value: Any) -> str:
    return str(value or "")


def is_context_control_command_text(text: Any) -> bool:
    return text_value(text).strip().lower() in CONTEXT_CONTROL_COMMANDS


def is_contextual_user_text(text: Any) -> bool:
    trimmed = text_value(text).lstrip()
    lowered = trimmed.lower()
    return (
        trimmed.startswith("# AGENTS.md instructions for ")
        or lowered.startswith("<environment_context>")
        or lowered.startswith("<skills>")
        or lowered.startswith("<user_shell_command>")
        or lowered.startswith("<turn_aborted>")
        or lowered.startswith("<subagent_notification>")
    )


def is_conversation_record(record: dict[str, Any]) -> bool:
    role = text_value(record.get("role")).strip()
    if role == "assistant":
        return True
    if role != "user":
        return False
    text = text_value(record.get("text"))
    return (
        bool(text.strip())
        and not is_contextual_user_text(text)
        and not is_context_control_command_text(text)
    )


def conversation_record_count(transcript: list[dict[str, Any]]) -> int:
    return sum(1 for record in transcript if is_conversation_record(record))
