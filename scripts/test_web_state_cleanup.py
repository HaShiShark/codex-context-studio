from __future__ import annotations

import json
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


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="hash-web-state-cleanup-") as raw_tmp_dir:
        tmp_dir = Path(raw_tmp_dir)
        state_dir = tmp_dir / "state"
        state_dir.mkdir(parents=True)
        os.environ["HASH_DATA_DIR"] = str(state_dir)

        state_file = state_dir / "hash_web_state.json"
        state_file.write_text(
            json.dumps(
                {
                    "projects": [{"id": "legacy-project"}],
                    "chat_session_ids": ["legacy-project-session"],
                    "sessions": {
                        "legacy-project-session": {
                            "title": "Legacy project session",
                            "scope": "project",
                            "project_id": "missing-project",
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        from backend import web_constants, web_state
        from backend.proxy_routes_support import is_title_generation_request
        from backend.web_constants import NEW_SESSION_TITLE
        from backend.web_handler import HashHTTPRequestHandler
        from backend.web_state import AppState

        app_state = AppState(_settings(tmp_dir))
        bootstrap = app_state.bootstrap_payload("legacy-project-session")
        legacy_session = app_state.get_session("legacy-project-session")

        if legacy_session.title != "Legacy project session":
            raise AssertionError("legacy session metadata was not loaded")
        for forbidden_key in ("projects", "chat_session_ids", "sessions"):
            if forbidden_key in bootstrap:
                raise AssertionError(f"bootstrap payload leaked old state key: {forbidden_key}")
        if hasattr(app_state, "projects") or hasattr(app_state, "chat_session_ids"):
            raise AssertionError("old project/local chat state should not exist")

        proxy_session = app_state.upsert_proxy_session(
            session_id="proxy-without-title",
            title="",
            transcript=[],
        )
        if proxy_session.title != NEW_SESSION_TITLE:
            raise AssertionError("empty proxy title should use the neutral new-session placeholder")

        for attribute in (
            "create_project",
            "pin_project",
            "rename_project",
            "archive_project_sessions",
            "delete_project",
        ):
            if hasattr(app_state, attribute):
                raise AssertionError(f"project management method still exists: {attribute}")

        for attribute in (
            "create_session",
            "reset_session",
            "truncate_session",
            "delete_transcript_message",
            "delete_session",
            "append_turn",
            "ensure_agent_hydrated",
            "session_payload",
        ):
            if hasattr(app_state, attribute):
                raise AssertionError(f"old local chat method still exists: {attribute}")

        for attribute in (
            "rename_session_from_message",
            "should_name_session_from_first_message",
            "name_session_from_first_message",
            "name_session_from_first_message_async",
        ):
            if hasattr(app_state, attribute):
                raise AssertionError(f"old title method still exists: {attribute}")

        for attribute in (
            "summarize_title",
            "clean_generated_title",
            "generate_session_title",
        ):
            if hasattr(web_state, attribute):
                raise AssertionError(f"old title helper still exists: {attribute}")

        for attribute in ("DEFAULT_PROJECT_ID", "TITLE_GENERATION_INSTRUCTIONS"):
            if hasattr(web_constants, attribute):
                raise AssertionError(f"old constant still exists: {attribute}")
        for attribute in ("ProjectState", "NEW_PROJECT_PREFIX"):
            if hasattr(web_constants, attribute):
                raise AssertionError(f"project management constant still exists: {attribute}")

        routes = HashHTTPRequestHandler.__new__(HashHTTPRequestHandler)._post_routes()
        stale_routes = sorted(
            {
                "/api/sessions",
                "/api/reset",
                "/api/truncate-session",
                "/api/delete-message",
                "/api/send-message",
                "/api/send-message-stream",
                "/api/delete-session",
            }.intersection(routes)
        )
        if stale_routes:
            raise AssertionError(f"old local chat routes are still registered: {stale_routes}")

        saved_state = json.loads(state_file.read_text(encoding="utf-8"))
        for forbidden_key in ("projects", "chat_session_ids"):
            if forbidden_key in saved_state:
                raise AssertionError(f"saved state persisted old key: {forbidden_key}")
        for session_item in saved_state.get("sessions", {}).values():
            if "scope" in session_item or "project_id" in session_item:
                raise AssertionError("saved sessions should not persist project scope fields")

        codex_title_body = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": "\n".join(
                        [
                            "You are a helpful assistant. You will be presented with a user prompt",
                            "provide a short title for a task that will be created from that prompt",
                            "generate a concise ui title",
                            "fill the structured title field with plain text",
                            "User Prompt: explain the proxy",
                        ]
                    ),
                }
            ]
        }
        if not is_title_generation_request(codex_title_body):
            raise AssertionError("Codex UI title generation should remain passthrough")

        old_hash_context_title_body = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": "Generate a local Hash Context chat title from the first user message.",
                }
            ]
        }
        if is_title_generation_request(old_hash_context_title_body):
            raise AssertionError("old Hash Context local title prompt should not be special-cased")

    print("web state cleanup checks passed")


if __name__ == "__main__":
    main()
