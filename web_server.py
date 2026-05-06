from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote, urlparse, urlunparse

from dotenv import load_dotenv

try:
    import tiktoken
except ImportError:  # pragma: no cover - dependency fallback for partially installed environments
    tiktoken = None

from simple_agent.agent import BridgedFunctionCall, SimpleAgent, ToolEvent, sanitize_text, sanitize_value
from simple_agent.config import (
    CODEX_PROXY_BASE_URL,
    CODEX_PROXY_PROVIDER_ID,
    Settings,
    _UNSET,
    load_settings,
    save_settings,
)
from simple_agent.tools import ToolExecution


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_PAGE = REPO_ROOT / "hash.html"
REACT_DIST_DIR = REPO_ROOT / "react_app" / "dist"
RAW_STATE_DIR = Path(os.getenv("HASH_DATA_DIR", str(REPO_ROOT / "data"))).expanduser()
STATE_DIR = RAW_STATE_DIR if RAW_STATE_DIR.is_absolute() else (REPO_ROOT / RAW_STATE_DIR).resolve()
STATE_FILE = STATE_DIR / "hash_web_state.json"
PROXY_STATE_FILE = STATE_DIR / "proxy_state.json"
CODEX_LOCAL_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CONTEXT_REQUEST_DEBUG_FILE = STATE_DIR / "context_request_debug.ndjson"
CONTEXT_EDIT_MARKERS_FILE = STATE_DIR / "context_edit_markers.json"
ATTACHMENTS_DIR = STATE_DIR / "uploads"
ATTACHMENTS_ROUTE = "uploads"
DEFAULT_PROJECT_ID = "project_root"
NEW_PROJECT_PREFIX = "新项目"
NEW_SESSION_TITLE = "新对话"
HIDDEN_WORKSPACE_ENTRIES = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "tmp_cherry_extract",
}
_TOKEN_ENCODING: Any | None = None
_TOKEN_ENCODING_LOAD_FAILED = False
CONTEXT_INPUT_MESSAGE_ROLES = {"system", "developer", "user", "assistant"}
CONTEXT_INPUT_RECORD_ROLES = {*CONTEXT_INPUT_MESSAGE_ROLES, "compaction", "context"}
CODEX_PAIRED_TOOL_CALL_ITEM_TYPES = {
    "function_call",
    "local_shell_call",
    "custom_tool_call",
    "tool_search_call",
}
CODEX_STANDALONE_TOOL_CALL_ITEM_TYPES = {
    "web_search_call",
    "image_generation_call",
}
CODEX_TOOL_CALL_ITEM_TYPES = {
    *CODEX_PAIRED_TOOL_CALL_ITEM_TYPES,
    *CODEX_STANDALONE_TOOL_CALL_ITEM_TYPES,
}
CODEX_TOOL_OUTPUT_ITEM_TYPES = {
    "function_call_output",
    "custom_tool_call_output",
    "mcp_tool_call_output",
    "tool_search_output",
    "local_shell_call_output",
}
CODEX_TOOL_OUTPUT_TYPES_BY_CALL_TYPE = {
    "function_call": {"function_call_output", "mcp_tool_call_output"},
    "local_shell_call": {"function_call_output", "local_shell_call_output"},
    "custom_tool_call": {"custom_tool_call_output"},
    "tool_search_call": {"tool_search_output"},
}
CODEX_TOOL_CALL_TYPES_BY_OUTPUT_TYPE: dict[str, set[str]] = {}
for _call_type, _output_types in CODEX_TOOL_OUTPUT_TYPES_BY_CALL_TYPE.items():
    for _output_type in _output_types:
        CODEX_TOOL_CALL_TYPES_BY_OUTPUT_TYPE.setdefault(_output_type, set()).add(_call_type)
CONTEXT_EDITABLE_PROVIDER_ITEM_TYPES = {
    "message",
    "reasoning",
    "compaction",
    "compaction_summary",
    *CODEX_TOOL_CALL_ITEM_TYPES,
    *CODEX_TOOL_OUTPUT_ITEM_TYPES,
}


def is_relative_to_path(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def attachment_url_path(stored_name: str) -> str:
    return f"{ATTACHMENTS_ROUTE}/{stored_name}"


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
DEFAULT_REASONING_OPTIONS = [
    {"value": "default", "label": "自动"},
    {"value": "none", "label": "关闭"},
    {"value": "low", "label": "低"},
    {"value": "medium", "label": "中"},
    {"value": "high", "label": "高"},
]
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 50 * 1024 * 1024
DATA_URL_PATTERN = re.compile(r"^data:(?P<mime>[^;,]+);base64,(?P<data>.+)$")
TITLE_GENERATION_INSTRUCTIONS = "\n".join(
    [
        "你只负责给一段新对话起标题。",
        "标题要短、具体、自然，优先使用用户的语言。",
        "不要解释，不要加引号，不要使用 Markdown。",
        "最多 18 个中文字符或 8 个英文单词。",
    ]
)


class ClientDisconnectedError(BrokenPipeError):
    """Raised when the front-end intentionally closes a stream early."""


class RequestCancelledError(RuntimeError):
    """Raised when the user explicitly stops the active request."""


@dataclass(slots=True)
class SessionState:
    session_id: str
    title: str
    scope: str
    project_id: str | None
    agent: SimpleAgent
    transcript: list[dict[str, object]]
    context_workbench_history: list[dict[str, str]]
    context_revisions: list[dict[str, object]]
    pending_context_restore: dict[str, object] | None
    active_request_mode: str | None = None
    active_request_id: str | None = None
    active_cancel_event: threading.Event | None = None


@dataclass(slots=True)
class ProjectState:
    project_id: str
    title: str
    session_ids: list[str]
    root_path: str | None = None
    archived_session_ids: list[str] | None = None


@dataclass(slots=True)
class ContextWorkbenchToolDefinition:
    name: str
    label: str
    description: str
    parameters: dict[str, Any]
    status: str
    handler: Callable[[dict[str, Any]], ToolExecution]

    def to_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def to_catalog_item(self) -> dict[str, str]:
        return {
            "id": self.name,
            "label": self.label,
            "description": self.description,
            "status": self.status,
        }


class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lock = threading.Lock()
        self.projects: list[ProjectState] = []
        self.chat_session_ids: list[str] = []
        self.sessions: dict[str, SessionState] = {}
        self._load_state()

    def refresh_settings(self, settings: Settings) -> None:
        with self.lock:
            self.settings = settings
            for session in self.sessions.values():
                session.agent = SimpleAgent(self._settings_for_session_locked(session))
                self._hydrate_agent_locked(session)
            self._save_state_locked()

    def create_project(self, title: str | None = None, root_path: str | None = None) -> ProjectState:
        with self.lock:
            normalized_root_path = self._coerce_project_root_path(root_path)
            project = ProjectState(
                project_id=uuid.uuid4().hex,
                title=self._coerce_project_title(title, normalized_root_path),
                session_ids=[],
                root_path=normalized_root_path,
                archived_session_ids=[],
            )
            self.projects.insert(0, project)
            self._save_state_locked()
            return project

    def pin_project(self, project_id: str | None) -> ProjectState:
        safe_project_id = sanitize_text(project_id or "").strip()
        if not safe_project_id:
            raise ValueError("project_id is required")

        with self.lock:
            project = self._find_project_locked(safe_project_id)
            if project is None:
                raise ValueError("project not found")
            self.projects = [item for item in self.projects if item.project_id != safe_project_id]
            self.projects.insert(0, project)
            self._save_state_locked()
            return project

    def rename_project(self, project_id: str | None, title: str | None) -> ProjectState:
        safe_project_id = sanitize_text(project_id or "").strip()
        safe_title = sanitize_text(title or "").strip()
        if not safe_project_id:
            raise ValueError("project_id is required")
        if not safe_title:
            raise ValueError("project title is required")

        with self.lock:
            project = self._find_project_locked(safe_project_id)
            if project is None:
                raise ValueError("project not found")
            project.title = safe_title
            self._save_state_locked()
            return project

    def archive_project_sessions(self, project_id: str | None) -> tuple[ProjectState, list[str]]:
        safe_project_id = sanitize_text(project_id or "").strip()
        if not safe_project_id:
            raise ValueError("project_id is required")

        with self.lock:
            project = self._find_project_locked(safe_project_id)
            if project is None:
                raise ValueError("project not found")
            archived_session_ids = list(project.session_ids)
            existing_archived_ids = list(project.archived_session_ids or [])
            for session_id in archived_session_ids:
                if session_id not in existing_archived_ids:
                    existing_archived_ids.insert(0, session_id)
            project.session_ids = []
            project.archived_session_ids = existing_archived_ids
            self._save_state_locked()
            return project, archived_session_ids

    def create_session(
        self,
        *,
        scope: str = "chat",
        project_id: str | None = None,
    ) -> SessionState:
        normalized_scope = self._normalize_scope(scope)

        with self.lock:
            target_project_id: str | None = None
            if normalized_scope == "project":
                project = self._find_project_locked(project_id) or self._ensure_default_project_locked()
                target_project_id = project.project_id
            session = SessionState(
                session_id=uuid.uuid4().hex,
                title=NEW_SESSION_TITLE,
                scope=normalized_scope,
                project_id=target_project_id,
                agent=SimpleAgent(self._settings_for_project_locked(target_project_id)),
                transcript=[],
                context_workbench_history=[],
                context_revisions=[],
                pending_context_restore=None,
            )
            ensure_initial_context_revision(session)
            self.sessions[session.session_id] = session
            self._insert_session_locked(session)
            self._save_state_locked()
            return session

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
                raise ValueError("当前主聊天和上下文工作区不能并行，请等这一轮先结束。")
            if active_mode == safe_mode:
                if active_cancelled:
                    request_id = uuid.uuid4().hex
                    session.active_request_id = request_id
                    session.active_cancel_event = threading.Event()
                    return request_id
                if safe_mode == "main":
                    raise ValueError("当前这条主对话还没结束。")
                raise ValueError("当前上下文工作区还在处理中。")
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

    def touch_session(self, session_id: str) -> None:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                return
            self._remove_session_from_lists_locked(session_id)
            self._insert_session_locked(session)
            self._save_state_locked()

    def upsert_proxy_session(
        self,
        *,
        session_id: str,
        title: str,
        transcript: list[dict[str, object]],
        is_running: bool = False,
    ) -> SessionState:
        safe_session_id = sanitize_text(session_id or "").strip()
        if not safe_session_id:
            raise ValueError("session_id is required")

        with self.lock:
            session = self.sessions.get(safe_session_id)
            if session is None:
                session = SessionState(
                    session_id=safe_session_id,
                    title=sanitize_text(title or "").strip() or "Codex Context",
                    scope="chat",
                    project_id=None,
                    agent=SimpleAgent(self._settings_for_project_locked(None)),
                    transcript=[],
                    context_workbench_history=[],
                    context_revisions=[],
                    pending_context_restore=None,
                )
                self.sessions[safe_session_id] = session
                self._insert_session_locked(session)

            active_mode = sanitize_text(session.active_request_mode or "").strip()
            active_request_id = sanitize_text(session.active_request_id or "").strip()
            next_transcript = normalize_transcript(transcript)

            session.title = sanitize_text(title or "").strip() or session.title or "Codex Context"
            session.scope = "chat"
            session.project_id = None
            if active_mode != "context":
                transcript_changed = next_transcript != normalize_transcript(session.transcript)
                session.transcript = next_transcript
                if transcript_changed:
                    session.pending_context_restore = None
            if active_mode != "context":
                if is_running:
                    session.active_request_mode = "main"
                    session.active_request_id = "proxy-running"
                    session.active_cancel_event = threading.Event()
                elif active_mode == "main" and active_request_id == "proxy-running":
                    session.active_request_mode = None
                    session.active_request_id = None
                    session.active_cancel_event = None
            ensure_initial_context_revision(session)
            sync_active_context_revision_snapshot(session)
            self._hydrate_agent_locked(session)
            self._remove_session_from_lists_locked(session.session_id)
            self._insert_session_locked(session)
            self._save_state_locked()
            return session

    def reset_session(self, session_id: str) -> SessionState:
        session = self.get_session(session_id)
        with self.lock:
            session.agent.reset()
            session.title = NEW_SESSION_TITLE
            session.transcript = []
            session.context_workbench_history = []
            session.context_revisions = []
            session.pending_context_restore = None
            ensure_initial_context_revision(session)
            self._save_state_locked()
        return session

    def truncate_session(self, session_id: str, from_index: int) -> SessionState:
        session = self.get_session(session_id)
        with self.lock:
            safe_index = max(0, min(from_index, len(session.transcript)))
            session.transcript = session.transcript[:safe_index]
            session.context_workbench_history = []
            session.context_revisions = []
            session.pending_context_restore = None
            ensure_initial_context_revision(session)
            self._hydrate_agent_locked(session)
            if not session.transcript:
                session.title = NEW_SESSION_TITLE
            self._save_state_locked()
        return session

    def delete_transcript_message(
        self,
        session_id: str,
        message_index: int,
    ) -> SessionState:
        session = self.get_session(session_id)
        with self.lock:
            normalized_transcript = normalize_transcript(session.transcript)
            if not normalized_transcript:
                raise ValueError("当前没有可删除的消息")

            safe_index = int(message_index)
            if safe_index < 0 or safe_index >= len(normalized_transcript):
                raise ValueError("message_index is out of range")

            session.transcript = [
                record
                for index, record in enumerate(normalized_transcript)
                if index != safe_index
            ]
            ensure_initial_context_revision(session)
            sync_active_context_revision_snapshot(session)
            self._hydrate_agent_locked(session)
            if not session.transcript:
                session.title = NEW_SESSION_TITLE
            self._save_state_locked()
        return session

    def delete_session(self, session_id: str) -> SessionState:
        session = self.get_session(session_id)
        with self.lock:
            self.sessions.pop(session.session_id, None)
            self._remove_session_from_lists_locked(session.session_id)
            self._save_state_locked()
        return session

    def delete_project(self, project_id: str | None) -> tuple[ProjectState, list[str]]:
        safe_project_id = sanitize_text(project_id or "").strip()
        if not safe_project_id:
            raise ValueError("project_id is required")

        with self.lock:
            project_index = next(
                (index for index, project in enumerate(self.projects) if project.project_id == safe_project_id),
                None,
            )
            if project_index is None:
                raise ValueError("project not found")

            project = self.projects.pop(project_index)
            deleted_session_ids = list(project.session_ids)
            for session_id in deleted_session_ids:
                self.sessions.pop(session_id, None)

            self._save_state_locked()
            return project, deleted_session_ids

    def rename_session_from_message(self, session: SessionState, message: str) -> None:
        compact = summarize_title(message)
        with self.lock:
            if session.title == NEW_SESSION_TITLE and compact:
                session.title = compact
                self._save_state_locked()

    def should_name_session_from_first_message(self, session: SessionState) -> bool:
        with self.lock:
            return session.title == NEW_SESSION_TITLE and not normalize_transcript(session.transcript)

    def name_session_from_first_message(
        self,
        session: SessionState,
        message: str,
        *,
        model: str | None = None,
    ) -> None:
        safe_message = sanitize_text(message).strip()
        if not safe_message:
            return

        with self.lock:
            if session.title != NEW_SESSION_TITLE or normalize_transcript(session.transcript):
                return

        title = generate_session_title(
            self.settings,
            safe_message,
            model=model,
        )
        if not title:
            return

        with self.lock:
            if session.title == NEW_SESSION_TITLE and not normalize_transcript(session.transcript):
                session.title = title
                self._save_state_locked()

    def name_session_from_first_message_async(
        self,
        session: SessionState,
        message: str,
        *,
        model: str | None = None,
    ) -> None:
        safe_message = sanitize_text(message).strip()
        if not safe_message:
            return

        fallback_title = summarize_title(safe_message)
        if not fallback_title:
            return

        with self.lock:
            if session.title != NEW_SESSION_TITLE or normalize_transcript(session.transcript):
                return

            session.title = fallback_title
            session_id = session.session_id
            self._save_state_locked()

        def worker() -> None:
            title = generate_session_title(
                self.settings,
                safe_message,
                model=model,
            )
            if not title or title == fallback_title:
                return

            with self.lock:
                target_session = self.sessions.get(session_id)
                if target_session is None or target_session.title != fallback_title:
                    return

                target_session.title = title
                self._save_state_locked()

        threading.Thread(
            target=worker,
            name=f"hash-title-{session_id}",
            daemon=True,
        ).start()

    def append_context_workbench_turn(
        self,
        session: SessionState,
        *,
        user_message: str,
        answer: str,
    ) -> list[dict[str, str]]:
        with self.lock:
            session.pending_context_restore = None
            session.context_workbench_history = normalize_context_chat_history(
                [
                    *session.context_workbench_history,
                    {"role": "user", "content": sanitize_text(user_message)},
                    {"role": "assistant", "content": sanitize_text(answer)},
                ]
            )
            ensure_initial_context_revision(session)
            sync_active_context_revision_snapshot(session)
            self._save_state_locked()
            return sanitize_value(session.context_workbench_history)

    def delete_context_workbench_history_message(
        self,
        session: SessionState,
        *,
        message_index: int,
    ) -> tuple[list[dict[str, object]], list[dict[str, str]], list[dict[str, object]], dict[str, object] | None]:
        with self.lock:
            normalized_history = normalize_context_chat_history(session.context_workbench_history)
            if not normalized_history:
                raise ValueError("当前没有可删除的手动消息")

            safe_index = int(message_index)
            if safe_index < 0 or safe_index >= len(normalized_history):
                raise ValueError("message_index is out of range")

            session.context_workbench_history = [
                item
                for index, item in enumerate(normalized_history)
                if index != safe_index
            ]
            session.pending_context_restore = None
            sync_active_context_revision_snapshot(session)
            self._save_state_locked()
            return (
                sanitize_value(session.transcript),
                sanitize_value(session.context_workbench_history),
                context_revision_summaries(session.context_revisions),
                None,
            )

    def clear_context_workbench_history(
        self,
        session: SessionState,
    ) -> tuple[list[dict[str, object]], list[dict[str, str]], list[dict[str, object]], dict[str, object] | None]:
        with self.lock:
            session.context_workbench_history = []
            session.pending_context_restore = None
            sync_active_context_revision_snapshot(session)
            self._save_state_locked()
            return (
                sanitize_value(session.transcript),
                [],
                context_revision_summaries(session.context_revisions),
                None,
            )

    def apply_context_workbench_mutation(
        self,
        session: SessionState,
        *,
        transcript: list[dict[str, object]],
        revision_label: str,
        revision_summary: str,
        operations: list[dict[str, object]],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object] | None]:
        with self.lock:
            ensure_initial_context_revision(session)
            next_revision_number = next_context_revision_number(session.context_revisions)
            session.transcript = normalize_transcript(transcript)
            session.pending_context_restore = None
            mark_active_context_revision(session.context_revisions, None)
            session.context_revisions.append(
                build_context_revision_entry(
                    transcript=session.transcript,
                    context_workbench_history=session.context_workbench_history,
                    revision_label=revision_label,
                    revision_summary=revision_summary,
                    operations=operations,
                    revision_number=next_revision_number,
                )
            )
            self._hydrate_agent_locked(session)
            self._save_state_locked()
            return (
                sanitize_value(session.transcript),
                context_revision_summaries(session.context_revisions),
                None,
            )

    def restore_context_revision(
        self,
        session: SessionState,
        revision_id: str,
    ) -> tuple[list[dict[str, object]], list[dict[str, str]], list[dict[str, object]], dict[str, object]]:
        with self.lock:
            safe_revision_id = sanitize_text(revision_id).strip()
            target = next(
                (
                    revision
                    for revision in reversed(session.context_revisions)
                    if sanitize_text(revision.get("id") or "").strip() == safe_revision_id
                ),
                None,
            )
            if target is None:
                raise ValueError("revision not found")

            raw_snapshot = target.get("snapshot")
            snapshot = normalize_transcript(raw_snapshot)
            if not snapshot and session.transcript and "snapshot" not in target:
                raise ValueError("target revision snapshot is unavailable")
            workbench_history_snapshot = normalize_context_chat_history(
                target.get("context_workbench_history_snapshot")
            )

            undo_active_revision_id = find_active_context_revision_id(session.context_revisions)
            session.pending_context_restore = {
                "undo_transcript": sanitize_value(session.transcript),
                "undo_context_workbench_history": sanitize_value(session.context_workbench_history),
                "target_revision_id": safe_revision_id,
                "target_label": sanitize_text(target.get("label") or "").strip() or "Revision",
                "created_at": utc_timestamp(),
                "undo_active_revision_id": undo_active_revision_id or "",
            }
            session.transcript = snapshot
            session.context_workbench_history = workbench_history_snapshot
            mark_active_context_revision(session.context_revisions, safe_revision_id)
            sync_active_context_revision_snapshot(session)
            self._hydrate_agent_locked(session)
            self._save_state_locked()
            return (
                sanitize_value(session.transcript),
                sanitize_value(session.context_workbench_history),
                context_revision_summaries(session.context_revisions),
                context_pending_restore_payload(session.pending_context_restore),
            )

    def undo_context_restore(
        self,
        session: SessionState,
    ) -> tuple[list[dict[str, object]], list[dict[str, str]], list[dict[str, object]], dict[str, object] | None]:
        with self.lock:
            pending_restore = session.pending_context_restore
            if not isinstance(pending_restore, dict):
                raise ValueError("there is no context restore to undo")

            undo_transcript = normalize_transcript(pending_restore.get("undo_transcript"))
            undo_context_workbench_history = normalize_context_chat_history(
                pending_restore.get("undo_context_workbench_history")
            )
            undo_active_revision_id = sanitize_text(pending_restore.get("undo_active_revision_id") or "").strip()
            session.transcript = undo_transcript
            session.context_workbench_history = undo_context_workbench_history
            session.pending_context_restore = None
            mark_active_context_revision(session.context_revisions, undo_active_revision_id or None)
            sync_active_context_revision_snapshot(session)
            self._hydrate_agent_locked(session)
            self._save_state_locked()
            return (
                sanitize_value(session.transcript),
                sanitize_value(session.context_workbench_history),
                context_revision_summaries(session.context_revisions),
                None,
            )

    def append_turn(
        self,
        session: SessionState,
        *,
        user_message: str,
        answer: str,
        tool_events: list[ToolEvent],
        assistant_blocks: list[dict[str, object]] | None = None,
        user_attachments: list[dict[str, object]] | None = None,
    ) -> None:
        with self.lock:
            session.pending_context_restore = None
            safe_user_message = sanitize_text(user_message)
            safe_user_attachments = normalize_attachment_records(user_attachments)
            user_record_index = len(session.transcript)
            user_blocks = (
                [{"kind": "text", "text": safe_user_message}]
                if safe_user_message
                else []
            )
            safe_assistant_blocks = sanitize_value(assistant_blocks or [])
            assistant_text = message_blocks_to_text(safe_assistant_blocks) or sanitize_text(answer)
            assistant_record_index = user_record_index + 1
            assistant_tool_events = [serialize_tool_event(event) for event in tool_events]
            session.transcript.append(
                {
                    "role": "user",
                    "text": safe_user_message,
                    "attachments": safe_user_attachments,
                    "toolEvents": [],
                    "blocks": user_blocks,
                    "providerItems": build_provider_items_for_record(
                        role="user",
                        text=safe_user_message,
                        attachments=safe_user_attachments,
                        tool_events=[],
                        blocks=user_blocks,
                        record_index=user_record_index,
                    ),
                }
            )
            session.transcript.append(
                {
                    "role": "assistant",
                    "text": assistant_text,
                    "attachments": [],
                    "toolEvents": assistant_tool_events,
                    "blocks": safe_assistant_blocks,
                    "providerItems": build_provider_items_for_record(
                        role="assistant",
                        text=assistant_text,
                        attachments=[],
                        tool_events=assistant_tool_events,
                        blocks=safe_assistant_blocks,
                        record_index=assistant_record_index,
                    ),
                }
            )
            ensure_initial_context_revision(session)
            sync_active_context_revision_snapshot(session)
            self._hydrate_agent_locked(session)
            self._remove_session_from_lists_locked(session.session_id)
            self._insert_session_locked(session)
            self._save_state_locked()

    def bootstrap_payload(self) -> dict[str, object]:
        with self.lock:
            self._ensure_default_project_locked()
            return {
                "project_name": self.settings.project_root.name or str(self.settings.project_root),
                "project_root": str(self.settings.project_root),
                "default_model": self.settings.model,
                "models": model_options(self.settings.model, active_provider_models(self.settings)),
                "reasoning_options": DEFAULT_REASONING_OPTIONS,
                "settings": settings_payload(self.settings),
                "projects": self._projects_payload_locked(),
                "chat_sessions": self._chat_sessions_payload_locked(),
                "conversations": self._conversation_map_locked(),
                "context_workbench_histories": self._context_workbench_history_map_locked(),
                "context_revision_histories": self._context_revision_map_locked(),
                "pending_context_restores": self._pending_context_restore_map_locked(),
            }

    def sidebar_payload(self) -> dict[str, object]:
        with self.lock:
            return {
                "projects": self._projects_payload_locked(),
                "chat_sessions": self._chat_sessions_payload_locked(),
            }

    def session_payload(self, session: SessionState) -> dict[str, object]:
        return {
            "id": session.session_id,
            "title": session.title,
            "scope": session.scope,
            "project_id": session.project_id,
        }

    def _load_state(self) -> None:
        raw_state: dict[str, Any] = {}
        if STATE_FILE.exists():
            try:
                raw_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw_state = {}

        projects_data = raw_state.get("projects")
        if isinstance(projects_data, list):
            for item in projects_data:
                if not isinstance(item, dict):
                    continue
                project_id = sanitize_text(item.get("id") or uuid.uuid4().hex).strip()
                title = sanitize_text(item.get("title") or "").strip()
                session_ids = [
                    sanitize_text(session_id).strip()
                    for session_id in item.get("session_ids", [])
                    if sanitize_text(session_id).strip()
                ]
                if not title:
                    continue
                archived_session_ids = [
                    sanitize_text(session_id).strip()
                    for session_id in item.get("archived_session_ids", [])
                    if sanitize_text(session_id).strip()
                ]
                root_path = self._coerce_project_root_path(item.get("root_path"))
                self.projects.append(
                    ProjectState(
                        project_id=project_id,
                        title=title,
                        session_ids=session_ids,
                        root_path=root_path,
                        archived_session_ids=archived_session_ids,
                    )
                )

        sessions_data = raw_state.get("sessions")
        if isinstance(sessions_data, dict):
            for session_id, item in sessions_data.items():
                if not isinstance(item, dict):
                    continue
                safe_session_id = sanitize_text(session_id).strip()
                if not safe_session_id:
                    continue
                scope = self._normalize_scope(item.get("scope"))
                project_id = sanitize_text(item.get("project_id") or "").strip() or None
                transcript = normalize_transcript(item.get("transcript"))
                session = SessionState(
                    session_id=safe_session_id,
                    title=sanitize_text(item.get("title") or NEW_SESSION_TITLE).strip() or NEW_SESSION_TITLE,
                    scope=scope,
                    project_id=project_id if scope == "project" else None,
                    agent=SimpleAgent(self._settings_for_project_locked(project_id if scope == "project" else None)),
                    transcript=transcript,
                    context_workbench_history=normalize_context_chat_history(item.get("context_workbench_history")),
                    context_revisions=normalize_context_revision_entries(item.get("context_revisions")),
                    pending_context_restore=normalize_pending_context_restore(item.get("pending_context_restore")),
                )
                self._hydrate_agent_locked(session)
                self.sessions[safe_session_id] = session

        raw_chat_session_ids = raw_state.get("chat_session_ids", [])
        if isinstance(raw_chat_session_ids, list):
            self.chat_session_ids = [
                sanitize_text(session_id).strip()
                for session_id in raw_chat_session_ids
                if sanitize_text(session_id).strip()
            ]

        with self.lock:
            self._repair_state_locked()
            self._save_state_locked()

    def _repair_state_locked(self) -> None:
        default_project = self._ensure_default_project_locked()

        known_project_ids = {project.project_id for project in self.projects}
        for project in self.projects:
            cleaned_ids: list[str] = []
            for session_id in project.session_ids:
                session = self.sessions.get(session_id)
                if session is None:
                    continue
                if session.scope != "project":
                    continue
                if session.project_id != project.project_id:
                    session.project_id = project.project_id
                if session_id not in cleaned_ids:
                    cleaned_ids.append(session_id)
            project.session_ids = cleaned_ids

            cleaned_archived_ids: list[str] = []
            for session_id in project.archived_session_ids or []:
                session = self.sessions.get(session_id)
                if session is None:
                    continue
                if session.scope != "project":
                    continue
                if session.project_id != project.project_id:
                    session.project_id = project.project_id
                if session_id not in cleaned_archived_ids:
                    cleaned_archived_ids.append(session_id)
            project.archived_session_ids = cleaned_archived_ids

        cleaned_chat_ids: list[str] = []
        for session_id in self.chat_session_ids:
            session = self.sessions.get(session_id)
            if session is None or session.scope != "chat":
                continue
            if session_id not in cleaned_chat_ids:
                cleaned_chat_ids.append(session_id)
        self.chat_session_ids = cleaned_chat_ids

        referenced_session_ids = set(self.chat_session_ids)
        for project in self.projects:
            referenced_session_ids.update(project.session_ids)
            referenced_session_ids.update(project.archived_session_ids or [])

        for session in self.sessions.values():
            ensure_initial_context_revision(session)
            if session.scope == "chat":
                if session.session_id not in referenced_session_ids:
                    self.chat_session_ids.append(session.session_id)
                continue

            if session.project_id not in known_project_ids:
                session.project_id = default_project.project_id

            owning_project = self._find_project_locked(session.project_id) or default_project
            if (
                session.session_id not in owning_project.session_ids
                and session.session_id not in (owning_project.archived_session_ids or [])
            ):
                owning_project.session_ids.append(session.session_id)

    def _save_state_locked(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "projects": [
                {
                    "id": project.project_id,
                    "title": project.title,
                    "session_ids": project.session_ids,
                    "archived_session_ids": project.archived_session_ids or [],
                    "root_path": project.root_path or "",
                }
                for project in self.projects
            ],
            "chat_session_ids": self.chat_session_ids,
            "sessions": {
                session_id: {
                    "title": session.title,
                    "scope": session.scope,
                    "project_id": session.project_id,
                    "transcript": sanitize_value(session.transcript),
                    "context_workbench_history": sanitize_value(session.context_workbench_history),
                    "context_revisions": sanitize_value(session.context_revisions),
                    "pending_context_restore": sanitize_value(session.pending_context_restore),
                }
                for session_id, session in self.sessions.items()
            },
        }
        STATE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _ensure_default_project_locked(self) -> ProjectState:
        project = self._find_project_locked(DEFAULT_PROJECT_ID)
        title = self.settings.project_root.name or str(self.settings.project_root)
        if project is not None:
            if not project.title:
                project.title = title
            if not project.root_path:
                project.root_path = str(self.settings.project_root)
            if project.archived_session_ids is None:
                project.archived_session_ids = []
            return project

        project = ProjectState(
            project_id=DEFAULT_PROJECT_ID,
            title=title,
            session_ids=[],
            root_path=str(self.settings.project_root),
            archived_session_ids=[],
        )
        self.projects.append(project)
        return project

    def _find_project_locked(self, project_id: str | None) -> ProjectState | None:
        safe_project_id = sanitize_text(project_id or "").strip()
        if not safe_project_id:
            return None
        for project in self.projects:
            if project.project_id == safe_project_id:
                return project
        return None

    def _projects_payload_locked(self) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = []
        for project in self.projects:
            payload.append(
                {
                    "id": project.project_id,
                    "title": project.title,
                    "root_path": project.root_path or "",
                    "sessions": [
                        self.session_payload(self.sessions[session_id])
                        for session_id in project.session_ids
                        if session_id in self.sessions
                    ],
                }
            )
        return payload

    def _context_workbench_history_map_locked(self) -> dict[str, list[dict[str, str]]]:
        return {
            session_id: sanitize_value(session.context_workbench_history)
            for session_id, session in self.sessions.items()
            if session.context_workbench_history
        }

    def _context_revision_map_locked(self) -> dict[str, list[dict[str, object]]]:
        return {
            session_id: context_revision_summaries(session.context_revisions)
            for session_id, session in self.sessions.items()
            if session.context_revisions
        }

    def _pending_context_restore_map_locked(self) -> dict[str, dict[str, object]]:
        return {
            session_id: context_pending_restore_payload(session.pending_context_restore)
            for session_id, session in self.sessions.items()
            if session.pending_context_restore
        }

    def _chat_sessions_payload_locked(self) -> list[dict[str, object]]:
        return [
            self.session_payload(self.sessions[session_id])
            for session_id in self.chat_session_ids
            if session_id in self.sessions
        ]

    def _conversation_map_locked(self) -> dict[str, list[dict[str, object]]]:
        return {
            session_id: sanitize_value(session.transcript)
            for session_id, session in self.sessions.items()
        }

    def _insert_session_locked(self, session: SessionState) -> None:
        if session.scope == "project":
            project = self._find_project_locked(session.project_id) or self._ensure_default_project_locked()
            session.project_id = project.project_id
            project.session_ids.insert(0, session.session_id)
            return

        self.chat_session_ids.insert(0, session.session_id)

    def _remove_session_from_lists_locked(self, session_id: str) -> None:
        if session_id in self.chat_session_ids:
            self.chat_session_ids.remove(session_id)
        for project in self.projects:
            if session_id in project.session_ids:
                project.session_ids.remove(session_id)

    def _coerce_project_title(self, raw_title: str | None, root_path: str | None = None) -> str:
        safe_title = sanitize_text(raw_title or "").strip()
        if safe_title:
            return safe_title

        if root_path:
            path_title = Path(root_path).name
            if path_title:
                return path_title

        existing_titles = {project.title for project in self.projects}
        index = 1
        while True:
            candidate = f"{NEW_PROJECT_PREFIX} {index}"
            if candidate not in existing_titles:
                return candidate
            index += 1

    def _coerce_project_root_path(self, raw_root_path: Any) -> str | None:
        safe_root_path = sanitize_text(raw_root_path or "").strip()
        if not safe_root_path:
            return None

        try:
            root_path = Path(safe_root_path).expanduser()
            if not root_path.is_absolute():
                root_path = (REPO_ROOT / root_path).resolve()
            else:
                root_path = root_path.resolve()
        except (OSError, RuntimeError, ValueError):
            return None

        return str(root_path) if root_path.is_dir() else None

    def _settings_for_session_locked(self, session: SessionState) -> Settings:
        return self._settings_for_project_locked(session.project_id if session.scope == "project" else None)

    def _settings_for_project_locked(self, project_id: str | None) -> Settings:
        project = self._find_project_locked(project_id)
        root_path = self.settings.project_root
        if project and project.root_path:
            try:
                candidate = Path(project.root_path).expanduser().resolve()
                if candidate.is_dir():
                    root_path = candidate
            except (OSError, RuntimeError, ValueError):
                root_path = self.settings.project_root
        return replace(self.settings, project_root=root_path)

    def _normalize_scope(self, raw_scope: Any) -> str:
        return "project" if sanitize_text(raw_scope or "").strip() == "project" else "chat"

    def _hydrate_agent_locked(self, session: SessionState) -> None:
        session.agent.reset()
        session.agent.history = []
        normalized_transcript = normalize_transcript(session.transcript)
        session.transcript = normalized_transcript
        for record_index, record in enumerate(normalized_transcript):
            role = sanitize_text(record.get("role") or "").strip()
            if role not in {"user", "assistant"}:
                continue

            provider_items = build_provider_items_for_record(
                role=role,
                text=sanitize_text(record.get("text") or ""),
                attachments=normalize_attachment_records(record.get("attachments")),
                tool_events=sanitize_value(record.get("toolEvents")) if isinstance(record.get("toolEvents"), list) else [],
                blocks=normalize_message_blocks(record.get("blocks")),
                record_index=record_index,
            )
            session.agent.history.extend(provider_items)


