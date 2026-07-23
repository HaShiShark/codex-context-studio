from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Literal, TypeAlias


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

PromptBlockKind: TypeAlias = Literal["system", "developer", "memory", "summary"]
# agent_runtime is a lightweight agent adapter layer. Its transcript role is not
# the lossless proxy TranscriptNode.role contract from backend/transcript_codec.py.
TranscriptRole: TypeAlias = Literal["user", "assistant"]
CanonicalItemType: TypeAlias = Literal[
    "message",
    "reasoning",
    "tool_call",
    "tool_result",
    "provider_item",
]
CanonicalStatus: TypeAlias = Literal[
    "pending",
    "running",
    "completed",
    "error",
    "skipped",
]


@dataclass(slots=True)
class ProviderRaw:
    """Opaque, request-replayable provider payload.

    ``payload`` is intentionally outside product logic.  Adapters use it only
    when the next request targets the same provider, preserving fields such as
    Responses reasoning items, Claude thinking signatures, and Gemini thought
    signatures that have no safe cross-provider representation.
    """

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
    """Provider-neutral item used to replay one complete model/tool round.

    The generic fields let the runtime execute tools without knowing a provider
    protocol.  ``provider_raw`` keeps the exact provider-native assistant item
    so the originating adapter can replay required opaque fields on the next
    tool round.
    """

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


def is_transcript_role(value: str) -> bool:
    return value in ("user", "assistant")


def assert_transcript_role(value: str) -> TranscriptRole:
    if not is_transcript_role(value):
        raise ValueError(f"transcript role must be user or assistant, got {value!r}")
    return value  # type: ignore[return-value]


__all__ = [
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
