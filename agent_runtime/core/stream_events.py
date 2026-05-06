from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from agent_runtime.core.canonical_types import CanonicalItem
else:
    CanonicalItem = Mapping[str, Any]


AdapterEventType = Literal[
    "text_delta",
    "reasoning_start",
    "reasoning_delta",
    "reasoning_done",
    "tool_call_ready",
    "provider_done",
    "round_reset",
    "error",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class AdapterStreamEvent:
    """Base class for provider-neutral runtime stream events."""

    type: AdapterEventType
    provider_raw: Any | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class TextDeltaEvent(AdapterStreamEvent):
    delta: str = ""
    type: Literal["text_delta"] = "text_delta"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReasoningStartEvent(AdapterStreamEvent):
    type: Literal["reasoning_start"] = "reasoning_start"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReasoningDeltaEvent(AdapterStreamEvent):
    delta: str = ""
    type: Literal["reasoning_delta"] = "reasoning_delta"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReasoningDoneEvent(AdapterStreamEvent):
    type: Literal["reasoning_done"] = "reasoning_done"


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallReadyEvent(AdapterStreamEvent):
    name: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    raw_arguments: str | None = None
    index: int | None = None
    type: Literal["tool_call_ready"] = "tool_call_ready"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderDoneEvent(AdapterStreamEvent):
    output_text: str = ""
    finish_reason: str | None = None
    usage: Mapping[str, Any] | None = None
    canonical_items: Sequence[CanonicalItem] = field(default_factory=tuple)
    type: Literal["provider_done"] = "provider_done"


@dataclass(frozen=True, slots=True, kw_only=True)
class RoundResetEvent(AdapterStreamEvent):
    reason: str | None = None
    type: Literal["round_reset"] = "round_reset"


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorEvent(AdapterStreamEvent):
    message: str = ""
    code: str | None = None
    retryable: bool = False
    type: Literal["error"] = "error"