def summarize_title(message: str) -> str:
    compact = " ".join(sanitize_text(message).split())
    if not compact:
        return NEW_SESSION_TITLE
    if len(compact) <= 18:
        return compact
    return f"{compact[:18]}..."


def clean_generated_title(raw_title: str) -> str:
    safe_title = sanitize_text(raw_title).strip()
    if not safe_title:
        return ""

    first_line = next((line.strip() for line in safe_title.splitlines() if line.strip()), "")
    if not first_line:
        return ""

    cleaned = first_line.strip(" \t\r\n\"'`“”‘’「」『』《》")
    cleaned = re.sub(r"^(标题|对话标题)\s*[:：]\s*", "", cleaned).strip()
    cleaned = cleaned.rstrip("。.!！?？")
    if not cleaned or cleaned == NEW_SESSION_TITLE:
        return ""
    if len(cleaned) <= 18:
        return cleaned
    return f"{cleaned[:18]}..."


def generate_session_title(
    settings: Settings,
    message: str,
    *,
    model: str | None = None,
) -> str:
    safe_message = sanitize_text(message).strip()
    fallback_title = summarize_title(safe_message)
    if not safe_message:
        return fallback_title

    title_agent = SimpleAgent(settings)
    request_model = sanitize_text(model or settings.model).strip() or settings.model
    title_prompt = "\n".join(
        [
            "请根据下面这条新对话的第一条用户消息，生成一个对话标题。",
            "",
            safe_message,
        ]
    )

    try:
        response = title_agent._stream_response(
            model=request_model,
            instructions=TITLE_GENERATION_INSTRUCTIONS,
            input=[
                SimpleAgent._message(
                    "user",
                    title_prompt,
                )
            ],
            tools=[],
        )
    except Exception:  # noqa: BLE001
        return fallback_title

    title = clean_generated_title(getattr(response, "output_text", ""))
    return title or fallback_title


def sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {
            sanitize_value(key): sanitize_value(item)
            for key, item in value.items()
        }
    return value


def fallback_blocks_from_text_and_tools(
    role: str,
    text: str,
    tool_events: list[dict[str, object]],
) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    safe_text = sanitize_text(text)

    if safe_text:
        blocks.append(
            {
                "kind": "text",
                "text": safe_text,
            }
        )

    if role == "assistant":
        for tool_event in tool_events:
            blocks.append(
                {
                    "kind": "tool",
                    "tool_event": sanitize_value(tool_event),
                }
            )

    return blocks


def _find_tag(value: str, tag: str) -> int:
    return value.lower().find(tag)


def _safe_emit_split(value: str, tag: str) -> tuple[str, str]:
    lower_value = value.lower()
    max_suffix_length = min(len(value), len(tag) - 1)
    for suffix_length in range(max_suffix_length, 0, -1):
        if tag.startswith(lower_value[-suffix_length:]):
            return value[:-suffix_length], value[-suffix_length:]
    return value, ""


class ThinkTagStreamParser:
    def __init__(
        self,
        *,
        on_text_delta: Callable[[str], None],
        on_reasoning_start: Callable[[], None],
        on_reasoning_delta: Callable[[str], None],
        on_reasoning_done: Callable[[], None],
    ) -> None:
        self.on_text_delta = on_text_delta
        self.on_reasoning_start = on_reasoning_start
        self.on_reasoning_delta = on_reasoning_delta
        self.on_reasoning_done = on_reasoning_done
        self.buffer = ""
        self.in_reasoning = False

    def feed(self, delta: str) -> None:
        safe_delta = sanitize_text(delta)
        if not safe_delta:
            return

        self.buffer = f"{self.buffer}{safe_delta}"
        self._drain()

    def finish(self) -> None:
        if self.buffer:
            if self.in_reasoning:
                self.on_reasoning_delta(self.buffer)
            else:
                self.on_text_delta(self.buffer)
            self.buffer = ""

        if self.in_reasoning:
            self.in_reasoning = False
            self.on_reasoning_done()

    def _drain(self) -> None:
        while self.buffer:
            if self.in_reasoning:
                close_index = _find_tag(self.buffer, "</think>")
                if close_index >= 0:
                    before_close = self.buffer[:close_index]
                    if before_close:
                        self.on_reasoning_delta(before_close)
                    self.buffer = self.buffer[close_index + len("</think>") :]
                    self.in_reasoning = False
                    self.on_reasoning_done()
                    continue

                emit_text, retained = _safe_emit_split(self.buffer, "</think>")
                if emit_text:
                    self.on_reasoning_delta(emit_text)
                self.buffer = retained
                return

            open_index = _find_tag(self.buffer, "<think>")
            if open_index >= 0:
                before_open = self.buffer[:open_index]
                if before_open:
                    self.on_text_delta(before_open)
                self.buffer = self.buffer[open_index + len("<think>") :]
                self.in_reasoning = True
                self.on_reasoning_start()
                continue

            emit_text, retained = _safe_emit_split(self.buffer, "<think>")
            if emit_text:
                self.on_text_delta(emit_text)
            self.buffer = retained
            return


def blocks_from_text_and_tools(
    role: str,
    text: str,
    tool_events: list[dict[str, object]],
) -> list[dict[str, object]]:
    if role != "assistant":
        return fallback_blocks_from_text_and_tools(role, text, tool_events)

    blocks: list[dict[str, object]] = []
    active_reasoning_index: int | None = None

    def append_text_delta(delta: str) -> None:
        safe_delta = sanitize_text(delta)
        if not safe_delta:
            return
        if blocks and blocks[-1].get("kind") == "text":
            blocks[-1]["text"] = sanitize_text(f"{blocks[-1].get('text', '')}{safe_delta}")
            return
        blocks.append({"kind": "text", "text": safe_delta})

    def start_reasoning() -> None:
        nonlocal active_reasoning_index
        if active_reasoning_index is not None:
            return
        blocks.append({"kind": "reasoning", "text": "", "status": "streaming"})
        active_reasoning_index = len(blocks) - 1

    def append_reasoning_delta(delta: str) -> None:
        nonlocal active_reasoning_index
        safe_delta = sanitize_text(delta)
        if not safe_delta:
            return
        if active_reasoning_index is None:
            start_reasoning()
        if active_reasoning_index is None:
            return
        block = blocks[active_reasoning_index]
        block["text"] = sanitize_text(f"{block.get('text', '')}{safe_delta}")

    def finish_reasoning() -> None:
        nonlocal active_reasoning_index
        if active_reasoning_index is None:
            return
        blocks[active_reasoning_index]["status"] = "completed"
        active_reasoning_index = None

    parser = ThinkTagStreamParser(
        on_text_delta=append_text_delta,
        on_reasoning_start=start_reasoning,
        on_reasoning_delta=append_reasoning_delta,
        on_reasoning_done=finish_reasoning,
    )
    parser.feed(text)
    parser.finish()

    for tool_event in tool_events:
        blocks.append(
            {
                "kind": "tool",
                "tool_event": sanitize_value(tool_event),
            }
        )

    return blocks


def normalize_message_blocks(raw_blocks: Any) -> list[dict[str, object]]:
    if not isinstance(raw_blocks, list):
        return []

    normalized: list[dict[str, object]] = []
    for item in raw_blocks:
        if not isinstance(item, dict):
            continue

        kind = sanitize_text(item.get("kind") or "").strip()
        if kind == "text":
            text = sanitize_text(item.get("text") or "")
            if not text:
                continue
            normalized.append(
                {
                    "kind": "text",
                    "text": text,
                }
            )
            continue

        if kind == "reasoning":
            text = sanitize_text(item.get("text") or "")
            status = sanitize_text(item.get("status") or "").strip() or "completed"
            if not text and status != "streaming":
                continue
            normalized.append(
                {
                    "kind": "reasoning",
                    "text": text,
                    "status": "streaming" if status == "streaming" else "completed",
                }
            )
            continue

        if kind == "tool" and isinstance(item.get("tool_event"), dict):
            normalized.append(
                {
                    "kind": "tool",
                    "tool_event": sanitize_value(item.get("tool_event")),
                }
            )

    return normalized


def extract_tool_events_from_blocks(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    tool_events: list[dict[str, object]] = []
    for block in blocks:
        if sanitize_text(block.get("kind") or "").strip() != "tool":
            continue
        tool_event = block.get("tool_event")
        if isinstance(tool_event, dict):
            tool_events.append(sanitize_value(tool_event))
    return tool_events


def append_tool_provider_items(
    provider_items: list[dict[str, Any]],
    *,
    tool_event: dict[str, object],
    record_index: int,
    tool_index: int,
) -> None:
    safe_tool_event = sanitize_value(tool_event)
    tool_name = sanitize_text(safe_tool_event.get("name") or "").strip() or f"tool_{tool_index}"
    call_id = f"stored_{record_index}_{tool_index}"
    arguments_value = safe_tool_event.get("arguments")

    if isinstance(arguments_value, str):
        arguments_text = sanitize_text(arguments_value) or "{}"
    else:
        arguments_text = json.dumps(sanitize_value(arguments_value), ensure_ascii=False)

    tool_output = (
        sanitize_text(safe_tool_event.get("raw_output") or "")
        or sanitize_text(safe_tool_event.get("display_result") or "")
        or sanitize_text(safe_tool_event.get("output_preview") or "")
    )

    provider_items.append(
        {
            "type": "function_call",
            "call_id": call_id,
            "name": tool_name,
            "arguments": arguments_text or "{}",
        }
    )
    provider_items.append(
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": tool_output,
        }
    )


def flush_assistant_text_buffer(
    provider_items: list[dict[str, Any]],
    text_buffer: list[str],
) -> None:
    if not text_buffer:
        return

    provider_items.append(
        SimpleAgent._message(
            "assistant",
            "".join(text_buffer),
        )
    )
    text_buffer.clear()


def normalize_provider_items(raw_items: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue

        item_type = sanitize_text(item.get("type") or "").strip()
        if item_type == "message":
            role = sanitize_text(item.get("role") or "").strip()
            if role not in CONTEXT_INPUT_MESSAGE_ROLES:
                continue

            content = item.get("content")
            if isinstance(content, list):
                safe_content = sanitize_value(content)
            else:
                safe_content = sanitize_text(content or "")

            normalized.append(
                {
                    "type": "message",
                    "role": role,
                    "content": safe_content,
                }
            )
            continue

        if item_type in {"compaction", "compaction_summary"}:
            normalized.append(sanitize_value(item))
            continue

        if item_type == "function_call":
            call_id = sanitize_text(item.get("call_id") or "").strip()
            name = sanitize_text(item.get("name") or "").strip()
            if not call_id or not name:
                continue

            normalized.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": sanitize_text(item.get("arguments") or "{}") or "{}",
                }
            )
            continue

        if item_type == "function_call_output":
            call_id = sanitize_text(item.get("call_id") or "").strip()
            if not call_id:
                continue

            normalized.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": sanitize_value(item.get("output") if "output" in item else ""),
                }
            )

        elif item_type:
            normalized_item = sanitize_value(item)
            if isinstance(normalized_item, dict):
                normalized.append(normalized_item)

    return normalized


def build_provider_items_for_record(
    *,
    role: str,
    text: str,
    attachments: list[dict[str, object]],
    tool_events: list[dict[str, object]],
    blocks: list[dict[str, object]],
    record_index: int,
) -> list[dict[str, Any]]:
    safe_role = sanitize_text(role).strip()
    if safe_role in {"system", "developer", "user"}:
        return [
            SimpleAgent._message(
                safe_role,
                sanitize_text(text),
                attachments=attachment_inputs_from_records(attachments) if safe_role == "user" else None,
            )
        ]

    if safe_role != "assistant":
        return []

    effective_tool_events = tool_events or extract_tool_events_from_blocks(blocks)
    provider_items: list[dict[str, Any]] = []
    text_buffer: list[str] = []
    saw_tool = False
    next_tool_index = 1

    for block in blocks:
        kind = sanitize_text(block.get("kind") or "").strip()
        if kind == "text":
            block_text = sanitize_text(block.get("text") or "")
            if block_text:
                text_buffer.append(block_text)
            continue

        if kind != "tool":
            continue

        saw_tool = True
        flush_assistant_text_buffer(provider_items, text_buffer)

        raw_tool_event = block.get("tool_event")
        if isinstance(raw_tool_event, dict):
            append_tool_provider_items(
                provider_items,
                tool_event=raw_tool_event,
                record_index=record_index,
                tool_index=next_tool_index,
            )
            next_tool_index += 1
            continue

        if next_tool_index - 1 < len(effective_tool_events):
            append_tool_provider_items(
                provider_items,
                tool_event=effective_tool_events[next_tool_index - 1],
                record_index=record_index,
                tool_index=next_tool_index,
            )
            next_tool_index += 1

    while next_tool_index - 1 < len(effective_tool_events):
        saw_tool = True
        append_tool_provider_items(
            provider_items,
            tool_event=effective_tool_events[next_tool_index - 1],
            record_index=record_index,
            tool_index=next_tool_index,
        )
        next_tool_index += 1

    flush_assistant_text_buffer(provider_items, text_buffer)

    if not provider_items:
        provider_items.append(
            SimpleAgent._message(
                "assistant",
                sanitize_text(text),
            )
        )
    elif provider_items[-1].get("type") != "message":
        fallback_text = sanitize_text(text or "")
        provider_items.append(
            SimpleAgent._message(
                "assistant",
                fallback_text,
            )
        )
    elif saw_tool:
        last_item_content = provider_items[-1].get("content")
        if not sanitize_text(last_item_content or "").strip():
            provider_items[-1] = SimpleAgent._message(
                "assistant",
                sanitize_text(text or ""),
            )

    return normalize_provider_items(provider_items)


def message_blocks_to_text(blocks: list[dict[str, object]]) -> str:
    text_parts: list[str] = []
    for block in blocks:
        if sanitize_text(block.get("kind") or "").strip() != "text":
            continue
        text = sanitize_text(block.get("text") or "")
        if text:
            text_parts.append(text)

    return "".join(text_parts)


def message_blocks_have_reasoning(blocks: list[dict[str, object]]) -> bool:
    return any(sanitize_text(block.get("kind") or "").strip() == "reasoning" for block in blocks)


def normalize_transcript(raw_records: Any) -> list[dict[str, object]]:
    if not isinstance(raw_records, list):
        return []

    records: list[dict[str, object]] = []
    for record_index, item in enumerate(raw_records):
        if not isinstance(item, dict):
            continue
        role = sanitize_text(item.get("role") or "").strip()
        if role not in CONTEXT_INPUT_RECORD_ROLES:
            continue
        tool_events = item.get("toolEvents")
        attachments = item.get("attachments")
        normalized_attachments = normalize_attachment_records(attachments)
        normalized_provider_items = normalize_provider_items(item.get("providerItems"))
        recovered_record = (
            compile_record_from_provider_items(
                {
                    "role": role,
                    "attachments": normalized_attachments,
                },
                normalized_provider_items,
            )
            if normalized_provider_items
            else None
        )

        if isinstance(recovered_record, dict):
            safe_text = sanitize_text(recovered_record.get("text") or "")
            safe_tool_events = (
                sanitize_value(recovered_record.get("toolEvents"))
                if isinstance(recovered_record.get("toolEvents"), list)
                else []
            )
            blocks = normalize_message_blocks(recovered_record.get("blocks"))
            provider_items = normalize_provider_items(recovered_record.get("providerItems"))
        else:
            safe_text = sanitize_text(item.get("text") or "")
            safe_tool_events = sanitize_value(tool_events) if isinstance(tool_events, list) else []
            blocks = normalize_message_blocks(item.get("blocks"))
            if not blocks:
                blocks = blocks_from_text_and_tools(
                    role,
                    safe_text,
                    safe_tool_events,
                )
            if role == "assistant" and not safe_tool_events:
                safe_tool_events = extract_tool_events_from_blocks(blocks)
            if not safe_text:
                safe_text = message_blocks_to_text(blocks)
            provider_items = build_provider_items_for_record(
                role=role,
                text=safe_text,
                attachments=normalized_attachments,
                tool_events=safe_tool_events,
                blocks=blocks,
                record_index=record_index,
            )
        records.append(
            {
                "role": role,
                "text": safe_text,
                "attachments": normalized_attachments,
                "toolEvents": safe_tool_events,
                "blocks": blocks,
                "providerItems": provider_items,
            }
        )
    return records


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


def serialize_tool_event(event: ToolEvent) -> dict[str, object]:
    return {
        "name": event.name,
        "arguments": event.arguments,
        "output_preview": event.output_preview,
        "raw_output": event.raw_output,
        "display_title": event.display_title,
        "display_detail": event.display_detail,
        "display_result": event.display_result,
        "status": event.status,
    }


def settings_payload(settings: Settings) -> dict[str, object]:
    return settings.public_payload()


def context_workbench_settings_payload(settings: Settings) -> dict[str, object]:
    return {
        "context_workbench_model": sanitize_text(settings.context_workbench_model or settings.model).strip()
        or sanitize_text(settings.model).strip()
        or "gpt-5.4-mini",
        "context_workbench_provider_id": CODEX_PROXY_PROVIDER_ID,
        "context_token_warning_threshold": int(settings.context_token_warning_threshold or 5000),
        "context_token_critical_threshold": int(settings.context_token_critical_threshold or 10000),
        "user_locale": sanitize_text(settings.user_locale or "").strip() or "en-US",
    }


def estimate_provider_item_token_count(item: dict[str, Any]) -> int:
    item_type = sanitize_text(item.get("type") or "").strip()
    if item_type == "message":
        return estimate_token_count(extract_text_from_provider_message_content(item.get("content")))

    if item_type == "function_call":
        source = "\n".join(
            part
            for part in [
                sanitize_text(item.get("name") or ""),
                sanitize_text(item.get("arguments") or ""),
            ]
            if part.strip()
        )
        return estimate_token_count(source)

    if item_type == "function_call_output":
        return estimate_token_count(sanitize_text(item.get("output") or ""))

    return 0


def estimate_tool_schema_token_count(schema: dict[str, Any]) -> int:
    parts = [
        sanitize_text(schema.get("name") or ""),
        sanitize_text(schema.get("description") or ""),
    ]
    parameters = schema.get("parameters")
    if isinstance(parameters, dict):
        parts.append(json.dumps(sanitize_value(parameters), ensure_ascii=False))
    elif parameters is not None:
        parameter_text = sanitize_text(parameters)
        if parameter_text.strip():
            parts.append(parameter_text)

    return estimate_token_count("\n".join(part for part in parts if part.strip()))


def debug_request_item_summary(item: Any, index: int) -> dict[str, object]:
    item_json = json.dumps(sanitize_value(item), ensure_ascii=False)
    summary: dict[str, object] = {
        "index": index,
        "json_chars": len(item_json),
    }
    if not isinstance(item, dict):
        summary["type"] = type(item).__name__
        return summary

    item_type = sanitize_text(item.get("type") or "").strip()
    summary["type"] = item_type or "unknown"
    if item_type == "message":
        summary["role"] = sanitize_text(item.get("role") or "").strip()
        text = extract_text_from_provider_message_content(item.get("content"))
        summary["text_chars"] = len(text)
        summary["preview"] = block_text_preview(text, limit=120)
        return summary

    if item_type == "function_call":
        summary["name"] = sanitize_text(item.get("name") or "").strip()
        summary["call_id"] = sanitize_text(item.get("call_id") or "").strip()
        summary["arguments_chars"] = len(sanitize_text(item.get("arguments") or ""))
        return summary

    if item_type == "function_call_output":
        output = sanitize_text(item.get("output") or "")
        summary["call_id"] = sanitize_text(item.get("call_id") or "").strip()
        summary["output_chars"] = len(output)
        summary["preview"] = block_text_preview(output, limit=120)
        return summary

    return summary


def write_context_request_debug(
    *,
    session_id: str,
    request_model: str,
    round_count: int,
    request: dict[str, Any],
    note: str,
) -> None:
    try:
        input_items = request.get("input")
        tools = request.get("tools")
        input_list = input_items if isinstance(input_items, list) else []
        tool_list = tools if isinstance(tools, list) else []
        payload = {
            "created_at": utc_timestamp(),
            "pid": os.getpid(),
            "state_file": str(STATE_FILE),
            "session_id": session_id,
            "model": request_model,
            "round_count": round_count,
            "note": note,
            "request_json_chars": len(json.dumps(sanitize_value(request), ensure_ascii=False)),
            "input_count": len(input_list),
            "input_json_chars": len(json.dumps(sanitize_value(input_list), ensure_ascii=False)),
            "tools_count": len(tool_list),
            "tools_json_chars": len(json.dumps(sanitize_value(tool_list), ensure_ascii=False)),
            "items": [
                debug_request_item_summary(item, index)
                for index, item in enumerate(input_list)
            ],
        }
        CONTEXT_REQUEST_DEBUG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with CONTEXT_REQUEST_DEBUG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        return


def provider_items_tool_token_count(items: list[dict[str, Any]]) -> int:
    total = 0
    for item in items:
        if sanitize_text(item.get("type") or "").strip() not in {"function_call", "function_call_output"}:
            continue
        total += estimate_provider_item_token_count(item)
    return total


def is_environment_context_record(record: dict[str, object]) -> bool:
    if sanitize_text(record.get("role") or "").strip() != "user":
        return False
    return sanitize_text(record.get("text") or "").lstrip().lower().startswith("<environment_context>")


def internal_context_prefix_indexes(transcript: list[dict[str, object]]) -> set[int]:
    internal_indexes: set[int] = set()
    environment_index: int | None = None

    for index, record in enumerate(transcript):
        role = sanitize_text(record.get("role") or "").strip()
        if role in {"system", "developer"}:
            internal_indexes.add(index)
            continue
        if environment_index is None and is_environment_context_record(record):
            environment_index = index

    if environment_index is not None:
        internal_indexes.add(environment_index)

    return internal_indexes


def editable_context_node_entries(transcript: list[dict[str, object]]) -> list[dict[str, object]]:
    internal_indexes = internal_context_prefix_indexes(transcript)
    entries: list[dict[str, object]] = []
    node_number = 0
    for index, record in enumerate(transcript):
        if index in internal_indexes:
            continue
        node_number += 1
        entries.append(
            {
                "record": record,
                "raw_index": index,
                "node_number": node_number,
            }
        )
    return entries


def editable_context_node_count(transcript: list[dict[str, object]]) -> int:
    return len(editable_context_node_entries(transcript))


def selected_display_node_numbers(
    transcript: list[dict[str, object]],
    selected_indexes: list[int],
) -> list[int]:
    selected_index_set = set(selected_indexes)
    return [
        int(entry["node_number"])
        for entry in editable_context_node_entries(transcript)
        if int(entry["raw_index"]) in selected_index_set
    ]


def context_workbench_suggestions_payload(session: SessionState) -> dict[str, object]:
    nodes: list[dict[str, object]] = []
    transcript = normalize_transcript(session.transcript)
    internal_indexes = internal_context_prefix_indexes(transcript)
    display_number_by_raw_index = {
        int(entry["raw_index"]): int(entry["node_number"])
        for entry in editable_context_node_entries(transcript)
    }
    stats_total_token_count = 0
    stats_tool_token_count = 0

    for index, record in enumerate(transcript):
        node_number = display_number_by_raw_index.get(index, index + 1)
        overview = context_record_overview(record, node_number=node_number)
        token_count = int(overview.get("token_estimate") or 0)
        tool_token_count = int(overview.get("tool_token_estimate") or 0)
        stats_total_token_count += token_count
        stats_tool_token_count += tool_token_count
        if index in internal_indexes:
            continue
        nodes.append(
            {
                "node_index": index,
                "node_number": node_number,
                "role": sanitize_text(overview.get("role") or "").strip() or "assistant",
                "token_count": token_count,
                "tool_token_count": tool_token_count,
                "preview": sanitize_text(overview.get("preview") or "").strip(),
            }
        )

    nodes.sort(
        key=lambda item: (
            -int(item.get("token_count") or 0),
            int(item.get("node_number") or 0),
        )
    )

    return {
        "stats": {
            "total_token_count": stats_total_token_count,
            "tool_token_count": stats_tool_token_count,
        },
        "nodes": sanitize_value(nodes),
    }


def normalize_selected_node_indexes(raw_indexes: Any, transcript_length: int) -> list[int]:
    if not isinstance(raw_indexes, list):
        return []

    selected_indexes: list[int] = []
    for raw_item in raw_indexes:
        try:
            index = int(raw_item)
        except (TypeError, ValueError):
            continue

        if 0 <= index < transcript_length and index not in selected_indexes:
            selected_indexes.append(index)

    return selected_indexes


def block_text_preview(text: str, limit: int = 280) -> str:
    compact = " ".join(sanitize_text(text).split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: max(0, limit - 3)]}..."


def collapsed_context_map_preview(text: str, limit: int = 72) -> str:
    compact = " ".join(sanitize_text(text).split())
    if not compact:
        return ""

    sentence_match = re.search(r"[。！？!?\.]", compact)
    if sentence_match:
        end_index = sentence_match.end()
        preview = compact[:end_index].strip()
    else:
        preview = compact[:limit].strip()

    was_shortened = len(preview) < len(compact)
    if len(preview) > limit:
        preview = preview[: max(0, limit - 3)].rstrip()
        was_shortened = True

    if was_shortened and not preview.endswith("..."):
        preview = f"{preview.rstrip()}..."

    return preview


def normalize_node_numbers(raw_numbers: Any, max_node_number: int) -> list[int]:
    if not isinstance(raw_numbers, list):
        return []

    normalized: list[int] = []
    for raw_item in raw_numbers:
        try:
            node_number = int(raw_item)
        except (TypeError, ValueError):
            continue

        if 1 <= node_number <= max_node_number and node_number not in normalized:
            normalized.append(node_number)

    return normalized


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_token_encoding() -> Any | None:
    global _TOKEN_ENCODING, _TOKEN_ENCODING_LOAD_FAILED

    if _TOKEN_ENCODING is not None:
        return _TOKEN_ENCODING
    if _TOKEN_ENCODING_LOAD_FAILED or tiktoken is None:
        return None

    try:
        _TOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _TOKEN_ENCODING_LOAD_FAILED = True
        return None

    return _TOKEN_ENCODING


def estimate_token_count(text: str) -> int:
    safe_text = sanitize_text(text)
    if not safe_text.strip():
        return 0

    encoding = get_token_encoding()
    if encoding is not None:
        try:
            return len(encoding.encode(safe_text))
        except Exception:
            pass

    compact = safe_text.strip()
    ascii_tokens = re.findall(r"[A-Za-z0-9_]+", compact)
    non_ascii_chars = [char for char in compact if not char.isspace() and not char.isascii()]
    return max(1, len(ascii_tokens) + len(non_ascii_chars))


def unique_int_list(values: Any) -> list[int]:
    if not isinstance(values, list):
        return []

    unique_values: list[int] = []
    for raw_value in values:
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            continue
        if value not in unique_values:
            unique_values.append(value)
    return unique_values


def unique_text_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []

    unique_values: list[str] = []
    for raw_value in values:
        value = sanitize_text(raw_value or "").strip()
        if value and value not in unique_values:
            unique_values.append(value)
    return unique_values


def operation_changed_nodes(operation: dict[str, object]) -> list[int]:
    explicit_nodes = unique_int_list(operation.get("changed_nodes"))
    if explicit_nodes:
        return explicit_nodes

    target_nodes = unique_int_list(operation.get("target_node_numbers"))
    if target_nodes:
        return target_nodes

    target_items = operation.get("target_items")
    if isinstance(target_items, list):
        item_nodes: list[int] = []
        for item in target_items:
            if not isinstance(item, dict):
                continue
            try:
                node_number = int(item.get("node_number") or 0)
            except (TypeError, ValueError):
                continue
            if node_number > 0 and node_number not in item_nodes:
                item_nodes.append(node_number)
        if item_nodes:
            return item_nodes

    return []


def normalize_change_type(raw_value: Any) -> str:
    value = sanitize_text(raw_value or "").strip().lower()
    if value in {"delete", "replace", "compress", "mixed", "update"}:
        return value
    if value.startswith("delete"):
        return "delete"
    if value.startswith("replace"):
        return "replace"
    if value.startswith("compress"):
        return "compress"
    return "update"


