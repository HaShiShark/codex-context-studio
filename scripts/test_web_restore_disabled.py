from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _settings(project_root: Path):
    from simple_agent.config import (
        CODEX_PROXY_PROVIDER_ID,
        DEFAULT_CODEX_PROXY_MODELS,
        DEFAULT_RESPONSE_PROVIDERS,
        Settings,
    )

    providers = [dict(provider) for provider in DEFAULT_RESPONSE_PROVIDERS]
    for provider in providers:
        if provider.get("id") == CODEX_PROXY_PROVIDER_ID:
            provider["models"] = [dict(model) for model in DEFAULT_CODEX_PROXY_MODELS]

    return Settings(
        model="gpt-5.4-mini",
        default_reasoning_effort="default",
        context_workbench_model="gpt-5.5",
        context_workbench_provider_id=CODEX_PROXY_PROVIDER_ID,
        project_root=project_root,
        max_tool_rounds=4,
        tool_settings=[],
        response_providers=providers,
        active_provider_id="openai",
    )


def _assert_bootstrap_has_no_restore_fields(payload: dict[str, object]) -> None:
    forbidden_keys = {
        "context_revision_histories",
        "pending_context_restores",
    }
    leaked_keys = sorted(forbidden_keys.intersection(payload))
    if leaked_keys:
        raise AssertionError(f"bootstrap payload leaked restore/revision keys: {leaked_keys}")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="hash-web-restore-disabled-") as raw_tmp_dir:
        tmp_dir = Path(raw_tmp_dir)
        os.environ["HASH_DATA_DIR"] = str(tmp_dir / "state")

        from backend.web_handler import HashHTTPRequestHandler
        from backend.web_state import AppState

        app_state = AppState(_settings(tmp_dir))
        session = app_state.create_session()

        bootstrap = app_state.bootstrap_payload(session.session_id)
        _assert_bootstrap_has_no_restore_fields(bootstrap)
        conversation = app_state.apply_context_workbench_mutation(
            session,
            transcript=[
                {
                    "role": "user",
                    "text": "keep draft/commit semantics",
                    "attachments": [],
                    "toolEvents": [],
                    "blocks": [{"kind": "text", "text": "keep draft/commit semantics"}],
                    "providerItems": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": "keep draft/commit semantics",
                        }
                    ],
                }
            ],
        )
        if not conversation:
            raise AssertionError("mutation did not commit transcript")
        if hasattr(session, "context_revisions") or hasattr(session, "pending_context_restore"):
            raise AssertionError("session model still exposes revision/restore state")

        bootstrap_after_mutation = app_state.bootstrap_payload(session.session_id)
        _assert_bootstrap_has_no_restore_fields(bootstrap_after_mutation)

        session_dir = next((tmp_dir / "state" / "sessions").glob("*"))
        forbidden_files = {"revisions.jsonl", "restore.json", "transcript.jsonl", "transcript_tail.json"}
        written_forbidden = sorted(path.name for path in session_dir.iterdir() if path.name in forbidden_files)
        if written_forbidden:
            raise AssertionError(f"restore/revision files were written: {written_forbidden}")
        if not (session_dir / "workbench.jsonl").exists():
            raise AssertionError("workbench history file was not written")

        routes = HashHTTPRequestHandler.__new__(HashHTTPRequestHandler)._post_routes()
        stale_routes = sorted(
            {
                "/api/context-restore",
                "/api/context-undo-restore",
                "/api/proxy-session-reset",
            }.intersection(routes)
        )
        if stale_routes:
            raise AssertionError(f"stale routes are still registered: {stale_routes}")
        if hasattr(HashHTTPRequestHandler, "_handle_context_restore_post"):
            raise AssertionError("context restore handler still exists")
        if hasattr(HashHTTPRequestHandler, "_handle_context_undo_restore_post"):
            raise AssertionError("context undo restore handler still exists")
        if hasattr(HashHTTPRequestHandler, "_handle_proxy_session_reset_post"):
            raise AssertionError("proxy session reset handler still exists")

    print("web restore/revision disabled checks passed")


if __name__ == "__main__":
    main()
