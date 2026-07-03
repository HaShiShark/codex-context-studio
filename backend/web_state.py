from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from simple_agent.agent import sanitize_text
from simple_agent.config import Settings

from backend.web_constants import (
    ATTACHMENTS_DIR,
    ATTACHMENTS_ROUTE,
    HIDDEN_WORKSPACE_ENTRIES,
    NEW_SESSION_TITLE,
    REPO_ROOT,
    SessionState,
    STATE_DIR,
    STATE_FILE,
)

from backend.web_context import (
    normalize_context_chat_history,
    normalize_transcript,
    read_jsonl_state_file,
    sanitize_value,
    utc_timestamp,
)
from backend.node_locking import normalize_node_locks


def is_relative_to_path(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def resolve_attachment_file_path(relative_path: str) -> Path | None:
    safe_relative_path = sanitize_text(relative_path or "").replace("\\", "/").lstrip("/")
    if not safe_relative_path:
        return None

    route_prefix = f"{ATTACHMENTS_ROUTE}/"
    if safe_relative_path.startswith(route_prefix):
        attachment_name = safe_relative_path.removeprefix(route_prefix).strip("/")
        if not attachment_name or "/" in attachment_name:
            return None

        attachments_root = ATTACHMENTS_DIR.resolve()
        candidate = (ATTACHMENTS_DIR / attachment_name).resolve()
        return candidate if is_relative_to_path(candidate, attachments_root) else None

    repo_root = REPO_ROOT.resolve()
    candidate = (REPO_ROOT / safe_relative_path).resolve()
    return candidate if is_relative_to_path(candidate, repo_root) else None


class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lock = threading.Lock()
        self.sessions: dict[str, SessionState] = {}
        self._persisted_session_payloads: dict[str, dict[str, Any]] = {}
        self._load_state()

    def refresh_settings(self, settings: Settings) -> None:
        with self.lock:
            self.settings = settings

    def get_session(self, session_id: str | None) -> SessionState:
        safe_session_id = sanitize_text(session_id or "").strip()
        if not safe_session_id:
            raise ValueError("session_id is required")

        with self.lock:
            session = self.sessions.get(safe_session_id)
            if session is None:
                raise ValueError("session not found")
            return session

    def acquire_session_request(self, session: SessionState, mode: str) -> str:
        safe_mode = sanitize_text(mode).strip()
        if safe_mode not in {"main", "context"}:
            raise ValueError("invalid session request mode")

        with self.lock:
            active_mode = sanitize_text(session.active_request_mode or "").strip()
            active_cancelled = bool(session.active_cancel_event and session.active_cancel_event.is_set())
            if active_mode and active_mode != safe_mode:
                raise ValueError("The main Codex request and context workbench request cannot run together.")
            if active_mode == safe_mode:
                if active_cancelled:
                    request_id = uuid.uuid4().hex
                    session.active_request_id = request_id
                    session.active_cancel_event = threading.Event()
                    return request_id
                if safe_mode == "main":
                    raise ValueError("The main Codex request is still running.")
                raise ValueError("The context workbench request is still running.")
            request_id = uuid.uuid4().hex
            session.active_request_mode = safe_mode
            session.active_request_id = request_id
            session.active_cancel_event = threading.Event()
            return request_id

    def release_session_request(self, session: SessionState, mode: str, request_id: str | None = None) -> None:
        safe_mode = sanitize_text(mode).strip()
        if safe_mode not in {"main", "context"}:
            return

        with self.lock:
            if request_id is not None and session.active_request_id != request_id:
                return
            if session.active_request_mode == safe_mode:
                session.active_request_mode = None
                session.active_request_id = None
                session.active_cancel_event = None

    def cancel_session_request(self, session: SessionState, mode: str) -> bool:
        safe_mode = sanitize_text(mode).strip()
        if safe_mode not in {"main", "context"}:
            raise ValueError("invalid session request mode")

        with self.lock:
            if session.active_request_mode != safe_mode or session.active_cancel_event is None:
                return False
            session.active_cancel_event.set()
            return True

    def is_session_request_cancelled(self, session: SessionState, request_id: str) -> bool:
        with self.lock:
            if session.active_request_id != request_id:
                return True
            return bool(session.active_cancel_event and session.active_cancel_event.is_set())

    def upsert_proxy_session(
        self,
        *,
        session_id: str,
        title: str,
        transcript: list[dict[str, object]],
        is_main_turn_running: bool = False,
        main_turn_id: str = "",
        main_turn_started_at: str = "",
        main_turn_updated_at: str = "",
        node_locks: dict[str, object] | None = None,
        node_lock_revision: int = 0,
    ) -> SessionState:
        safe_session_id = sanitize_text(session_id or "").strip()
        if not safe_session_id:
            raise ValueError("session_id is required")

        with self.lock:
            session = self.sessions.get(safe_session_id)
            created_session = False
            if session is None:
                session = SessionState(
                    session_id=safe_session_id,
                    title=sanitize_text(title or "").strip() or NEW_SESSION_TITLE,
                    transcript=[],
                    context_workbench_history=[],
                )
                self.sessions[safe_session_id] = session
                created_session = True

            active_mode = sanitize_text(session.active_request_mode or "").strip()
            active_request_id = sanitize_text(session.active_request_id or "").strip()
            next_transcript = normalize_transcript(transcript)
            next_title = sanitize_text(title or "").strip() or session.title or NEW_SESSION_TITLE
            next_main_turn_id = sanitize_text(main_turn_id or "").strip()
            next_main_turn_started_at = sanitize_text(main_turn_started_at or "").strip()
            next_main_turn_updated_at = sanitize_text(main_turn_updated_at or "").strip()
            has_node_locks_update = node_locks is not None
            next_node_locks = normalize_node_locks(node_locks or {})
            next_node_lock_revision = max(0, int(node_lock_revision or 0))
            should_persist = created_session

            if session.title != next_title:
                session.title = next_title
                should_persist = True
            if active_mode != "context" and has_node_locks_update:
                if session.node_locks != next_node_locks:
                    session.node_locks = next_node_locks
                    should_persist = True
                if int(session.node_lock_revision or 0) != next_node_lock_revision:
                    session.node_lock_revision = next_node_lock_revision
                    should_persist = True
            if active_mode != "context":
                transcript_changed = next_transcript != normalize_transcript(session.transcript)
                if transcript_changed:
                    session.transcript = next_transcript
                    should_persist = True
            if active_mode != "context":
                if is_main_turn_running:
                    safe_turn_request_id = next_main_turn_id or "main-turn-running"
                    if active_mode != "main" or active_request_id != safe_turn_request_id:
                        session.active_request_mode = "main"
                        session.active_request_id = safe_turn_request_id
                        session.active_cancel_event = threading.Event()
                    session.main_turn_id = next_main_turn_id
                    session.main_turn_started_at = next_main_turn_started_at
                    session.main_turn_updated_at = next_main_turn_updated_at
                elif active_mode == "main":
                    session.active_request_mode = None
                    session.active_request_id = None
                    session.active_cancel_event = None
                    session.main_turn_id = ""
                    session.main_turn_started_at = ""
                    session.main_turn_updated_at = ""
            if should_persist:
                self._save_state_locked()
            return session

    def append_context_workbench_turn(
        self,
        session: SessionState,
        *,
        user_message: str,
        answer: str,
    ) -> list[dict[str, str]]:
        with self.lock:
            session.context_workbench_history = normalize_context_chat_history(
                [
                    *session.context_workbench_history,
                    {"role": "user", "content": sanitize_text(user_message)},
                    {"role": "assistant", "content": sanitize_text(answer)},
                ]
            )
            self._save_state_locked()
            return sanitize_value(session.context_workbench_history)

    def delete_context_workbench_history_message(
        self,
        session: SessionState,
        *,
        message_index: int,
    ) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
        with self.lock:
            normalized_history = normalize_context_chat_history(session.context_workbench_history)
            if not normalized_history:
                raise ValueError("There is no context workbench message to delete.")

            safe_index = int(message_index)
            if safe_index < 0 or safe_index >= len(normalized_history):
                raise ValueError("message_index is out of range")

            session.context_workbench_history = [
                item
                for index, item in enumerate(normalized_history)
                if index != safe_index
            ]
            self._save_state_locked()
            return (
                sanitize_value(session.transcript),
                sanitize_value(session.context_workbench_history),
            )

    def clear_context_workbench_history(
        self,
        session: SessionState,
    ) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
        with self.lock:
            session.context_workbench_history = []
            self._save_state_locked()
            return (
                sanitize_value(session.transcript),
                [],
            )

    def apply_context_workbench_mutation(
        self,
        session: SessionState,
        *,
        transcript: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        with self.lock:
            session.transcript = normalize_transcript(transcript)
            self._save_state_locked()
            return sanitize_value(session.transcript)

    def bootstrap_payload(self, session_id: str = "", include_conversation: bool = True) -> dict[str, object]:
        with self.lock:
            safe_session_id = sanitize_text(session_id or "").strip()
            if not include_conversation:
                conversations = {}
            elif safe_session_id:
                conversations = (
                    {safe_session_id: sanitize_value(self.sessions[safe_session_id].transcript)}
                    if safe_session_id in self.sessions
                    else {}
                )
            else:
                conversations = self._conversation_map_locked()
            context_workbench_histories = (
                {safe_session_id: sanitize_value(self.sessions[safe_session_id].context_workbench_history)}
                if safe_session_id
                and safe_session_id in self.sessions
                and self.sessions[safe_session_id].context_workbench_history
                else ({} if safe_session_id else self._context_workbench_history_map_locked())
            )
            return {
                "settings": {
                    "workbench_model": sanitize_text(self.settings.context_workbench_model or "").strip(),
                    "theme_mode": "dark" if sanitize_text(self.settings.theme_mode or "").strip() == "dark" else "light",
                    "ui_font": sanitize_text(self.settings.ui_font or "").strip(),
                    "ui_font_size": int(self.settings.ui_font_size or 15),
                    "user_locale": sanitize_text(self.settings.user_locale or "").strip(),
                },
                "conversations": conversations,
                "context_workbench_histories": context_workbench_histories,
            }

    def _safe_session_path_part(self, session_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", sanitize_text(session_id).strip()).strip(".-")
        return safe or uuid.uuid4().hex

    def _session_storage_dir(self, session_id: str) -> Path:
        return STATE_DIR / "sessions" / self._safe_session_path_part(session_id)

    def _session_state_path(self, session_id: str, name: str) -> Path:
        return self._session_storage_dir(session_id) / name

    def _load_session_payload(self, session_id: str) -> dict[str, Any]:
        workbench_history = self._load_workbench_history(session_id)
        return {
            "transcript": [],
            "context_workbench_history": normalize_context_chat_history(workbench_history),
        }

    def _load_workbench_history(self, session_id: str) -> list[dict[str, str]]:
        path = self._session_state_path(session_id, "workbench.jsonl")
        if not path.exists():
            return []
        records: list[dict[str, str]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("role") in {"user", "assistant"}:
                records.append(
                    {
                        "role": sanitize_text(item.get("role") or ""),
                        "content": sanitize_text(item.get("content") or ""),
                    }
                )
        if records:
            return records
        return normalize_context_chat_history(read_jsonl_state_file(path, []))

    def _persist_session_payload(self, session: SessionState) -> None:
        previous = self._persisted_session_payloads.get(
            session.session_id,
            {
                "context_workbench_history": [],
            },
        )
        current = {
            "context_workbench_history": copy.deepcopy(session.context_workbench_history),
        }
        path = self._session_state_path(session.session_id, "workbench.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        if previous.get("context_workbench_history", []) != current["context_workbench_history"] or not path.exists():
            lines = [
                json.dumps(
                    {
                        "role": item.get("role"),
                        "content": item.get("content"),
                        "created_at": utc_timestamp(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                for item in normalize_context_chat_history(current["context_workbench_history"])
            ]
            path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
        self._persisted_session_payloads[session.session_id] = current

    def _load_state(self) -> None:
        raw_state: dict[str, Any] = {}
        if STATE_FILE.exists():
            try:
                raw_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw_state = {}

        sessions_data = raw_state.get("sessions")
        if isinstance(sessions_data, dict):
            for session_id, item in sessions_data.items():
                if not isinstance(item, dict):
                    continue
                safe_session_id = sanitize_text(session_id).strip()
                if not safe_session_id:
                    continue
                session_payload = self._load_session_payload(safe_session_id)
                session = SessionState(
                    session_id=safe_session_id,
                    title=sanitize_text(item.get("title") or NEW_SESSION_TITLE).strip() or NEW_SESSION_TITLE,
                    transcript=session_payload["transcript"],
                    context_workbench_history=session_payload["context_workbench_history"],
                    node_locks=normalize_node_locks(item.get("node_locks") or {}),
                    node_lock_revision=max(0, int(item.get("node_lock_revision") or 0)),
                )
                self._persisted_session_payloads[safe_session_id] = copy.deepcopy(session_payload)
                self.sessions[safe_session_id] = session

    def _save_state_locked(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        for session in self.sessions.values():
            self._persist_session_payload(session)
        payload = {
            "sessions": {
                session_id: {
                    "title": session.title,
                    "session_dir": self._session_storage_dir(session_id).relative_to(STATE_DIR).as_posix(),
                    "node_locks": copy.deepcopy(session.node_locks),
                    "node_lock_revision": int(session.node_lock_revision or 0),
                }
                for session_id, session in self.sessions.items()
            },
        }
        STATE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _context_workbench_history_map_locked(self) -> dict[str, list[dict[str, str]]]:
        return {
            session_id: sanitize_value(session.context_workbench_history)
            for session_id, session in self.sessions.items()
            if session.context_workbench_history
        }

    def _conversation_map_locked(self) -> dict[str, list[dict[str, object]]]:
        return {
            session_id: sanitize_value(session.transcript)
            for session_id, session in self.sessions.items()
        }


def should_show_workspace_entry(name: str) -> bool:
    return name not in HIDDEN_WORKSPACE_ENTRIES


def has_visible_children(directory_path: Path) -> bool:
    try:
        return any(should_show_workspace_entry(child.name) for child in directory_path.iterdir())
    except OSError:
        return False


def list_workspace_entries(project_root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for child in sorted(
        (entry for entry in project_root.iterdir() if should_show_workspace_entry(entry.name)),
        key=lambda item: (not item.is_dir(), item.name.lower()),
    )[:200]:
        entries.append(
            {
                "name": child.name,
                "type": "directory" if child.is_dir() else "file",
                "relative_path": child.relative_to(project_root).as_posix(),
                "has_children": child.is_dir() and has_visible_children(child),
            }
        )
    return entries