def operation_change_type(operation: dict[str, object]) -> str:
    return normalize_change_type(
        operation.get("change_type")
        or operation.get("operation_type")
        or operation.get("type")
        or "update"
    )


def summarize_change_type(change_types: list[str]) -> str:
    normalized = [normalize_change_type(item) for item in change_types if sanitize_text(item).strip()]
    unique_types = [item for item in normalized if item]
    if not unique_types:
        return "update"
    if len(set(unique_types)) == 1:
        return unique_types[0]
    return "mixed"


def summarize_changed_nodes_from_operations(operations: list[dict[str, object]]) -> list[int]:
    changed_nodes: list[int] = []
    for operation in operations:
        for node_number in operation_changed_nodes(operation):
            if node_number not in changed_nodes:
                changed_nodes.append(node_number)
    return changed_nodes


def fallback_context_revision_summary(label: str, operations: list[dict[str, object]]) -> str:
    safe_label = sanitize_text(label).strip() or "Context update"
    if not operations:
        return safe_label

    if len(operations) == 1:
        operation = operations[0]
        operation_type = sanitize_text(operation.get("operation_type") or "").strip()
        target_nodes = unique_int_list(operation.get("target_node_numbers") or operation.get("changed_nodes"))
        node_text = f"节点 #{format_node_ranges(target_nodes)}" if target_nodes else "当前上下文"
        target_items = operation.get("target_items")
        first_item = target_items[0] if isinstance(target_items, list) and target_items else {}
        item_number = int(first_item.get("item_number") or 0) if isinstance(first_item, dict) else 0
        item_text = f"{node_text} 的第 {item_number} 个条目" if item_number else node_text

        if operation_type == "compress_nodes":
            return f"把{node_text}压缩成了更短的摘要，尽量保留主要信息。"
        if operation_type == "delete_nodes":
            return f"删除了{node_text}，让当前上下文更紧凑。"
        if operation_type == "delete_item":
            return f"删除了{item_text}，去掉了不再需要的上下文内容。"
        if operation_type == "compress_item":
            return f"压缩了{item_text}，保留原有条目类型的同时缩短了内容。"
        if operation_type == "replace_item":
            return f"改写了{item_text}，把它换成了更合适的新内容。"

    changed_nodes = summarize_changed_nodes_from_operations(operations)
    if changed_nodes:
        return f"这一轮集中更新了节点 #{format_node_ranges(changed_nodes)} 的内容，并把它们整理成了新的上下文版本。"
    return safe_label


def find_active_context_revision_id(revisions: list[dict[str, object]]) -> str | None:
    for revision in revisions:
        revision_id = sanitize_text(revision.get("id") or "").strip()
        if revision_id and bool(revision.get("is_active")):
            return revision_id
    return None


def mark_active_context_revision(revisions: list[dict[str, object]], revision_id: str | None) -> None:
    safe_revision_id = sanitize_text(revision_id or "").strip()
    for revision in revisions:
        current_id = sanitize_text(revision.get("id") or "").strip()
        revision["is_active"] = bool(safe_revision_id and current_id == safe_revision_id)


def coerce_context_revision_number(raw_value: Any, fallback: int, *, minimum: int = 0) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = int(fallback)
    return max(minimum, value)


def has_initial_context_revision(revisions: list[dict[str, object]]) -> bool:
    return any(
        coerce_context_revision_number(revision.get("revision_number"), 1) == 0
        for revision in revisions
    )


def next_context_revision_number(revisions: list[dict[str, object]]) -> int:
    numbers = [
        coerce_context_revision_number(revision.get("revision_number"), 0)
        for revision in revisions
    ]
    return max([number for number in numbers if number > 0], default=0) + 1


def ensure_initial_context_revision(session: SessionState) -> None:
    if has_initial_context_revision(session.context_revisions):
        return
    if session.context_revisions:
        return

    session.context_revisions.append(
        build_context_revision_entry(
            transcript=normalize_transcript(session.transcript),
            context_workbench_history=normalize_context_chat_history(session.context_workbench_history),
            revision_label="初始版本",
            revision_summary="还没有进行压缩、删除或替换时的完整上下文。",
            operations=[],
            revision_number=0,
        )
    )


def sync_active_context_revision_snapshot(session: SessionState) -> None:
    active_revision_id = find_active_context_revision_id(session.context_revisions)
    if not active_revision_id:
        return

    safe_snapshot = sanitize_value(normalize_transcript(session.transcript))
    safe_context_workbench_history = sanitize_value(
        normalize_context_chat_history(session.context_workbench_history)
    )
    for revision in reversed(session.context_revisions):
        current_id = sanitize_text(revision.get("id") or "").strip()
        if current_id != active_revision_id:
            continue
        revision["snapshot"] = safe_snapshot
        revision["context_workbench_history_snapshot"] = safe_context_workbench_history
        revision["node_count"] = editable_context_node_count(normalize_transcript(session.transcript))
        return


def build_context_revision_entry(
    *,
    transcript: list[dict[str, object]],
    context_workbench_history: list[dict[str, str]],
    revision_label: str,
    revision_summary: str,
    operations: list[dict[str, object]],
    revision_number: int,
) -> dict[str, object]:
    sanitized_operations = [
        sanitize_value(operation)
        for operation in operations
        if isinstance(operation, dict)
    ]
    changed_nodes = summarize_changed_nodes_from_operations(sanitized_operations)
    change_types = [
        operation_change_type(operation)
        for operation in sanitized_operations
    ]
    label = sanitize_text(revision_label).strip() or "Context update"
    summary = sanitize_text(revision_summary).strip() or fallback_context_revision_summary(label, sanitized_operations)
    return {
        "id": uuid.uuid4().hex,
        "label": label,
        "summary": summary,
        "created_at": utc_timestamp(),
        "revision_number": coerce_context_revision_number(revision_number, 1),
        "change_type": summarize_change_type(change_types),
        "change_types": unique_text_list(change_types),
        "changed_nodes": changed_nodes,
        "operations": sanitized_operations,
        "node_count": editable_context_node_count(normalize_transcript(transcript)),
        "snapshot": sanitize_value(transcript),
        "context_workbench_history_snapshot": sanitize_value(
            normalize_context_chat_history(context_workbench_history)
        ),
        "is_active": True,
    }


def normalize_context_revision_entries(raw_entries: Any) -> list[dict[str, object]]:
    if not isinstance(raw_entries, list):
        return []

    normalized: list[dict[str, object]] = []
    for index, item in enumerate(raw_entries, start=1):
        if not isinstance(item, dict):
            continue

        revision_id = sanitize_text(item.get("id") or "").strip()
        label = sanitize_text(item.get("label") or "").strip()
        created_at = sanitize_text(item.get("created_at") or "").strip() or utc_timestamp()
        snapshot = normalize_transcript(item.get("snapshot"))
        context_workbench_history_snapshot = normalize_context_chat_history(
            item.get("context_workbench_history_snapshot")
        )
        operations = sanitize_value(item.get("operations")) if isinstance(item.get("operations"), list) else []
        if not revision_id or not label:
            continue

        changed_nodes = unique_int_list(item.get("changed_nodes")) or summarize_changed_nodes_from_operations(operations)
        change_types = unique_text_list(item.get("change_types"))
        if not change_types:
            change_types = [operation_change_type(operation) for operation in operations if isinstance(operation, dict)]
        change_type = normalize_change_type(item.get("change_type") or summarize_change_type(change_types))

        summary = sanitize_text(item.get("summary") or "").strip()
        if not summary or summary == label:
            summary = fallback_context_revision_summary(label, operations)

        normalized.append(
            {
                "id": revision_id,
                "label": label,
                "summary": summary,
                "created_at": created_at,
                "revision_number": coerce_context_revision_number(
                    item.get("revision_number"),
                    index,
                ),
                "change_type": change_type,
                "change_types": unique_text_list(change_types) or [change_type],
                "changed_nodes": changed_nodes,
                "operations": operations,
                "node_count": len(snapshot),
                "snapshot": sanitize_value(snapshot),
                "context_workbench_history_snapshot": sanitize_value(context_workbench_history_snapshot),
                "is_active": bool(item.get("is_active")),
            }
        )

    if normalized and not any(bool(revision.get("is_active")) for revision in normalized):
        normalized[-1]["is_active"] = True

    for revision_number, revision in enumerate(normalized, start=1):
        revision["revision_number"] = coerce_context_revision_number(
            revision.get("revision_number"),
            revision_number,
        )

    return normalized


def normalize_pending_context_restore(raw_restore: Any) -> dict[str, object] | None:
    if not isinstance(raw_restore, dict):
        return None

    undo_transcript = normalize_transcript(raw_restore.get("undo_transcript"))
    undo_context_workbench_history = normalize_context_chat_history(
        raw_restore.get("undo_context_workbench_history")
    )
    target_revision_id = sanitize_text(raw_restore.get("target_revision_id") or "").strip()
    target_label = sanitize_text(raw_restore.get("target_label") or "").strip()
    created_at = sanitize_text(raw_restore.get("created_at") or "").strip() or utc_timestamp()
    undo_active_revision_id = sanitize_text(raw_restore.get("undo_active_revision_id") or "").strip()
    if not undo_transcript or not target_revision_id:
        return None

    return {
        "undo_transcript": sanitize_value(undo_transcript),
        "undo_context_workbench_history": sanitize_value(undo_context_workbench_history),
        "target_revision_id": target_revision_id,
        "target_label": target_label or "Revision",
        "created_at": created_at,
        "undo_active_revision_id": undo_active_revision_id,
    }


def context_revision_summaries(revisions: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "id": sanitize_text(revision.get("id") or "").strip(),
            "label": sanitize_text(revision.get("label") or "").strip() or "Revision",
            "summary": (
                lambda label, summary, operations: (
                    fallback_context_revision_summary(label, operations)
                    if not summary or summary == label
                    else summary
                )
            )(
                sanitize_text(revision.get("label") or "").strip() or "Revision",
                sanitize_text(revision.get("summary") or "").strip(),
                sanitize_value(revision.get("operations")) if isinstance(revision.get("operations"), list) else [],
            ),
            "created_at": sanitize_text(revision.get("created_at") or "").strip() or utc_timestamp(),
            "revision_number": coerce_context_revision_number(revision.get("revision_number"), 0),
            "change_type": normalize_change_type(revision.get("change_type") or "update"),
            "change_types": unique_text_list(revision.get("change_types")) or [
                normalize_change_type(revision.get("change_type") or "update")
            ],
            "changed_nodes": unique_int_list(revision.get("changed_nodes")),
            "is_active": bool(revision.get("is_active")),
            "operation_count": len(revision.get("operations") or []),
            "node_count": int(revision.get("node_count") or 0),
        }
        for revision in reversed(revisions)
        if sanitize_text(revision.get("id") or "").strip()
    ]


def load_context_edit_markers() -> dict[str, dict[str, object]]:
    try:
        raw = json.loads(CONTEXT_EDIT_MARKERS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        sanitize_text(session_id).strip(): sanitize_value(marker)
        for session_id, marker in raw.items()
        if sanitize_text(session_id).strip() and isinstance(marker, dict)
    }


