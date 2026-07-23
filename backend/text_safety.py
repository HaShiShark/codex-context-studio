from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any


def sanitize_text(value: Any) -> str:
    return "".join(
        char if not (0xD800 <= ord(char) <= 0xDFFF) else "\ufffd"
        for char in str(value)
    )


def sanitize_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return sanitize_value(asdict(value))
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {
            sanitize_value(key): sanitize_value(item)
            for key, item in value.items()
        }
    return value
