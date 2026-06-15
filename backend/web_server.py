from __future__ import annotations

import simple_agent.config as _config
import backend.web_constants as _constants
import backend.web_context as _context
import backend.web_runtime as _runtime
import backend.web_state as _state

from backend.web_handler import main

CODEX_LOCAL_SESSIONS_DIR = _constants.CODEX_LOCAL_SESSIONS_DIR
PROXY_STATE_FILE = _constants.PROXY_STATE_FILE
CONTEXT_EDIT_MARKERS_FILE = _constants.CONTEXT_EDIT_MARKERS_FILE
CODEX_PROXY_BASE_URL = _config.CODEX_PROXY_BASE_URL


def _sync_compat_globals() -> None:
    _constants.CODEX_LOCAL_SESSIONS_DIR = CODEX_LOCAL_SESSIONS_DIR
    _context.CODEX_LOCAL_SESSIONS_DIR = CODEX_LOCAL_SESSIONS_DIR
    _runtime.CODEX_LOCAL_SESSIONS_DIR = CODEX_LOCAL_SESSIONS_DIR

    _constants.PROXY_STATE_FILE = PROXY_STATE_FILE
    _context.PROXY_STATE_FILE = PROXY_STATE_FILE
    _runtime.PROXY_STATE_FILE = PROXY_STATE_FILE

    _constants.CONTEXT_EDIT_MARKERS_FILE = CONTEXT_EDIT_MARKERS_FILE
    _context.CONTEXT_EDIT_MARKERS_FILE = CONTEXT_EDIT_MARKERS_FILE

    _config.CODEX_PROXY_BASE_URL = CODEX_PROXY_BASE_URL
    _runtime.CODEX_PROXY_BASE_URL = CODEX_PROXY_BASE_URL


def __getattr__(name: str):
    for module in (_constants, _context, _runtime, _state):
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module 'backend.web_server' has no attribute {name!r}")


def codex_local_session_transcript(session_id: str):  # type: ignore[no-untyped-def]
    _sync_compat_globals()
    return _context.codex_local_session_transcript(session_id)


def latest_proxy_instruction_prefix_records():  # type: ignore[no-untyped-def]
    _sync_compat_globals()
    return _context.latest_proxy_instruction_prefix_records()


def proxy_state_sqlite_file():  # type: ignore[no-untyped-def]
    _sync_compat_globals()
    return _context.proxy_state_sqlite_file()


def sync_proxy_session_override_if_known(session, transcript):  # type: ignore[no-untyped-def]
    _sync_compat_globals()
    return _runtime.sync_proxy_session_override_if_known(session, transcript)


if __name__ == "__main__":
    main()