def save_context_edit_markers(markers: dict[str, dict[str, object]]) -> None:
    CONTEXT_EDIT_MARKERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONTEXT_EDIT_MARKERS_FILE.write_text(
        json.dumps(sanitize_value(markers), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_context_edit_marker(
    session_id: str,
    *,
    summary: str,
    revision_number: int,
    node_count: int,
) -> None:
    safe_session_id = sanitize_text(session_id).strip()
    if not safe_session_id:
        return
    markers = load_context_edit_markers()
    markers[safe_session_id] = {
        "session_id": safe_session_id,
        "summary": sanitize_text(summary).strip() or "Context has been edited.",
        "revision_number": revision_number,
        "node_count": max(0, int(node_count or 0)),
        "created_at": utc_timestamp(),
    }
    save_context_edit_markers(markers)


def consume_context_edit_marker(session_id: str) -> dict[str, object] | None:
    safe_session_id = sanitize_text(session_id).strip()
    if not safe_session_id:
        return None
    markers = load_context_edit_markers()
    marker = markers.pop(safe_session_id, None)
    if marker is not None:
        save_context_edit_markers(markers)
    return sanitize_value(marker) if isinstance(marker, dict) else None


def context_pending_restore_payload(raw_restore: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(raw_restore, dict):
        return None

    target_revision_id = sanitize_text(raw_restore.get("target_revision_id") or "").strip()
    if not target_revision_id:
        return None

    return {
        "target_revision_id": target_revision_id,
        "target_label": sanitize_text(raw_restore.get("target_label") or "").strip() or "Revision",
        "created_at": sanitize_text(raw_restore.get("created_at") or "").strip() or utc_timestamp(),
        "undo_active_revision_id": sanitize_text(raw_restore.get("undo_active_revision_id") or "").strip(),
        "can_undo": True,
    }


def context_record_preview(record: dict[str, object], *, limit: int = 140) -> str:
    blocks = normalize_message_blocks(record.get("blocks"))
    attachments = normalize_attachment_records(record.get("attachments"))
    text = sanitize_text(record.get("text") or "")

    if blocks:
        for block in blocks:
            kind = sanitize_text(block.get("kind") or "").strip()
            if kind == "text":
                preview = block_text_preview(block.get("text") or "", limit=limit)
                if preview:
                    return preview
                continue

            if kind != "tool":
                continue

            tool_event = block.get("tool_event")
            if not isinstance(tool_event, dict):
                continue
            tool_name = sanitize_text(tool_event.get("name") or tool_event.get("display_title") or "").strip() or "tool"
            tool_detail = block_text_preview(tool_event.get("display_detail") or "", limit=max(40, min(limit, 88)))
            if tool_detail:
                return f"{tool_name}: {tool_detail}"
            return tool_name

    if text:
        return block_text_preview(text, limit=limit)

    if attachments:
        attachment_names = ", ".join(
            sanitize_text(item.get("name") or "").strip()
            for item in attachments
            if sanitize_text(item.get("name") or "").strip()
        )
        if attachment_names:
            return f"Attachments: {attachment_names}"

    return "[empty]"


def record_tool_usage(record: dict[str, object]) -> list[dict[str, object]]:
    tool_events = sanitize_value(record.get("toolEvents")) if isinstance(record.get("toolEvents"), list) else []
    if not tool_events:
        tool_events = extract_tool_events_from_blocks(normalize_message_blocks(record.get("blocks")))

    counts: dict[str, int] = {}
    for tool_event in tool_events:
        if not isinstance(tool_event, dict):
            continue
        tool_name = sanitize_text(tool_event.get("name") or tool_event.get("display_title") or "").strip() or "tool"
        counts[tool_name] = counts.get(tool_name, 0) + 1

    return [
        {"name": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def format_tool_usage(tool_usage: list[dict[str, object]]) -> str:
    if not tool_usage:
        return "none"

    return ", ".join(
        f"{sanitize_text(item.get('name') or '').strip() or 'tool'} x{int(item.get('count') or 0)}"
        for item in tool_usage
    )


def format_token_count(token_estimate: int) -> str:
    safe_value = max(0, int(token_estimate or 0))
    if safe_value >= 1000:
        return f"{safe_value / 1000:.1f}k"
    return str(safe_value)


def record_context_tool_weight_source(record: dict[str, object]) -> str:
    parts: list[str] = []
    for block in normalize_message_blocks(record.get("blocks")):
        kind = sanitize_text(block.get("kind") or "").strip()
        if kind != "tool":
            continue

        tool_event = block.get("tool_event")
        if not isinstance(tool_event, dict):
            continue

        tool_parts = [
            sanitize_text(tool_event.get("display_title") or "").strip(),
            sanitize_text(tool_event.get("display_detail") or "").strip(),
            sanitize_text(tool_event.get("output_preview") or "").strip(),
            sanitize_text(tool_event.get("display_result") or "").strip(),
            sanitize_text(tool_event.get("raw_output") or "").strip(),
        ]
        joined = "\n".join(part for part in tool_parts if part)
        if joined:
            parts.append(joined)

    return "\n\n".join(parts)


def record_context_weight_source(record: dict[str, object]) -> str:
    parts: list[str] = []
    for block in normalize_message_blocks(record.get("blocks")):
        kind = sanitize_text(block.get("kind") or "").strip()
        if kind == "text":
            text = sanitize_text(block.get("text") or "")
            if text.strip():
                parts.append(text)
            continue

        if kind in {"reasoning", "thinking"}:
            continue

        tool_event = block.get("tool_event")
        if not isinstance(tool_event, dict):
            continue

        tool_source = record_context_tool_weight_source({"blocks": [block]})
        if tool_source:
            parts.append(tool_source)

    if not parts:
        text = sanitize_text(record.get("text") or "")
        if text.strip():
            parts.append(text)

    raw_attachments = record.get("attachments")
    attachments = raw_attachments if isinstance(raw_attachments, list) else []
    attachment_names = "\n".join(
        sanitize_text(attachment.get("name") or "").strip()
        for attachment in attachments
        if isinstance(attachment, dict) and sanitize_text(attachment.get("name") or "").strip()
    )
    if attachment_names:
        parts.append(attachment_names)

    return "\n\n".join(part for part in parts if part.strip())


def context_record_overview(record: dict[str, object], *, node_number: int, selected: bool = False) -> dict[str, object]:
    role = sanitize_text(record.get("role") or "").strip() or "unknown"
    preview = context_record_preview(record)
    if role == "assistant":
        preview = collapsed_context_map_preview(preview) or "[empty]"
    tool_usage = record_tool_usage(record)
    provider_items = normalize_provider_items(record.get("providerItems"))
    token_estimate = estimate_token_count(record_context_weight_source(record))
    tool_token_estimate = estimate_token_count(record_context_tool_weight_source(record))
    return {
        "node_number": node_number,
        "role": role,
        "selected": selected,
        "preview": preview,
        "token_estimate": token_estimate,
        "tool_token_estimate": tool_token_estimate,
        "tool_usage": tool_usage,
        "tool_count": sum(int(item.get("count") or 0) for item in tool_usage),
        "item_count": len(provider_items),
        "item_types": [
            sanitize_text(item.get("type") or "").strip() or "unknown"
            for item in provider_items
        ],
        "full_text": sanitize_text(record.get("text") or "") if role != "assistant" else "",
    }


def extract_text_from_provider_message_content(content: Any) -> str:
    if isinstance(content, str):
        return sanitize_text(content)

    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            text = sanitize_text(item)
            if text:
                parts.append(text)
            continue

        if not isinstance(item, dict):
            continue

        text = sanitize_text(item.get("text") or item.get("content") or "")
        if text:
            parts.append(text)

    return "".join(parts)


def replace_provider_message_text(content: Any, replacement_text: str) -> str | list[dict[str, Any]]:
    safe_text = sanitize_text(replacement_text)
    if isinstance(content, list):
        rewritten: list[dict[str, Any]] = []
        text_item_type = "input_text"
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = sanitize_text(item.get("type") or "").strip()
            if item_type in {"input_text", "output_text", "text"} or "text" in item:
                if item_type == "output_text":
                    text_item_type = "output_text"
                continue
            rewritten.append(sanitize_value(item))

        if safe_text:
            rewritten.insert(
                0,
                {
                    "type": text_item_type,
                    "text": safe_text,
                },
            )
        return rewritten

    return safe_text


def provider_item_type(item: dict[str, Any] | None) -> str:
    return sanitize_text((item or {}).get("type") or "").strip()


def provider_item_call_id(item: dict[str, Any] | None) -> str:
    safe_item = item or {}
    return sanitize_text(safe_item.get("call_id") or safe_item.get("id") or "").strip()


def provider_payload_text(value: Any) -> str:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        parts: list[str] = []
        for entry in value:
            if isinstance(entry, str):
                text = sanitize_text(entry)
            elif isinstance(entry, dict):
                text = sanitize_text(entry.get("text") or entry.get("content") or entry.get("summary") or "")
            else:
                text = ""
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts)
    if isinstance(value, dict):
        for key in ("text", "summary"):
            text = sanitize_text(value.get(key) or "")
            if text:
                return text
        content = value.get("content")
        if isinstance(content, list):
            text = provider_payload_text(content)
            if text:
                return text
        if isinstance(content, str):
            text = sanitize_text(content)
            if text:
                return text
    if value is None:
        return ""
    return json.dumps(sanitize_value(value), ensure_ascii=False)


def provider_jsonish_value(value: Any) -> Any:
    if isinstance(value, str):
        safe_text = sanitize_text(value)
        if not safe_text.strip():
            return ""
        try:
            return sanitize_value(json.loads(safe_text))
        except json.JSONDecodeError:
            return safe_text
    return sanitize_value(value)


def tool_call_arguments_value(item: dict[str, Any] | None) -> Any:
    if not isinstance(item, dict):
        return ""
    item_type = provider_item_type(item)
    if item_type == "function_call":
        return provider_jsonish_value(item.get("arguments") or "{}")
    if item_type == "custom_tool_call":
        return provider_jsonish_value(item.get("input") or "")
    if item_type in {"local_shell_call", "web_search_call"}:
        return sanitize_value(item.get("action"))
    if item_type == "tool_search_call":
        return provider_jsonish_value(item.get("arguments"))
    if item_type == "image_generation_call":
        return sanitize_text(item.get("revised_prompt") or "")
    return provider_jsonish_value(item.get("arguments") or item.get("input") or item.get("action"))


def tool_output_text_from_provider_item(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    item_type = provider_item_type(item)
    if item_type == "tool_search_output":
        return provider_payload_text(item.get("tools"))
    if item_type == "image_generation_call":
        return provider_payload_text(item.get("result"))
    if item_type == "web_search_call":
        return ""
    return provider_payload_text(item.get("output"))


def tool_display_title_from_provider_item(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return "tool"
    item_type = provider_item_type(item)
    if item_type in {"function_call", "custom_tool_call"}:
        return sanitize_text(item.get("name") or "").strip() or "tool"
    if item_type == "local_shell_call":
        return "local_shell"
    if item_type == "tool_search_call":
        return "tool_search"
    if item_type == "web_search_call":
        return "web_search"
    if item_type == "image_generation_call":
        return "image_generation"
    if item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES:
        return sanitize_text(item.get("name") or item_type or "tool_output").strip()
    return item_type or "tool"


def tool_display_detail_from_provider_item(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    item_type = provider_item_type(item)
    call_name = sanitize_text(item.get("name") or "").strip()
    arguments_value = tool_call_arguments_value(item)

    if call_name in {"shell_command", "exec_command"} and isinstance(arguments_value, dict):
        command = arguments_value.get("command")
        if isinstance(command, list):
            return " ".join(sanitize_text(part) for part in command)
        if command is not None:
            return sanitize_text(command)
    if call_name == "write_stdin" and isinstance(arguments_value, dict):
        return sanitize_text(arguments_value.get("stdin") or arguments_value.get("input") or "")
    if item_type == "local_shell_call":
        action = item.get("action")
        if isinstance(action, dict):
            command = action.get("command")
            if isinstance(command, list):
                return " ".join(sanitize_text(part) for part in command)
            if command is not None:
                return sanitize_text(command)
        return block_text_preview(provider_payload_text(action), limit=160)
    if item_type == "web_search_call":
        action = item.get("action")
        if isinstance(action, dict):
            return sanitize_text(action.get("query") or action.get("type") or "")
        return block_text_preview(provider_payload_text(action), limit=160)
    if item_type == "image_generation_call":
        return block_text_preview(sanitize_text(item.get("revised_prompt") or ""), limit=160)

    detail_text = provider_payload_text(arguments_value)
    return block_text_preview(detail_text, limit=160) if detail_text.strip() not in {"", "{}", "[]"} else ""


def provider_item_detail(item: dict[str, Any], item_number: int) -> dict[str, object]:
    item_type = provider_item_type(item) or "unknown"
    detail: dict[str, object] = {
        "item_number": item_number,
        "item_label": f"item #{item_number}",
        "item_type": item_type,
        "type": item_type,
        "provider_item_ref": f"provider_items[{item_number - 1}]",
        "delete_supported": True,
        "replace_supported": item_type in CONTEXT_EDITABLE_PROVIDER_ITEM_TYPES,
        "compress_supported": item_type in {"message", "function_call", "custom_tool_call", *CODEX_TOOL_OUTPUT_ITEM_TYPES},
    }

    if item_type == "message":
        content = item.get("content")
        detail["role"] = sanitize_text(item.get("role") or "").strip() or "assistant"
        text = extract_text_from_provider_message_content(content)
        detail["text_preview"] = block_text_preview(text, limit=220)
        detail["editable_text_ref"] = f"provider_items[{item_number - 1}].content"
        preview_source = (
            json.dumps(sanitize_value(content), ensure_ascii=False)
            if isinstance(content, list)
            else sanitize_text(content or "")
        )
        detail["preview"] = block_text_preview(preview_source, limit=180)
        return detail

    if item_type in CODEX_TOOL_CALL_ITEM_TYPES:
        detail["name"] = tool_display_title_from_provider_item(item)
        detail["call_id"] = provider_item_call_id(item)
        arguments = provider_payload_text(tool_call_arguments_value(item))
        detail["arguments_preview"] = block_text_preview(arguments, limit=220)
        detail["editable_text_ref"] = (
            f"provider_items[{item_number - 1}].input"
            if item_type == "custom_tool_call"
            else f"provider_items[{item_number - 1}].arguments"
        )
        detail["preview"] = block_text_preview(arguments, limit=180)
        return detail

    if item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES:
        detail["name"] = sanitize_text(item.get("name") or "").strip()
        detail["call_id"] = provider_item_call_id(item)
        output = tool_output_text_from_provider_item(item)
        detail["output_preview"] = block_text_preview(output, limit=220)
        detail["editable_text_ref"] = (
            f"provider_items[{item_number - 1}].tools"
            if item_type == "tool_search_output"
            else f"provider_items[{item_number - 1}].output"
        )
        detail["preview"] = block_text_preview(output, limit=180)
        return detail

    if item_type in {"compaction", "compaction_summary"}:
        encoded_content = sanitize_text(item.get("encrypted_content") or "")
        detail["encoded_content_preview"] = block_text_preview(encoded_content, limit=220)
        detail["preview"] = block_text_preview(encoded_content, limit=180)
        return detail

    return detail


def visible_text_from_compaction_provider_item(item: dict[str, Any]) -> str:
    parts: list[str] = []

    def append_visible(value: Any) -> None:
        if isinstance(value, str):
            text = sanitize_text(value).strip()
            if text:
                parts.append(text)
            return
        if isinstance(value, list):
            for entry in value:
                append_visible(entry)
            return
        if isinstance(value, dict):
            for key in ("text", "summary", "content"):
                if key in value:
                    append_visible(value.get(key))

    for key in ("summary", "content", "text"):
        append_visible(item.get(key))
    if not parts:
        append_visible(item.get("encrypted_content"))
    return "\n".join(part for part in parts if part)


def tool_status_from_output(output_text: str, fallback: str = "completed") -> str:
    lines = sanitize_text(output_text).splitlines()
    first_line = lines[0] if lines else ""
    if first_line.lower().startswith("exit code:"):
        raw_code = first_line.split(":", 1)[1].strip().split(maxsplit=1)[0]
        try:
            return "completed" if int(raw_code) == 0 else "error"
        except ValueError:
            return fallback
    return fallback


def build_tool_event_from_provider_items(
    tool_call_item: dict[str, Any] | None,
    tool_output_item: dict[str, Any] | None,
) -> dict[str, object]:
    identity_item = tool_call_item or tool_output_item or {}
    call_name = tool_display_title_from_provider_item(identity_item)
    output_text = tool_output_text_from_provider_item(tool_output_item or tool_call_item)
    fallback_status = sanitize_text((identity_item or {}).get("status") or "").strip() or "completed"
    has_output = tool_output_item is not None or provider_item_type(tool_call_item) in CODEX_STANDALONE_TOOL_CALL_ITEM_TYPES
    return {
        "name": call_name,
        "arguments": tool_call_arguments_value(tool_call_item),
        "call_id": provider_item_call_id(identity_item),
        "output_preview": block_text_preview(output_text, limit=180) if output_text else "",
        "raw_output": output_text,
        "display_title": call_name,
        "display_detail": tool_display_detail_from_provider_item(tool_call_item),
        "display_result": block_text_preview(output_text, limit=180) if output_text else "",
        "status": tool_status_from_output(output_text, fallback_status) if has_output else "pending",
    }


def context_detail_block(block: dict[str, object], block_number: int) -> dict[str, object]:
    safe_block = sanitize_value(block)
    if sanitize_text(safe_block.get("kind") or "").strip() != "tool":
        return {
            "block_number": block_number,
            **safe_block,
        }

    tool_event = safe_block.get("tool_event")
    if not isinstance(tool_event, dict):
        return {
            "block_number": block_number,
            **safe_block,
        }

    slim_tool_event = {
        "name": sanitize_text(tool_event.get("name") or ""),
        "call_id": sanitize_text(tool_event.get("call_id") or ""),
        "arguments": sanitize_value(tool_event.get("arguments")),
        "output_preview": sanitize_text(tool_event.get("output_preview") or ""),
        "display_title": sanitize_text(tool_event.get("display_title") or ""),
        "display_detail": sanitize_text(tool_event.get("display_detail") or ""),
        "display_result": sanitize_text(tool_event.get("display_result") or ""),
        "status": sanitize_text(tool_event.get("status") or ""),
    }
    return {
        "block_number": block_number,
        "kind": "tool",
        "tool_event": slim_tool_event,
        "full_output_source": "provider_items tool output item with the same call_id",
    }


def compile_record_from_provider_items(
    original_record: dict[str, object],
    provider_items: list[dict[str, Any]],
) -> dict[str, object]:
    normalized_provider_items = normalize_provider_items(provider_items)
    role = sanitize_text(original_record.get("role") or "").strip() or "assistant"
    attachments = normalize_attachment_records(original_record.get("attachments"))

    blocks: list[dict[str, object]] = []
    tool_events: list[dict[str, object]] = []
    consumed_output_indexes: set[int] = set()
    output_indexes_by_call_id: dict[str, list[int]] = {}

    for index, item in enumerate(normalized_provider_items):
        if provider_item_type(item) not in CODEX_TOOL_OUTPUT_ITEM_TYPES:
            continue
        call_id = provider_item_call_id(item)
        if not call_id:
            continue
        output_indexes_by_call_id.setdefault(call_id, []).append(index)

    for index, item in enumerate(normalized_provider_items):
        item_type = provider_item_type(item)
        if item_type == "message":
            message_text = extract_text_from_provider_message_content(item.get("content"))
            if message_text:
                blocks.append(
                    {
                        "kind": "text",
                        "text": message_text,
                    }
                )
            continue

        if item_type in {"compaction", "compaction_summary"}:
            visible_text = visible_text_from_compaction_provider_item(item)
            if visible_text:
                blocks.append(
                    {
                        "kind": "text",
                        "text": visible_text,
                    }
                )
            continue

        if item_type == "reasoning":
            reasoning_text = provider_payload_text(item.get("summary") or item.get("content") or item.get("text"))
            if reasoning_text:
                blocks.append(
                    {
                        "kind": "reasoning",
                        "text": reasoning_text,
                        "status": "completed",
                    }
                )
            continue

        if item_type in CODEX_PAIRED_TOOL_CALL_ITEM_TYPES:
            call_id = provider_item_call_id(item)
            allowed_output_types = CODEX_TOOL_OUTPUT_TYPES_BY_CALL_TYPE.get(item_type, set())
            output_item = None
            for output_index in output_indexes_by_call_id.get(call_id, []):
                if output_index in consumed_output_indexes:
                    continue
                candidate_output = normalized_provider_items[output_index]
                if allowed_output_types and provider_item_type(candidate_output) not in allowed_output_types:
                    continue
                output_item = candidate_output
                consumed_output_indexes.add(output_index)
                break

            tool_event = build_tool_event_from_provider_items(item, output_item)
            tool_events.append(tool_event)
            blocks.append(
                {
                    "kind": "tool",
                    "tool_event": tool_event,
                }
            )
            continue

        if item_type in CODEX_STANDALONE_TOOL_CALL_ITEM_TYPES:
            tool_event = build_tool_event_from_provider_items(item, None)
            tool_events.append(tool_event)
            blocks.append(
                {
                    "kind": "tool",
                    "tool_event": tool_event,
                }
            )
            continue

        if item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES and index not in consumed_output_indexes:
            tool_event = build_tool_event_from_provider_items(None, item)
            tool_events.append(tool_event)
            blocks.append(
                {
                    "kind": "tool",
                    "tool_event": tool_event,
                }
            )

    return {
        "role": role,
        "text": message_blocks_to_text(blocks),
        "attachments": sanitize_value(attachments),
        "toolEvents": sanitize_value(tool_events),
        "blocks": sanitize_value(blocks),
        "providerItems": sanitize_value(normalized_provider_items),
    }


def context_record_details_payload(record: dict[str, object], *, node_number: int) -> dict[str, object]:
    overview = context_record_overview(record, node_number=node_number)
    provider_items = normalize_provider_items(record.get("providerItems"))
    return {
        "node_number": node_number,
        "role": overview["role"],
        "token_estimate": overview["token_estimate"],
        "tool_token_estimate": overview["tool_token_estimate"],
        "tool_usage": overview["tool_usage"],
        "preview": overview["preview"],
        "item_count": len(provider_items),
        "text": sanitize_text(record.get("text") or ""),
        "attachments": sanitize_value(normalize_attachment_records(record.get("attachments"))),
        "blocks": [
            context_detail_block(block, block_number)
            for block_number, block in enumerate(normalize_message_blocks(record.get("blocks")), start=1)
        ],
        "provider_items": provider_items,
        "items": [
            provider_item_detail(item, item_number)
            for item_number, item in enumerate(provider_items, start=1)
        ],
    }


def build_context_workspace_snapshot(
    session: SessionState,
    *,
    selected_indexes: list[int] | None = None,
) -> str:
    transcript = normalize_transcript(session.transcript)
    safe_selected_indexes = normalize_selected_node_indexes(selected_indexes or [], len(transcript))
    selected_numbers = selected_display_node_numbers(transcript, safe_selected_indexes)
    editable_entries = editable_context_node_entries(transcript)
    lines = [
        "# 当前上下文快照",
        f"- 会话标题：{session.title}",
        f"- 会话类型：{session.scope}",
        f"- 当前节点数：{len(editable_entries)}",
        f"- 当前选中节点：{format_node_ranges(selected_numbers) or '未单独选中，默认面向全局'}",
        "- 这一轮里所有 Node # 都以这份快照为准。",
        "- 系统/开发者指令和默认环境说明属于内部前缀，不在本快照中展示，也不能被选择或编辑。",
        "- 非 assistant 节点直接给全文，assistant 节点默认只给上下文地图折叠态同款首句预览，预览后面的内容你并不可见。",
        "- 如果你需要定位某类 provider item，优先调用 find_context_items；只有确实需要完整协议层细节时，再调用 get_context_node_details。",
        "",
        "## 节点概览",
    ]

    for entry in editable_entries:
        node_number = int(entry["node_number"])
        raw_index = int(entry["raw_index"])
        record = sanitize_value(entry["record"])
        overview = context_record_overview(
            record,
            node_number=node_number,
            selected=raw_index in safe_selected_indexes,
        )
        marker = " | selected" if overview["selected"] else ""
        token_label = format_token_count(int(overview["token_estimate"] or 0))
        tool_token_estimate = int(overview.get("tool_token_estimate") or 0)
        tool_token_label = (
            f" | tool {format_token_count(tool_token_estimate)} tokens"
            if tool_token_estimate > 0
            else ""
        )
        role = sanitize_text(overview["role"] or "").strip() or "unknown"
        if role != "assistant":
            node_text = sanitize_text(overview["full_text"] or "").strip() or "[empty]"
            lines.append(f"- Node #{node_number} | {role}{marker} | {token_label} tokens")
            lines.append("  content:")
            for content_line in node_text.splitlines() or ["[empty]"]:
                lines.append(f"    {content_line}")
            continue

        lines.append(
            f"- Node #{node_number} | {role}{marker} | {token_label} tokens{tool_token_label} | {format_tool_usage(overview['tool_usage'])} | {int(overview['item_count'] or 0)} items"
        )
        lines.append(f"  preview: {sanitize_text(overview['preview'] or '') or '[empty]'}")

    return "\n".join(lines).strip()


def find_codex_local_session_file(session_id: str) -> Path | None:
    safe_session_id = sanitize_text(session_id or "").strip()
    if not safe_session_id or not CODEX_LOCAL_SESSIONS_DIR.exists():
        return None

    try:
        matches = [
            path
            for path in CODEX_LOCAL_SESSIONS_DIR.rglob(f"*{safe_session_id}.jsonl")
            if path.is_file()
        ]
    except OSError:
        return None

    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def codex_message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return sanitize_text(content)
    if not isinstance(content, list):
        return sanitize_text(content)

    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text") or item.get("input_text") or item.get("output_text")
            if text:
                parts.append(sanitize_text(text))
        elif item is not None:
            parts.append(sanitize_text(item))
    return "\n".join(part for part in parts if part).strip()


def transcript_has_instruction_prefix(records: list[dict[str, Any]]) -> bool:
    return any(
        sanitize_text(record.get("role") or "").strip() in {"system", "developer"}
        for record in records
    )


def provider_message_record(item: dict[str, Any]) -> dict[str, Any] | None:
    role = sanitize_text(item.get("role") or "").strip()
    if role not in CONTEXT_INPUT_MESSAGE_ROLES:
        return None

    text = codex_message_content_text(item.get("content")).strip()
    return {
        "role": role,
        "text": text,
        "attachments": [],
        "toolEvents": [],
        "blocks": [{"kind": "text", "text": text}] if text else [],
        "providerItems": [sanitize_value(item)],
    }


def latest_proxy_instruction_prefix_records() -> list[dict[str, Any]]:
    try:
        data = json.loads(PROXY_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    raw_sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(raw_sessions, list):
        return []

    active_session_id = sanitize_text(data.get("active_session_id") or "").strip()
    sessions = [session for session in raw_sessions if isinstance(session, dict)]
    sessions.sort(
        key=lambda session: (
            sanitize_text(session.get("id") or "").strip() == active_session_id,
            sanitize_text(session.get("updated_at") or ""),
        ),
        reverse=True,
    )

    for session in sessions:
        request_log = session.get("request_log")
        if not isinstance(request_log, list):
            continue
        for entry in reversed(request_log):
            if not isinstance(entry, dict):
                continue
            body = entry.get("forwarded_body") if isinstance(entry.get("forwarded_body"), dict) else entry.get("body")
            if not isinstance(body, dict):
                continue
            input_items = body.get("input")
            if not isinstance(input_items, list):
                continue

            prefix: list[dict[str, Any]] = []
            for raw_item in input_items:
                if not isinstance(raw_item, dict):
                    continue
                item_type = sanitize_text(raw_item.get("type") or "").strip()
                role = sanitize_text(raw_item.get("role") or "").strip()
                if item_type == "message" and role in {"system", "developer"}:
                    record = provider_message_record(raw_item)
                    if record is not None:
                        prefix.append(record)
                    continue
                if prefix:
                    break

            if prefix:
                return normalize_transcript(prefix)

    return []


def codex_local_session_transcript(session_id: str) -> list[dict[str, Any]]:
    session_file = find_codex_local_session_file(session_id)
    if session_file is None:
        return latest_proxy_instruction_prefix_records()

    records: list[dict[str, Any]] = []
    try:
        lines = session_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        role = sanitize_text(payload.get("role") or "").strip()
        if role not in {"system", "developer", "user", "assistant"}:
            continue
        text = codex_message_content_text(payload.get("content")).strip()
        if not text:
            continue
        records.append(
            {
                "role": role,
                "text": text,
                "attachments": [],
                "toolEvents": [],
                "blocks": [{"kind": "text", "text": text}],
                "providerItems": [{"type": "message", "role": role, "content": text}],
            }
        )

    if not transcript_has_instruction_prefix(records):
        records = [*latest_proxy_instruction_prefix_records(), *records]

    return normalize_transcript(records)


def format_node_ranges(node_numbers: list[int]) -> str:
    if not node_numbers:
        return ""

    ordered = sorted(set(node_numbers))
    segments: list[str] = []
    range_start = ordered[0]
    previous = ordered[0]
    for current in ordered[1:]:
        if current == previous + 1:
            previous = current
            continue
        segments.append(f"{range_start}" if range_start == previous else f"{range_start}-{previous}")
        range_start = current
        previous = current
    segments.append(f"{range_start}" if range_start == previous else f"{range_start}-{previous}")
    return ", ".join(segments)


def tool_output_type_matches_call_type(output_type: str, call_type: str) -> bool:
    return output_type in CODEX_TOOL_OUTPUT_TYPES_BY_CALL_TYPE.get(call_type, set())


def paired_tool_item_indexes(provider_items: list[dict[str, Any]], item_index: int) -> list[int]:
    if item_index < 0 or item_index >= len(provider_items):
        return []
    item = provider_items[item_index]
    item_type = provider_item_type(item)
    call_id = provider_item_call_id(item)
    if not call_id:
        return [item_index]

    if item_type in CODEX_PAIRED_TOOL_CALL_ITEM_TYPES:
        paired = [item_index]
        allowed_output_types = CODEX_TOOL_OUTPUT_TYPES_BY_CALL_TYPE.get(item_type, set())
        for index, candidate in enumerate(provider_items):
            if index == item_index or provider_item_call_id(candidate) != call_id:
                continue
            if provider_item_type(candidate) in allowed_output_types:
                paired.append(index)
        return sorted(set(paired))

    if item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES:
        paired = [item_index]
        allowed_call_types = CODEX_TOOL_CALL_TYPES_BY_OUTPUT_TYPE.get(item_type, set())
        for index, candidate in enumerate(provider_items):
            if index == item_index or provider_item_call_id(candidate) != call_id:
                continue
            if provider_item_type(candidate) in allowed_call_types:
                paired.append(index)
        return sorted(set(paired))

    return [item_index]


def validate_context_provider_items(provider_items: list[dict[str, Any]]) -> None:
    calls_by_id: dict[str, list[tuple[int, str]]] = {}
    outputs_by_id: dict[str, list[tuple[int, str]]] = {}

    for index, item in enumerate(provider_items):
        item_type = provider_item_type(item)
        call_id = provider_item_call_id(item)
        if item_type in CODEX_PAIRED_TOOL_CALL_ITEM_TYPES:
            if not call_id:
                raise ValueError(f"tool call item #{index + 1} is missing call_id")
            calls_by_id.setdefault(call_id, []).append((index, item_type))
        elif item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES:
            if not call_id:
                raise ValueError(f"tool output item #{index + 1} is missing call_id")
            outputs_by_id.setdefault(call_id, []).append((index, item_type))

    for call_id, calls in calls_by_id.items():
        outputs = outputs_by_id.get(call_id, [])
        for call_index, call_type in calls:
            if not any(tool_output_type_matches_call_type(output_type, call_type) for _, output_type in outputs):
                raise ValueError(
                    f"tool call item #{call_index + 1} ({call_type}, call_id={call_id}) has no matching output item"
                )

    for call_id, outputs in outputs_by_id.items():
        calls = calls_by_id.get(call_id, [])
        for output_index, output_type in outputs:
            if not any(tool_output_type_matches_call_type(output_type, call_type) for _, call_type in calls):
                raise ValueError(
                    f"tool output item #{output_index + 1} ({output_type}, call_id={call_id}) has no matching call item"
                )


def validate_context_replacement_identity(original_item: dict[str, Any], replacement_item: dict[str, Any]) -> None:
    original_type = provider_item_type(original_item)
    replacement_type = provider_item_type(replacement_item)
    if not replacement_type:
        raise ValueError("replacement_item.type is required")
    if original_type and replacement_type != original_type:
        raise ValueError(
            f"replacement_item must keep item type {original_type!r}; use node compression/deletion for structural rewrites"
        )

    if original_type == "message":
        original_role = sanitize_text(original_item.get("role") or "").strip()
        replacement_role = sanitize_text(replacement_item.get("role") or "").strip()
        if original_role and replacement_role != original_role:
            raise ValueError(f"replacement message must keep role {original_role!r}")

    if original_type in CODEX_TOOL_CALL_ITEM_TYPES or original_type in CODEX_TOOL_OUTPUT_ITEM_TYPES:
        original_call_id = provider_item_call_id(original_item)
        replacement_call_id = provider_item_call_id(replacement_item)
        if original_call_id and replacement_call_id != original_call_id:
            raise ValueError(f"replacement tool item must keep call_id {original_call_id!r}")


def letter_index(value: int) -> str:
    result = ""
    current = max(1, value)
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        result = f"{chr(65 + remainder)}{result}"
    return result


@dataclass(slots=True)
class ContextWorkbenchDraftNode:
    order: float
    label: str
    record: dict[str, object]
    active: bool
    source_node_number: int | None = None
    source_index: int | None = None
    kind: str = "existing"
    status: str = "active"
    editable: bool = True


class ContextWorkbenchDraft:
    def __init__(self, transcript: list[dict[str, object]], selected_indexes: list[int]) -> None:
        normalized_transcript = normalize_transcript(transcript)
        safe_selected = normalize_selected_node_indexes(selected_indexes, len(normalized_transcript))
        self.selected_node_numbers = selected_display_node_numbers(normalized_transcript, safe_selected)
        internal_indexes = internal_context_prefix_indexes(normalized_transcript)
        editable_numbers_by_raw_index = {
            int(entry["raw_index"]): int(entry["node_number"])
            for entry in editable_context_node_entries(normalized_transcript)
        }
        self.nodes: list[ContextWorkbenchDraftNode] = []
        for raw_index, record in enumerate(normalized_transcript):
            node_number = editable_numbers_by_raw_index.get(raw_index)
            is_internal = raw_index in internal_indexes
            label = f"Node #{node_number}" if node_number is not None else "Internal Prefix"
            self.nodes.append(
                ContextWorkbenchDraftNode(
                    order=float(raw_index + 1),
                    label=label,
                    record=sanitize_value(record),
                    active=True,
                    source_node_number=node_number,
                    source_index=raw_index,
                    kind="internal" if is_internal else "existing",
                    status="locked" if is_internal else "active",
                    editable=not is_internal,
                )
            )
        self.operations: list[dict[str, object]] = []
        self._draft_counter = 0
        self._revision_summary = ""

    @property
    def has_changes(self) -> bool:
        return bool(self.operations)

    def _record_operation(self, operation: dict[str, object]) -> None:
        self.operations.append(operation)
        self._revision_summary = ""

    def set_revision_summary(self, summary: str) -> dict[str, object]:
        if not self.operations:
            raise ValueError("no working snapshot edits exist yet")

        safe_summary = re.sub(r"\s+", " ", sanitize_text(summary)).strip()
        if not safe_summary:
            raise ValueError("summary is required")
        if len(safe_summary) > 220:
            safe_summary = f"{safe_summary[:219].rstrip()}…"

        self._revision_summary = safe_summary
        return {
            "payload_kind": "revision_summary",
            "summary": safe_summary,
            "change_count": len(self.operations),
            "working_overview": self.current_overview_items(),
        }

    def _fallback_revision_summary(self) -> str:
        if not self.operations:
            return "这次更新了当前上下文。"
        return fallback_context_revision_summary("Context update", self.operations)

    def revision_summary(self) -> str:
        return self._revision_summary or self._fallback_revision_summary()

    def active_nodes(self) -> list[ContextWorkbenchDraftNode]:
        return [
            node
            for node in sorted(self.nodes, key=lambda item: item.order)
            if node.active and node.editable
        ]

    def committed_nodes(self) -> list[ContextWorkbenchDraftNode]:
        return [node for node in sorted(self.nodes, key=lambda item: item.order) if node.active]

    def max_node_number(self) -> int:
        return max((node.source_node_number or 0) for node in self.nodes) if self.nodes else 0

    def _nodes_by_number(self, node_numbers: list[int], *, include_inactive: bool = False) -> list[ContextWorkbenchDraftNode]:
        targets: list[ContextWorkbenchDraftNode] = []
        for node_number in node_numbers:
            node = next(
                (
                    item
                    for item in self.nodes
                    if item.source_node_number == node_number and (include_inactive or item.active)
                ),
                None,
            )
            if node is not None:
                targets.append(node)
        return targets

    def _node_search_text(self, node: ContextWorkbenchDraftNode) -> str:
        overview = context_record_overview(
            node.record,
            node_number=node.source_node_number or 1,
            selected=(node.source_node_number or 0) in self.selected_node_numbers,
        )
        parts = [
            node.label,
            sanitize_text(overview.get("role") or ""),
            sanitize_text(overview.get("preview") or ""),
            sanitize_text(overview.get("full_text") or ""),
            format_tool_usage(sanitize_value(overview.get("tool_usage"))),
            record_context_weight_source(node.record),
        ]
        return "\n".join(part for part in parts if sanitize_text(part).strip())

    def _candidate_score(self, node: ContextWorkbenchDraftNode, target_hint: str) -> int:
        safe_hint = sanitize_text(target_hint).strip()
        overview = self._overview_for_node(node)
        if not safe_hint:
            return int(overview.get("token_estimate") or 0) + int(overview.get("tool_count") or 0) * 120

        hint_text = safe_hint.lower()
        haystack = self._node_search_text(node).lower()
        score = 0

        if sanitize_text(node.label).strip().lower() in hint_text:
            score += 400

        for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", hint_text):
            if len(token) <= 1:
                continue
            if token in haystack:
                score += 120

        if any(keyword in hint_text for keyword in ["latest", "recent", "last", "最近", "最后"]):
            score += int(node.source_node_number or 0) * 10

        if any(keyword in hint_text for keyword in ["tool", "tools", "工具", "调用"]):
            score += int(overview.get("tool_count") or 0) * 160

        if any(keyword in hint_text for keyword in ["long", "heavy", "verbose", "冗长", "很重", "最长"]):
            score += int(overview.get("token_estimate") or 0)

        if any(keyword in hint_text for keyword in ["user", "用户"]):
            score += 160 if sanitize_text(overview.get("role") or "") == "user" else 0

        if any(keyword in hint_text for keyword in ["assistant", "助手"]):
            score += 160 if sanitize_text(overview.get("role") or "") == "assistant" else 0

        return score

    def suggest_target_nodes(self, target_hint: str = "", *, limit: int = 4) -> list[dict[str, object]]:
        candidates = [
            (self._candidate_score(node, target_hint), node)
            for node in self.active_nodes()
        ]
        candidates.sort(
            key=lambda item: (
                -item[0],
                -int(self._overview_for_node(item[1]).get("token_estimate") or 0),
                -(item[1].source_node_number or 0),
            )
        )
        ranked_nodes = [node for score, node in candidates if score > 0][: max(1, limit)]
        if not ranked_nodes and not target_hint:
            ranked_nodes = self.active_nodes()[: max(1, limit)]
        return self.overview_items(ranked_nodes)

    def _resolve_target_nodes_from_hint(
        self,
        target_hint: str,
        *,
        include_inactive: bool = False,
    ) -> list[ContextWorkbenchDraftNode]:
        searchable_nodes = self.nodes if include_inactive else self.active_nodes()
        ranked = [
            (self._candidate_score(node, target_hint), node)
            for node in searchable_nodes
        ]
        ranked = [item for item in ranked if item[0] > 0]
        ranked.sort(
            key=lambda item: (
                -item[0],
                -int(self._overview_for_node(item[1]).get("token_estimate") or 0),
                -(item[1].source_node_number or 0),
            )
        )
        if not ranked:
            return []

        best_score = ranked[0][0]
        second_score = ranked[1][0] if len(ranked) > 1 else -1
        if len(ranked) == 1 or best_score >= second_score + 120:
            return [ranked[0][1]]
        return []

    def resolve_target_nodes(
        self,
        arguments: dict[str, Any],
        *,
        allow_selected: bool = True,
        allow_all_active: bool = False,
        include_inactive: bool = False,
    ) -> list[ContextWorkbenchDraftNode]:
        explicit_numbers = normalize_node_numbers(arguments.get("node_numbers"), self.max_node_number())
        if explicit_numbers:
            return self._nodes_by_number(explicit_numbers, include_inactive=include_inactive)

        legacy_indexes = normalize_selected_node_indexes(arguments.get("node_indexes"), self.max_node_number())
        if legacy_indexes:
            return self._nodes_by_number([index + 1 for index in legacy_indexes], include_inactive=include_inactive)

        if allow_selected and self.selected_node_numbers:
            return self._nodes_by_number(self.selected_node_numbers, include_inactive=include_inactive)

        target_hint = sanitize_text(arguments.get("target_hint") or "").strip()
        if target_hint:
            resolved_from_hint = self._resolve_target_nodes_from_hint(
                target_hint,
                include_inactive=include_inactive,
            )
            if resolved_from_hint:
                return resolved_from_hint

        if allow_all_active:
            return self.active_nodes()

        return []

    def _overview_for_node(self, node: ContextWorkbenchDraftNode) -> dict[str, object]:
        display_number = node.source_node_number or 1
        overview = context_record_overview(
            node.record,
            node_number=display_number,
            selected=(node.source_node_number or 0) in self.selected_node_numbers,
        )
        overview["payload_kind"] = "node_overview"
        overview["node_number"] = node.source_node_number
        overview["label"] = node.label
        overview["status"] = node.status
        overview["node_kind"] = node.kind
        overview["active"] = node.active
        return overview

    def current_overview_items(self) -> list[dict[str, object]]:
        return [self._overview_for_node(node) for node in self.active_nodes()]

    def overview_items(self, nodes: list[ContextWorkbenchDraftNode]) -> list[dict[str, object]]:
        return [self._overview_for_node(node) for node in nodes]

    def node_details(self, nodes: list[ContextWorkbenchDraftNode]) -> list[dict[str, object]]:
        details: list[dict[str, object]] = []
        for node in nodes:
            detail = context_record_details_payload(node.record, node_number=node.source_node_number or 1)
            detail["payload_kind"] = "node_detail"
            detail["node_number"] = node.source_node_number
            detail["label"] = node.label
            detail["status"] = node.status
            detail["active"] = node.active
            detail["node_kind"] = node.kind
            details.append(detail)
        return details

    def mutation_node_details(self, nodes: list[ContextWorkbenchDraftNode]) -> list[dict[str, object]]:
        details: list[dict[str, object]] = []
        for node in nodes:
            provider_items = self._provider_items_for_node(node)
            overview = self._overview_for_node(node)
            details.append(
                {
                    "payload_kind": "node_mutation_detail",
                    "node_number": node.source_node_number,
                    "label": node.label,
                    "status": node.status,
                    "active": node.active,
                    "node_kind": node.kind,
                    "overview": overview,
                    "item_count": len(provider_items),
                    "full_detail_note": (
                        "Mutation results intentionally omit full provider_items and per-item detail to avoid repeating large node content. "
                        "For simple delete/replace/compress steps, do not re-open node details just to verify; use this result and working_overview. "
                        "Only call get_context_node_details again when the next edit requires exact updated provider_items from the current working snapshot."
                    ),
                }
            )
        return details

    def _next_draft_label(self) -> str:
        self._draft_counter += 1
        return f"Draft Node {letter_index(self._draft_counter)}"

    def _set_node_record(self, node: ContextWorkbenchDraftNode, record: dict[str, object], *, status: str = "updated") -> None:
        normalized_record = normalize_transcript([record])
        if not normalized_record:
            raise ValueError("record could not be normalized after mutation")
        node.record = normalized_record[0]
        if node.kind == "existing":
            node.status = status

    def _provider_items_for_node(self, node: ContextWorkbenchDraftNode) -> list[dict[str, Any]]:
        return normalize_provider_items(node.record.get("providerItems"))

    def _resolve_item_detail(self, node: ContextWorkbenchDraftNode, item_number: int) -> dict[str, object]:
        items = self.node_details([node])[0].get("items")
        if not isinstance(items, list):
            raise ValueError("node detail items are unavailable")
        if item_number < 1 or item_number > len(items):
            raise ValueError(f"item #{item_number} does not exist in {node.label}")
        item = items[item_number - 1]
        if not isinstance(item, dict):
            raise ValueError(f"item #{item_number} could not be resolved in {node.label}")
        return item

    def _item_ref(self, node: ContextWorkbenchDraftNode, item_number: int) -> str:
        return f"node:{int(node.source_node_number or 0)}:item:{item_number}"

    def _parse_item_ref(self, raw_ref: Any) -> tuple[int, int] | None:
        safe_ref = sanitize_text(raw_ref or "").strip().lower()
        if not safe_ref:
            return None

        match = re.search(r"node\D*(\d+)\D+item\D*(\d+)", safe_ref)
        if match is None:
            match = re.fullmatch(r"(\d+)\s*[:/]\s*(\d+)", safe_ref)
        if match is None:
            return None

        try:
            node_number = int(match.group(1))
            item_number = int(match.group(2))
        except (TypeError, ValueError):
            return None
        if node_number <= 0 or item_number <= 0:
            return None
        return node_number, item_number

    def _item_text_source(self, item: dict[str, Any]) -> str:
        item_type = provider_item_type(item)
        if item_type == "message":
            return extract_text_from_provider_message_content(item.get("content"))
        if item_type == "reasoning":
            return provider_payload_text(item.get("summary") or item.get("content") or item.get("text"))
        if item_type in {"compaction", "compaction_summary"}:
            return visible_text_from_compaction_provider_item(item)
        if item_type in CODEX_TOOL_CALL_ITEM_TYPES:
            return provider_payload_text(tool_call_arguments_value(item))
        if item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES or item_type == "image_generation_call":
            return tool_output_text_from_provider_item(item)
        return provider_payload_text(item)

    def _can_replace_item_content(self, item: dict[str, Any]) -> bool:
        return provider_item_type(item) in {
            "message",
            "function_call",
            "custom_tool_call",
            "function_call_output",
            "custom_tool_call_output",
            "local_shell_call_output",
            "mcp_tool_call_output",
            "tool_search_output",
        }

    def _replace_item_content(self, item: dict[str, Any], replacement_text: str) -> dict[str, Any]:
        item_type = provider_item_type(item)
        replacement_item = sanitize_value(item)
        safe_content = sanitize_text(replacement_text)

        if item_type == "message":
            replacement_item["content"] = replace_provider_message_text(item.get("content"), safe_content)
            return replacement_item
        if item_type == "function_call":
            replacement_item["arguments"] = safe_content
            return replacement_item
        if item_type == "custom_tool_call":
            replacement_item["input"] = safe_content
            return replacement_item
        if item_type in {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}:
            replacement_item["output"] = safe_content
            return replacement_item
        if item_type == "mcp_tool_call_output":
            replacement_item["output"] = {
                "content": [{"type": "text", "text": safe_content}],
                "structured_content": None,
                "is_error": False,
                "meta": None,
            }
            return replacement_item
        if item_type == "tool_search_output":
            replacement_item["tools"] = [{"summary": safe_content}]
            return replacement_item

        raise ValueError(f"{item_type or 'unknown'} items do not support batch content replacement")

    def _light_item_entry(
        self,
        node: ContextWorkbenchDraftNode,
        provider_items: list[dict[str, Any]],
        item_index: int,
    ) -> dict[str, object]:
        item = provider_items[item_index]
        item_number = item_index + 1
        item_type = provider_item_type(item) or "unknown"
        text_source = self._item_text_source(item)
        paired_indexes = paired_tool_item_indexes(provider_items, item_index)
        detail = provider_item_detail(item, item_number)
        entry: dict[str, object] = {
            "node_number": node.source_node_number,
            "node_label": node.label,
            "item_number": item_number,
            "item_ref": self._item_ref(node, item_number),
            "item_type": item_type,
            "type": item_type,
            "role": sanitize_text(item.get("role") or "").strip(),
            "name": tool_display_title_from_provider_item(item)
            if item_type in CODEX_TOOL_CALL_ITEM_TYPES or item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES
            else "",
            "call_id": provider_item_call_id(item),
            "token_estimate": estimate_token_count(text_source),
            "text_chars": len(text_source),
            "preview": block_text_preview(text_source, limit=160),
            "display_detail": tool_display_detail_from_provider_item(item)
            if item_type in CODEX_TOOL_CALL_ITEM_TYPES
            else "",
            "paired_item_numbers": [index + 1 for index in paired_indexes],
            "is_tool_call": item_type in CODEX_TOOL_CALL_ITEM_TYPES,
            "is_tool_output": item_type in CODEX_TOOL_OUTPUT_ITEM_TYPES,
            "delete_supported": True,
            "replace_content_supported": self._can_replace_item_content(item),
        }
        for key in ("text_preview", "arguments_preview", "output_preview", "encoded_content_preview"):
            if key in detail:
                entry[key] = detail[key]
        return entry

    def _selector_item_refs(self, selector: dict[str, Any]) -> set[tuple[int, int]]:
        raw_refs = selector.get("item_refs")
        if not isinstance(raw_refs, list):
            return set()
        refs: set[tuple[int, int]] = set()
        for raw_ref in raw_refs:
            parsed_ref = self._parse_item_ref(raw_ref)
            if parsed_ref is not None:
                refs.add(parsed_ref)
        return refs

    def _selector_nodes(self, selector: dict[str, Any], item_refs: set[tuple[int, int]]) -> list[ContextWorkbenchDraftNode]:
        explicit_numbers = normalize_node_numbers(selector.get("node_numbers"), self.max_node_number())
        if explicit_numbers:
            return self._nodes_by_number(explicit_numbers)

        if item_refs:
            return self._nodes_by_number(sorted({node_number for node_number, _item_number in item_refs}))

        target_hint = sanitize_text(selector.get("target_hint") or "").strip()
        if target_hint:
            return self.resolve_target_nodes(
                {"target_hint": target_hint},
                allow_selected=False,
                allow_all_active=False,
            )

        if bool(selector.get("selected_only")) and self.selected_node_numbers:
            return self._nodes_by_number(self.selected_node_numbers)

        return self.active_nodes()

    def _selector_item_numbers(self, selector: dict[str, Any]) -> set[int]:
        raw_numbers = selector.get("item_numbers")
        if not isinstance(raw_numbers, list):
            return set()

        numbers: set[int] = set()
        for raw_number in raw_numbers:
            try:
                item_number = int(raw_number)
            except (TypeError, ValueError):
                continue
            if item_number > 0:
                numbers.add(item_number)
        return numbers

    def _selector_text_list(self, raw_value: Any) -> set[str]:
        if not isinstance(raw_value, list):
            return set()
        return {
            sanitize_text(item).strip()
            for item in raw_value
            if sanitize_text(item).strip()
        }

    def _match_context_items(
        self,
        selector: dict[str, Any],
        *,
        limit: int | None = None,
    ) -> list[tuple[ContextWorkbenchDraftNode, int, dict[str, object]]]:
        safe_selector = selector if isinstance(selector, dict) else {}
        item_refs = self._selector_item_refs(safe_selector)
        nodes = self._selector_nodes(safe_selector, item_refs)
        item_numbers = self._selector_item_numbers(safe_selector)
        item_types = self._selector_text_list(safe_selector.get("item_types"))
        roles = self._selector_text_list(safe_selector.get("roles"))
        text_contains = sanitize_text(safe_selector.get("text_contains") or "").strip().lower()
        try:
            min_token_estimate = int(safe_selector.get("min_token_estimate") or 0)
        except (TypeError, ValueError):
            min_token_estimate = 0

        tool_type_filters: set[str] = set()
        if bool(safe_selector.get("tool_output_only")):
            tool_type_filters.update(CODEX_TOOL_OUTPUT_ITEM_TYPES)
        if bool(safe_selector.get("tool_call_only")):
            tool_type_filters.update(CODEX_TOOL_CALL_ITEM_TYPES)

        matches: list[tuple[ContextWorkbenchDraftNode, int, dict[str, object]]] = []
        for node in nodes:
            provider_items = self._provider_items_for_node(node)
            for item_index, item in enumerate(provider_items):
                item_number = item_index + 1
                if item_refs and (int(node.source_node_number or 0), item_number) not in item_refs:
                    continue
                if item_numbers and item_number not in item_numbers:
                    continue

                item_type = provider_item_type(item) or "unknown"
                if item_types and item_type not in item_types:
                    continue
                if tool_type_filters and item_type not in tool_type_filters:
                    continue

                role = sanitize_text(item.get("role") or "").strip()
                if roles and role not in roles:
                    continue

                text_source = self._item_text_source(item)
                if text_contains and text_contains not in text_source.lower():
                    continue

                entry = self._light_item_entry(node, provider_items, item_index)
                if min_token_estimate > 0 and int(entry.get("token_estimate") or 0) < min_token_estimate:
                    continue

                matches.append((node, item_index, entry))
                if limit is not None and len(matches) >= limit:
                    return matches

        return matches

    def find_context_items(self, selector: dict[str, Any]) -> dict[str, object]:
        safe_selector = selector if isinstance(selector, dict) else {}
        try:
            max_results = int(safe_selector.get("max_results") or 120)
        except (TypeError, ValueError):
            max_results = 120
        max_results = max(1, min(max_results, 500))

        all_matches = self._match_context_items(safe_selector)
        visible_matches = all_matches[:max_results]
        items = [entry for _node, _index, entry in visible_matches]
        total_tokens = sum(int(entry.get("token_estimate") or 0) for _node, _index, entry in all_matches)
        return {
            "payload_kind": "context_item_list",
            "matched_count": len(all_matches),
            "returned_count": len(items),
            "truncated": len(all_matches) > len(items),
            "total_token_estimate": total_tokens,
            "items": items,
            "selector": sanitize_value(safe_selector),
            "note": (
                "This is a lightweight item inventory. It intentionally contains previews and metadata only, not full item content."
            ),
        }

    def _compact_batch_mutation_result(
        self,
        *,
        summary: str,
        change_type: str,
        matched_count: int,
        changed_items: list[dict[str, object]],
        changed_nodes: list[int],
        dry_run: bool,
        selector: dict[str, Any],
        operation: dict[str, Any],
        before_tokens: int,
        after_tokens: int,
    ) -> dict[str, object]:
        visible_changed_items = changed_items[:80]
        return {
            "payload_kind": "batch_mutation_result",
            "summary": summary,
            "change_type": normalize_change_type(change_type),
            "dry_run": dry_run,
            "matched_count": matched_count,
            "changed_count": len(changed_items),
            "changed_nodes": unique_int_list(changed_nodes),
            "changed_items": visible_changed_items,
            "omitted_changed_items": max(0, len(changed_items) - len(visible_changed_items)),
            "token_delta_estimate": {
                "before": max(0, before_tokens),
                "after": max(0, after_tokens),
                "saved": before_tokens - after_tokens,
            },
            "working_overview": self.current_overview_items(),
            "selector": sanitize_value(selector),
            "operation": sanitize_value(operation),
            "note": (
                "Mutation results are compact by design. They omit full provider_items and old full content; call get_context_node_details only if the next edit needs exact current items."
            ),
        }

    def edit_context_items(
        self,
        *,
        selector: dict[str, Any],
        operation: dict[str, Any],
        reason: str,
        dry_run: bool = False,
    ) -> dict[str, object]:
        safe_selector = selector if isinstance(selector, dict) else {}
        safe_operation = operation if isinstance(operation, dict) else {}
        operation_type = sanitize_text(safe_operation.get("type") or "").strip()
        if operation_type not in {"replace_content", "compress_content", "delete"}:
            raise ValueError("operation.type must be replace_content, compress_content, or delete")

        matches = self._match_context_items(safe_selector)
        if not matches:
            return self._compact_batch_mutation_result(
                summary="No matching context items found.",
                change_type=operation_type,
                matched_count=0,
                changed_items=[],
                changed_nodes=[],
                dry_run=dry_run,
                selector=safe_selector,
                operation=safe_operation,
                before_tokens=0,
                after_tokens=0,
            )

        working_by_node: dict[int, tuple[ContextWorkbenchDraftNode, list[dict[str, Any]]]] = {}
        before_tokens = 0
        after_tokens = 0
        changed_items: list[dict[str, object]] = []

        if operation_type == "delete":
            remove_indexes_by_node: dict[int, set[int]] = {}
            for node, item_index, entry in matches:
                node_key = id(node)
                provider_items = self._provider_items_for_node(node)
                remove_indexes_by_node.setdefault(node_key, set()).update(
                    paired_tool_item_indexes(provider_items, item_index)
                )
                working_by_node.setdefault(node_key, (node, sanitize_value(provider_items)))

            for node_key, remove_indexes in remove_indexes_by_node.items():
                node, provider_items = working_by_node[node_key]
                removed_indexes = sorted(index for index in remove_indexes if 0 <= index < len(provider_items))
                for remove_index in sorted(removed_indexes, reverse=True):
                    removed_entry = self._light_item_entry(node, provider_items, remove_index)
                    before_tokens += int(removed_entry.get("token_estimate") or 0)
                    changed_items.append(
                        {
                            "node_number": removed_entry.get("node_number"),
                            "item_number": removed_entry.get("item_number"),
                            "item_ref": removed_entry.get("item_ref"),
                            "item_type": removed_entry.get("item_type"),
                            "call_id": removed_entry.get("call_id"),
                            "change": "delete",
                            "before_preview": removed_entry.get("preview"),
                        }
                    )
                    del provider_items[remove_index]
                validate_context_provider_items(provider_items)
        else:
            if "content" not in safe_operation:
                raise ValueError("operation.content is required for replace_content and compress_content")
            replacement_content = sanitize_text(safe_operation.get("content") or "")
            for node, item_index, entry in matches:
                node_key = id(node)
                if node_key not in working_by_node:
                    working_by_node[node_key] = (node, self._provider_items_for_node(node))
                working_node, provider_items = working_by_node[node_key]
                original_item = provider_items[item_index]
                replacement_item = self._replace_item_content(original_item, replacement_content)
                validate_context_replacement_identity(original_item, replacement_item)
                before_tokens += int(entry.get("token_estimate") or 0)
                after_tokens += estimate_token_count(self._item_text_source(replacement_item))
                provider_items[item_index] = replacement_item
                changed_items.append(
                    {
                        "node_number": entry.get("node_number"),
                        "item_number": entry.get("item_number"),
                        "item_ref": entry.get("item_ref"),
                        "item_type": entry.get("item_type"),
                        "call_id": entry.get("call_id"),
                        "change": "compress" if operation_type == "compress_content" else "replace",
                        "before_preview": entry.get("preview"),
                        "after_preview": block_text_preview(replacement_content, limit=160),
                    }
                )
                working_by_node[node_key] = (working_node, provider_items)

            for _node_key, (_node, provider_items) in working_by_node.items():
                validate_context_provider_items(provider_items)

        changed_nodes = [
            node.source_node_number
            for node, _provider_items in working_by_node.values()
            if node.source_node_number is not None
        ]
        changed_node_numbers = unique_int_list(changed_nodes)
        change_type = "delete" if operation_type == "delete" else (
            "compress" if operation_type == "compress_content" else "replace"
        )
        summary_action = {
            "delete": "Delete",
            "replace": "Replace content in",
            "compress": "Compress content in",
        }.get(change_type, "Update")
        summary = f"{summary_action} {len(changed_items)} context item(s)"
        if changed_node_numbers:
            summary = f"{summary} across Node #{format_node_ranges(changed_node_numbers)}"

        if not dry_run:
            for _node_key, (node, provider_items) in working_by_node.items():
                self._set_node_record(node, compile_record_from_provider_items(node.record, provider_items))
            self._record_operation(
                {
                    "operation_type": "edit_context_items",
                    "change_type": change_type,
                    "label": summary,
                    "summary": summary,
                    "changed_nodes": changed_node_numbers,
                    "target_node_numbers": changed_node_numbers,
                    "target_items": [
                        {
                            "node_number": item.get("node_number"),
                            "item_number": item.get("item_number"),
                            "item_type": item.get("item_type"),
                            "call_id": item.get("call_id"),
                            "change": item.get("change"),
                        }
                        for item in changed_items
                    ],
                    "selector": sanitize_value(safe_selector),
                    "operation": sanitize_value(safe_operation),
                    "reason": sanitize_text(reason).strip(),
                }
            )

        return self._compact_batch_mutation_result(
            summary=summary if not dry_run else f"Dry run: {summary}",
            change_type=change_type,
            matched_count=len(matches),
            changed_items=changed_items,
            changed_nodes=changed_node_numbers,
            dry_run=dry_run,
            selector=safe_selector,
            operation=safe_operation,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
        )

    def _build_mutation_result(
        self,
        *,
        summary: str,
        change_type: str,
        changed_nodes: list[int],
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        changed_node_details = self.mutation_node_details(
            self._nodes_by_number(changed_nodes, include_inactive=True)
        )
        payload: dict[str, object] = {
            "payload_kind": "mutation_result",
            "summary": summary,
            "change_type": normalize_change_type(change_type),
            "changed_nodes": unique_int_list(changed_nodes),
            "working_overview": self.current_overview_items(),
            "changed_node_details": changed_node_details,
        }
        if extra:
            payload.update(sanitize_value(extra))
        return payload

    def delete_nodes(self, nodes: list[ContextWorkbenchDraftNode], *, reason: str) -> dict[str, object]:
        active_nodes = [node for node in nodes if node.active]
        if not active_nodes:
            raise ValueError("No active nodes were resolved for deletion.")

        deleted_numbers = [
            node.source_node_number
            for node in active_nodes
            if node.source_node_number is not None
        ]
        for node in active_nodes:
            node.active = False
            node.status = "deleted"

        summary = f"Delete nodes #{format_node_ranges(deleted_numbers)}"
        self._record_operation(
            {
                "operation_type": "delete_nodes",
                "change_type": "delete",
                "label": summary,
                "summary": summary,
                "changed_nodes": deleted_numbers,
                "target_node_numbers": deleted_numbers,
                "reason": sanitize_text(reason),
            }
        )
        return self._build_mutation_result(
            summary=summary,
            change_type="delete",
            changed_nodes=deleted_numbers,
            extra={
                "deleted_node_numbers": deleted_numbers,
            },
        )

    def compress_nodes(
        self,
        nodes: list[ContextWorkbenchDraftNode],
        *,
        summary_markdown: str,
        style: str,
        title: str,
    ) -> dict[str, object]:
        active_nodes = [node for node in nodes if node.active]
        if not active_nodes:
            raise ValueError("No active nodes were resolved for compression.")

        safe_summary = sanitize_text(summary_markdown).strip()
        if not safe_summary:
            raise ValueError("summary_markdown is required")

        target_numbers = [
            node.source_node_number
            for node in active_nodes
            if node.source_node_number is not None
        ]
        for node in active_nodes:
            node.active = False
            node.status = "compressed"

        label = self._next_draft_label()
        heading = sanitize_text(title).strip()
        summary_text = safe_summary if not heading else f"### {heading}\n\n{safe_summary}"
        self.nodes.append(
            ContextWorkbenchDraftNode(
                order=min(node.order for node in active_nodes) + 0.01,
                label=label,
                record={
                    "role": "user",
                    "text": summary_text,
                    "attachments": [],
                    "toolEvents": [],
                    "blocks": [{"kind": "text", "text": summary_text}],
                    "providerItems": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": summary_text,
                        }
                    ],
                },
                active=True,
                source_node_number=None,
                kind="draft",
                status="created",
            )
        )

        summary = f"Compress nodes #{format_node_ranges(target_numbers)}"
        self._record_operation(
            {
                "operation_type": "compress_nodes",
                "change_type": "compress",
                "label": summary,
                "summary": summary,
                "changed_nodes": target_numbers,
                "target_node_numbers": target_numbers,
                "style": sanitize_text(style).strip(),
                "created_label": label,
            }
        )
        return self._build_mutation_result(
            summary=summary,
            change_type="compress",
            changed_nodes=target_numbers,
            extra={
                "compressed_node_numbers": target_numbers,
                "created_label": label,
            },
        )

    def delete_items(self, node: ContextWorkbenchDraftNode, *, item_numbers: list[int], reason: str) -> dict[str, object]:
        provider_items = self._provider_items_for_node(node)
        if not item_numbers:
            raise ValueError("at least one item_number is required")

        resolved_items = []
        removed_indexes: list[int] = []
        for item_number in sorted(set(item_numbers)):
            resolved_items.append(self._resolve_item_detail(node, item_number))
            removed_indexes.extend(paired_tool_item_indexes(provider_items, item_number - 1))
        removed_indexes = sorted(set(removed_indexes))
        for remove_index in sorted(removed_indexes, reverse=True):
            del provider_items[remove_index]
        validate_context_provider_items(provider_items)
        self._set_node_record(node, compile_record_from_provider_items(node.record, provider_items))

        changed_nodes = [node.source_node_number] if node.source_node_number is not None else []
        paired_suffix = " pair" if len(removed_indexes) > 1 else ""
        requested_label = format_node_ranges(sorted(set(item_numbers)))
        summary = f"Delete {node.label} item #{requested_label}{paired_suffix}"
        self._record_operation(
            {
                "operation_type": "delete_items",
                "change_type": "delete",
                "label": summary,
                "summary": summary,
                "changed_nodes": changed_nodes,
                "target_node_numbers": changed_nodes,
                "target_items": [
                    {
                        "node_number": node.source_node_number,
                        "item_number": sanitize_value(item.get("item_number")),
                        "item_type": sanitize_text(item.get("item_type") or ""),
                        "paired_item_numbers": [index + 1 for index in removed_indexes],
                    }
                    for item in resolved_items
                ],
                "reason": sanitize_text(reason).strip(),
            }
        )
        return self._build_mutation_result(
            summary=summary,
            change_type="delete",
            changed_nodes=changed_nodes,
            extra={
                "deleted_items": [
                    {
                        "node_number": node.source_node_number,
                        "item_number": sanitize_value(item.get("item_number")),
                        "paired_item_numbers": [index + 1 for index in removed_indexes],
                        "item": item,
                    }
                    for item in resolved_items
                ],
            },
        )

    def delete_item(self, node: ContextWorkbenchDraftNode, *, item_number: int, reason: str) -> dict[str, object]:
        return self.delete_items(node, item_numbers=[item_number], reason=reason)

    def replace_item(
        self,
        node: ContextWorkbenchDraftNode,
        *,
        item_number: int,
        replacement_item: dict[str, Any],
        reason: str,
        change_type: str = "replace",
    ) -> dict[str, object]:
        provider_items = self._provider_items_for_node(node)
        original_item = self._resolve_item_detail(node, item_number)
        original_provider_item = provider_items[item_number - 1]
        normalized_replacement = normalize_provider_items([replacement_item])
        if len(normalized_replacement) != 1:
            raise ValueError("replacement_item must normalize into exactly one provider item")
        validate_context_replacement_identity(original_provider_item, normalized_replacement[0])
        provider_items[item_number - 1] = normalized_replacement[0]
        validate_context_provider_items(provider_items)
        self._set_node_record(node, compile_record_from_provider_items(node.record, provider_items))

        changed_nodes = [node.source_node_number] if node.source_node_number is not None else []
        summary_prefix = "Compress" if normalize_change_type(change_type) == "compress" else "Replace"
        summary = f"{summary_prefix} {node.label} item #{item_number}"
        self._record_operation(
            {
                "operation_type": "compress_item"
                if normalize_change_type(change_type) == "compress"
                else "replace_item",
                "change_type": normalize_change_type(change_type),
                "label": summary,
                "summary": summary,
                "changed_nodes": changed_nodes,
                "target_node_numbers": changed_nodes,
                "target_items": [
                    {
                        "node_number": node.source_node_number,
                        "item_number": item_number,
                        "item_type": sanitize_text(original_item.get("item_type") or ""),
                    }
                ],
                "replacement_item": sanitize_value(normalized_replacement[0]),
                "reason": sanitize_text(reason).strip(),
            }
        )
        return self._build_mutation_result(
            summary=summary,
            change_type=change_type,
            changed_nodes=changed_nodes,
            extra={
                "replaced_items": [
                    {
                        "node_number": node.source_node_number,
                        "item_number": item_number,
                        "before": original_item,
                        "after": provider_item_detail(normalized_replacement[0], item_number),
                    }
                ],
            },
        )

    def compress_item(
        self,
        node: ContextWorkbenchDraftNode,
        *,
        item_number: int,
        compressed_content: str,
        style: str,
    ) -> dict[str, object]:
        provider_items = self._provider_items_for_node(node)
        if item_number < 1 or item_number > len(provider_items):
            raise ValueError(f"item #{item_number} does not exist in {node.label}")

        original_item = provider_items[item_number - 1]
        item_type = sanitize_text(original_item.get("type") or "").strip()
        safe_content = sanitize_text(compressed_content).strip()
        if not safe_content:
            raise ValueError("compressed_content is required")

        replacement_item = sanitize_value(original_item)
        if item_type == "message":
            replacement_item["content"] = replace_provider_message_text(original_item.get("content"), safe_content)
        elif item_type == "function_call":
            replacement_item["arguments"] = safe_content
        elif item_type == "custom_tool_call":
            replacement_item["input"] = safe_content
        elif item_type in {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}:
            replacement_item["output"] = safe_content
        elif item_type == "mcp_tool_call_output":
            replacement_item["output"] = {
                "content": [{"type": "text", "text": safe_content}],
                "structured_content": None,
                "is_error": False,
                "meta": None,
            }
        elif item_type == "tool_search_output":
            replacement_item["tools"] = [{"summary": safe_content}]
        else:
            raise ValueError(f"{node.label} item #{item_number} cannot be compressed")

        return self.replace_item(
            node,
            item_number=item_number,
            replacement_item=replacement_item,
            reason=sanitize_text(style).strip(),
            change_type="compress",
        )

    def committed_transcript(self) -> list[dict[str, object]]:
        return normalize_transcript([node.record for node in self.committed_nodes()])

    def revision_label(self) -> str:
        if not self.operations:
            return "Context update"
        if len(self.operations) == 1:
            return sanitize_text(self.operations[0].get("summary") or self.operations[0].get("label") or "").strip() or "Context update"
        first_label = sanitize_text(
            self.operations[0].get("summary") or self.operations[0].get("label") or ""
        ).strip() or "Context update"
        return f"{first_label} + {len(self.operations) - 1} more"


class ContextWorkbenchToolRegistry:
    def __init__(self, draft: ContextWorkbenchDraft) -> None:
        self._returned_detail_node_numbers: set[int] = set()
        self.draft = draft
        self._tools = {
            definition.name: definition
            for definition in [
                self._build_preview_selection_tool(),
                self._build_node_detail_tool(),
                self._build_find_items_tool(),
                self._build_edit_items_tool(),
                self._build_delete_item_tool(),
                self._build_replace_item_tool(),
                self._build_compress_item_tool(),
                self._build_compress_nodes_tool(),
                self._build_delete_nodes_tool(),
                self._build_set_revision_summary_tool(),
            ]
        }

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_schema() for tool in self._tools.values()]

    @classmethod
    def tool_catalog(cls) -> list[dict[str, str]]:
        return [
            {
                "id": "preview_context_selection",
                "label": "Preview Selection",
                "description": "Inspect the current overview for specific nodes or the whole snapshot.",
                "status": "available",
            },
            {
                "id": "get_context_node_details",
                "label": "Node Details",
                "description": "Expand one or more nodes into full blocks and provider items before editing them.",
                "status": "available",
            },
            {
                "id": "find_context_items",
                "label": "Find Items",
                "description": "Search provider items using lightweight metadata and previews, without returning full node content.",
                "status": "available",
            },
            {
                "id": "edit_context_items",
                "label": "Edit Items",
                "description": "Batch delete, replace, or compress provider item content selected by node/item/type filters.",
                "status": "available",
            },
            {
                "id": "delete_context_item",
                "label": "Delete Item",
                "description": "Delete one or more items inside a single node; tool pairs are removed atomically.",
                "status": "available",
            },
            {
                "id": "replace_context_item",
                "label": "Replace Item",
                "description": "Replace one item inside a single node with a new provider item.",
                "status": "available",
            },
            {
                "id": "compress_context_item",
                "label": "Compress Item",
                "description": "Replace one item with a shorter version while keeping the same item type.",
                "status": "available",
            },
            {
                "id": "compress_context_nodes",
                "label": "Compress Nodes",
                "description": "Replace one or more nodes with a new summary node inside the current working snapshot.",
                "status": "available",
            },
            {
                "id": "delete_context_nodes",
                "label": "Delete Nodes",
                "description": "Delete one or more nodes from the current working snapshot.",
                "status": "available",
            },
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolExecution:
        tool = self._tools.get(name)
        if tool is None:
            return ToolExecution(
                output_text=json.dumps({"error": f"unknown workbench tool: {name}"}, ensure_ascii=False),
                display_title=name,
                display_detail="unknown context workbench tool",
                display_result="The requested context workbench tool does not exist.",
                status="error",
            )

        try:
            return tool.handler(arguments)
        except Exception as exc:  # noqa: BLE001
            return ToolExecution(
                output_text=json.dumps({"error": str(exc), "tool": name}, ensure_ascii=False),
                display_title=tool.label,
                display_detail="context workbench tool failed",
                display_result=sanitize_text(str(exc) or "The context workbench tool failed."),
                status="error",
            )

    def _target_resolution_execution(
        self,
        *,
        action_name: str,
        message: str,
        target_hint: str = "",
        candidates: list[dict[str, object]] | None = None,
        requires_single_node: bool = False,
        should_expand_details: bool = False,
    ) -> ToolExecution:
        payload = {
            "payload_kind": "target_resolution",
            "resolved": False,
            "action": action_name,
            "message": message,
            "target_hint": sanitize_text(target_hint).strip(),
            "requires_single_node": requires_single_node,
            "should_expand_details": should_expand_details,
            "candidates": sanitize_value(candidates or self.draft.suggest_target_nodes(target_hint)),
        }
        return ToolExecution(
            output_text=json.dumps(payload, ensure_ascii=False),
            display_title="Target Resolution",
            display_detail=action_name,
            display_result=message,
            status="needs_input",
        )

    def _item_resolution_execution(
        self,
        *,
        node: ContextWorkbenchDraftNode,
        item_number: int,
        message: str,
    ) -> ToolExecution:
        payload = {
            "payload_kind": "item_resolution",
            "resolved": False,
            "message": message,
            "requested_item_number": item_number,
            "node_detail": self.draft.node_details([node])[0],
        }
        return ToolExecution(
            output_text=json.dumps(payload, ensure_ascii=False),
            display_title="Item Resolution",
            display_detail=node.label,
            display_result=message,
            status="needs_input",
        )

    def _build_preview_selection_tool(self) -> ContextWorkbenchToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            nodes = self.draft.resolve_target_nodes(arguments, allow_all_active=True)
            items = self.draft.overview_items(nodes)
            preview_lines = [
                f"- {sanitize_text(item.get('label') or '').strip() or 'Node'} | "
                f"{sanitize_text(item.get('role') or '').strip() or 'unknown'} | "
                f"{format_token_count(int(item.get('token_estimate') or 0))} tokens | "
                f"tool {format_token_count(int(item.get('tool_token_estimate') or 0))} tokens | "
                f"{format_tool_usage(sanitize_value(item.get('tool_usage')))} | "
                f"{sanitize_text(item.get('preview') or '').strip() or '[empty]'}"
                for item in items
            ]
            return ToolExecution(
                output_text=json.dumps(
                    {
                        "payload_kind": "node_overview_list",
                        "selected_node_numbers": list(self.draft.selected_node_numbers),
                        "items": items,
                    },
                    ensure_ascii=False,
                ),
                display_title="Preview Selection",
                display_detail="Inspect the current snapshot overview",
                display_result="\n".join(preview_lines) or "No active nodes are available.",
            )

        return ContextWorkbenchToolDefinition(
            name="preview_context_selection",
            label="Preview Selection",
            description="Inspect the current overview for specific nodes or the whole snapshot.",
            parameters={
                "type": "object",
                "properties": {
                    "node_numbers": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional 1-based Node # values from the current snapshot.",
                    },
                    "target_hint": {
                        "type": "string",
                        "description": "Optional natural-language target hint when no Node # is specified.",
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
            status="available",
            handler=handler,
        )

    def _mark_detail_nodes_returned(self, nodes: list[ContextWorkbenchDraftNode]) -> None:
        for node in nodes:
            if node.source_node_number is not None:
                self._returned_detail_node_numbers.add(node.source_node_number)

    def _filter_new_detail_nodes(
        self,
        nodes: list[ContextWorkbenchDraftNode],
    ) -> tuple[list[ContextWorkbenchDraftNode], list[int]]:
        fresh_nodes: list[ContextWorkbenchDraftNode] = []
        cached_numbers: list[int] = []
        for node in nodes:
            if node.source_node_number is None:
                fresh_nodes.append(node)
                continue
            if node.source_node_number in self._returned_detail_node_numbers:
                cached_numbers.append(node.source_node_number)
                continue
            fresh_nodes.append(node)
        return fresh_nodes, cached_numbers

    def _invalidate_detail_cache(self) -> None:
        self._returned_detail_node_numbers.clear()

    def _build_node_detail_tool(self) -> ContextWorkbenchToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            nodes = self.draft.resolve_target_nodes(arguments)
            if not nodes:
                return self._target_resolution_execution(
                    action_name="get_context_node_details",
                    message="I could not resolve a target node. Mention Node #, keep a node selected, or use target_hint.",
                    target_hint=sanitize_text(arguments.get("target_hint") or ""),
                    should_expand_details=True,
                )

            fresh_nodes, cached_node_numbers = self._filter_new_detail_nodes(nodes)
            details = self.draft.node_details(fresh_nodes)
            self._mark_detail_nodes_returned(fresh_nodes)
            labels = ", ".join(
                sanitize_text(item.get("label") or "").strip()
                for item in details
                if sanitize_text(item.get("label") or "").strip()
            )
            cached_label = format_node_ranges(cached_node_numbers)
            display_result_parts: list[str] = []
            if labels:
                display_result_parts.append(f"Returned details for {labels}.")
            if cached_label:
                display_result_parts.append(
                    f"Skipped duplicate details for Node #{cached_label}; use the previous result from this turn."
                )
            return ToolExecution(
                output_text=json.dumps(
                    {
                        "payload_kind": "node_detail_list",
                        "selected_node_numbers": list(self.draft.selected_node_numbers),
                        "items": details,
                        "cached_node_numbers": cached_node_numbers,
                        "cached_message": (
                            f"Node #{cached_label} details were already returned earlier in this same workbench turn. "
                            "Use the previous function_call_output for those nodes."
                            if cached_node_numbers
                            else ""
                        ),
                    },
                    ensure_ascii=False,
                ),
                display_title="Node Details",
                display_detail=labels or "node details",
                display_result=" ".join(display_result_parts)
                or "The requested node details were already returned earlier in this turn.",
            )

        return ContextWorkbenchToolDefinition(
            name="get_context_node_details",
            label="Node Details",
            description="Expand one or more nodes into full blocks and provider items before editing them.",
            parameters={
                "type": "object",
                "properties": {
                    "node_numbers": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional 1-based Node # values from the current snapshot.",
                    },
                    "target_hint": {
                        "type": "string",
                        "description": "Optional natural-language target hint when no Node # is specified.",
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
            status="available",
            handler=handler,
        )

    def _item_selector_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "node_numbers": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Optional 1-based Node # values. If omitted, the selector searches all active editable nodes unless target_hint or selected_only is supplied.",
                },
                "item_numbers": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Optional item # values to match inside each resolved node.",
                },
                "item_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional exact item refs returned by find_context_items, e.g. node:2:item:4.",
                },
                "item_types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": sorted(CONTEXT_EDITABLE_PROVIDER_ITEM_TYPES),
                    },
                    "description": "Optional provider item types to match.",
                },
                "roles": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["system", "developer", "user", "assistant"],
                    },
                    "description": "Optional message roles to match.",
                },
                "tool_output_only": {
                    "type": "boolean",
                    "description": "When true, match only tool output provider items.",
                },
                "tool_call_only": {
                    "type": "boolean",
                    "description": "When true, match only tool call provider items.",
                },
                "text_contains": {
                    "type": "string",
                    "description": "Optional case-insensitive substring filter over the item's editable text/source.",
                },
                "min_token_estimate": {
                    "type": "integer",
                    "description": "Optional minimum estimated token count for matched items.",
                },
                "target_hint": {
                    "type": "string",
                    "description": "Optional natural-language hint if you want the workbench to resolve a likely node first.",
                },
                "selected_only": {
                    "type": "boolean",
                    "description": "When true and nodes are selected, search only selected nodes.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "For find_context_items only: maximum lightweight matches to return.",
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    def _build_find_items_tool(self) -> ContextWorkbenchToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            selector = arguments.get("selector")
            result = self.draft.find_context_items(selector if isinstance(selector, dict) else {})
            item_lines = []
            result_items = result.get("items")
            for item in result_items if isinstance(result_items, list) else []:
                if not isinstance(item, dict):
                    continue
                item_lines.append(
                    f"- {sanitize_text(item.get('item_ref') or '')} | "
                    f"{sanitize_text(item.get('item_type') or '')} | "
                    f"{format_token_count(int(item.get('token_estimate') or 0))} tokens | "
                    f"{sanitize_text(item.get('preview') or '').strip() or '[empty]'}"
                )
            if bool(result.get("truncated")):
                item_lines.append(
                    f"... truncated after {int(result.get('returned_count') or 0)} of {int(result.get('matched_count') or 0)} matches"
                )
            return ToolExecution(
                output_text=json.dumps(result, ensure_ascii=False),
                display_title="Find Items",
                display_detail=f"{int(result.get('matched_count') or 0)} match(es)",
                display_result="\n".join(item_lines) or "No matching context items found.",
            )

        return ContextWorkbenchToolDefinition(
            name="find_context_items",
            label="Find Items",
            description=(
                "Search editable provider items by node, item ref, type, role, tool-call/tool-output kind, or text substring. "
                "Returns lightweight previews and metadata only; use this before get_context_node_details for bulk edits."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "selector": self._item_selector_schema(),
                },
                "required": [],
                "additionalProperties": False,
            },
            status="available",
            handler=handler,
        )

    def _build_edit_items_tool(self) -> ContextWorkbenchToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            selector = arguments.get("selector")
            operation = arguments.get("operation")
            result = self.draft.edit_context_items(
                selector=selector if isinstance(selector, dict) else {},
                operation=operation if isinstance(operation, dict) else {},
                reason=sanitize_text(arguments.get("reason") or "").strip(),
                dry_run=bool(arguments.get("dry_run")),
            )
            if not bool(result.get("dry_run")) and int(result.get("changed_count") or 0) > 0:
                self._invalidate_detail_cache()
            return ToolExecution(
                output_text=json.dumps(result, ensure_ascii=False),
                display_title="Edit Items",
                display_detail=result["summary"],
                display_result=result["summary"],
            )

        return ContextWorkbenchToolDefinition(
            name="edit_context_items",
            label="Edit Items",
            description=(
                "Batch edit provider items selected by find-style filters. "
                "Use replace_content or compress_content to rewrite only the editable content while preserving type/call_id; "
                "use delete to remove selected items, with tool call/output pairs removed atomically so Codex input stays valid."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "selector": self._item_selector_schema(),
                    "operation": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["replace_content", "compress_content", "delete"],
                                "description": "The batch operation to apply.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Replacement content for replace_content or compress_content. Ignored for delete.",
                            },
                        },
                        "required": ["type"],
                        "additionalProperties": False,
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview the matched edits without mutating the working snapshot.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional reason for this batch edit.",
                    },
                },
                "required": ["selector", "operation"],
                "additionalProperties": False,
            },
            status="available",
            handler=handler,
        )

    def _build_delete_item_tool(self) -> ContextWorkbenchToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            nodes = self.draft.resolve_target_nodes(arguments)
            if not nodes:
                return self._target_resolution_execution(
                    action_name="delete_context_item",
                    message="I could not resolve which node should lose this item. Mention Node #, keep one node selected, or use target_hint.",
                    target_hint=sanitize_text(arguments.get("target_hint") or ""),
                    requires_single_node=True,
                    should_expand_details=True,
                )
            if len(nodes) != 1:
                return self._target_resolution_execution(
                    action_name="delete_context_item",
                    message="delete_context_item needs exactly one target node. Narrow it to a single Node # first.",
                    target_hint=sanitize_text(arguments.get("target_hint") or ""),
                    candidates=self.draft.overview_items(nodes),
                    requires_single_node=True,
                    should_expand_details=True,
                )

            node = nodes[0]
            provider_items = self.draft._provider_items_for_node(node)
            item_numbers: list[int] = []
            raw_item_numbers = arguments.get("item_numbers")
            if isinstance(raw_item_numbers, list):
                for raw_number in raw_item_numbers:
                    try:
                        item_numbers.append(int(raw_number))
                    except (TypeError, ValueError):
                        continue
            try:
                single_item_number = int(arguments.get("item_number") or 0)
            except (TypeError, ValueError):
                single_item_number = 0
            if single_item_number:
                item_numbers.append(single_item_number)
            item_numbers = [
                item_number
                for item_number in sorted(set(item_numbers))
                if 1 <= item_number <= len(provider_items)
            ]
            if not item_numbers:
                return self._item_resolution_execution(
                    node=node,
                    item_number=single_item_number,
                    message="delete_context_item needs item_number or item_numbers from the current node details.",
                )
            try:
                for item_number in item_numbers:
                    self.draft._resolve_item_detail(node, item_number)
            except ValueError as exc:
                return self._item_resolution_execution(node=node, item_number=item_numbers[0], message=str(exc))

            result = self.draft.delete_items(
                node,
                item_numbers=item_numbers,
                reason=sanitize_text(arguments.get("reason") or "").strip(),
            )
            self._invalidate_detail_cache()
            return ToolExecution(
                output_text=json.dumps(result, ensure_ascii=False),
                display_title="Delete Item",
                display_detail=result["summary"],
                display_result=result["summary"],
            )

        return ContextWorkbenchToolDefinition(
            name="delete_context_item",
            label="Delete Item",
            description="Delete one or more items inside a single node from the current working snapshot. Tool call/output pairs are removed together.",
            parameters={
                "type": "object",
                "properties": {
                    "node_numbers": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional single 1-based Node # value from the current snapshot.",
                    },
                    "target_hint": {
                        "type": "string",
                        "description": "Optional natural-language target hint when no Node # is specified.",
                    },
                    "item_number": {
                        "type": "integer",
                        "description": "Optional single item # inside the resolved node.",
                    },
                    "item_numbers": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional multiple item # values from the same node. Prefer this when deleting many tool call/output items to avoid item-number drift.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional reason for deleting this item.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            status="available",
            handler=handler,
        )

    def _replacement_item_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": sorted(CONTEXT_EDITABLE_PROVIDER_ITEM_TYPES),
                },
                "role": {
                    "type": "string",
                    "enum": ["system", "developer", "user", "assistant"],
                },
                "content": {
                    "anyOf": [
                        {"type": "string"},
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                    ],
                },
                "call_id": {
                    "type": "string",
                },
                "name": {
                    "type": "string",
                },
                "arguments": {
                    "type": "string",
                },
                "input": {
                    "type": "string",
                },
                "action": {
                    "type": "object",
                    "additionalProperties": True,
                },
                "status": {
                    "type": "string",
                },
                "execution": {
                    "type": "string",
                },
                "tools": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
                "output": {
                    "anyOf": [
                        {"type": "string"},
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                        {
                            "type": "object",
                            "additionalProperties": True,
                        },
                    ],
                },
            },
            "required": ["type"],
            "additionalProperties": True,
        }

    def _build_replace_item_tool(self) -> ContextWorkbenchToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            item_number = int(arguments.get("item_number") or 0)
            nodes = self.draft.resolve_target_nodes(arguments)
            if not nodes:
                return self._target_resolution_execution(
                    action_name="replace_context_item",
                    message="I could not resolve which node should receive the replacement item. Mention Node #, keep one node selected, or use target_hint.",
                    target_hint=sanitize_text(arguments.get("target_hint") or ""),
                    requires_single_node=True,
                    should_expand_details=True,
                )
            if len(nodes) != 1:
                return self._target_resolution_execution(
                    action_name="replace_context_item",
                    message="replace_context_item needs exactly one target node. Narrow it to a single Node # first.",
                    target_hint=sanitize_text(arguments.get("target_hint") or ""),
                    candidates=self.draft.overview_items(nodes),
                    requires_single_node=True,
                    should_expand_details=True,
                )

            node = nodes[0]
            try:
                self.draft._resolve_item_detail(node, item_number)
            except ValueError as exc:
                return self._item_resolution_execution(node=node, item_number=item_number, message=str(exc))

            replacement_item = arguments.get("replacement_item")
            if not isinstance(replacement_item, dict):
                return self._item_resolution_execution(
                    node=node,
                    item_number=item_number,
                    message="replacement_item must be an object that matches one editable provider item.",
                )

            result = self.draft.replace_item(
                node,
                item_number=item_number,
                replacement_item=replacement_item,
                reason=sanitize_text(arguments.get("reason") or "").strip(),
            )
            self._invalidate_detail_cache()
            return ToolExecution(
                output_text=json.dumps(result, ensure_ascii=False),
                display_title="Replace Item",
                display_detail=result["summary"],
                display_result=result["summary"],
            )

        return ContextWorkbenchToolDefinition(
            name="replace_context_item",
            label="Replace Item",
            description="Replace one item inside a single node with a new provider item.",
            parameters={
                "type": "object",
                "properties": {
                    "node_numbers": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional single 1-based Node # value from the current snapshot.",
                    },
                    "target_hint": {
                        "type": "string",
                        "description": "Optional natural-language target hint when no Node # is specified.",
                    },
                    "item_number": {
                        "type": "integer",
                        "description": "Required item # inside the resolved node.",
                    },
                    "replacement_item": self._replacement_item_schema(),
                    "reason": {
                        "type": "string",
                        "description": "Optional reason for replacing this item.",
                    },
                },
                "required": ["item_number", "replacement_item"],
                "additionalProperties": False,
            },
            status="available",
            handler=handler,
        )

    def _build_compress_item_tool(self) -> ContextWorkbenchToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            item_number = int(arguments.get("item_number") or 0)
            nodes = self.draft.resolve_target_nodes(arguments)
            if not nodes:
                return self._target_resolution_execution(
                    action_name="compress_context_item",
                    message="I could not resolve which node contains the item to compress. Mention Node #, keep one node selected, or use target_hint.",
                    target_hint=sanitize_text(arguments.get("target_hint") or ""),
                    requires_single_node=True,
                    should_expand_details=True,
                )
            if len(nodes) != 1:
                return self._target_resolution_execution(
                    action_name="compress_context_item",
                    message="compress_context_item needs exactly one target node. Narrow it to a single Node # first.",
                    target_hint=sanitize_text(arguments.get("target_hint") or ""),
                    candidates=self.draft.overview_items(nodes),
                    requires_single_node=True,
                    should_expand_details=True,
                )

            node = nodes[0]
            try:
                self.draft._resolve_item_detail(node, item_number)
            except ValueError as exc:
                return self._item_resolution_execution(node=node, item_number=item_number, message=str(exc))

            result = self.draft.compress_item(
                node,
                item_number=item_number,
                compressed_content=sanitize_text(arguments.get("compressed_content") or ""),
                style=sanitize_text(arguments.get("style") or "").strip(),
            )
            self._invalidate_detail_cache()
            return ToolExecution(
                output_text=json.dumps(result, ensure_ascii=False),
                display_title="Compress Item",
                display_detail=result["summary"],
                display_result=result["summary"],
            )

        return ContextWorkbenchToolDefinition(
            name="compress_context_item",
            label="Compress Item",
            description="Replace one item with a shorter version while keeping the same item type.",
            parameters={
                "type": "object",
                "properties": {
                    "node_numbers": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional single 1-based Node # value from the current snapshot.",
                    },
                    "target_hint": {
                        "type": "string",
                        "description": "Optional natural-language target hint when no Node # is specified.",
                    },
                    "item_number": {
                        "type": "integer",
                        "description": "Required item # inside the resolved node.",
                    },
                    "compressed_content": {
                        "type": "string",
                        "description": "The shorter replacement content for this item.",
                    },
                    "style": {
                        "type": "string",
                        "description": "Optional note about the compression style.",
                    },
                },
                "required": ["item_number", "compressed_content"],
                "additionalProperties": False,
            },
            status="available",
            handler=handler,
        )

    def _build_compress_nodes_tool(self) -> ContextWorkbenchToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            nodes = self.draft.resolve_target_nodes(arguments)
            if not nodes:
                return self._target_resolution_execution(
                    action_name="compress_context_nodes",
                    message="I could not resolve which nodes should be compressed. Mention Node #, keep nodes selected, or use target_hint.",
                    target_hint=sanitize_text(arguments.get("target_hint") or ""),
                )

            result = self.draft.compress_nodes(
                nodes,
                summary_markdown=sanitize_text(arguments.get("summary_markdown") or ""),
                style=sanitize_text(arguments.get("style") or "").strip() or "tight summary",
                title=sanitize_text(arguments.get("title") or "").strip(),
            )
            self._invalidate_detail_cache()
            return ToolExecution(
                output_text=json.dumps(result, ensure_ascii=False),
                display_title="Compress Nodes",
                display_detail=result["summary"],
                display_result=result["summary"],
            )

        return ContextWorkbenchToolDefinition(
            name="compress_context_nodes",
            label="Compress Nodes",
            description="Replace one or more nodes with a new summary node inside the current working snapshot.",
            parameters={
                "type": "object",
                "properties": {
                    "node_numbers": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional 1-based Node # values from the current snapshot.",
                    },
                    "target_hint": {
                        "type": "string",
                        "description": "Optional natural-language target hint when no Node # is specified.",
                    },
                    "summary_markdown": {
                        "type": "string",
                        "description": "Markdown content that should become the new summary node.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional heading for the created summary node.",
                    },
                    "style": {
                        "type": "string",
                        "description": "Short note about the compression style.",
                    },
                },
                "required": ["summary_markdown"],
                "additionalProperties": False,
            },
            status="available",
            handler=handler,
        )

    def _build_delete_nodes_tool(self) -> ContextWorkbenchToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            nodes = self.draft.resolve_target_nodes(arguments)
            if not nodes:
                return self._target_resolution_execution(
                    action_name="delete_context_nodes",
                    message="I could not resolve which nodes should be deleted. Mention Node #, keep nodes selected, or use target_hint.",
                    target_hint=sanitize_text(arguments.get("target_hint") or ""),
                )

            result = self.draft.delete_nodes(
                nodes,
                reason=sanitize_text(arguments.get("reason") or "").strip(),
            )
            self._invalidate_detail_cache()
            return ToolExecution(
                output_text=json.dumps(result, ensure_ascii=False),
                display_title="Delete Nodes",
                display_detail=result["summary"],
                display_result=result["summary"],
            )

        return ContextWorkbenchToolDefinition(
            name="delete_context_nodes",
            label="Delete Nodes",
            description="Delete one or more nodes from the current working snapshot.",
            parameters={
                "type": "object",
                "properties": {
                    "node_numbers": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional 1-based Node # values from the current snapshot.",
                    },
                    "target_hint": {
                        "type": "string",
                        "description": "Optional natural-language target hint when no Node # is specified.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional reason for deleting these nodes.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            status="available",
            handler=handler,
        )

    def _build_set_revision_summary_tool(self) -> ContextWorkbenchToolDefinition:
        def handler(arguments: dict[str, Any]) -> ToolExecution:
            try:
                result = self.draft.set_revision_summary(
                    sanitize_text(arguments.get("summary") or ""),
                )
            except ValueError as exc:
                return ToolExecution(
                    output_text=json.dumps(
                        {
                            "payload_kind": "revision_summary",
                            "saved": False,
                            "message": str(exc),
                        },
                        ensure_ascii=False,
                    ),
                    display_title="Revision Summary",
                    display_detail="summary not saved",
                    display_result=str(exc),
                    status="needs_input",
                )

            return ToolExecution(
                output_text=json.dumps(result, ensure_ascii=False),
                display_title="Revision Summary",
                display_detail="saved",
                display_result=result["summary"],
            )

        return ContextWorkbenchToolDefinition(
            name="set_context_revision_summary",
            label="Revision Summary",
            description="After finishing working-snapshot edits, save one short summary (matching user language) that explains what this commit changed. Describe the content changed (e.g. 'compressed tool outputs'), not the node numbers. This text will be shown in the restore history.",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "One short summary (matching user language) of the content that changed in the context snapshot.",
                    },
                },
                "required": ["summary"],
                "additionalProperties": False,
            },
            status="available",
            handler=handler,
        )


