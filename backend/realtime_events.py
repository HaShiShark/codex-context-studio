from __future__ import annotations

import copy
from typing import Any


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _session_id(session: dict[str, Any] | None, explicit: str = "") -> str:
    if explicit:
        return explicit
    if isinstance(session, dict):
        return str(session.get("id") or "")
    return ""


def _session_metadata(session: dict[str, Any]) -> dict[str, Any]:
    payload = _copy(session)
    payload.pop("transcript", None)
    return payload


def snapshot(
    *,
    session: dict[str, Any] | None,
    session_list: dict[str, Any],
    session_id: str = "",
) -> dict[str, Any]:
    safe_session_id = _session_id(session, session_id)
    return {
        "type": "snapshot",
        "session_id": safe_session_id,
        "session": _copy(session),
        "session_list": _copy(session_list),
        "usage_summary": _copy((session or {}).get("usage_summary") if session else {}),
    }


def session_list_update(session_list: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "session_list_update",
        "session_list": _copy(session_list),
    }


def session_status(session: dict[str, Any], *, reason: str) -> dict[str, Any]:
    return {
        "type": "session_status",
        "session_id": _session_id(session),
        "status": str(session.get("status") or "mirror"),
        "is_running": bool(session.get("is_running")),
        "last_error": str(session.get("last_error") or ""),
        "reason": reason,
        "session": _session_metadata(session),
    }


def transcript_update(session: dict[str, Any], *, reason: str) -> dict[str, Any]:
    return {
        "type": "transcript_update",
        "session_id": _session_id(session),
        "transcript_version": int(session.get("transcript_version") or 0),
        "reason": reason,
        "transcript": _copy(session.get("transcript") or []),
        "session": _copy(session),
    }


def transcript_patch(
    session: dict[str, Any],
    *,
    base_version: int,
    reason: str,
    ops: list[dict[str, Any]],
) -> dict[str, Any]:
    next_version = int(session.get("transcript_version") or 0)
    return {
        "type": "transcript_patch",
        "session_id": _session_id(session),
        "base_version": base_version,
        "next_version": next_version,
        "reason": reason,
        "ops": _copy(ops),
        "session": _session_metadata(session),
    }


def compact_update(session: dict[str, Any], *, phase: str) -> dict[str, Any]:
    return {
        "type": "compact_update",
        "session_id": _session_id(session),
        "phase": phase,
        "compact_pending": bool(session.get("compact_pending")),
        "compact_kind": str(session.get("compact_kind") or ""),
        "last_error": str(session.get("last_error") or ""),
        "session": _session_metadata(session),
    }


def usage_update(session_id: str, summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "usage_update",
        "session_id": session_id,
        "usage_summary": _copy(summary),
    }


def error_event(session_id: str, message: str, *, code: str = "") -> dict[str, Any]:
    return {
        "type": "error",
        "session_id": session_id,
        "message": message,
        "code": code,
    }
