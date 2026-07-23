from __future__ import annotations

from abc import ABC, abstractmethod
from base64 import b64encode
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from types import GeneratorType
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from agent_runtime.core.prompt_blocks import PromptBlock
from agent_runtime.core.stream_events import AdapterStreamEvent

if TYPE_CHECKING:
    from agent_runtime.core.canonical_types import CanonicalItem
else:
    CanonicalItem = Mapping[str, Any]


ProviderRequestT = TypeVar("ProviderRequestT")
CancelCallback = Callable[[], None]
CancelRegistrar = Callable[[CancelCallback | None], None]
REASONING_EFFORT_TOKEN_BUDGETS = {
    "low": 512,
    "medium": 2048,
    "high": 8192,
}


def reasoning_effort_token_budget(reasoning_effort: str | None) -> int | None:
    return REASONING_EFFORT_TOKEN_BUDGETS.get((reasoning_effort or "").strip())


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Provider-neutral tool schema consumed by a runtime owner."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderRequestContext:
    """Canonical runtime state available when an adapter builds a request."""

    prompt_blocks: Sequence[PromptBlock] = field(default_factory=tuple)
    transcript: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    current_turn: Sequence[CanonicalItem] = field(default_factory=tuple)
    tools: Sequence[ToolSpec | Mapping[str, Any]] = field(default_factory=tuple)
    provider_config: Mapping[str, Any] = field(default_factory=dict)
    model: str | None = None
    reasoning_effort: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    register_cancel: CancelRegistrar | None = None


def register_stream_cancel(
    context: ProviderRequestContext | None,
    stream: Any,
) -> bool:
    """Register the stream's real network abort operation with the runtime.

    Registration happens only after the provider has returned its live stream.
    The runtime owns synchronization, including the race where cancellation was
    requested immediately before this callback was registered.
    """

    if context is None or context.register_cancel is None:
        return False

    callback = _stream_cancel_callback(stream)
    if callback is None:
        raise RuntimeError(
            "Provider stream does not expose close(), cancel(), or a context "
            "manager exit; request cancellation cannot be guaranteed"
        )
    context.register_cancel(callback)
    return True


def clear_stream_cancel(context: ProviderRequestContext | None) -> None:
    if context is not None and context.register_cancel is not None:
        context.register_cancel(None)


def provider_replay_payload(item: Any, provider_id: str) -> Any | None:
    provider_raw = _get_value(item, "provider_raw")
    if _get_value(provider_raw, "provider_id") != provider_id:
        return None
    return to_plain_value(_get_value(provider_raw, "payload"))


def to_plain_value(value: Any) -> Any:
    """Convert SDK models/dataclasses to stable JSON-compatible values."""

    if isinstance(value, (bytes, bytearray)):
        return b64encode(bytes(value)).decode("ascii")
    if is_dataclass(value) and not isinstance(value, type):
        return to_plain_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): to_plain_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_plain_value(item) for item in value]

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return to_plain_value(
                model_dump(mode="json", by_alias=True, exclude_none=True)
            )
        except TypeError:
            return to_plain_value(model_dump())

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_plain_value(to_dict())
    if hasattr(value, "__dict__"):
        return to_plain_value(vars(value))
    return value


def _stream_cancel_callback(stream: Any) -> CancelCallback | None:
    # generator.close() raises ``ValueError: generator already executing`` when
    # another thread is blocked in iteration; it is not a transport abort.
    if isinstance(stream, GeneratorType):
        return None

    close = getattr(stream, "close", None)
    if callable(close):
        return close

    cancel = getattr(stream, "cancel", None)
    if callable(cancel):
        return cancel

    exit_context = getattr(stream, "__exit__", None)
    if callable(exit_context):
        return lambda: exit_context(None, None, None)
    return None


def _get_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


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
        ToolCallReadyEvent instances for the runtime owner.
        """

    def estimate_tokens(self, context: ProviderRequestContext) -> int | None:
        """Optionally estimate tokens for provider-specific accounting."""

        return None
