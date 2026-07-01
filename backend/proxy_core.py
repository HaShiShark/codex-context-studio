from __future__ import annotations

import copy
import importlib
import importlib.util
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .codex_input_cursor import canonical_provider_items_for_request, compute_diff
from .transcript_codec import transcript_to_input_items
from .transcript_delta_applier import TranscriptDeltaApplier


_AUTO_COMPACT_CONTROLLER = object()


@dataclass
class ProxyState:
    """Minimal business state for the unified proxy path."""

    transcript: list[Any] = field(default_factory=list)
    codex_input_cursor: list[Any] = field(default_factory=list)
    tail_conflict: bool = False
    compact_pending: bool = False
    compact_kind: str = ""
    compact_error: str | None = None


@dataclass(frozen=True)
class ResponseCompletedResult:
    appended: int
    compact_handled: bool
    compact_controller_used: bool


def handle_request(
    state: ProxyState,
    body: Mapping[str, Any],
    *,
    compact_controller: Any = _AUTO_COMPACT_CONTROLLER,
) -> dict[str, Any]:
    """Apply one Codex request to transcript and return the upstream body."""

    forwarded_body = copy.deepcopy(dict(body))
    if "input" not in forwarded_body:
        raise KeyError("handle_request expected body['input']")
    raw_new_input = forwarded_body["input"]
    if not isinstance(raw_new_input, list):
        raise TypeError("handle_request expected body['input'] to be a list")
    new_input = canonical_provider_items_for_request(raw_new_input)
    cursor_after_request = copy.deepcopy(new_input)

    compact_meta = _compact_metadata(forwarded_body)
    if compact_meta is not None:
        state.compact_pending = True
        state.compact_kind = _compact_kind(compact_meta)
        state.compact_error = None

    diff = compute_diff(state.codex_input_cursor, new_input)

    state.tail_conflict = False
    pop_result = TranscriptDeltaApplier.pop(state.transcript, diff.pop)
    state.tail_conflict = pop_result.tail_conflict
    TranscriptDeltaApplier.append(state.transcript, diff.append)

    controller = _resolve_compact_controller(compact_controller)
    if state.compact_pending and controller is not None:
        _replace_compact_prompt(controller, state)
    elif state.compact_pending:
        # TODO(compact_controller): C-owned local compact module can replace
        # the built-in compact prompt by mutating state.transcript here.
        # ProxyCore deliberately does not implement remote compact.
        pass

    forwarded_body["input"] = transcript_to_input_items(state.transcript)
    state.codex_input_cursor = cursor_after_request
    return forwarded_body


def handle_response_completed(
    state: ProxyState,
    response_items: Sequence[Any],
    text: str = "",
    *,
    compact_controller: Any = _AUTO_COMPACT_CONTROLLER,
) -> ResponseCompletedResult:
    """Absorb completed upstream response items into cursor/transcript."""

    items = canonical_provider_items_for_request(list(response_items))

    if state.compact_pending:
        controller = _resolve_compact_controller(compact_controller)
        if controller is not None:
            _on_compact_success(controller, state, items, text)
            return ResponseCompletedResult(
                appended=0,
                compact_handled=True,
                compact_controller_used=True,
            )

        state.compact_pending = False
        state.compact_kind = ""
        state.compact_error = "compact_controller unavailable; compact response was not applied"
        return ResponseCompletedResult(
            appended=0,
            compact_handled=True,
            compact_controller_used=False,
        )

    if items:
        TranscriptDeltaApplier.append(state.transcript, items)
        state.codex_input_cursor.extend(copy.deepcopy(items))

    return ResponseCompletedResult(
        appended=len(items),
        compact_handled=False,
        compact_controller_used=False,
    )


def _compact_metadata(body: Mapping[str, Any]) -> dict[str, Any] | None:
    client_metadata = body.get("client_metadata")
    if not isinstance(client_metadata, Mapping):
        return None

    raw_metadata = client_metadata.get("x-codex-turn-metadata")
    if isinstance(raw_metadata, Mapping):
        turn_metadata = dict(raw_metadata)
    elif isinstance(raw_metadata, str) and raw_metadata.strip():
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        turn_metadata = parsed
    else:
        return None

    if turn_metadata.get("request_kind") != "compaction":
        return None
    return turn_metadata


def _compact_kind(turn_metadata: Mapping[str, Any]) -> str:
    trigger = turn_metadata.get("trigger")
    if trigger in {"auto", "manual"}:
        return str(trigger)
    return ""


def _resolve_compact_controller(compact_controller: Any) -> Any | None:
    if compact_controller is not _AUTO_COMPACT_CONTROLLER:
        return compact_controller

    package_name = __package__ or "backend"
    module_name = f"{package_name}.compact_controller"
    if importlib.util.find_spec(module_name) is None:
        return None
    return importlib.import_module(module_name)


def _replace_compact_prompt(controller: Any, state: ProxyState) -> None:
    replace_prompt = getattr(controller, "replace_compact_prompt", None)
    if callable(replace_prompt):
        replace_prompt(state)


def _on_compact_success(
    controller: Any,
    state: ProxyState,
    response_items: list[Any],
    text: str,
) -> None:
    on_success = getattr(controller, "on_compact_success", None)
    if not callable(on_success):
        state.compact_pending = False
        state.compact_kind = ""
        state.compact_error = "compact_controller.on_compact_success is unavailable"
        return
    on_success(state, response_items, text=text)