def normalize_context_chat_history(raw_history: Any) -> list[dict[str, str]]:
    if not isinstance(raw_history, list):
        return []

    history: list[dict[str, str]] = []
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        role = sanitize_text(item.get("role") or "").strip()
        if role not in {"user", "assistant"}:
            continue
        content = sanitize_text(item.get("content") or "").strip()
        if not content:
            continue
        history.append(
            {
                "role": role,
                "content": content,
            }
        )
    return history


def prepare_context_chat_history_for_model(raw_history: Any, *, limit: int = 12) -> list[dict[str, str]]:
    history = normalize_context_chat_history(raw_history)
    filtered: list[dict[str, str]] = []

    for item in history:
        if item["role"] == "assistant":
            content = sanitize_text(item["content"])
            if "我已经读完当前上下文了，但这次没能稳定产出文字答复" in content:
                continue
        filtered.append(item)

    if limit > 0:
        return filtered[-limit:]
    return filtered


def extract_response_output_text(response: Any) -> str:
    direct_text = sanitize_text(getattr(response, "output_text", "") or "").strip()
    if direct_text:
        return direct_text

    text_parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        if sanitize_text(getattr(item, "type", "")).strip() != "message":
            continue

        for content_item in getattr(item, "content", None) or []:
            if sanitize_text(getattr(content_item, "type", "")).strip() != "output_text":
                continue
            text_parts.append(sanitize_text(getattr(content_item, "text", "") or ""))

    return sanitize_text("".join(text_parts)).strip()


