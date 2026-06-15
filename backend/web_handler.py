from __future__ import annotations

import json
import mimetypes
import os
from collections.abc import Callable
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from dotenv import load_dotenv
from simple_agent.agent import ToolEvent, sanitize_text, sanitize_value
from simple_agent.config import CODEX_PROXY_PROVIDER_ID, load_settings, save_settings, _UNSET

from backend.web_constants import (
    ATTACHMENTS_ROUTE,
    CONTEXT_REQUEST_DEBUG_FILE,
    DEFAULT_PAGE,
    REACT_DIST_DIR,
    REPO_ROOT,
    ClientDisconnectedError,
    RequestCancelledError,
)

from backend.web_state import AppState, resolve_attachment_file_path

from backend.web_runtime import (
    active_context_revision_marker,
    build_context_chat_response_payload,
    clone_provider_settings_payloads,
    codex_proxy_session_exists,
    context_workbench_models_payload,
    context_workbench_provider_payloads,
    context_workbench_settings_payload,
    fetch_models_from_provider,
    get_codex_proxy_control_json,
    normalize_provider_type,
    post_codex_proxy_control_json,
    refresh_session_from_proxy_active_context_if_known,
    persist_request_attachments,
    run_context_chat_turn,
    safe_sync_proxy_session_override_if_known,
)

from backend.web_context import (
    ContextWorkbenchToolRegistry,
    ThinkTagStreamParser,
    active_provider_models,
    blocks_from_text_and_tools,
    consume_context_edit_marker,
    codex_local_session_transcript,
    context_pending_restore_payload,
    context_revision_summaries,
    context_workbench_suggestions_payload,
    editable_context_node_count,
    message_blocks_have_reasoning,
    message_blocks_to_text,
    model_options,
    normalize_message_blocks,
    normalize_selected_node_indexes,
    normalize_transcript,
    serialize_tool_event,
    settings_payload,
    transcript_has_conversation_records,
    write_context_edit_marker,
)

from backend.web_state import list_workspace_entries

class HashHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "HashCodeWeb/0.2"

    @property
    def app_state(self) -> AppState:
        return self.server.app_state  # type: ignore[attr-defined]

    def _get_proxy_control_json_or_none(self, path: str) -> dict[str, Any] | None:
        try:
            return get_codex_proxy_control_json(path)
        except ValueError:
            return None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        handler = self._get_routes().get(parsed.path)
        if handler is not None:
            handler(parsed)
            return

        if parsed.path == "/api/proxy/sessions":
            proxy_payload = self._get_proxy_control_json_or_none("/api/proxy/sessions")
            self._send_json(proxy_payload or {"active_session_id": "", "sessions": []})
            return

        if parsed.path == "/api/proxy/usage":
            proxy_payload = self._get_proxy_control_json_or_none("/api/proxy/usage")
            self._send_json(proxy_payload or {"overall": {}, "sessions": {}})
            return

        if parsed.path.startswith("/api/proxy/sessions/") and parsed.path.endswith("/usage"):
            session_id = quote(parsed.path.split("/api/proxy/sessions/", 1)[1].rsplit("/", 1)[0], safe="")
            proxy_payload = self._get_proxy_control_json_or_none(f"/api/proxy/sessions/{session_id}/usage")
            self._send_json(proxy_payload or {"summary": {}})
            return

        if parsed.path.startswith("/api/proxy/sessions/"):
            session_id = quote(parsed.path.split("/api/proxy/sessions/", 1)[1].split("/", 1)[0], safe="")
            proxy_payload = self._get_proxy_control_json_or_none(f"/api/proxy/sessions/{session_id}")
            if proxy_payload is None:
                self._send_json({"error": "session not found"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(proxy_payload)
            return

        self._serve_static(parsed.path)

    def _get_routes(self) -> dict[str, Callable[[Any], None]]:
        return {
            "/api/health": self._handle_health_get,
            "/api/init": self._handle_init_get,
            "/api/settings": self._handle_settings_get,
            "/api/context-workbench-settings": self._handle_context_workbench_settings_get,
            "/api/workspace": self._handle_workspace_get,
        }

    def _handle_health_get(self, _parsed: Any) -> None:
        self._send_json({"ok": True})

    def _handle_init_get(self, parsed: Any) -> None:
        query = parse_qs(parsed.query)
        session_id = sanitize_text((query.get("session_id") or [""])[0]).strip()
        include_conversation = sanitize_text((query.get("include_conversation") or ["1"])[0]).strip() not in {
            "0",
            "false",
            "no",
        }
        self._send_json(
            self.app_state.bootstrap_payload(
                session_id=session_id,
                include_conversation=include_conversation,
            )
        )

    def _handle_settings_get(self, _parsed: Any) -> None:
        self._send_json(
            {
                "settings": settings_payload(self.app_state.settings),
                "models": model_options(self.app_state.settings.model, active_provider_models(self.app_state.settings)),
            }
        )

    def _handle_context_workbench_settings_get(self, parsed: Any) -> None:
        query = parse_qs(parsed.query)
        refresh_models = sanitize_text((query.get("refresh_models") or ["0"])[0]).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        provider_payloads = context_workbench_provider_payloads(
            self.app_state.settings,
            refresh_codex_proxy_models=refresh_models,
        )
        self._send_json(
            {
                "settings": context_workbench_settings_payload(self.app_state.settings),
                "models": context_workbench_models_payload(self.app_state.settings, provider_payloads),
                "response_providers": provider_payloads,
                "tool_catalog": ContextWorkbenchToolRegistry.tool_catalog(),
            }
        )

    def _handle_workspace_get(self, _parsed: Any) -> None:
        self._send_json(
            {
                "entries": list_workspace_entries(self.app_state.settings.project_root),
            }
        )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json_body()
            handler = self._post_routes().get(parsed.path)
            if handler is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "route not found")
                return
            handler(payload)
        except ValueError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, sanitize_text(str(exc) or "服务异常"))

    def _post_routes(self) -> dict[str, Callable[[dict[str, object]], None]]:
        return {
            "/api/projects": self._handle_projects_post,
            "/api/pin-project": self._handle_pin_project_post,
            "/api/rename-project": self._handle_rename_project_post,
            "/api/archive-project-sessions": self._handle_archive_project_sessions_post,
            "/api/sessions": self._handle_sessions_post,
            "/api/proxy-sync-session": self._handle_proxy_sync_session_post,
            "/api/codex-local-session-sync": self._handle_codex_local_session_sync_post,
            "/api/context-edit-marker-consume": self._handle_context_edit_marker_consume_post,
            "/api/reset": self._handle_reset_post,
            "/api/truncate-session": self._handle_truncate_session_post,
            "/api/delete-message": self._handle_delete_message_post,
            "/api/settings": self._handle_settings_post,
            "/api/provider-model-candidates": self._handle_provider_model_candidates_post,
            "/api/provider-models": self._handle_provider_models_post,
            "/api/context-workbench-settings": self._handle_context_workbench_settings_post,
            "/api/proxy-session-override": self._handle_proxy_session_override_post,
            "/api/proxy-session-reset": self._handle_proxy_session_reset_post,
            "/api/proxy-session-usage-reset": self._handle_proxy_session_usage_reset_post,
            "/api/context-workbench-suggestions": self._handle_context_workbench_suggestions_post,
            "/api/delete-session": self._handle_delete_session_post,
            "/api/delete-project": self._handle_delete_project_post,
            "/api/cancel-request": self._handle_cancel_request_post,
            "/api/context-chat": self._handle_context_chat_post,
            "/api/context-chat-stream": self._handle_context_chat_stream_post,
            "/api/context-restore": self._handle_context_restore_post,
            "/api/context-workbench-history-message-delete": self._handle_context_workbench_history_message_delete_post,
            "/api/context-workbench-history-clear": self._handle_context_workbench_history_clear_post,
            "/api/context-undo-restore": self._handle_context_undo_restore_post,
            "/api/send-message-stream": self._handle_send_message_stream_post,
            "/api/send-message": self._handle_send_message_post,
        }

    def _handle_projects_post(self, payload: dict[str, object]) -> None:
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

    def _handle_pin_project_post(self, payload: dict[str, object]) -> None:
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

    def _handle_rename_project_post(self, payload: dict[str, object]) -> None:
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

    def _handle_archive_project_sessions_post(self, payload: dict[str, object]) -> None:
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

    def _handle_sessions_post(self, payload: dict[str, object]) -> None:
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

    def _handle_proxy_sync_session_post(self, payload: dict[str, object]) -> None:
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

    def _handle_codex_local_session_sync_post(self, payload: dict[str, object]) -> None:
        session_id = sanitize_text(payload.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        if codex_proxy_session_exists(session_id):
            self._send_json(
                {
                    "status": "skipped",
                    "reason": "proxy_session_exists",
                    "session_id": session_id,
                }
            )
            return
        transcript = codex_local_session_transcript(session_id)
        if not transcript or not transcript_has_conversation_records(transcript):
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

    def _handle_context_edit_marker_consume_post(self, payload: dict[str, object]) -> None:
        session_id = sanitize_text(payload.get("session_id") or "").strip()
        marker = consume_context_edit_marker(session_id)
        self._send_json({"marker": marker})
        return

    def _handle_reset_post(self, payload: dict[str, object]) -> None:
        session_id = sanitize_text(payload.get("session_id", "")).strip()
        session = self.app_state.reset_session(session_id)
        self._send_json(
            {
                "session": self.app_state.session_payload(session),
                **self.app_state.sidebar_payload(),
            }
        )
        return

    def _handle_truncate_session_post(self, payload: dict[str, object]) -> None:
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

    def _handle_delete_message_post(self, payload: dict[str, object]) -> None:
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

    def _handle_settings_post(self, payload: dict[str, object]) -> None:
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

    def _handle_provider_model_candidates_post(self, payload: dict[str, object]) -> None:
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

    def _handle_provider_models_post(self, payload: dict[str, object]) -> None:
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

    def _handle_context_workbench_settings_post(self, payload: dict[str, object]) -> None:
        updated_settings = save_settings(
            context_workbench_model=sanitize_text(payload.get("context_workbench_model") or "").strip()
            or None,
            context_workbench_provider_id=CODEX_PROXY_PROVIDER_ID,
            context_token_warning_threshold=payload.get("context_token_warning_threshold"),
            context_token_critical_threshold=payload.get("context_token_critical_threshold"),
            user_locale=payload.get("user_locale") if isinstance(payload.get("user_locale"), str) else None,
        )
        self.app_state.settings = updated_settings
        provider_payloads = context_workbench_provider_payloads(
            updated_settings,
            refresh_codex_proxy_models=True,
        )
        self._send_json(
            {
                "settings": context_workbench_settings_payload(updated_settings),
                "models": context_workbench_models_payload(updated_settings, provider_payloads),
                "response_providers": provider_payloads,
                "tool_catalog": ContextWorkbenchToolRegistry.tool_catalog(),
            }
        )
        return

    def _handle_proxy_session_override_post(self, payload: dict[str, object]) -> None:
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

    def _handle_proxy_session_reset_post(self, payload: dict[str, object]) -> None:
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

    def _handle_proxy_session_usage_reset_post(self, payload: dict[str, object]) -> None:
        session_id = sanitize_text(payload.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        proxy_payload = post_codex_proxy_control_json(
            f"/api/proxy/sessions/{quote(session_id, safe='')}/usage/reset",
            {},
        )
        self._send_json(proxy_payload)
        return

    def _handle_context_workbench_suggestions_post(self, payload: dict[str, object]) -> None:
        session = self.app_state.get_session(payload.get("session_id"))
        session = refresh_session_from_proxy_active_context_if_known(self.app_state, session)
        self._send_json(context_workbench_suggestions_payload(session))
        return

    def _handle_delete_session_post(self, payload: dict[str, object]) -> None:
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

    def _handle_delete_project_post(self, payload: dict[str, object]) -> None:
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

    def _handle_cancel_request_post(self, payload: dict[str, object]) -> None:
        session = self.app_state.get_session(payload.get("session_id"))
        mode = sanitize_text(payload.get("mode") or "main").strip() or "main"
        cancelled = self.app_state.cancel_session_request(session, mode)
        self._send_json({"cancelled": cancelled})
        return

    def _handle_context_chat_post(self, payload: dict[str, object]) -> None:
        session = self.app_state.get_session(payload.get("session_id"))
        session = refresh_session_from_proxy_active_context_if_known(self.app_state, session)
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
            self.app_state.ensure_agent_hydrated(session)
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

    def _handle_context_chat_stream_post(self, payload: dict[str, object]) -> None:
        session = self.app_state.get_session(payload.get("session_id"))
        session = refresh_session_from_proxy_active_context_if_known(self.app_state, session)
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
            self.app_state.ensure_agent_hydrated(session)
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
            if draft.has_changes:
                self._write_stream_event({"type": "finalizing", "stage": "commit", "has_changes": True})
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

    def _handle_context_restore_post(self, payload: dict[str, object]) -> None:
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

    def _handle_context_workbench_history_message_delete_post(self, payload: dict[str, object]) -> None:
        session = self.app_state.get_session(payload.get("session_id"))
        raw_message_index = payload.get("message_index")
        try:
            message_index = int(raw_message_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("message_index must be a number") from exc

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
        return

    def _handle_context_workbench_history_clear_post(self, payload: dict[str, object]) -> None:
        session = self.app_state.get_session(payload.get("session_id"))
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
        return

    def _handle_context_undo_restore_post(self, payload: dict[str, object]) -> None:
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

    def _handle_send_message_stream_post(self, payload: dict[str, object]) -> None:
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
            agent = self.app_state.ensure_agent_hydrated(session)
            answer, tool_events = agent.run_turn(
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

    def _handle_send_message_post(self, payload: dict[str, object]) -> None:
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
            agent = self.app_state.ensure_agent_hydrated(session)
            answer, tool_events = agent.run_turn(
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
    host = os.getenv("HASH_WEB_HOST", os.getenv("HASH_CONTEXT_HOST", "localhost"))
    CONTEXT_REQUEST_DEBUG_FILE.write_text("", encoding="utf-8")
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

