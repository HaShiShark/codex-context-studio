from __future__ import annotations

from dataclasses import field
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

if TYPE_CHECKING:
    from agent_runtime.core.tool_events import ToolEventRecord


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

PromptBlockKind: TypeAlias = Literal["system", "developer", "memory", "summary"]
TranscriptRole: TypeAlias = Literal["user", "assistant"]
CanonicalItemType: TypeAlias = Literal["message", "tool_call", "tool_result"]
CanonicalStatus: TypeAlias = Literal[
    "pending",
    "running",
    "completed",
    "error",
    "skipped",
]


@dataclass(slots=True)
class ProviderRaw:
    """Opaque provider payload kept out of product-level logic."""

    provider_id: str = ""
    model: str = ""
    request_id: str = ""
    event_type: str = ""
    payload: JsonValue = None
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PromptBlock:
    """Provider-neutral prompt material that is not transcript history."""

    kind: PromptBlockKind
    text: str
    editable: bool = False
    source: str = ""
    id: str = ""
    metadata: JsonObject = field(default_factory=dict)


@dataclass(slots=True)
class CanonicalItem:
    """Provider-neutral item used by adapters and context workbench."""

    type: CanonicalItemType
    role: TranscriptRole | None = None
    content: JsonValue = None
    name: str = ""
    call_id: str = ""
    arguments: JsonValue = None
    output: JsonValue = None
    status: CanonicalStatus | str = "completed"
    provider_raw: ProviderRaw | None = None
    metadata: JsonObject = field(default_factory=dict)


@dataclass(slots=True)
class AssistantRoundState:
    """Transient aggregation for one assistant round before transcript write."""

    round_id: str = ""
    answer_text: str = ""
    canonical_items: list[CanonicalItem] = field(default_factory=list)
    tool_events: list["ToolEventRecord"] = field(default_factory=list)
    provider_raw: ProviderRaw | None = None
    is_final: bool = False
    error: str = ""
    metadata: JsonObject = field(default_factory=dict)


def is_transcript_role(value: str) -> bool:
    return value in ("user", "assistant")


def assert_transcript_role(value: str) -> TranscriptRole:
    if not is_transcript_role(value):
        raise ValueError(f"transcript role must be user or assistant, got {value!r}")
    return value  # type: ignore[return-value]


__all__ = [
    "AssistantRoundState",
    "CanonicalItem",
    "CanonicalItemType",
    "CanonicalStatus",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "PromptBlock",
    "PromptBlockKind",
    "ProviderRaw",
    "TranscriptRole",
    "assert_transcript_role",
    "is_transcript_role",
]