def response_output_to_turn_items(response: Any) -> tuple[list[dict[str, Any]], list[Any]]:
    turn_items: list[dict[str, Any]] = []
    function_calls: list[Any] = []

    for item in getattr(response, "output", []) or []:
        item_type = sanitize_text(getattr(item, "type", "")).strip()
        if item_type == "message":
            role = sanitize_text(getattr(item, "role", "")).strip() or "assistant"
            text_parts: list[str] = []
            for content_item in getattr(item, "content", None) or []:
                if sanitize_text(getattr(content_item, "type", "")).strip() != "output_text":
                    continue
                text_parts.append(sanitize_text(getattr(content_item, "text", "") or ""))

            message_text = "".join(text_parts)
            if message_text.strip():
                turn_items.append(SimpleAgent._message(role, message_text))
            continue

        if item_type == "function_call":
            function_calls.append(item)
            turn_items.append(
                {
                    "type": "function_call",
                    "call_id": sanitize_text(getattr(item, "call_id", "") or ""),
                    "name": sanitize_text(getattr(item, "name", "") or ""),
                    "arguments": sanitize_text(getattr(item, "arguments", "") or "{}") or "{}",
                }
            )

    return normalize_provider_items(turn_items), function_calls


def build_context_chat_runtime(
    session: SessionState,
    *,
    message: str,
    selected_indexes: list[int] | None = None,
) -> tuple[str, str, ContextWorkbenchDraft, ContextWorkbenchToolRegistry, list[dict[str, Any]]]:
    safe_selected_indexes = normalize_selected_node_indexes(selected_indexes or [], len(session.transcript))
    draft = ContextWorkbenchDraft(normalize_transcript(session.transcript), safe_selected_indexes)
    snapshot = build_context_workspace_snapshot(session, selected_indexes=safe_selected_indexes)
    tool_registry = ContextWorkbenchToolRegistry(draft)
    history = prepare_context_chat_history_for_model(session.context_workbench_history)

    context_input: list[dict[str, Any]] = []

    context_input.append(
        SimpleAgent._message(
            "developer",
            "\n\n".join(
                [
                    "这里是主 Codex 对话的当前上下文快照。本轮回答和编辑都以这份快照为准；后面的右侧手动页历史可能提到旧节点或旧内容。",
                    snapshot,
                ]
            ),
        )
    )

    for item in history:
        context_input.append(
            SimpleAgent._message(
                item["role"],
                item["content"],
            )
        )

    context_input.append(
        SimpleAgent._message(
            "user",
            "\n\n".join(
                [
                    "CURRENT WORKBENCH USER MESSAGE:",
                    sanitize_text(message),
                ]
            ),
        )
    )

    request_model = sanitize_text(
        session.agent.settings.context_workbench_model or session.agent.settings.model
    ).strip() or "gpt-5.4-mini"
    instructions = "\n".join(
        [
            "你在右侧手动页里工作，这里是一个独立聊天窗口。",
            "默认先像正常聊天助手一样回应用户当前这句话，不要先背职责，不要先讲工具。",
            "你只处理当前上下文，不继续用户的主聊天任务。",
            "如果用户只是打招呼、测试你能不能正常聊天、或者问这里怎么用，直接正常回答，不要调用工具。",
            "只有在定位、核实、修改上下文时，才需要调用工具。",
            "主 Codex 上下文快照位于 input[0] 的 developer 消息里；这一轮里所有 Node # 都只以 input[0] 的当前快照为准。",
            "右侧手动页历史只是你和用户的连续对话记录，里面提到的节点数、Node #、内容摘要可能已经过期；回答当前上下文、定位节点、执行编辑时必须以本轮最新快照为准。",
            "分析类问题如果能靠全局概览直接回答，就先直接回答。",
            "用户问“你现在看到什么上下文 / 是摘要还是原文 / 有哪些节点”时，只根据当前快照的可见层回答，不要为了这种问题调用 get_context_node_details。",
            "user 节点直接给全文；assistant 节点默认只给上下文地图折叠态同款首句预览，预览后面的内容你并不可见；需要协议层细节或完整内容时，再调用 get_context_node_details。",
            "Node Detail 里会给出 item #1 / item #2 / item #3 这样的当轮可编辑 item 视图。",
            "如果你要定位某类 item（例如所有 tool output、某类 tool call、某个文本片段），优先调用 find_context_items；它只返回预览和元数据，不会把大段内容塞进你的上下文。",
            "如果你要批量删除、改写、压缩 assistant text / tool call / tool output，优先调用 edit_context_items，用 selector 一次选中目标，再用 operation 一次完成；不要逐个 item 反复调用单项工具。",
            "edit_context_items 的 replace_content / compress_content 会保留原 item 的 type 和 call_id，只改可编辑内容；delete 会原子删除工具调用/输出配对，保证 Codex 输入仍然合法。",
            "delete_context_item / replace_context_item / compress_context_item 只用于少数精细单点修改，或者当用户明确要求按完整 provider item 改写时使用。",
            "选中只是强提示，不是门槛。显式 node_numbers 优先，其次是当前选中；如果都没有，可以用 target_hint 让系统帮你定位候选节点。",
            "当你调用 mutation tool 时，你是在改 working snapshot，UI 会在这一轮结束后统一提交。",
            "简单删除、替换、压缩完成后，不要为了确认结果再次展开节点详情；直接依据工具返回和 working_overview 继续或收尾。只有下一步编辑确实需要修改后的完整 provider_items 时，才再次调用 get_context_node_details。",
            "如果这一轮做过任何编辑，在所有 mutation 都完成后，再调用一次 set_context_revision_summary，用 1 到 2 句话概括这次具体改了什么；这句会显示在恢复页。注意：总结必须说明修改了【什么具体的上下文内容】（例如“压缩了所有工具输出”或“压缩了关于计划讨论的部分”），绝对不要简单说“修改了节点”等废话。",
            "如果工具返回了 working_overview，就把它当成这一轮最新的上下文状态。",
            "如果工具返回 target_resolution 或 item_resolution，不要硬猜；先根据候选或详情重新定位，再继续。",
            "这一轮结束前，你必须给用户一个明确的答复（语言与用户沟通语言一致），不能只停在工具调用上。",
            "回答保持简洁、具体，说人话，可以使用 Markdown。",
        ]
    )
    return instructions, request_model, draft, tool_registry, context_input


def resolve_context_workbench_provider_id(settings: Settings, model_id: str) -> str:
    requested_provider_id = sanitize_text(
        settings.context_workbench_provider_id or settings.active_provider_id
    ).strip()
    enabled_providers = [
        provider
        for provider in settings.response_providers
        if bool(provider.get("enabled"))
    ]
    enabled_provider_ids = {
        sanitize_text(provider.get("id") or "").strip()
        for provider in enabled_providers
        if sanitize_text(provider.get("id") or "").strip()
    }
    if CODEX_PROXY_PROVIDER_ID in enabled_provider_ids:
        return CODEX_PROXY_PROVIDER_ID

    cleaned_model_id = sanitize_text(model_id).strip()
    if cleaned_model_id:
        if requested_provider_id and requested_provider_id in enabled_provider_ids:
            requested_provider = next(
                (
                    provider
                    for provider in enabled_providers
                    if sanitize_text(provider.get("id") or "").strip() == requested_provider_id
                ),
                None,
            )
            requested_provider_model_ids = {
                sanitize_text(model.get("id") or "").strip()
                for model in (requested_provider or {}).get("models") or []
                if sanitize_text(model.get("id") or "").strip()
            }
            if cleaned_model_id in requested_provider_model_ids:
                return requested_provider_id

        for provider in enabled_providers:
            provider_id = sanitize_text(provider.get("id") or "").strip()
            if not provider_id:
                continue
            provider_model_ids = {
                sanitize_text(model.get("id") or "").strip()
                for model in provider.get("models") or []
                if sanitize_text(model.get("id") or "").strip()
            }
            if cleaned_model_id in provider_model_ids:
                return provider_id

    if requested_provider_id and requested_provider_id in enabled_provider_ids:
        return requested_provider_id

    if CODEX_PROXY_PROVIDER_ID in enabled_provider_ids:
        return CODEX_PROXY_PROVIDER_ID

    active_provider_id = sanitize_text(settings.active_provider_id or "").strip()
    if active_provider_id in enabled_provider_ids:
        return active_provider_id

    return next(iter(enabled_provider_ids), active_provider_id or "openai")


def context_workbench_provider(settings: Settings, provider_id: str) -> dict[str, Any]:
    cleaned_provider_id = sanitize_text(provider_id).strip()
    return next(
        (
            item
            for item in settings.response_providers
            if sanitize_text(item.get("id") or "").strip() == cleaned_provider_id
        ),
        settings.active_provider(),
    )


def model_supports_minimal_reasoning(model_id: str) -> bool:
    cleaned_model_id = sanitize_text(model_id).strip().lower()
    return cleaned_model_id.startswith("gpt-5") or cleaned_model_id.startswith("gpt-oss")


def resolve_context_reasoning_effort(
    settings: Settings,
    *,
    provider_id: str,
    model_id: str,
    requested_effort: str | None,
) -> str | None:
    cleaned_effort = sanitize_text(requested_effort or "").strip()
    if cleaned_effort == "default":
        cleaned_effort = sanitize_text(settings.default_reasoning_effort).strip()

    if cleaned_effort in {"", "default"}:
        return None

    if cleaned_effort == "none":
        provider = context_workbench_provider(settings, provider_id)
        provider_type = sanitize_text(provider.get("provider_type") or "").strip()
        if (
            provider_type in {"responses", "chat_completion"}
            and model_supports_minimal_reasoning(model_id)
        ):
            return "minimal"
        return None

    if cleaned_effort in {"minimal", "low", "medium", "high", "xhigh"}:
        return cleaned_effort

    return None


def build_context_workbench_agent(settings: Settings, provider_id: str) -> SimpleAgent:
    resolved_provider_id = sanitize_text(provider_id).strip() or sanitize_text(settings.active_provider_id).strip() or "openai"
    provider = context_workbench_provider(settings, resolved_provider_id)
    if resolved_provider_id == CODEX_PROXY_PROVIDER_ID:
        provider_api_key = "not-needed"
        provider_base_url = CODEX_PROXY_BASE_URL
    else:
        provider_api_key = sanitize_text(provider.get("api_key") or "").strip() or settings.openai_api_key
        provider_base_url = sanitize_text(provider.get("api_base_url") or "").strip() or settings.openai_base_url
    scoped_settings = Settings(
        model=settings.model,
        default_reasoning_effort=settings.default_reasoning_effort,
        context_workbench_model=settings.context_workbench_model,
        context_workbench_provider_id=resolved_provider_id,
        project_root=settings.project_root,
        max_tool_rounds=settings.max_tool_rounds,
        tool_settings=settings.tool_settings,
        response_providers=settings.response_providers,
        active_provider_id=resolved_provider_id,
        context_token_warning_threshold=settings.context_token_warning_threshold,
        context_token_critical_threshold=settings.context_token_critical_threshold,
        openai_api_key=provider_api_key,
        openai_base_url=provider_base_url,
        assistant_name="",
        assistant_greeting="",
        assistant_prompt="",
        user_name="",
        user_locale="",
        user_timezone="",
        user_profile="",
    )
    return SimpleAgent(scoped_settings, include_default_instructions=False)


def extract_context_proxy_message_text(item: dict[str, Any]) -> str:
    if sanitize_text(item.get("type") or "").strip() != "message":
        return ""
    return extract_text_from_provider_message_content(item.get("content"))


def append_context_proxy_function_call(
    function_calls_by_id: dict[str, BridgedFunctionCall],
    item: dict[str, Any],
) -> None:
    if sanitize_text(item.get("type") or "").strip() != "function_call":
        return

    name = sanitize_text(item.get("name") or "").strip()
    if not name:
        return

    call_id = sanitize_text(item.get("call_id") or item.get("id") or "").strip()
    if not call_id:
        call_id = uuid.uuid4().hex

    arguments = sanitize_text(item.get("arguments") or "{}") or "{}"
    function_calls_by_id[call_id] = BridgedFunctionCall(
        name=name,
        arguments=arguments,
        call_id=call_id,
    )


def parse_context_proxy_sse_event(
    raw_event: str,
    *,
    text_parts: list[str],
    function_calls_by_id: dict[str, BridgedFunctionCall],
    saw_text_delta: list[bool],
    on_text_delta: Callable[[str], None] | None = None,
) -> None:
    if raw_event == "[DONE]":
        return

    try:
        event = json.loads(raw_event)
    except json.JSONDecodeError:
        return

    if not isinstance(event, dict):
        return

    event_type = sanitize_text(event.get("type") or "").strip()
    if event_type in {"response.output_text.delta", "response.refusal.delta"}:
        delta = sanitize_text(event.get("delta") or "")
        if delta:
            saw_text_delta[0] = True
            text_parts.append(delta)
            if on_text_delta is not None:
                on_text_delta(delta)
        return

    if event_type == "response.output_text.done" and not saw_text_delta[0]:
        text = sanitize_text(event.get("text") or "")
        if text:
            text_parts.append(text)
            if on_text_delta is not None:
                on_text_delta(text)
        return

    if event_type in {"response.output_item.done", "response.output_item.added"}:
        item = event.get("item")
        if isinstance(item, dict):
            append_context_proxy_function_call(function_calls_by_id, item)
        return

    if event_type == "response.completed":
        response = event.get("response")
        output = response.get("output") if isinstance(response, dict) else None
        if not isinstance(output, list):
            return
        fallback_text_parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            append_context_proxy_function_call(function_calls_by_id, item)
            if not saw_text_delta[0]:
                item_text = extract_context_proxy_message_text(item)
                if item_text:
                    fallback_text_parts.append(item_text)
        if fallback_text_parts and not text_parts:
            text = sanitize_text("".join(fallback_text_parts))
            if text:
                text_parts.append(text)
                if on_text_delta is not None:
                    on_text_delta(text)
        return

    if event_type == "response.failed":
        response = event.get("response")
        error = response.get("error") if isinstance(response, dict) else None
        if isinstance(error, dict):
            message = sanitize_text(error.get("message") or error.get("code") or "")
            if message:
                raise RuntimeError(f"response failed: {message}")
        raise RuntimeError("response failed")

    if event_type == "error":
        message = sanitize_text(event.get("message") or event.get("error") or "")
        raise RuntimeError(f"response stream error: {message or 'unknown error'}")


