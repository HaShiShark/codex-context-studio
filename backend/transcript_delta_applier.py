"""Apply cursor suffix deltas to transcript.

This module mutates only the transcript list it is given.  It deliberately has
no HTTP, compaction, editing, or persistence state.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any

from .codex_input_cursor import fingerprint_provider_item, provider_items_equal


@dataclass(frozen=True)
class PopResult:
    requested: int
    removed: int
    tail_conflict: bool


@dataclass(frozen=True)
class AppendResult:
    appended: int


class TranscriptDeltaApplier:
    """Conservative pop plus codec-backed append for transcript deltas."""

    @staticmethod
    def pop(transcript: list[Any], pop_items: Sequence[Any]) -> PopResult:
        """Remove matching provider items from the transcript tail.

        ``pop_items`` is ordered like the old cursor suffix, so deletion checks
        it from the end.  On the first fingerprint mismatch, deletion stops and
        ``tail_conflict`` is returned as true.
        """

        removed = 0
        requested = len(pop_items)
        for expected_item in reversed(pop_items):
            tail = _tail_provider_item(transcript)
            if tail is None:
                return PopResult(
                    requested=requested,
                    removed=removed,
                    tail_conflict=True,
                )

            node_index, item_index, provider_item = tail
            if not provider_items_equal(provider_item, expected_item):
                return PopResult(
                    requested=requested,
                    removed=removed,
                    tail_conflict=True,
                )

            _remove_node_item(transcript, node_index, item_index, provider_item)
            removed += 1

        return PopResult(
            requested=requested,
            removed=removed,
            tail_conflict=False,
        )

    @staticmethod
    def append(
        transcript: list[Any],
        append_items: Sequence[Any],
        *,
        codec: Any | None = None,
    ) -> AppendResult:
        """Append provider items using TranscriptCodec grouping rules.

        Expected codec interface, in priority order:

        1. ``append_input_items(transcript, input_items)`` mutates ``transcript``
           in place, or returns the replacement transcript.
        2. ``to_input_items(transcript)`` plus ``to_transcript(input_items)``.
        3. ``transcript_to_input_items(transcript)`` plus
           ``input_items_to_transcript(input_items)``.

        The first form is preferred because it preserves existing node identity
        while applying only the suffix.  The second form is a compatibility
        fallback if the codec only exposes round-trip methods.
        """

        if not append_items:
            return AppendResult(appended=0)

        resolved_codec = _resolve_codec(codec)
        items = list(append_items)

        append_fn = getattr(resolved_codec, "append_input_items", None)
        if callable(append_fn):
            result = append_fn(transcript, items)
            _replace_transcript_if_returned(transcript, result)
            return AppendResult(appended=len(items))

        to_input_items = getattr(resolved_codec, "to_input_items", None)
        to_transcript = getattr(resolved_codec, "to_transcript", None)
        if not callable(to_input_items):
            to_input_items = getattr(resolved_codec, "transcript_to_input_items", None)
        if not callable(to_transcript):
            to_transcript = getattr(resolved_codec, "input_items_to_transcript", None)
        if callable(to_input_items) and callable(to_transcript):
            rebuilt = to_transcript(list(to_input_items(transcript)) + items)
            _replace_transcript_if_returned(transcript, rebuilt)
            return AppendResult(appended=len(items))

        raise TypeError(
            "TranscriptDeltaApplier.append requires a codec with "
            "append_input_items(transcript, input_items), to_input_items/"
            "to_transcript, or transcript_to_input_items/"
            "input_items_to_transcript."
        )


def _resolve_codec(codec: Any | None) -> Any:
    if codec is not None:
        return codec

    try:
        from . import transcript_codec
    except ImportError as exc:
        raise ImportError(
            "backend.transcript_codec is required for append. "
            "Expected interface: append_input_items(transcript, input_items), "
            "to_input_items/to_transcript, or transcript_to_input_items/"
            "input_items_to_transcript."
        ) from exc

    return getattr(transcript_codec, "TranscriptCodec", transcript_codec)


def _tail_provider_item(transcript: list[Any]) -> tuple[int, int, Any] | None:
    for node_index in range(len(transcript) - 1, -1, -1):
        node = transcript[node_index]
        items = _node_items(node)
        if not items:
            return None
        item_index = len(items) - 1
        provider_item = _provider_item(items[item_index])
        if provider_item is None:
            return None
        return node_index, item_index, provider_item
    return None


def _node_items(node: Any) -> list[Any] | None:
    if isinstance(node, MutableMapping):
        items = node.get("items")
    else:
        items = getattr(node, "items", None)
    return items if isinstance(items, list) else None


def _provider_item(node_item: Any) -> Any | None:
    if isinstance(node_item, Mapping) and "providerItem" in node_item:
        return node_item["providerItem"]
    return None


def _remove_node_item(
    transcript: list[Any],
    node_index: int,
    item_index: int,
    provider_item: Any,
) -> None:
    node = transcript[node_index]
    items = _node_items(node)
    if items is None:
        return

    _remove_source_map_entry(node, fingerprint_provider_item(provider_item), item_index)
    del items[item_index]
    if not items:
        del transcript[node_index]


def _remove_source_map_entry(node: Any, fingerprint: str, item_index: int) -> None:
    if not isinstance(node, MutableMapping):
        return
    source_map = node.get("source_map")
    if not isinstance(source_map, MutableMapping):
        return

    expected_pointer = f"items[{item_index}]"
    if source_map.get(fingerprint) == expected_pointer:
        del source_map[fingerprint]


def _replace_transcript_if_returned(transcript: list[Any], result: Any) -> None:
    if result is None or result is transcript:
        return
    transcript[:] = list(result)
