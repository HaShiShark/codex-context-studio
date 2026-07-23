from __future__ import annotations

from typing import Any

from backend.text_safety import sanitize_text, sanitize_value


def provider_message(
    role: str,
    text: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    safe_text = sanitize_text(text)
    if not attachments:
        return {"type": "message", "role": role, "content": safe_text}

    content: list[dict[str, Any]] = []
    if safe_text:
        content.append({"type": "input_text", "text": safe_text})
    content.extend(sanitize_value(attachments))
    return {"type": "message", "role": role, "content": content}


def result_preview(result: str, limit: int = 120) -> str:
    compact = sanitize_text(result).replace("\n", " ")
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."
