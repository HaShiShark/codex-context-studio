from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REGISTRY_FILENAME = "codex-item-registry.json"
REGISTRY_RELATIVE_PATH = Path("shared") / REGISTRY_FILENAME


@dataclass(frozen=True, slots=True)
class CodexItemRegistry:
    schema_version: int
    tool_call_item_types: frozenset[str]
    paired_tool_call_item_types: frozenset[str]
    standalone_tool_call_item_types: frozenset[str]
    tool_output_item_types: frozenset[str]
    tool_output_types_by_call_type: dict[str, frozenset[str]]
    tool_call_types_by_output_type: dict[str, frozenset[str]]
    compaction_item_types: frozenset[str]
    subagent_message_item_types: frozenset[str]
    developer_context_item_types: frozenset[str]
    display_hints_by_item_type: dict[str, dict[str, str]]


def load_codex_item_registry(path: Path | None = None) -> CodexItemRegistry:
    registry_path = path or _resolve_registry_path()
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"Codex item registry is not readable: {registry_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Codex item registry is invalid JSON: {registry_path}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"Codex item registry must be a JSON object: {registry_path}")

    schema_version = payload.get("schema_version")
    if schema_version != 1:
        raise RuntimeError(
            f"Unsupported Codex item registry schema_version {schema_version!r}: {registry_path}"
        )

    tool_call_item_types = _required_string_set(payload, "tool_call_item_types", registry_path)
    standalone_tool_call_item_types = _required_string_set(
        payload, "standalone_tool_call_item_types", registry_path
    )
    tool_output_item_types = _required_string_set(payload, "tool_output_item_types", registry_path)
    output_types_by_call_type = _required_string_set_map(
        payload, "paired_tool_output_types_by_call_type", registry_path
    )
    compaction_item_types = _required_string_set(payload, "compaction_item_types", registry_path)
    subagent_message_item_types = _optional_string_set(payload, "subagent_message_item_types")
    developer_context_item_types = _optional_string_set(payload, "developer_context_item_types")
    display_hints = _optional_display_hints(payload)

    paired_call_types = frozenset(output_types_by_call_type)
    missing_paired_calls = paired_call_types - tool_call_item_types
    if missing_paired_calls:
        raise RuntimeError(
            "Codex item registry paired_tool_output_types_by_call_type contains call types "
            f"not listed in tool_call_item_types: {sorted(missing_paired_calls)}"
        )

    unknown_standalone_calls = standalone_tool_call_item_types - tool_call_item_types
    if unknown_standalone_calls:
        raise RuntimeError(
            "Codex item registry standalone_tool_call_item_types contains call types "
            f"not listed in tool_call_item_types: {sorted(unknown_standalone_calls)}"
        )

    for call_type, output_types in output_types_by_call_type.items():
        unknown_outputs = output_types - tool_output_item_types
        if unknown_outputs:
            raise RuntimeError(
                "Codex item registry paired_tool_output_types_by_call_type "
                f"for {call_type!r} contains unknown output types: {sorted(unknown_outputs)}"
            )

    call_types_by_output_type: dict[str, set[str]] = {}
    for call_type, output_types in output_types_by_call_type.items():
        for output_type in output_types:
            call_types_by_output_type.setdefault(output_type, set()).add(call_type)

    return CodexItemRegistry(
        schema_version=schema_version,
        tool_call_item_types=tool_call_item_types,
        paired_tool_call_item_types=paired_call_types,
        standalone_tool_call_item_types=standalone_tool_call_item_types,
        tool_output_item_types=tool_output_item_types,
        tool_output_types_by_call_type=output_types_by_call_type,
        tool_call_types_by_output_type={
            output_type: frozenset(call_types)
            for output_type, call_types in call_types_by_output_type.items()
        },
        compaction_item_types=compaction_item_types,
        subagent_message_item_types=subagent_message_item_types,
        developer_context_item_types=developer_context_item_types,
        display_hints_by_item_type=display_hints,
    )


def _resolve_registry_path() -> Path:
    candidates = [
        Path(__file__).resolve().parents[1] / REGISTRY_RELATIVE_PATH,
        Path.cwd() / REGISTRY_RELATIVE_PATH,
    ]
    pyinstaller_root = getattr(sys, "_MEIPASS", None)
    if pyinstaller_root:
        candidates.insert(0, Path(pyinstaller_root) / REGISTRY_RELATIVE_PATH)

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    searched = ", ".join(str(candidate) for candidate in candidates)
    raise RuntimeError(f"Codex item registry not found; searched: {searched}")


def _required_string_set(payload: dict[str, Any], key: str, path: Path) -> frozenset[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"Codex item registry {key!r} must be a non-empty string array: {path}")

    normalized = frozenset(_clean_string(entry) for entry in value)
    if "" in normalized or len(normalized) != len(value):
        raise RuntimeError(f"Codex item registry {key!r} contains blank or duplicate values: {path}")
    return normalized


def _optional_string_set(payload: dict[str, Any], key: str) -> frozenset[str]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        raise RuntimeError(f"Codex item registry {key!r} must be a string array")

    normalized = frozenset(_clean_string(entry) for entry in value)
    if "" in normalized or len(normalized) != len(value):
        raise RuntimeError(f"Codex item registry {key!r} contains blank or duplicate values")
    return normalized


def _required_string_set_map(
    payload: dict[str, Any],
    key: str,
    path: Path,
) -> dict[str, frozenset[str]]:
    value = payload.get(key)
    if not isinstance(value, dict) or not value:
        raise RuntimeError(f"Codex item registry {key!r} must be a non-empty object: {path}")

    result: dict[str, frozenset[str]] = {}
    for raw_key, raw_values in value.items():
        clean_key = _clean_string(raw_key)
        if not clean_key:
            raise RuntimeError(f"Codex item registry {key!r} contains a blank map key: {path}")
        if clean_key in result:
            raise RuntimeError(f"Codex item registry {key!r} contains duplicate map key {clean_key!r}: {path}")
        if not isinstance(raw_values, list) or not raw_values:
            raise RuntimeError(
                f"Codex item registry {key!r}.{clean_key} must be a non-empty string array: {path}"
            )
        clean_values = frozenset(_clean_string(entry) for entry in raw_values)
        if "" in clean_values or len(clean_values) != len(raw_values):
            raise RuntimeError(
                f"Codex item registry {key!r}.{clean_key} contains blank or duplicate values: {path}"
            )
        result[clean_key] = clean_values
    return result


def _optional_display_hints(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    value = payload.get("display_hints_by_item_type", {})
    if not isinstance(value, dict):
        raise RuntimeError("Codex item registry 'display_hints_by_item_type' must be an object")

    result: dict[str, dict[str, str]] = {}
    for raw_item_type, raw_hint in value.items():
        item_type = _clean_string(raw_item_type)
        if not item_type:
            raise RuntimeError("Codex item registry display hint contains a blank item type")
        if not isinstance(raw_hint, dict):
            raise RuntimeError(f"Codex item registry display hint for {item_type!r} must be an object")

        hint: dict[str, str] = {}
        for key in ("title", "event_name"):
            raw_value = raw_hint.get(key)
            if raw_value is None:
                continue
            clean_value = _clean_string(raw_value)
            if clean_value:
                hint[key] = clean_value
        result[item_type] = hint
    return result


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


CODEX_ITEM_REGISTRY = load_codex_item_registry()
