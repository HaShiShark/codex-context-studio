from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias

from agent_runtime.core.canonical_types import (
    CanonicalStatus,
    JsonObject,
    JsonValue,
    ProviderRaw,
)


ToolEventStatus: TypeAlias = CanonicalStatus | str


@dataclass(slots=True)
class ToolEventRecord:
    """Provider-neutral record of a tool execution visible to the product."""

    name: str
    arguments: JsonValue = None
    output_preview: str = ""
    raw_output: str = ""
    display_title: str = ""
    display_detail: str = ""
    display_result: str = ""
    status: ToolEventStatus = "completed"
    call_id: str = ""
    error: str = ""
    provider_raw: ProviderRaw | None = None
    metadata: JsonObject = field(default_factory=dict)


ToolEvent = ToolEventRecord


__all__ = [
    "ToolEvent",
    "ToolEventRecord",
    "ToolEventStatus",
]