def stream_context_codex_proxy_response(
    request: dict[str, Any],
    *,
    on_text_delta: Callable[[str], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> object:
    request_body = {
        key: value
        for key, value in request.items()
        if key != "extra_headers"
    }
    request_body["stream"] = True
    extra_headers = request.get("extra_headers")
    headers = {
        "Authorization": "Bearer not-needed",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }
    if isinstance(extra_headers, dict):
        for key, value in extra_headers.items():
            safe_key = sanitize_text(key).strip()
            safe_value = sanitize_text(value).strip()
            if safe_key and safe_value:
                headers[safe_key] = safe_value

    proxy_url = f"{CODEX_PROXY_BASE_URL.rstrip('/')}/responses"
    payload = json.dumps(sanitize_value(request_body), ensure_ascii=False).encode("utf-8")
    http_request = urllib_request.Request(proxy_url, data=payload, headers=headers, method="POST")

    text_parts: list[str] = []
    function_calls_by_id: dict[str, BridgedFunctionCall] = {}
    saw_text_delta = [False]
    buffer = ""

    try:
        response = urllib_request.urlopen(http_request, timeout=600)
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(detail or f"Context proxy request failed with HTTP {exc.code}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"Context proxy request failed: {exc.reason}") from exc

    with response:
        while True:
            if check_cancelled is not None:
                check_cancelled()

            chunk = response.read(4096)
            if not chunk:
                break

            buffer += chunk.decode("utf-8", errors="ignore")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                data_lines = [
                    line[5:].strip()
                    for line in block.splitlines()
                    if line.startswith("data:")
                ]
                if not data_lines:
                    continue
                parse_context_proxy_sse_event(
                    "\n".join(data_lines),
                    text_parts=text_parts,
                    function_calls_by_id=function_calls_by_id,
                    saw_text_delta=saw_text_delta,
                    on_text_delta=on_text_delta,
                )

        if buffer.strip():
            data_lines = [
                line[5:].strip()
                for line in buffer.splitlines()
                if line.startswith("data:")
            ]
            if data_lines:
                parse_context_proxy_sse_event(
                    "\n".join(data_lines),
                    text_parts=text_parts,
                    function_calls_by_id=function_calls_by_id,
                    saw_text_delta=saw_text_delta,
                    on_text_delta=on_text_delta,
                )

    return type(
        "ContextProxyStreamResult",
        (),
        {
            "output_text": "".join(text_parts),
            "function_calls": list(function_calls_by_id.values()),
            "finish_reason": None,
        },
    )()


def stream_context_codex_proxy_response_with_retry(
    request: dict[str, Any],
    *,
    on_text_delta: Callable[[str], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
    max_attempts: int = 3,
) -> object:
    last_response: object | None = None
    last_error: Exception | None = None

    for attempt in range(max(1, max_attempts)):
        if check_cancelled is not None:
            check_cancelled()

        try:
            response = stream_context_codex_proxy_response(
                request,
                on_text_delta=on_text_delta,
                check_cancelled=check_cancelled,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= max_attempts - 1:
                raise
        else:
            last_response = response
            output_text = sanitize_text(getattr(response, "output_text", "") or "")
            function_calls = getattr(response, "function_calls", None) or []
            if output_text or function_calls:
                return response
            last_error = RuntimeError("Context proxy stream returned no events")

        if attempt < max_attempts - 1:
            time.sleep(0.5 * (attempt + 1))

    if last_response is not None:
        return last_response
    if last_error is not None:
        raise last_error
    raise RuntimeError("Context proxy stream returned no response")


def run_context_chat_turn(
    session: SessionState,
    *,
    message: str,
    selected_indexes: list[int] | None = None,
    reasoning_effort: str | None = None,
    on_text_delta: Callable[[str], None] | None = None,
    on_round_reset: Callable[[], None] | None = None,
    on_tool_event: Callable[[ToolEvent], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[str, str, ContextWorkbenchDraft, list[ToolEvent]]:
    instructions, request_model, draft, tool_registry, context_input = build_context_chat_runtime(
        session,
        message=message,
        selected_indexes=selected_indexes,
    )
    context_provider_id = resolve_context_workbench_provider_id(session.agent.settings, request_model)
    request_reasoning_effort = resolve_context_reasoning_effort(
        session.agent.settings,
        provider_id=context_provider_id,
        model_id=request_model,
        requested_effort=reasoning_effort,
    )
    context_agent = build_context_workbench_agent(session.agent.settings, context_provider_id)
    tool_events: list[ToolEvent] = []
    readonly_tool_result_cache: dict[str, str] = {}
    readonly_tool_cache_names = {"preview_context_selection", "get_context_node_details"}

    round_count = 0
    while True:
        round_count += 1

        if check_cancelled is not None:
            check_cancelled()

        def build_request() -> dict[str, Any]:
            request = {
                "model": request_model,
                "instructions": instructions,
                "input": sanitize_value(context_input),
                "tools": tool_registry.schemas,
                "store": False,
                "extra_headers": {
                    "x-hash-context-internal": "context-workbench",
                },
            }
            if request_reasoning_effort:
                request["reasoning"] = {"effort": request_reasoning_effort}
            write_context_request_debug(
                session_id=session.session_id,
                request_model=request_model,
                round_count=round_count,
                request=request,
                note="context_workbench_request",
            )
            return request

        try:
            request = build_request()
            if context_provider_id == CODEX_PROXY_PROVIDER_ID:
                response = stream_context_codex_proxy_response_with_retry(
                    request,
                    on_text_delta=on_text_delta,
                    check_cancelled=check_cancelled,
                )
            else:
                response = context_agent._stream_response(
                    **request,
                    on_text_delta=on_text_delta,
                )
        except Exception as exc:
            if (
                context_provider_id == CODEX_PROXY_PROVIDER_ID
                or not context_agent._should_fallback_to_developer(exc)
            ):
                raise

            context_agent._fallback_to_developer_context()
            response = context_agent._stream_response(
                **build_request(),
                on_text_delta=on_text_delta,
            )
        if check_cancelled is not None:
            check_cancelled()

        if not response.function_calls:
            final_answer = sanitize_text(response.output_text).strip()
            if not final_answer:
                error_msg = "Model returned empty response"
                if response.finish_reason:
                    error_msg += f" (Finish reason: {response.finish_reason})"
                raise RuntimeError(error_msg)
            if check_cancelled is not None:
                check_cancelled()
            return final_answer, request_model, draft, tool_events

        if response.output_text and on_round_reset is not None:
            if check_cancelled is not None:
                check_cancelled()
            on_round_reset()

        for call in response.function_calls:
            if check_cancelled is not None:
                check_cancelled()
            safe_call_name = sanitize_text(getattr(call, "name", "") or "")
            safe_call_id = sanitize_text(getattr(call, "call_id", "") or "")
            safe_call_arguments = sanitize_text(getattr(call, "arguments", "") or "{}") or "{}"

            try:
                raw_arguments = json.loads(safe_call_arguments)
                arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
                cache_key = ""
                if safe_call_name in readonly_tool_cache_names:
                    cache_key = json.dumps(
                        {
                            "name": safe_call_name,
                            "arguments": sanitize_value(arguments),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )

                if cache_key and cache_key in readonly_tool_result_cache:
                    result = json.dumps(
                        {
                            "payload_kind": "cached_tool_result",
                            "tool_name": safe_call_name,
                            "message": "This exact read-only context tool call already ran in this workbench turn. Use the previous function_call_output result instead of requesting it again.",
                        },
                        ensure_ascii=False,
                    )
                    execution = ToolExecution(
                        output_text=result,
                        display_title=safe_call_name,
                        display_detail="cached duplicate tool call",
                        display_result="Duplicate read-only tool call skipped; use the previous result.",
                        status="completed",
                    )
                else:
                    execution = tool_registry.execute(safe_call_name, arguments)
                    if cache_key:
                        readonly_tool_result_cache[cache_key] = sanitize_text(execution.output_text)
                    else:
                        readonly_tool_result_cache.clear()
                result = sanitize_text(execution.output_text)
            except json.JSONDecodeError as exc:
                arguments = {}
                result = json.dumps(
                    {"error": f"invalid tool arguments: {exc.msg}"},
                    ensure_ascii=False,
                )
                execution = ToolExecution(
                    output_text=result,
                    display_title=safe_call_name or "context_workbench_tool",
                    display_detail="tool arguments invalid",
                    display_result=f"Tool arguments are not valid JSON: {exc.msg}",
                    status="error",
                )
            else:
                result = sanitize_text(execution.output_text)

            if check_cancelled is not None:
                check_cancelled()
            safe_arguments = sanitize_value(arguments)
            tool_event = ToolEvent(
                name=safe_call_name,
                arguments=safe_arguments,
                output_preview=session.agent._preview(result),
                raw_output=result,
                display_title=execution.display_title,
                display_detail=execution.display_detail,
                display_result=execution.display_result,
                status=execution.status,
            )
            tool_events.append(tool_event)
            if on_tool_event is not None:
                on_tool_event(tool_event)

            context_input.append(
                {
                    "type": "function_call",
                    "call_id": safe_call_id,
                    "name": safe_call_name,
                    "arguments": safe_call_arguments,
                }
            )
            context_input.append(
                {
                    "type": "function_call_output",
                    "call_id": safe_call_id,
                    "output": result,
                }
            )

    # Note: Loop continues until returns or error inside


def create_context_chat_answer(
    session: SessionState,
    *,
    message: str,
    selected_indexes: list[int] | None = None,
    reasoning_effort: str | None = None,
) -> tuple[str, str, ContextWorkbenchDraft]:
    answer, request_model, draft, _tool_events = run_context_chat_turn(
        session,
        message=message,
        selected_indexes=selected_indexes,
        reasoning_effort=reasoning_effort,
    )
    return answer, request_model, draft


def build_context_chat_response_payload(
    app_state: AppState,
    session: SessionState,
    *,
    user_message: str,
    answer: str,
    used_model: str,
    draft: ContextWorkbenchDraft,
    tool_events: list[ToolEvent] | None = None,
) -> dict[str, object]:
    proxy_override: dict[str, object] | None = None
    if draft.has_changes:
        conversation, revisions, pending_restore = app_state.apply_context_workbench_mutation(
            session,
            transcript=draft.committed_transcript(),
            revision_label=draft.revision_label(),
            revision_summary=draft.revision_summary(),
            operations=draft.operations,
        )
        proxy_override = safe_sync_proxy_session_override_if_known(session, conversation)
        if sanitize_text(proxy_override.get("status") or "") == "error":
            answer = append_proxy_override_warning(answer, sanitize_text(proxy_override.get("error") or ""))
    else:
        conversation = sanitize_value(session.transcript)
        revisions = context_revision_summaries(session.context_revisions)
        pending_restore = None

    history = app_state.append_context_workbench_turn(
        session,
        user_message=user_message,
        answer=answer,
    )
    payload: dict[str, object] = {
        "answer": answer,
        "used_model": used_model,
        "history": history,
        "conversation": conversation,
        "revisions": revisions,
        "pending_restore": pending_restore,
    }
    if tool_events is not None:
        payload["tool_events"] = [serialize_tool_event(event) for event in tool_events]
    if proxy_override is not None:
        payload["proxy_override"] = proxy_override
    return payload


def normalize_attachment_records(raw_attachments: Any) -> list[dict[str, object]]:
    if not isinstance(raw_attachments, list):
        return []

    normalized: list[dict[str, object]] = []
    for item in raw_attachments:
        if not isinstance(item, dict):
            continue

        name = sanitize_text(item.get("name") or "").strip()
        relative_path = sanitize_text(item.get("relative_path") or "").strip()
        mime_type = sanitize_text(item.get("mime_type") or "").strip()
        kind = sanitize_text(item.get("kind") or "").strip() or "file"
        attachment_id = sanitize_text(item.get("id") or "").strip()

        if not name or not relative_path:
            continue

        size_bytes = item.get("size_bytes")
        if not isinstance(size_bytes, int):
            try:
                size_bytes = int(size_bytes)
            except (TypeError, ValueError):
                size_bytes = 0

        normalized.append(
            {
                "id": attachment_id or uuid.uuid4().hex,
                "name": name,
                "mime_type": mime_type or "application/octet-stream",
                "kind": "image" if kind == "image" else "file",
                "size_bytes": max(0, size_bytes),
                "relative_path": relative_path,
                "url": f"/{relative_path}",
            }
        )

    return normalized


def parse_data_url(data_url: str) -> tuple[str, bytes]:
    match = DATA_URL_PATTERN.match(sanitize_text(data_url))
    if not match:
        raise ValueError("attachment data_url is invalid")

    mime_type = sanitize_text(match.group("mime") or "").strip() or "application/octet-stream"
    try:
        raw_bytes = base64.b64decode(match.group("data"), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("附件编码解析失败") from exc

    if not raw_bytes:
        raise ValueError("附件内容为空")

    return mime_type, raw_bytes


def build_attachment_input(name: str, mime_type: str, data_url: str) -> dict[str, Any]:
    safe_name = sanitize_text(name).strip() or "upload"
    safe_mime_type = sanitize_text(mime_type).strip() or "application/octet-stream"
    safe_data_url = sanitize_text(data_url)

    if safe_mime_type.startswith("image/"):
        return {
            "type": "input_image",
            "image_url": safe_data_url,
            "detail": "auto",
        }

    return {
        "type": "input_file",
        "filename": safe_name,
        "file_data": safe_data_url,
    }


def build_attachment_path_note(name: str, mime_type: str, file_path: Path) -> dict[str, str]:
    safe_name = sanitize_text(name).strip() or file_path.name
    safe_mime_type = sanitize_text(mime_type).strip() or "application/octet-stream"
    return {
        "type": "input_text",
        "text": (
            f"Attachment available locally: {safe_name}\n"
            f"MIME type: {safe_mime_type}\n"
            f"Local path for tools: {file_path}"
        ),
    }


def persist_request_attachments(raw_attachments: Any) -> tuple[list[dict[str, object]], list[dict[str, Any]]]:
    if raw_attachments in (None, ""):
        return [], []
    if not isinstance(raw_attachments, list):
        raise ValueError("attachments must be a list")

    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    transcript_attachments: list[dict[str, object]] = []
    agent_inputs: list[dict[str, Any]] = []
    total_size = 0

    for raw_item in raw_attachments:
        if not isinstance(raw_item, dict):
            continue

        original_name = sanitize_text(raw_item.get("name") or "").strip() or "upload"
        data_url = sanitize_text(raw_item.get("data_url") or "")
        payload_mime_type = sanitize_text(raw_item.get("mime_type") or "").strip()
        parsed_mime_type, raw_bytes = parse_data_url(data_url)
        mime_type = payload_mime_type or parsed_mime_type or "application/octet-stream"
        total_size += len(raw_bytes)

        if len(raw_bytes) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"附件 {original_name} 超过 50 MB")
        if total_size > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError("本轮附件总大小超过 50 MB")

        suffix = Path(original_name).suffix
        if not suffix:
            guessed_extension = mimetypes.guess_extension(mime_type or "") or ""
            suffix = guessed_extension

        attachment_id = uuid.uuid4().hex
        stored_name = f"{attachment_id}{suffix}"
        stored_path = ATTACHMENTS_DIR / stored_name
        stored_path.write_bytes(raw_bytes)

        relative_path = attachment_url_path(stored_name)
        kind = "image" if mime_type.startswith("image/") else "file"

        transcript_attachments.append(
            {
                "id": attachment_id,
                "name": original_name,
                "mime_type": mime_type,
                "kind": kind,
                "size_bytes": len(raw_bytes),
                "relative_path": relative_path,
                "url": f"/{relative_path}",
            }
        )
        agent_inputs.append(build_attachment_path_note(original_name, mime_type, stored_path.resolve()))
        agent_inputs.append(build_attachment_input(original_name, mime_type, data_url))

    return transcript_attachments, agent_inputs


def attachment_inputs_from_records(attachments: list[dict[str, object]]) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    for attachment in attachments:
        relative_path = sanitize_text(attachment.get("relative_path") or "").strip()
        name = sanitize_text(attachment.get("name") or "").strip()
        mime_type = sanitize_text(attachment.get("mime_type") or "").strip()
        if not relative_path:
            continue

        file_path = resolve_attachment_file_path(relative_path)
        if file_path is None or not file_path.exists() or not file_path.is_file():
            continue

        raw_bytes = file_path.read_bytes()
        if not raw_bytes:
            continue

        safe_mime_type = mime_type or mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        data_url = f"data:{safe_mime_type};base64,{base64.b64encode(raw_bytes).decode('ascii')}"
        inputs.append(build_attachment_path_note(name or file_path.name, safe_mime_type, file_path))
        inputs.append(build_attachment_input(name or file_path.name, safe_mime_type, data_url))

    return inputs


def model_options(default_model: str, configured_models: list[str] | None = None) -> list[str]:
    ordered = [default_model, *(configured_models or []), "gpt-5.4", "gpt-5.4-mini", "gpt-5.2"]
    unique_models: list[str] = []
    for model in ordered:
        safe_model = sanitize_text(model).strip()
        if safe_model and safe_model not in unique_models:
            unique_models.append(safe_model)
    return unique_models


def active_provider_models(settings: Settings) -> list[str]:
    return settings.active_provider_model_ids()


PROVIDER_MODEL_TYPES = {"chat_completion", "responses", "gemini", "claude"}


def normalize_provider_type(raw_type: Any, provider_id: str = "") -> str:
    cleaned_type = sanitize_text(raw_type or "").strip()
    if cleaned_type in PROVIDER_MODEL_TYPES:
        return cleaned_type
    if provider_id == "gemini":
        return "gemini"
    if provider_id in {"anthropic", "claude"}:
        return "claude"
    return "responses"


def normalize_provider_api_base_url(raw_url: str, provider_type: str = "responses") -> str:
    cleaned_url = sanitize_text(raw_url).strip().rstrip("/")
    if not cleaned_url:
        return ""

    parsed = urlparse(cleaned_url)
    if not parsed.scheme or not parsed.netloc:
        return cleaned_url

    path = parsed.path.rstrip("/")
    suffixes_by_type = {
        "responses": ("/responses", "/chat/completions", "/completions", "/models"),
        "chat_completion": ("/chat/completions", "/completions", "/models"),
        "gemini": ("/models",),
        "claude": ("/messages", "/models"),
    }
    suffixes = suffixes_by_type.get(provider_type, suffixes_by_type["responses"])
    for suffix in suffixes:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break

    return urlunparse((parsed.scheme, parsed.netloc, path or "", "", "", "")).rstrip("/")


def build_provider_models_url(api_base_url: str, provider_type: str = "responses") -> str:
    normalized_base_url = normalize_provider_api_base_url(api_base_url, provider_type)
    if not normalized_base_url:
        return ""
    return f"{normalized_base_url}/models"


def build_provider_models_url_candidates(api_base_url: str, provider_type: str = "responses") -> list[str]:
    primary_url = build_provider_models_url(api_base_url, provider_type)
    if not primary_url:
        return []

    urls = [primary_url]
    parsed = urlparse(primary_url)
    if parsed.scheme and parsed.netloc and parsed.path not in {"", "/models"}:
        root_models_url = urlunparse((parsed.scheme, parsed.netloc, "/models", "", "", ""))
        if root_models_url not in urls:
            urls.append(root_models_url)
    return urls


def normalize_fetched_provider_models(raw_payload: Any, provider_type: str = "responses") -> list[dict[str, str]]:
    if not isinstance(raw_payload, dict):
        return []

    raw_models = raw_payload.get("models") if provider_type == "gemini" else raw_payload.get("data")
    if not isinstance(raw_models, list):
        return []

    normalized_models: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for item in raw_models:
        if not isinstance(item, dict):
            continue

        if provider_type == "gemini":
            raw_model_id = sanitize_text(item.get("name") or item.get("id") or "").strip()
            model_id = raw_model_id.removeprefix("models/")
            label = sanitize_text(item.get("displayName") or model_id).strip() or model_id
            group = "Gemini"
        elif provider_type == "claude":
            model_id = sanitize_text(item.get("id") or "").strip()
            label = sanitize_text(item.get("display_name") or item.get("displayName") or model_id).strip() or model_id
            group = "Claude"
        else:
            model_id = sanitize_text(item.get("id") or "").strip()
            label = model_id
            group = sanitize_text(item.get("owned_by") or item.get("object") or "Models").strip() or "Models"

        if not model_id or model_id in seen_ids:
            continue

        seen_ids.add(model_id)
        normalized_models.append(
            {
                "id": model_id,
                "label": label,
                "group": group,
                "provider": group,
            }
        )

    normalized_models.sort(key=lambda item: item["id"].lower())
    return normalized_models


def fetch_models_from_provider(
    api_base_url: str,
    api_key: str | None,
    provider_type: str = "responses",
    timeout_seconds: float = 18,
) -> list[dict[str, str]]:
    safe_provider_type = normalize_provider_type(provider_type)
    models_urls = build_provider_models_url_candidates(api_base_url, safe_provider_type)
    if not models_urls:
        raise ValueError("请先填写有效的 API 地址")

    headers = {
        "Accept": "application/json",
        "User-Agent": "hash-code/0.2",
    }
    safe_api_key = sanitize_text(api_key or "").strip()
    if safe_provider_type == "gemini" and safe_api_key:
        headers["x-goog-api-key"] = safe_api_key
    elif safe_provider_type == "claude" and safe_api_key:
        headers["x-api-key"] = safe_api_key
        headers["anthropic-version"] = "2023-06-01"
    elif safe_api_key:
        headers["Authorization"] = f"Bearer {safe_api_key}"

    last_error: ValueError | None = None

    for models_url in models_urls:
        request = urllib_request.Request(models_url, headers=headers, method="GET")

        try:
            with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore").strip()
            message = sanitize_text(detail or exc.reason or f"HTTP {exc.code}")
            if exc.code in {404, 405} and models_url != models_urls[-1]:
                last_error = ValueError(message)
                continue
            raise ValueError(message) from exc
        except urllib_error.URLError as exc:
            raise ValueError(sanitize_text(exc.reason or str(exc))) from exc
        except json.JSONDecodeError as exc:
            raise ValueError("模型接口返回的不是合法 JSON") from exc

        models = normalize_fetched_provider_models(payload, safe_provider_type)
        if models:
            return models
        last_error = ValueError("这个供应商没有返回可用模型")

    raise last_error or ValueError("这个供应商没有返回可用模型")


def clone_provider_settings_payloads(settings: Settings) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for provider in settings.response_providers:
        payloads.append(
            {
                "id": sanitize_text(provider.get("id") or "").strip(),
                "enabled": bool(provider.get("enabled")),
                "supports_model_fetch": bool(provider.get("supports_model_fetch")),
                "supports_responses": bool(provider.get("supports_responses")),
                "api_base_url": sanitize_text(provider.get("api_base_url") or "").strip(),
                "default_model": sanitize_text(provider.get("default_model") or "").strip(),
                "models": sanitize_value(provider.get("models") or []),
                "last_sync_at": sanitize_text(provider.get("last_sync_at") or "").strip(),
                "last_sync_error": sanitize_text(provider.get("last_sync_error") or "").strip(),
            }
        )
    return payloads


def provider_model_ids_from_payloads(provider_payloads: list[dict[str, Any]], provider_id: str) -> list[str]:
    cleaned_provider_id = sanitize_text(provider_id).strip()
    provider = next(
        (
            item
            for item in provider_payloads
            if sanitize_text(item.get("id") or "").strip() == cleaned_provider_id
        ),
        None,
    )
    if provider is None:
        return []
    model_ids: list[str] = []
    for model in provider.get("models") or []:
        if not isinstance(model, dict):
            continue
        model_id = sanitize_text(model.get("id") or "").strip()
        if model_id and model_id not in model_ids:
            model_ids.append(model_id)
    return model_ids


def context_workbench_provider_payloads(settings: Settings, *, refresh_codex_proxy_models: bool = False) -> list[dict[str, Any]]:
    payload = settings.public_payload()
    raw_providers = payload.get("response_providers")
    provider_payloads = [dict(item) for item in raw_providers if isinstance(item, dict)] if isinstance(raw_providers, list) else []
    for provider in provider_payloads:
        provider_id = sanitize_text(provider.get("id") or "").strip()
        if provider_id != CODEX_PROXY_PROVIDER_ID:
            continue
        provider["api_base_url"] = CODEX_PROXY_BASE_URL
        if not refresh_codex_proxy_models:
            continue
        try:
            fetched_models = fetch_models_from_provider(
                CODEX_PROXY_BASE_URL,
                "not-needed",
                "responses",
                timeout_seconds=4,
            )
        except Exception as exc:
            provider["last_sync_error"] = "" if provider.get("models") else sanitize_text(str(exc))
            continue
        if fetched_models:
            provider["models"] = fetched_models
            provider["last_sync_error"] = ""
            provider["last_sync_at"] = datetime.now(timezone.utc).isoformat()
        break
    return provider_payloads


def context_workbench_models_payload(settings: Settings, provider_payloads: list[dict[str, Any]]) -> list[str]:
    settings_data = context_workbench_settings_payload(settings)
    context_model = sanitize_text(settings_data.get("context_workbench_model") or "").strip()
    provider_id = sanitize_text(settings_data.get("context_workbench_provider_id") or "").strip()
    return model_options(context_model, provider_model_ids_from_payloads(provider_payloads, provider_id))


def codex_proxy_control_url(path: str) -> str:
    control_base = CODEX_PROXY_BASE_URL.rstrip("/")
    if control_base.endswith("/v1"):
        control_base = control_base[:-3]
    return f"{control_base}{path}"


def post_codex_proxy_control_json(path: str, payload: dict[str, Any], timeout_seconds: float = 8) -> dict[str, Any]:
    request = urllib_request.Request(
        codex_proxy_control_url(path),
        data=json.dumps(sanitize_value(payload), ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise ValueError(sanitize_text(detail or exc.reason or f"HTTP {exc.code}")) from exc
    except urllib_error.URLError as exc:
        raise ValueError(sanitize_text(exc.reason or str(exc))) from exc

    try:
        result = json.loads(raw_body or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Proxy returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("Proxy returned invalid payload")
    return sanitize_value(result)


def get_codex_proxy_control_json(path: str, timeout_seconds: float = 3) -> dict[str, Any] | None:
    request = urllib_request.Request(
        codex_proxy_control_url(path),
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if exc.code == HTTPStatus.NOT_FOUND:
            return None
        raise ValueError(sanitize_text(detail or exc.reason or f"HTTP {exc.code}")) from exc
    except urllib_error.URLError as exc:
        raise ValueError(sanitize_text(exc.reason or str(exc))) from exc

    try:
        result = json.loads(raw_body or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Proxy returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("Proxy returned invalid payload")
    return sanitize_value(result)


def proxy_state_contains_session(session_id: str) -> bool:
    safe_session_id = sanitize_text(session_id or "").strip()
    if not safe_session_id:
        return False
    try:
        raw_state = json.loads(PROXY_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    sessions = raw_state.get("sessions")
    if not isinstance(sessions, list):
        return False
    return any(
        isinstance(item, dict) and sanitize_text(item.get("id") or "").strip() == safe_session_id
        for item in sessions
    )


def codex_proxy_session_exists(session_id: str) -> bool:
    safe_session_id = sanitize_text(session_id or "").strip()
    if not safe_session_id:
        return False
    if proxy_state_contains_session(safe_session_id):
        return True
    try:
        return get_codex_proxy_control_json(
            f"/api/proxy/sessions/{quote(safe_session_id, safe='')}",
            timeout_seconds=1.5,
        ) is not None
    except ValueError:
        return False


def sync_proxy_session_override_if_known(
    session: SessionState,
    transcript: list[dict[str, object]],
) -> dict[str, object]:
    session_id = sanitize_text(session.session_id or "").strip()
    if not session_id:
        return {"status": "skipped", "reason": "missing_session_id"}
    if not codex_proxy_session_exists(session_id):
        return {"status": "skipped", "reason": "not_proxy_session"}

    proxy_payload = post_codex_proxy_control_json(
        f"/api/proxy/sessions/{quote(session_id, safe='')}/override",
        {"transcript": transcript},
    )
    if bool(proxy_payload.get("changed")):
        summary, revision_number = active_context_revision_marker(session)
        visible_transcript = normalize_transcript(proxy_payload.get("transcript"))
        write_context_edit_marker(
            session_id,
            summary=summary,
            revision_number=revision_number,
            node_count=editable_context_node_count(visible_transcript),
        )
    return {
        "status": "synced",
        "changed": bool(proxy_payload.get("changed")),
        "has_override": bool(proxy_payload.get("has_override")),
    }


def safe_sync_proxy_session_override_if_known(
    session: SessionState,
    transcript: list[dict[str, object]],
) -> dict[str, object]:
    try:
        return sync_proxy_session_override_if_known(session, transcript)
    except ValueError as exc:
        return {
            "status": "error",
            "error": sanitize_text(str(exc) or "proxy override sync failed"),
        }


def append_proxy_override_warning(answer: str, error_message: str) -> str:
    warning = (
        "注意：这次上下文编辑已经写入本地视图，但同步到 Codex 代理 override 失败："
        f"{sanitize_text(error_message)}。下一轮主模型可能仍会看到旧上下文。"
    )
    safe_answer = sanitize_text(answer).rstrip()
    if not safe_answer:
        return warning
    return f"{safe_answer}\n\n{warning}"


def active_context_revision_marker(session: SessionState) -> tuple[str, int]:
    active_revision_id = find_active_context_revision_id(session.context_revisions)
    active_revision = next(
        (
            revision
            for revision in reversed(session.context_revisions)
            if sanitize_text(revision.get("id") or "").strip() == active_revision_id
        ),
        None,
    )
    if active_revision is None and session.context_revisions:
        active_revision = session.context_revisions[-1]
    if not isinstance(active_revision, dict):
        return "Context has been edited.", 0
    summary = sanitize_text(active_revision.get("summary") or active_revision.get("label") or "").strip()
    revision_number = coerce_context_revision_number(active_revision.get("revision_number"), 0)
    return summary or "Context has been edited.", revision_number


class HashHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "HashCodeWeb/0.2"

    @property
    def app_state(self) -> AppState:
        return self.server.app_state  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/init":
            self._send_json(self.app_state.bootstrap_payload())
            return

        if parsed.path == "/api/settings":
            self._send_json(
                {
                    "settings": settings_payload(self.app_state.settings),
                    "models": model_options(self.app_state.settings.model, active_provider_models(self.app_state.settings)),
                }
            )
            return

        if parsed.path == "/api/context-workbench-settings":
            provider_payloads = context_workbench_provider_payloads(self.app_state.settings)
            self._send_json(
                {
                    "settings": context_workbench_settings_payload(self.app_state.settings),
                    "models": context_workbench_models_payload(self.app_state.settings, provider_payloads),
                    "response_providers": provider_payloads,
                    "tool_catalog": ContextWorkbenchToolRegistry.tool_catalog(),
                }
            )
            return

        if parsed.path == "/api/workspace":
            self._send_json(
                {
                    "entries": list_workspace_entries(self.app_state.settings.project_root),
                }
            )
            return

        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json_body()

            if parsed.path == "/api/projects":
                project = self.app_state.create_project(
                    sanitize_text(payload.get("title") or "").strip() or None,
                    sanitize_text(payload.get("root_path") or "").strip() or None,
                )
                self._send_json(
                    {
                        "project": {
                            "id": project.project_id,
                            "title": project.title,
                            "root_path": project.root_path or "",
                        },
                        **self.app_state.sidebar_payload(),
                    },
                    status=HTTPStatus.CREATED,
                )
                return

            if parsed.path == "/api/pin-project":
                project_id = sanitize_text(payload.get("project_id", "")).strip()
                project = self.app_state.pin_project(project_id)
                self._send_json(
                    {
                        "project": {
                            "id": project.project_id,
                            "title": project.title,
                            "root_path": project.root_path or "",
                        },
                        **self.app_state.sidebar_payload(),
                    }
                )
                return

            if parsed.path == "/api/rename-project":
                project_id = sanitize_text(payload.get("project_id", "")).strip()
                title = sanitize_text(payload.get("title", "")).strip()
                project = self.app_state.rename_project(project_id, title)
                self._send_json(
                    {
                        "project": {
                            "id": project.project_id,
                            "title": project.title,
                            "root_path": project.root_path or "",
                        },
                        **self.app_state.sidebar_payload(),
                    }
                )
                return

            if parsed.path == "/api/archive-project-sessions":
                project_id = sanitize_text(payload.get("project_id", "")).strip()
                project, archived_session_ids = self.app_state.archive_project_sessions(project_id)
                self._send_json(
                    {
                        "project": {
                            "id": project.project_id,
                            "title": project.title,
                            "root_path": project.root_path or "",
                        },
                        "archived_session_ids": archived_session_ids,
                        **self.app_state.sidebar_payload(),
                    }
                )
                return

            if parsed.path == "/api/sessions":
                session = self.app_state.create_session(
                    scope=sanitize_text(payload.get("scope") or "chat"),
                    project_id=sanitize_text(payload.get("project_id") or "").strip() or None,
                )
                self._send_json(
                    {
                        "session": self.app_state.session_payload(session),
                        **self.app_state.sidebar_payload(),
                    },
                    status=HTTPStatus.CREATED,
                )
                return

            if parsed.path == "/api/proxy-sync-session":
                transcript = payload.get("transcript")
                if not isinstance(transcript, list):
                    raise ValueError("transcript must be a list")
                session = self.app_state.upsert_proxy_session(
                    session_id=sanitize_text(payload.get("session_id") or "").strip(),
                    title=sanitize_text(payload.get("title") or "").strip(),
                    transcript=transcript,
                    is_running=bool(payload.get("is_running")),
                )
                self._send_json(
                    {
                        "session": self.app_state.session_payload(session),
                        "conversation": sanitize_value(session.transcript),
                        "context_workbench_history": sanitize_value(session.context_workbench_history),
                        "context_revision_history": context_revision_summaries(session.context_revisions),
                        "pending_context_restore": context_pending_restore_payload(session.pending_context_restore),
                        **self.app_state.sidebar_payload(),
                    }
                )
                return

            if parsed.path == "/api/codex-local-session-sync":
                session_id = sanitize_text(payload.get("session_id") or "").strip()
                if not session_id:
                    raise ValueError("session_id is required")
                transcript = codex_local_session_transcript(session_id)
                if not transcript:
                    self._send_json(
                        {
                            "error": "Codex local session was not found or has no transcript",
                            "session_id": session_id,
                        },
                        status=HTTPStatus.NOT_FOUND,
                    )
                    return
                session = self.app_state.upsert_proxy_session(
                    session_id=session_id,
                    title=sanitize_text(payload.get("title") or "").strip() or f"Codex {session_id[:8]}",
                    transcript=transcript,
                    is_running=False,
                )
                self._send_json(
                    {
                        "session": self.app_state.session_payload(session),
                        "conversation": sanitize_value(session.transcript),
                        "context_workbench_history": sanitize_value(session.context_workbench_history),
                        "context_revision_history": context_revision_summaries(session.context_revisions),
                        "pending_context_restore": context_pending_restore_payload(session.pending_context_restore),
                        **self.app_state.sidebar_payload(),
                    }
                )
                return

            if parsed.path == "/api/context-edit-marker-consume":
                session_id = sanitize_text(payload.get("session_id") or "").strip()
                marker = consume_context_edit_marker(session_id)
                self._send_json({"marker": marker})
                return

            if parsed.path == "/api/reset":
                session_id = sanitize_text(payload.get("session_id", "")).strip()
                session = self.app_state.reset_session(session_id)
                self._send_json(
                    {
                        "session": self.app_state.session_payload(session),
                        **self.app_state.sidebar_payload(),
                    }
                )
                return

            if parsed.path == "/api/truncate-session":
                session_id = sanitize_text(payload.get("session_id", "")).strip()
                raw_from_index = payload.get("from_index")
                try:
                    from_index = int(raw_from_index)
                except (TypeError, ValueError) as exc:
                    raise ValueError("from_index must be a number") from exc

                session = self.app_state.truncate_session(session_id, from_index)
                proxy_override = safe_sync_proxy_session_override_if_known(session, sanitize_value(session.transcript))
                self._send_json(
                    {
                        "session": self.app_state.session_payload(session),
                        "conversation": sanitize_value(session.transcript),
                        "proxy_override": proxy_override,
                        **self.app_state.sidebar_payload(),
                    }
                )
                return

            if parsed.path == "/api/delete-message":
                session_id = sanitize_text(payload.get("session_id", "")).strip()
                raw_message_index = payload.get("message_index")
                try:
                    message_index = int(raw_message_index)
                except (TypeError, ValueError) as exc:
                    raise ValueError("message_index must be a number") from exc

                session = self.app_state.get_session(session_id)
                request_id = self.app_state.acquire_session_request(session, "main")
                try:
                    session = self.app_state.delete_transcript_message(session_id, message_index)
                    proxy_override = safe_sync_proxy_session_override_if_known(
                        session,
                        sanitize_value(session.transcript),
                    )
                    self._send_json(
                        {
                            "session": self.app_state.session_payload(session),
                            "conversation": sanitize_value(session.transcript),
                            "proxy_override": proxy_override,
                            **self.app_state.sidebar_payload(),
                        }
                    )
                finally:
                    self.app_state.release_session_request(session, "main", request_id)
                return

            if parsed.path == "/api/settings":
                raw_max_tool_rounds = payload.get("max_tool_rounds")
                max_tool_rounds = None
                if raw_max_tool_rounds not in (None, ""):
                    try:
                        max_tool_rounds = int(raw_max_tool_rounds)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("max_tool_rounds must be a number") from exc

                updated_settings = save_settings(
                    default_model=sanitize_text(payload.get("default_model") or "").strip() or None,
                    default_reasoning_effort=sanitize_text(payload.get("default_reasoning_effort") or "").strip()
                    if "default_reasoning_effort" in payload
                    else None,
                    openai_base_url=sanitize_text(payload.get("openai_base_url") or "").strip(),
                    max_tool_rounds=max_tool_rounds,
                    assistant_name=payload.get("assistant_name") if isinstance(payload.get("assistant_name"), str) else None,
                    assistant_greeting=payload.get("assistant_greeting") if isinstance(payload.get("assistant_greeting"), str) else None,
                    assistant_prompt=payload.get("assistant_prompt") if isinstance(payload.get("assistant_prompt"), str) else None,
                    temperature=payload.get("temperature") if "temperature" in payload else _UNSET,
                    top_p=payload.get("top_p") if "top_p" in payload else _UNSET,
                    context_message_limit=payload.get("context_message_limit") if "context_message_limit" in payload else _UNSET,
                    streaming=bool(payload.get("streaming")) if "streaming" in payload else None,
                    user_name=payload.get("user_name") if isinstance(payload.get("user_name"), str) else None,
                    user_locale=payload.get("user_locale") if isinstance(payload.get("user_locale"), str) else None,
                    user_timezone=payload.get("user_timezone") if isinstance(payload.get("user_timezone"), str) else None,
                    user_profile=payload.get("user_profile") if isinstance(payload.get("user_profile"), str) else None,
                    theme_color=payload.get("theme_color") if isinstance(payload.get("theme_color"), str) else None,
                    theme_mode=payload.get("theme_mode") if isinstance(payload.get("theme_mode"), str) else None,
                    background_color=payload.get("background_color") if isinstance(payload.get("background_color"), str) else None,
                    ui_font=payload.get("ui_font") if isinstance(payload.get("ui_font"), str) else None,
                    code_font=payload.get("code_font") if isinstance(payload.get("code_font"), str) else None,
                    ui_font_size=payload.get("ui_font_size") if type(payload.get("ui_font_size")) is int else None,
                    code_font_size=payload.get("code_font_size") if type(payload.get("code_font_size")) is int else None,
                    appearance_contrast=payload.get("appearance_contrast")
                    if type(payload.get("appearance_contrast")) is int
                    else None,
                    service_hints_enabled=bool(payload.get("service_hints_enabled"))
                    if "service_hints_enabled" in payload
                    else None,
                    tool_settings=payload.get("tool_settings")
                    if isinstance(payload.get("tool_settings"), list)
                    else None,
                    openai_api_key=payload.get("openai_api_key") if isinstance(payload.get("openai_api_key"), str) else None,
                    clear_api_key=bool(payload.get("clear_api_key")),
                    active_provider_id=sanitize_text(payload.get("active_provider_id") or "").strip() or None,
                    deleted_provider_ids=payload.get("deleted_provider_ids")
                    if isinstance(payload.get("deleted_provider_ids"), list)
                    else None,
                    response_providers=payload.get("response_providers")
                    if isinstance(payload.get("response_providers"), list)
                    else None,
                )
                self.app_state.refresh_settings(updated_settings)
                self._send_json(
                    {
                        "settings": settings_payload(updated_settings),
                        "models": model_options(updated_settings.model, active_provider_models(updated_settings)),
                    }
                )
                return

            if parsed.path == "/api/provider-model-candidates":
                provider_id = sanitize_text(payload.get("provider_id") or "").strip()
                provider = next(
                    (
                        item
                        for item in self.app_state.settings.response_providers
                        if sanitize_text(item.get("id") or "").strip() == provider_id
                    ),
                    None,
                )
                provider_type = normalize_provider_type(
                    payload.get("provider_type") or (provider.get("provider_type") if provider else ""),
                    provider_id,
                )
                request_base_url = sanitize_text(
                    payload.get("api_base_url") or (provider.get("api_base_url") if provider else "") or ""
                ).strip()
                request_api_key = (
                    payload.get("api_key")
                    if isinstance(payload.get("api_key"), str)
                    else sanitize_text((provider.get("api_key") if provider else "") or "").strip()
                )

                fetched_models = fetch_models_from_provider(request_base_url, request_api_key, provider_type)
                self._send_json(
                    {
                        "provider_id": provider_id,
                        "fetched_count": len(fetched_models),
                        "models": fetched_models,
                    }
                )
                return

            if parsed.path == "/api/provider-models":
                provider_id = sanitize_text(payload.get("provider_id") or "").strip()
                preview_only = bool(payload.get("preview_only"))
                provider = next(
                    (
                        item
                        for item in self.app_state.settings.response_providers
                        if sanitize_text(item.get("id") or "").strip() == provider_id
                    ),
                    None,
                )
                if provider is None and not preview_only:
                    raise ValueError("provider_id is invalid")
                if provider is not None and not bool(provider.get("supports_model_fetch")):
                    raise ValueError("这个供应商暂时不支持拉取模型列表")

                request_base_url = sanitize_text(
                    payload.get("api_base_url") or (provider.get("api_base_url") if provider else "") or ""
                ).strip()
                request_api_key = (
                    payload.get("api_key")
                    if isinstance(payload.get("api_key"), str)
                    else sanitize_text((provider.get("api_key") if provider else "") or "").strip()
                )
                provider_type = normalize_provider_type(
                    payload.get("provider_type") or (provider.get("provider_type") if provider else ""),
                    provider_id,
                )
                provider_payloads = clone_provider_settings_payloads(self.app_state.settings)
                current_sync_time = datetime.now(timezone.utc).isoformat()

                try:
                    fetched_models = fetch_models_from_provider(request_base_url, request_api_key, provider_type)
                except Exception as exc:
                    if preview_only:
                        raise

                    for item in provider_payloads:
                        if sanitize_text(item.get("id") or "").strip() != provider_id:
                            continue
                        item["api_base_url"] = request_base_url
                        item["last_sync_at"] = current_sync_time
                        item["last_sync_error"] = sanitize_text(str(exc))
                        if isinstance(request_api_key, str) and request_api_key.strip():
                            item["api_key"] = request_api_key.strip()
                        break

                    failed_settings = save_settings(response_providers=provider_payloads)
                    self.app_state.refresh_settings(failed_settings)
                    raise

                if preview_only:
                    self._send_json(
                        {
                            "provider_id": provider_id,
                            "fetched_count": len(fetched_models),
                            "models": fetched_models,
                        }
                    )
                    return

                fetched_default_model = sanitize_text(provider.get("default_model") or "").strip()
                fetched_model_ids = [sanitize_text(model.get("id") or "").strip() for model in fetched_models]
                if not fetched_default_model or fetched_default_model not in fetched_model_ids:
                    fetched_default_model = fetched_model_ids[0]

                for item in provider_payloads:
                    if sanitize_text(item.get("id") or "").strip() != provider_id:
                        continue
                    item["api_base_url"] = request_base_url
                    item["default_model"] = fetched_default_model
                    item["models"] = fetched_models
                    item["last_sync_at"] = current_sync_time
                    item["last_sync_error"] = ""
                    if isinstance(request_api_key, str) and request_api_key.strip():
                        item["api_key"] = request_api_key.strip()
                    break

                updated_settings = save_settings(response_providers=provider_payloads)
                self.app_state.refresh_settings(updated_settings)
                self._send_json(
                    {
                        "settings": settings_payload(updated_settings),
                        "models": model_options(updated_settings.model, active_provider_models(updated_settings)),
                        "provider_id": provider_id,
                        "fetched_count": len(fetched_models),
                    }
                )
                return

            if parsed.path == "/api/context-workbench-settings":
                updated_settings = save_settings(
                    context_workbench_model=sanitize_text(payload.get("context_workbench_model") or "").strip()
                    or None,
                    context_workbench_provider_id=CODEX_PROXY_PROVIDER_ID,
                    context_token_warning_threshold=payload.get("context_token_warning_threshold"),
                    context_token_critical_threshold=payload.get("context_token_critical_threshold"),
                    user_locale=payload.get("user_locale") if isinstance(payload.get("user_locale"), str) else None,
                )
                self.app_state.settings = updated_settings
                provider_payloads = context_workbench_provider_payloads(updated_settings)
                self._send_json(
                    {
                        "settings": context_workbench_settings_payload(updated_settings),
                        "models": context_workbench_models_payload(updated_settings, provider_payloads),
                        "response_providers": provider_payloads,
                        "tool_catalog": ContextWorkbenchToolRegistry.tool_catalog(),
                    }
                )
                return

            if parsed.path == "/api/proxy-session-override":
                session_id = sanitize_text(payload.get("session_id") or "").strip()
                transcript = payload.get("transcript")
                if not session_id:
                    raise ValueError("session_id is required")
                if not isinstance(transcript, list):
                    raise ValueError("transcript must be a list")
                proxy_payload = post_codex_proxy_control_json(
                    f"/api/proxy/sessions/{quote(session_id, safe='')}/override",
                    {"transcript": transcript},
                )
                if bool(proxy_payload.get("changed")):
                    session = self.app_state.get_session(session_id)
                    summary, revision_number = active_context_revision_marker(session)
                    visible_transcript = normalize_transcript(proxy_payload.get("transcript"))
                    write_context_edit_marker(
                        session_id,
                        summary=summary,
                        revision_number=revision_number,
                        node_count=editable_context_node_count(visible_transcript),
                    )
                self._send_json(proxy_payload)
                return

            if parsed.path == "/api/proxy-session-reset":
                session_id = sanitize_text(payload.get("session_id") or "").strip()
                if not session_id:
                    raise ValueError("session_id is required")
                proxy_payload = post_codex_proxy_control_json(
                    f"/api/proxy/sessions/{quote(session_id, safe='')}/reset",
                    {},
                )
                if bool(proxy_payload.get("changed")):
                    session = self.app_state.get_session(session_id)
                    summary, revision_number = active_context_revision_marker(session)
                    visible_transcript = normalize_transcript(proxy_payload.get("transcript"))
                    write_context_edit_marker(
                        session_id,
                        summary=summary,
                        revision_number=revision_number,
                        node_count=editable_context_node_count(visible_transcript),
                    )
                self._send_json(proxy_payload)
                return

            if parsed.path == "/api/context-workbench-suggestions":
                session = self.app_state.get_session(payload.get("session_id"))
                self._send_json(context_workbench_suggestions_payload(session))
                return

            if parsed.path == "/api/delete-session":
                session_id = sanitize_text(payload.get("session_id", "")).strip()
                session = self.app_state.delete_session(session_id)
                self._send_json(
                    {
                        "deleted_session_id": session.session_id,
                        "deleted_scope": session.scope,
                        "deleted_project_id": session.project_id,
                        **self.app_state.sidebar_payload(),
                    }
                )
                return

            if parsed.path == "/api/delete-project":
                project_id = sanitize_text(payload.get("project_id", "")).strip()
                project, deleted_session_ids = self.app_state.delete_project(project_id)
                self._send_json(
                    {
                        "deleted_project_id": project.project_id,
                        "deleted_session_ids": deleted_session_ids,
                        **self.app_state.sidebar_payload(),
                    }
                )
                return

            if parsed.path == "/api/cancel-request":
                session = self.app_state.get_session(payload.get("session_id"))
                mode = sanitize_text(payload.get("mode") or "main").strip() or "main"
                cancelled = self.app_state.cancel_session_request(session, mode)
                self._send_json({"cancelled": cancelled})
                return

            if parsed.path == "/api/context-chat":
                session = self.app_state.get_session(payload.get("session_id"))
                message = sanitize_text(payload.get("message", "")).strip()
                if not message:
                    raise ValueError("message is required")

                reasoning_effort = sanitize_text(payload.get("reasoning_effort", "")).strip() or None
                selected_indexes = normalize_selected_node_indexes(
                    payload.get("selected_node_indexes"),
                    len(session.transcript),
                )
                request_id = self.app_state.acquire_session_request(session, "context")
                try:
                    answer, used_model, draft, tool_events = run_context_chat_turn(
                        session,
                        message=message,
                        selected_indexes=selected_indexes,
                        reasoning_effort=reasoning_effort,
                    )
                    self._send_json(
                        build_context_chat_response_payload(
                            self.app_state,
                            session,
                            user_message=message,
                            answer=answer,
                            used_model=used_model,
                            draft=draft,
                            tool_events=tool_events,
                        )
                    )
                finally:
                    self.app_state.release_session_request(session, "context", request_id)
                return

            if parsed.path == "/api/context-chat-stream":
                session = self.app_state.get_session(payload.get("session_id"))
                message = sanitize_text(payload.get("message", "")).strip()
                if not message:
                    raise ValueError("message is required")

                reasoning_effort = sanitize_text(payload.get("reasoning_effort", "")).strip() or None
                selected_indexes = normalize_selected_node_indexes(
                    payload.get("selected_node_indexes"),
                    len(session.transcript),
                )
                request_id = self.app_state.acquire_session_request(session, "context")
                self._start_stream_response()

                def raise_if_cancelled() -> None:
                    if self.app_state.is_session_request_cancelled(session, request_id):
                        raise RequestCancelledError()

                def handle_text_delta(delta: str) -> None:
                    raise_if_cancelled()
                    safe_delta = sanitize_text(delta)
                    if not safe_delta:
                        return
                    self._write_stream_event(
                        {
                            "type": "delta",
                            "delta": safe_delta,
                        }
                    )

                def handle_tool_event(event: ToolEvent) -> None:
                    raise_if_cancelled()
                    self._write_stream_event(
                        {
                            "type": "tool_event",
                            "tool_event": serialize_tool_event(event),
                        }
                    )

                def handle_round_reset() -> None:
                    raise_if_cancelled()
                    self._write_stream_event({"type": "reset"})

                try:
                    answer, used_model, draft, tool_events = run_context_chat_turn(
                        session,
                        message=message,
                        selected_indexes=selected_indexes,
                        reasoning_effort=reasoning_effort,
                        on_text_delta=handle_text_delta,
                        on_round_reset=handle_round_reset,
                        on_tool_event=handle_tool_event,
                        check_cancelled=raise_if_cancelled,
                    )
                    raise_if_cancelled()
                    payload_data = build_context_chat_response_payload(
                        self.app_state,
                        session,
                        user_message=message,
                        answer=answer,
                        used_model=used_model,
                        draft=draft,
                        tool_events=tool_events,
                    )
                    payload_data["type"] = "done"
                    self._write_stream_event(sanitize_value(payload_data))
                except (ClientDisconnectedError, RequestCancelledError):
                    pass
                except Exception as exc:  # noqa: BLE001
                    try:
                        self._write_stream_event(
                            {
                                "type": "error",
                                "error": sanitize_text(str(exc) or "服务异常"),
                            }
                        )
                    except ClientDisconnectedError:
                        pass
                finally:
                    self.app_state.release_session_request(session, "context", request_id)
                return

            if parsed.path == "/api/context-restore":
                session = self.app_state.get_session(payload.get("session_id"))
                revision_id = sanitize_text(payload.get("revision_id") or "").strip()
                if not revision_id:
                    raise ValueError("revision_id is required")

                request_id = self.app_state.acquire_session_request(session, "context")
                try:
                    conversation, history, revisions, pending_restore = self.app_state.restore_context_revision(
                        session,
                        revision_id,
                    )
                    proxy_override = safe_sync_proxy_session_override_if_known(session, conversation)
                    self._send_json(
                        {
                            "conversation": conversation,
                            "history": history,
                            "revisions": revisions,
                            "pending_restore": pending_restore,
                            "proxy_override": proxy_override,
                        }
                    )
                finally:
                    self.app_state.release_session_request(session, "context", request_id)
                return

            if parsed.path == "/api/context-workbench-history-message-delete":
                session = self.app_state.get_session(payload.get("session_id"))
                raw_message_index = payload.get("message_index")
                try:
                    message_index = int(raw_message_index)
                except (TypeError, ValueError) as exc:
                    raise ValueError("message_index must be a number") from exc

                request_id = self.app_state.acquire_session_request(session, "context")
                try:
                    conversation, history, revisions, pending_restore = self.app_state.delete_context_workbench_history_message(
                        session,
                        message_index=message_index,
                    )
                    self._send_json(
                        {
                            "conversation": conversation,
                            "history": history,
                            "revisions": revisions,
                            "pending_restore": pending_restore,
                        }
                    )
                finally:
                    self.app_state.release_session_request(session, "context", request_id)
                return

            if parsed.path == "/api/context-workbench-history-clear":
                session = self.app_state.get_session(payload.get("session_id"))
                request_id = self.app_state.acquire_session_request(session, "context")
                try:
                    conversation, history, revisions, pending_restore = self.app_state.clear_context_workbench_history(
                        session,
                    )
                    self._send_json(
                        {
                            "conversation": conversation,
                            "history": history,
                            "revisions": revisions,
                            "pending_restore": pending_restore,
                        }
                    )
                finally:
                    self.app_state.release_session_request(session, "context", request_id)
                return

            if parsed.path == "/api/context-undo-restore":
                session = self.app_state.get_session(payload.get("session_id"))
                request_id = self.app_state.acquire_session_request(session, "context")
                try:
                    conversation, history, revisions, pending_restore = self.app_state.undo_context_restore(session)
                    proxy_override = safe_sync_proxy_session_override_if_known(session, conversation)
                    self._send_json(
                        {
                            "conversation": conversation,
                            "history": history,
                            "revisions": revisions,
                            "pending_restore": pending_restore,
                            "proxy_override": proxy_override,
                        }
                    )
                finally:
                    self.app_state.release_session_request(session, "context", request_id)
                return

            if parsed.path == "/api/send-message-stream":
                session = self.app_state.get_session(payload.get("session_id"))
                message = sanitize_text(payload.get("message", "")).strip()
                transcript_attachments, agent_attachments = persist_request_attachments(payload.get("attachments"))
                if not message and not transcript_attachments:
                    raise ValueError("message is required")

                model = sanitize_text(payload.get("model", "")).strip() or None
                reasoning_effort = sanitize_text(payload.get("reasoning_effort", "")).strip() or None
                if reasoning_effort in {"default", "none"}:
                    reasoning_effort = None

                title_seed = message or sanitize_text(transcript_attachments[0].get("name") or "")
                should_name_session = self.app_state.should_name_session_from_first_message(session)
                request_id = self.app_state.acquire_session_request(session, "main")
                if should_name_session:
                    self.app_state.name_session_from_first_message_async(
                        session,
                        title_seed,
                        model=model,
                    )
                self._start_stream_response()
                assistant_blocks: list[dict[str, object]] = []
                active_reasoning_index: int | None = None
                streamed_tool_events: list[ToolEvent] = []
                turn_persisted = False

                def raise_if_cancelled() -> None:
                    if self.app_state.is_session_request_cancelled(session, request_id):
                        raise RequestCancelledError()

                def append_text_block(delta: str) -> None:
                    safe_delta = sanitize_text(delta)
                    if not safe_delta:
                        return

                    if assistant_blocks and assistant_blocks[-1].get("kind") == "text":
                        assistant_blocks[-1]["text"] = sanitize_text(
                            f"{assistant_blocks[-1].get('text', '')}{safe_delta}"
                        )
                    else:
                        assistant_blocks.append(
                            {
                                "kind": "text",
                                "text": safe_delta,
                            }
                        )

                def append_text_delta(delta: str) -> None:
                    safe_delta = sanitize_text(delta)
                    if not safe_delta:
                        return

                    append_text_block(safe_delta)
                    self._write_stream_event(
                        {
                            "type": "delta",
                            "kind": "text",
                            "delta": safe_delta,
                        }
                    )

                def handle_reasoning_start() -> None:
                    nonlocal active_reasoning_index
                    raise_if_cancelled()
                    if active_reasoning_index is not None:
                        return

                    assistant_blocks.append(
                        {
                            "kind": "reasoning",
                            "text": "",
                            "status": "streaming",
                        }
                    )
                    active_reasoning_index = len(assistant_blocks) - 1
                    self._write_stream_event({"type": "reasoning_start"})

                def append_reasoning_delta(delta: str) -> None:
                    nonlocal active_reasoning_index
                    safe_delta = sanitize_text(delta)
                    if not safe_delta:
                        return

                    if active_reasoning_index is None:
                        handle_reasoning_start()
                    if active_reasoning_index is None:
                        return

                    block = assistant_blocks[active_reasoning_index]
                    block["text"] = sanitize_text(f"{block.get('text', '')}{safe_delta}")
                    self._write_stream_event(
                        {
                            "type": "delta",
                            "kind": "reasoning",
                            "delta": safe_delta,
                        }
                    )

                def handle_reasoning_done() -> None:
                    nonlocal active_reasoning_index
                    raise_if_cancelled()
                    if active_reasoning_index is None:
                        return

                    assistant_blocks[active_reasoning_index]["status"] = "completed"
                    active_reasoning_index = None
                    self._write_stream_event({"type": "reasoning_done"})

                think_parser = ThinkTagStreamParser(
                    on_text_delta=append_text_delta,
                    on_reasoning_start=handle_reasoning_start,
                    on_reasoning_delta=append_reasoning_delta,
                    on_reasoning_done=handle_reasoning_done,
                )

                def persist_interrupted_turn() -> None:
                    nonlocal active_reasoning_index, turn_persisted
                    if turn_persisted:
                        return

                    if think_parser.buffer:
                        if think_parser.in_reasoning:
                            if active_reasoning_index is None:
                                assistant_blocks.append(
                                    {
                                        "kind": "reasoning",
                                        "text": "",
                                        "status": "streaming",
                                    }
                                )
                                active_reasoning_index = len(assistant_blocks) - 1
                            block = assistant_blocks[active_reasoning_index]
                            block["text"] = sanitize_text(f"{block.get('text', '')}{think_parser.buffer}")
                        else:
                            append_text_block(think_parser.buffer)
                        think_parser.buffer = ""

                    if active_reasoning_index is not None:
                        assistant_blocks[active_reasoning_index]["status"] = "completed"
                        active_reasoning_index = None

                    interrupted_blocks = normalize_message_blocks(assistant_blocks)
                    display_answer = message_blocks_to_text(interrupted_blocks)
                    has_visible_partial = bool(
                        display_answer
                        or message_blocks_have_reasoning(interrupted_blocks)
                        or any(block.get("kind") == "tool" for block in interrupted_blocks)
                    )
                    if not has_visible_partial:
                        return

                    self.app_state.append_turn(
                        session,
                        user_message=message,
                        answer=display_answer,
                        tool_events=streamed_tool_events,
                        assistant_blocks=interrupted_blocks,
                        user_attachments=transcript_attachments,
                    )
                    turn_persisted = True

                def handle_model_start() -> None:
                    raise_if_cancelled()
                    self._write_stream_event({"type": "model_start"})

                def handle_model_done() -> None:
                    raise_if_cancelled()
                    think_parser.finish()
                    self._write_stream_event({"type": "model_done"})

                def handle_text_delta(delta: str) -> None:
                    raise_if_cancelled()
                    think_parser.feed(delta)

                def handle_tool_event(event: ToolEvent) -> None:
                    raise_if_cancelled()
                    streamed_tool_events.append(event)
                    serialized_event = serialize_tool_event(event)
                    assistant_blocks.append(
                        {
                            "kind": "tool",
                            "tool_event": serialized_event,
                        }
                    )
                    self._write_stream_event(
                        {
                            "type": "tool_event",
                            "tool_event": serialized_event,
                        }
                    )

                def handle_round_reset() -> None:
                    raise_if_cancelled()
                    think_parser.finish()
                    self._write_stream_event({"type": "reset"})

                try:
                    answer, tool_events = session.agent.run_turn(
                        message,
                        attachments=agent_attachments,
                        model=model,
                        reasoning_effort=reasoning_effort,
                        on_text_delta=handle_text_delta,
                        on_reasoning_start=handle_reasoning_start,
                        on_reasoning_delta=append_reasoning_delta,
                        on_reasoning_done=handle_reasoning_done,
                        on_model_start=handle_model_start,
                        on_model_done=handle_model_done,
                        on_round_reset=handle_round_reset,
                        on_tool_event=handle_tool_event,
                        check_cancelled=raise_if_cancelled,
                    )
                    raise_if_cancelled()
                    think_parser.finish()
                    tool_events_payload = [serialize_tool_event(event) for event in tool_events]
                    if not assistant_blocks:
                        assistant_blocks = blocks_from_text_and_tools(
                            "assistant",
                            answer,
                            tool_events_payload,
                        )
                    else:
                        assistant_blocks = normalize_message_blocks(assistant_blocks)
                    display_answer = message_blocks_to_text(assistant_blocks)
                    if not display_answer and not message_blocks_have_reasoning(assistant_blocks):
                        display_answer = sanitize_text(answer)
                    self.app_state.append_turn(
                        session,
                        user_message=message,
                        answer=display_answer,
                        tool_events=tool_events,
                        assistant_blocks=assistant_blocks,
                        user_attachments=transcript_attachments,
                    )
                    turn_persisted = True
                    self._write_stream_event(
                        {
                            "type": "done",
                            "answer": display_answer,
                            "tool_events": tool_events_payload,
                            "blocks": assistant_blocks,
                            "session": self.app_state.session_payload(session),
                            **self.app_state.sidebar_payload(),
                        }
                    )
                except (ClientDisconnectedError, RequestCancelledError):
                    persist_interrupted_turn()
                except Exception as exc:  # noqa: BLE001
                    try:
                        self._write_stream_event(
                            {
                                "type": "error",
                                "error": sanitize_text(str(exc) or "服务异常"),
                            }
                        )
                    except ClientDisconnectedError:
                        pass
                finally:
                    self.app_state.release_session_request(session, "main", request_id)
                return

            if parsed.path == "/api/send-message":
                session = self.app_state.get_session(payload.get("session_id"))
                message = sanitize_text(payload.get("message", "")).strip()
                transcript_attachments, agent_attachments = persist_request_attachments(payload.get("attachments"))
                if not message and not transcript_attachments:
                    raise ValueError("message is required")

                model = sanitize_text(payload.get("model", "")).strip() or None
                reasoning_effort = sanitize_text(payload.get("reasoning_effort", "")).strip() or None
                if reasoning_effort in {"default", "none"}:
                    reasoning_effort = None

                title_seed = message or sanitize_text(transcript_attachments[0].get("name") or "")
                should_name_session = self.app_state.should_name_session_from_first_message(session)
                request_id = self.app_state.acquire_session_request(session, "main")
                if should_name_session:
                    self.app_state.name_session_from_first_message_async(
                        session,
                        title_seed,
                        model=model,
                    )
                try:
                    answer, tool_events = session.agent.run_turn(
                        message,
                        attachments=agent_attachments,
                        model=model,
                        reasoning_effort=reasoning_effort,
                    )
                    tool_events_payload = [serialize_tool_event(event) for event in tool_events]
                    assistant_blocks = blocks_from_text_and_tools(
                        "assistant",
                        answer,
                        tool_events_payload,
                    )
                    display_answer = message_blocks_to_text(assistant_blocks)
                    if not display_answer and not message_blocks_have_reasoning(assistant_blocks):
                        display_answer = sanitize_text(answer)
                    self.app_state.append_turn(
                        session,
                        user_message=message,
                        answer=display_answer,
                        tool_events=tool_events,
                        assistant_blocks=assistant_blocks,
                        user_attachments=transcript_attachments,
                    )
                    self._send_json(
                        {
                            "answer": display_answer,
                            "tool_events": tool_events_payload,
                            "blocks": assistant_blocks,
                            "session": self.app_state.session_payload(session),
                            **self.app_state.sidebar_payload(),
                        }
                    )
                finally:
                    self.app_state.release_session_request(session, "main", request_id)
                return

            self._send_error_json(HTTPStatus.NOT_FOUND, "route not found")
        except ValueError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, sanitize_text(str(exc) or "服务异常"))

    def _serve_static(self, request_path: str) -> None:
        normalized_path = request_path or "/"
        if normalized_path in {"/", "/hash.html"}:
            file_path = DEFAULT_PAGE
        elif normalized_path in {"/react", "/react/", "/react/index.html"}:
            file_path = self._resolve_react_asset("index.html")
            if file_path is None:
                return
        elif normalized_path.startswith("/react/"):
            react_relative_path = normalized_path.removeprefix("/react/")
            file_path = self._resolve_react_asset(react_relative_path)
            if file_path is None:
                return
        elif normalized_path.startswith(f"/{ATTACHMENTS_ROUTE}/"):
            file_path = resolve_attachment_file_path(normalized_path)
            if file_path is None:
                self._send_error_json(HTTPStatus.FORBIDDEN, "不允许访问该路径")
                return
        else:
            relative_path = normalized_path.lstrip("/")
            file_path = (REPO_ROOT / relative_path).resolve()
            if REPO_ROOT not in file_path.parents and file_path != REPO_ROOT:
                self._send_error_json(HTTPStatus.FORBIDDEN, "不允许访问该路径")
                return

        if not file_path.exists() or not file_path.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, "file not found")
            return

        content = file_path.read_bytes()
        mime_type = mimetypes.guess_type(file_path.name)[0] or "text/plain; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _resolve_react_asset(self, relative_path: str) -> Path | None:
        if not REACT_DIST_DIR.exists():
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                "React build not found. Run npm run build:react first.",
            )
            return None

        safe_relative_path = relative_path.strip("/") or "index.html"
        candidate = (REACT_DIST_DIR / safe_relative_path).resolve()
        if REACT_DIST_DIR not in candidate.parents and candidate != REACT_DIST_DIR:
            self._send_error_json(HTTPStatus.FORBIDDEN, "Forbidden path")
            return None

        if candidate.exists() and candidate.is_file():
            return candidate

        fallback_index = REACT_DIST_DIR / "index.html"
        if not Path(safe_relative_path).suffix and fallback_index.exists():
            return fallback_index

        self._send_error_json(HTTPStatus.NOT_FOUND, "React asset not found")
        return None

    def _start_stream_response(self) -> None:
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def _write_stream_event(self, payload: dict[str, object]) -> None:
        body = f"{json.dumps(payload, ensure_ascii=False)}\n".encode("utf-8")
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientDisconnectedError() from exc

    def _read_json_body(self) -> dict[str, object]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length 非法") from exc

        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("请求体不是合法 JSON") from exc

        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def _send_json(self, payload: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": sanitize_text(message)}, status=status)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return


class HashHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], app_state: AppState) -> None:
        super().__init__(server_address, HashHTTPRequestHandler)
        self.app_state = app_state


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    settings = load_settings()
    port = int(os.getenv("HASH_WEB_PORT", "8765"))
    host = os.getenv("HASH_WEB_HOST", "127.0.0.1")
    app_state = AppState(settings)
    server = HashHTTPServer((host, port), app_state)

    print(f"hash-code web ready: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
