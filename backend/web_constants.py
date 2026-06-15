from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simple_agent.agent import SimpleAgent

REPO_ROOT = Path(__file__).resolve().parents[1]
REACT_DIST_DIR = REPO_ROOT / "react_app" / "dist"
DEFAULT_PAGE = REACT_DIST_DIR / "index.html"
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
    agent: SimpleAgent | None
    transcript: list[dict[str, object]]
    context_workbench_history: list[dict[str, str]]
    context_revisions: list[dict[str, object]]
    pending_context_restore: dict[str, object] | None
    active_request_mode: str | None = None
    active_request_id: str | None = None
    active_cancel_event: threading.Event | None = None
    agent_hydrated: bool = True


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
    handler: Callable[[dict[str, Any]], Any]

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
