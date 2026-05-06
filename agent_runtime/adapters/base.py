from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from agent_runtime.core.prompt_blocks import PromptBlock
from agent_runtime.core.stream_events import AdapterStreamEvent

if TYPE_CHECKING:
    from agent_runtime.core.canonical_types import CanonicalItem
    from agent_runtime.core.transcript_contract import TranscriptRecord
else:
    CanonicalItem = Mapping[str, Any]
    TranscriptRecord = Mapping[str, Any]


ProviderRequestT = TypeVar("ProviderRequestT")
REASONING_EFFORT_TOKEN_BUDGETS = {
    "low": 512,
    "medium": 2048,
    "high": 8192,
}


def reasoning_effort_token_budget(reasoning_effort: str | None) -> int | None:
    return REASONING_EFFORT_TOKEN_BUDGETS.get((reasoning_effort or "").strip())


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Provider-neutral tool schema only; execution stays in AgentCore."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderRequestContext:
    """Canonical runtime state available when an adapter builds a request."""

    prompt_blocks: Sequence[PromptBlock] = field(default_factory=tuple)
    transcript: Sequence[TranscriptRecord] = field(default_factory=tuple)
    current_turn: Sequence[CanonicalItem] = field(default_factory=tuple)
    tools: Sequence[ToolSpec | Mapping[str, Any]] = field(default_factory=tuple)
    provider_config: Mapping[str, Any] = field(default_factory=dict)
    model: str | None = None
    reasoning_effort: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class BaseAdapter(ABC, Generic[ProviderRequestT]):
    """Boundary for translating runtime state to and from a provider API."""

    provider_name = "base"

    @abstractmethod
    def build_request(self, context: ProviderRequestContext) -> ProviderRequestT:
        """Translate runtime state into a provider-specific request payload."""

    @abstractmethod
    def stream_response(
        self,
        request: ProviderRequestT,
        context: ProviderRequestContext | None = None,
    ) -> Iterable[AdapterStreamEvent]:
        """Translate a provider response stream into runtime events.

        Implementations must not execute tools. They only surface
        ToolCallReadyEvent instances for AgentCore or another runtime owner.
        """

    def estimate_tokens(self, context: ProviderRequestContext) -> int | None:
        """Optionally estimate tokens for provider-specific accounting."""

        return None
