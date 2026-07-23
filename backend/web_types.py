from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ToolEvent:
    name: str
    arguments: dict[str, Any]
    output_preview: str
    raw_output: str = ""
    display_title: str = ""
    display_detail: str = ""
    display_result: str = ""
    status: str = "completed"


@dataclass(slots=True)
class ToolExecution:
    output_text: str
    display_title: str
    display_detail: str
    display_result: str
    status: str = "completed"


@dataclass(slots=True)
class BridgedFunctionCall:
    name: str
    arguments: str
    call_id: str = ""
