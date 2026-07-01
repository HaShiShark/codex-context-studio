from __future__ import annotations

import copy
import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STORAGE_VERSION = 1
DEFAULT_SESSION_TITLE = "Codex Session"


@dataclass(frozen=True)
class StoredSessionPayload:
    metadata: dict[str, Any]
    transcript: list[dict[str, Any]]
    cursor: list[Any]
    workbench_history: list[dict[str, Any]]
    path: Path


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_session_path_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return safe or f"sess_{uuid.uuid4().hex}"


def json_loads_value(value: str | None, default: Any) -> Any:
    if not value:
        return copy.deepcopy(default)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return copy.deepcopy(default)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(default)
    return loaded


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(dict(record), ensure_ascii=False, separators=(",", ":"))
        for record in records
        if isinstance(record, Mapping)
    ]
    payload = "\n".join(lines)
    if payload:
        payload += "\n"
    path.write_text(payload, encoding="utf-8")


class ProxySessionStorage:
    """Filesystem storage for the canonical proxy session layout."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    @property
    def sessions_root(self) -> Path:
        return self.root / "sessions"

    def session_dir(self, session_id: str) -> Path:
        return self.sessions_root / safe_session_path_part(session_id)

    def session_json_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "session.json"

    def transcript_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "transcript.json"

    def cursor_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "cursor.json"

    def workbench_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "workbench.jsonl"

    def attachments_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "attachments"

    def load_index(self) -> dict[str, Any]:
        raw = read_json(self.index_path, {})
        if not isinstance(raw, dict):
            raw = {}
        sessions = raw.get("sessions")
        return {
            "version": STORAGE_VERSION,
            "active_session_id": str(raw.get("active_session_id") or ""),
            "sessions": [item for item in sessions if isinstance(item, dict)] if isinstance(sessions, list) else [],
        }

    def load_all_sessions(self) -> tuple[str, list[StoredSessionPayload]]:
        index = self.load_index()
        payloads: list[StoredSessionPayload] = []
        seen: set[str] = set()

        for entry in index.get("sessions", []):
            session_id = str(entry.get("id") or "").strip()
            if not session_id or session_id in seen:
                continue
            loaded = self.load_session(session_id)
            if loaded is not None:
                payloads.append(loaded)
                seen.add(session_id)

        session_dirs = sorted(self.sessions_root.glob("*")) if self.sessions_root.exists() else []
        for session_dir in session_dirs:
            if not session_dir.is_dir():
                continue
            metadata = read_json(session_dir / "session.json", {})
            if not isinstance(metadata, dict):
                continue
            session_id = str(metadata.get("id") or "").strip()
            if not session_id or session_id in seen:
                continue
            loaded = self.load_session(session_id)
            if loaded is not None:
                payloads.append(loaded)
                seen.add(session_id)

        payloads.sort(key=lambda item: str(item.metadata.get("updated_at") or ""), reverse=True)
        active_session_id = str(index.get("active_session_id") or "")
        return active_session_id, payloads

    def load_session(self, session_id: str) -> StoredSessionPayload | None:
        metadata = read_json(self.session_json_path(session_id), {})
        if not isinstance(metadata, dict):
            return None

        stored_id = str(metadata.get("id") or session_id).strip()
        if not stored_id:
            return None

        transcript_payload = read_json(self.transcript_path(stored_id), {})
        cursor_payload = read_json(self.cursor_path(stored_id), {})
        transcript = transcript_payload.get("nodes") if isinstance(transcript_payload, dict) else []
        cursor = cursor_payload.get("items") if isinstance(cursor_payload, dict) else []

        return StoredSessionPayload(
            metadata=self._normalize_metadata(stored_id, metadata),
            transcript=copy.deepcopy(transcript) if isinstance(transcript, list) else [],
            cursor=copy.deepcopy(cursor) if isinstance(cursor, list) else [],
            workbench_history=read_jsonl(self.workbench_path(stored_id)),
            path=self.session_dir(stored_id),
        )

    def save_all(
        self,
        sessions: Iterable[Any],
        *,
        active_session_id: str = "",
        usage_summary: Callable[[Any], dict[str, Any]] | None = None,
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        index_sessions: list[dict[str, Any]] = []
        for session in sorted(sessions, key=lambda item: str(getattr(item, "updated_at", "")), reverse=True):
            self.save_session(session, usage_summary=usage_summary)
            index_sessions.append(self._index_entry(session))

        write_json(
            self.index_path,
            {
                "version": STORAGE_VERSION,
                "active_session_id": active_session_id,
                "sessions": index_sessions,
            },
        )

    def save_session(
        self,
        session: Any,
        *,
        usage_summary: Callable[[Any], dict[str, Any]] | None = None,
    ) -> None:
        session_id = str(getattr(session, "id", "") or "").strip()
        if not session_id:
            return

        session_dir = self.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_dir(session_id).mkdir(parents=True, exist_ok=True)

        metadata = self._metadata_from_session(session, usage_summary=usage_summary)
        write_json(session_dir / "session.json", metadata)
        write_json(
            session_dir / "transcript.json",
            {
                "version": STORAGE_VERSION,
                "updated_at": str(getattr(session, "updated_at", "") or utc_timestamp()),
                "nodes": copy.deepcopy(getattr(getattr(session, "proxy_state", None), "transcript", [])),
            },
        )
        write_json(
            session_dir / "cursor.json",
            {
                "version": STORAGE_VERSION,
                "updated_at": str(getattr(session, "updated_at", "") or utc_timestamp()),
                "items": copy.deepcopy(getattr(getattr(session, "proxy_state", None), "codex_input_cursor", [])),
            },
        )
        workbench_history = getattr(session, "workbench_history", []) or []
        workbench_path = session_dir / "workbench.jsonl"
        if workbench_history or not workbench_path.exists():
            write_jsonl(workbench_path, workbench_history)

    def update_index(
        self,
        sessions: Iterable[Any],
        *,
        active_session_id: str = "",
    ) -> None:
        write_json(
            self.index_path,
            {
                "version": STORAGE_VERSION,
                "active_session_id": active_session_id,
                "sessions": [
                    self._index_entry(session)
                    for session in sorted(sessions, key=lambda item: str(getattr(item, "updated_at", "")), reverse=True)
                ],
            },
        )

    def session_exists(self, session_id: str) -> bool:
        return self.session_json_path(session_id).exists()

    def _normalize_metadata(self, session_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        created_at = str(metadata.get("created_at") or utc_timestamp())
        updated_at = str(metadata.get("updated_at") or created_at)
        return {
            "version": STORAGE_VERSION,
            "id": session_id,
            "title": str(metadata.get("title") or DEFAULT_SESSION_TITLE),
            "created_at": created_at,
            "updated_at": updated_at,
            "last_codex_session_headers": copy.deepcopy(metadata.get("last_codex_session_headers") or {}),
            "last_turn_metadata_header": str(metadata.get("last_turn_metadata_header") or ""),
            "tail_conflict": bool(metadata.get("tail_conflict")),
            "compact_pending": bool(metadata.get("compact_pending")),
            "compact_kind": str(metadata.get("compact_kind") or ""),
            "transcript_version": int(metadata.get("transcript_version") or 0),
            "last_error": str(metadata.get("last_error") or ""),
            "status": str(metadata.get("status") or "mirror"),
            "usage_events": copy.deepcopy(metadata.get("usage_events") or []),
            "usage_summary": copy.deepcopy(metadata.get("usage_summary") or {}),
        }

    def _metadata_from_session(
        self,
        session: Any,
        *,
        usage_summary: Callable[[Any], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        proxy_state = getattr(session, "proxy_state", None)
        summary = usage_summary(session) if usage_summary is not None else {}
        return {
            "version": STORAGE_VERSION,
            "id": str(getattr(session, "id", "") or ""),
            "title": str(getattr(session, "title", "") or DEFAULT_SESSION_TITLE),
            "created_at": str(getattr(session, "created_at", "") or utc_timestamp()),
            "updated_at": str(getattr(session, "updated_at", "") or utc_timestamp()),
            "last_codex_session_headers": copy.deepcopy(getattr(session, "last_codex_session_headers", {}) or {}),
            "last_turn_metadata_header": str(getattr(session, "last_turn_metadata_header", "") or ""),
            "tail_conflict": bool(getattr(proxy_state, "tail_conflict", False)),
            "compact_pending": bool(getattr(proxy_state, "compact_pending", False)),
            "compact_kind": str(getattr(proxy_state, "compact_kind", "") or ""),
            "transcript_version": int(getattr(session, "transcript_version", 0) or 0),
            "last_error": str(getattr(session, "last_error", "") or ""),
            "status": str(getattr(session, "status", "") or "mirror"),
            "usage_events": copy.deepcopy(getattr(session, "usage_events", []) or []),
            "usage_summary": copy.deepcopy(summary),
        }

    def _index_entry(self, session: Any) -> dict[str, Any]:
        session_id = str(getattr(session, "id", "") or "")
        return {
            "id": session_id,
            "title": str(getattr(session, "title", "") or DEFAULT_SESSION_TITLE),
            "created_at": str(getattr(session, "created_at", "") or utc_timestamp()),
            "updated_at": str(getattr(session, "updated_at", "") or utc_timestamp()),
            "path": f"sessions/{safe_session_path_part(session_id)}",
        }
