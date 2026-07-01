from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from simple_agent.agent import SimpleAgent, ToolEvent, sanitize_text
from simple_agent.config import Settings

from backend.web_constants import (
    ATTACHMENTS_DIR,
    ATTACHMENTS_ROUTE,
    DEFAULT_PROJECT_ID,
    HIDDEN_WORKSPACE_ENTRIES,
    NEW_PROJECT_PREFIX,
    NEW_SESSION_TITLE,
    ProjectState,
    REPO_ROOT,
    SessionState,
    STATE_DIR,
    STATE_FILE,
)
from backend.transcript_codec import (
    append_input_items as core_append_input_items,
    transcript_to_input_items as core_transcript_to_input_items,
)

from backend.web_context import (
    _find_tag,
    _safe_emit_split,
    active_provider_models,
    append_jsonl_state_event,
    build_provider_items_for_record,
    message_blocks_to_text,
    model_options,
    normalize_attachment_records,
    normalize_context_chat_history,
    normalize_message_blocks,
    normalize_transcript,
    read_jsonl_state_file,
    sanitize_value,
    serialize_tool_event,
    settings_payload,
    utc_timestamp,
)

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

class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lock = threading.Lock()
        self.projects: list[ProjectState] = []
        self.chat_session_ids: list[str] = []
        self.sessions: dict[str, SessionState] = {}
        self._persisted_session_payloads: dict[str, dict[str, Any]] = {}
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
            )
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
            created_session = False
            if session is None:
                session = SessionState(
                    session_id=safe_session_id,
                    title=sanitize_text(title or "").strip() or "Codex Context",
                    scope="chat",
                    project_id=None,
                    agent=SimpleAgent(self._settings_for_project_locked(None)),
                    transcript=[],
                    context_workbench_history=[],
                )
                self.sessions[safe_session_id] = session
                self._insert_session_locked(session)
                created_session = True

            active_mode = sanitize_text(session.active_request_mode or "").strip()
            active_request_id = sanitize_text(session.active_request_id or "").strip()
            next_transcript = normalize_transcript(transcript)
            next_title = sanitize_text(title or "").strip() or session.title or "Codex Context"
            should_persist = created_session

            if session.title != next_title:
                session.title = next_title
                should_persist = True
            if session.scope != "chat":
                session.scope = "chat"
                should_persist = True
            if session.project_id is not None:
                session.project_id = None
                should_persist = True
            if active_mode != "context":
                transcript_changed = next_transcript != normalize_transcript(session.transcript)
                if transcript_changed:
                    session.transcript = next_transcript
                    should_persist = True
            if active_mode != "context":
                if is_running:
                    if active_mode != "main" or active_request_id != "proxy-running":
                        session.active_request_mode = "main"
                        session.active_request_id = "proxy-running"
                        session.active_cancel_event = threading.Event()
                        should_persist = True
                elif active_mode == "main" and active_request_id == "proxy-running":
                    session.active_request_mode = None
                    session.active_request_id = None
                    session.active_cancel_event = None
                    should_persist = True
            if should_persist:
                self._hydrate_agent_locked(session)
                self._remove_session_from_lists_locked(session.session_id)
                self._insert_session_locked(session)
                self._save_state_locked()
            return session

    def reset_session(self, session_id: str) -> SessionState:
        session = self.get_session(session_id)
        with self.lock:
            if session.agent is not None:
                session.agent.reset()
            session.agent_hydrated = True
            session.title = NEW_SESSION_TITLE
            session.transcript = []
            session.context_workbench_history = []
            self._save_state_locked()
        return session

    def truncate_session(self, session_id: str, from_index: int) -> SessionState:
        session = self.get_session(session_id)
        with self.lock:
            safe_index = max(0, min(from_index, len(session.transcript)))
            session.transcript = session.transcript[:safe_index]
            session.context_workbench_history = []
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
                raise ValueError("当前没有可删除的手动消息")

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
            self._hydrate_agent_locked(session)
            self._save_state_locked()
            return sanitize_value(session.transcript)

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
            safe_user_message = sanitize_text(user_message)
            safe_user_attachments = normalize_attachment_records(user_attachments)
            user_input_items = build_provider_items_for_record(
                role="user",
                text=safe_user_message,
                attachments=safe_user_attachments,
                tool_events=[],
                blocks=[{"kind": "text", "text": safe_user_message}] if safe_user_message else [],
                record_index=len(session.transcript),
            )
            safe_assistant_blocks = sanitize_value(assistant_blocks or [])
            assistant_text = message_blocks_to_text(safe_assistant_blocks) or sanitize_text(answer)
            assistant_tool_events = [serialize_tool_event(event) for event in tool_events]
            assistant_input_items = build_provider_items_for_record(
                role="assistant",
                text=assistant_text,
                attachments=[],
                tool_events=assistant_tool_events,
                blocks=safe_assistant_blocks,
                record_index=len(session.transcript) + 1,
            )
            session.transcript = normalize_transcript(session.transcript)
            core_append_input_items(session.transcript, [*user_input_items, *assistant_input_items])
            self._hydrate_agent_locked(session)
            self._remove_session_from_lists_locked(session.session_id)
            self._insert_session_locked(session)
            self._save_state_locked()

    def bootstrap_payload(self, session_id: str = "", include_conversation: bool = True) -> dict[str, object]:
        with self.lock:
            self._ensure_default_project_locked()
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
                "project_name": self.settings.project_root.name or str(self.settings.project_root),
                "project_root": str(self.settings.project_root),
                "default_model": self.settings.model,
                "models": model_options(self.settings.model, active_provider_models(self.settings)),
                "reasoning_options": DEFAULT_REASONING_OPTIONS,
                "settings": settings_payload(self.settings),
                "projects": self._projects_payload_locked(),
                "chat_sessions": self._chat_sessions_payload_locked(),
                "conversations": conversations,
                "context_workbench_histories": context_workbench_histories,
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

    def _append_session_list_delta(
        self,
        path: Path,
        previous: list[Any],
        current: list[Any],
    ) -> None:
        if previous == current:
            return
        if (
            isinstance(previous, list)
            and isinstance(current, list)
            and len(current) >= len(previous)
            and current[: len(previous)] == previous
        ):
            appended = current[len(previous) :]
            if appended:
                append_jsonl_state_event(
                    path,
                    {"type": "append", "created_at": utc_timestamp(), "records": appended},
                )
            return
        append_jsonl_state_event(
            path,
            {"type": "set", "created_at": utc_timestamp(), "records": current},
        )

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
                session_payload = self._load_session_payload(safe_session_id)
                session = SessionState(
                    session_id=safe_session_id,
                    title=sanitize_text(item.get("title") or NEW_SESSION_TITLE).strip() or NEW_SESSION_TITLE,
                    scope=scope,
                    project_id=project_id if scope == "project" else None,
                    agent=None,
                    transcript=session_payload["transcript"],
                    context_workbench_history=session_payload["context_workbench_history"],
                    agent_hydrated=False,
                )
                baseline_payload = copy.deepcopy(session_payload)
                self._persisted_session_payloads[safe_session_id] = baseline_payload
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
        for session in self.sessions.values():
            self._persist_session_payload(session)
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
                    "session_dir": self._session_storage_dir(session_id).relative_to(STATE_DIR).as_posix(),
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

    def _agent_locked(self, session: SessionState) -> SimpleAgent:
        if session.agent is None:
            session.agent = SimpleAgent(self._settings_for_session_locked(session))
            session.agent_hydrated = False
        return session.agent

    def _hydrate_agent_locked(self, session: SessionState) -> None:
        agent = self._agent_locked(session)
        agent.reset()
        normalized_transcript = normalize_transcript(session.transcript)
        session.transcript = normalized_transcript
        agent.history = core_transcript_to_input_items(normalized_transcript)
        session.agent_hydrated = True

    def ensure_agent_hydrated(self, session: SessionState) -> SimpleAgent:
        with self.lock:
            if not session.agent_hydrated:
                self._hydrate_agent_locked(session)
            return self._agent_locked(session)

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

