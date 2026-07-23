from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.codex_item_registry import CODEX_ITEM_REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[1]
REACT_DIST_DIR = REPO_ROOT / "react_app" / "dist"
DEFAULT_PAGE = REACT_DIST_DIR / "index.html"
DEFAULT_DATA_DIR = Path.home() / ".codex-context-studio" / "shared"
RAW_STATE_DIR = Path(os.getenv("CODEX_CONTEXT_STUDIO_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
STATE_DIR = RAW_STATE_DIR if RAW_STATE_DIR.is_absolute() else (REPO_ROOT / RAW_STATE_DIR).resolve()
STATE_FILE = STATE_DIR / "codex_context_studio_web_state.json"
CODEX_LOCAL_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CONTEXT_REQUEST_DEBUG_FILE = STATE_DIR / "context_request_debug.ndjson"
CONTEXT_EDIT_MARKERS_FILE = STATE_DIR / "context_edit_markers.json"
ATTACHMENTS_DIR = STATE_DIR / "uploads"
ATTACHMENTS_ROUTE = "uploads"
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
CODEX_PAIRED_TOOL_CALL_ITEM_TYPES = set(CODEX_ITEM_REGISTRY.paired_tool_call_item_types)
CODEX_STANDALONE_TOOL_CALL_ITEM_TYPES = set(CODEX_ITEM_REGISTRY.standalone_tool_call_item_types)
CODEX_TOOL_CALL_ITEM_TYPES = set(CODEX_ITEM_REGISTRY.tool_call_item_types)
CODEX_TOOL_OUTPUT_ITEM_TYPES = set(CODEX_ITEM_REGISTRY.tool_output_item_types)
CODEX_TOOL_OUTPUT_TYPES_BY_CALL_TYPE = {
    call_type: set(output_types)
    for call_type, output_types in CODEX_ITEM_REGISTRY.tool_output_types_by_call_type.items()
}
CODEX_COMPACTION_ITEM_TYPES = set(CODEX_ITEM_REGISTRY.compaction_item_types)
CODEX_ITEM_DISPLAY_HINTS_BY_ITEM_TYPE = {
    item_type: dict(hint)
    for item_type, hint in CODEX_ITEM_REGISTRY.display_hints_by_item_type.items()
}
PROVIDER_MODEL_TYPES = {"chat_completion", "responses", "gemini", "claude"}

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


class ClientDisconnectedError(BrokenPipeError):
    """Raised when the front-end intentionally closes a stream early."""


class RequestCancelledError(RuntimeError):
    """Raised when the user explicitly stops the active request."""


@dataclass(slots=True)
class ActiveRequestControl:
    """Owns cancellation and completion for one in-flight session request."""

    request_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    completed_event: threading.Event = field(default_factory=threading.Event)
    _callback_lock: threading.Lock = field(default_factory=threading.Lock)
    _cancel_callback: Callable[[], None] | None = None
    _commit_started: bool = False

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def register_cancel_callback(self, callback: Callable[[], None] | None) -> None:
        invoke_now = False
        with self._callback_lock:
            if callback is None:
                self._cancel_callback = None
                return
            if self.cancel_event.is_set() or self.completed_event.is_set():
                invoke_now = True
            else:
                self._cancel_callback = callback
        if invoke_now:
            try:
                callback()
            except Exception:  # noqa: BLE001 - transport close failures do not undo cancellation
                pass

    def cancel(self) -> bool:
        callback: Callable[[], None] | None = None
        with self._callback_lock:
            if self._commit_started or self.completed_event.is_set():
                return False
            self.cancel_event.set()
            callback = self._cancel_callback
            self._cancel_callback = None
        if callback is not None:
            try:
                callback()
            except Exception:  # noqa: BLE001 - the cancel flag remains authoritative
                pass
        return True

    def try_begin_commit(self) -> bool:
        with self._callback_lock:
            if self.cancel_event.is_set() or self.completed_event.is_set():
                return False
            self._commit_started = True
            self._cancel_callback = None
            return True

    def mark_completed(self) -> None:
        with self._callback_lock:
            self._cancel_callback = None
            self.completed_event.set()


@dataclass(slots=True)
class SessionState:
    session_id: str
    title: str
    transcript: list[dict[str, object]]
    context_workbench_history: list[dict[str, object]]
    node_locks: dict[str, bool] = field(default_factory=dict)
    node_lock_revision: int = 0
    main_turn_id: str = ""
    main_turn_started_at: str = ""
    main_turn_updated_at: str = ""
    active_request_mode: str | None = None
    active_request_id: str | None = None
    active_request_control: ActiveRequestControl | None = None

@dataclass(slots=True)
class ContextWorkbenchToolDefinition:
    name: str
    label: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]

    def to_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
