from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from agent_runtime.core.canonical_types import (
    CanonicalItem,
    JsonObject,
    ProviderRaw,
    TranscriptRole,
    assert_transcript_role,
)
from agent_runtime.core.tool_events import ToolEventRecord


AttachmentKind: TypeAlias = Literal["image", "file"]
TranscriptBlockKind: TypeAlias = Literal["text", "tool"]


@dataclass(slots=True)
class AttachmentRecord:
    name: str
    mime_type: str
    kind: AttachmentKind
    id: str = ""
    size_bytes: int = 0
    url: str = ""
    relative_path: str = ""
    metadata: JsonObject = field(default_factory=dict)


@dataclass(slots=True)
class TranscriptBlock:
    kind: TranscriptBlockKind
    text: str = ""
    tool_event: ToolEventRecord | None = None
    metadata: JsonObject = field(default_factory=dict)


@dataclass(slots=True)
class TranscriptRecord:
    """Product transcript record. Roles are intentionally limited."""

    role: TranscriptRole
    text: str
    attachments: list[AttachmentRecord] = field(default_factory=list)
    blocks: list[TranscriptBlock] = field(default_factory=list)
    tool_events: list[ToolEventRecord] = field(default_factory=list)
    canonical_items: list[CanonicalItem] = field(default_factory=list)
    provider_raw: ProviderRaw | None = None
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.role = assert_transcript_role(self.role)


def validate_transcript_record(record: TranscriptRecord) -> TranscriptRecord:
    record.role = assert_transcript_role(record.role)
    return record


__all__ = [
    "AttachmentKind",
    "AttachmentRecord",
    "TranscriptBlock",
    "TranscriptBlockKind",
    "TranscriptRecord",
    "validate_transcript_record",
]
