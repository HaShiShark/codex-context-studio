from __future__ import annotations

import json
import mimetypes
import os
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from dotenv import load_dotenv

from simple_agent.agent import ToolEvent, sanitize_text, sanitize_value
from simple_agent.config import load_settings, save_settings

from backend.context_review_scheduler import ContextReviewIdleScheduler
from backend.web_constants import (
    ATTACHMENTS_ROUTE,
    CONTEXT_REQUEST_DEBUG_FILE,
    DEFAULT_PAGE,
    REACT_DIST_DIR,
    REPO_ROOT,
    ClientDisconnectedError,
    RequestCancelledError,
)
from backend.web_context import (
    codex_local_session_transcript,
    consume_context_edit_marker,
    editable_context_node_count,
    normalize_selected_node_indexes,
    normalize_transcript,
    serialize_tool_event,
    transcript_has_conversation_records,
    write_context_edit_marker,
)
from backend.web_runtime import (
    apply_proxy_context_review,
    build_context_chat_response_payload,
    codex_proxy_session_exists,
    context_workbench_models_payload,
    context_workbench_provider_payloads,
    context_workbench_settings_payload,
    discard_proxy_context_review,
    generate_and_store_context_review,
    get_codex_proxy_control_json,
    post_codex_proxy_control_json,
    refresh_session_from_proxy_active_context_if_known,
    run_context_chat_turn,
    safe_set_proxy_context_run_state,
)
from backend.web_state import AppState, list_workspace_entries, resolve_attachment_file_path


class StudioHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "CodexContextStudioWeb/0.2"

    @property
    def app_state(self) -> AppState:
        return self.server.app_state  # type: ignore[attr-defined]

    def _get_proxy_control_json_or_none(self, path: str) -> dict[str, Any] | None:
        return get_codex_proxy_control_json(path)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        handler = self._get_routes().get(parsed.path)
        if handler is not None:
            handler(parsed)
            return

        if parsed.path == "/api/proxy/sessions":
            try:
                proxy_payload = self._get_proxy_control_json_or_none("/api/proxy/sessions")
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_GATEWAY)
                return
            if proxy_payload is None:
                self._send_json({"error": "proxy returned empty sessions payload"}, status=HTTPStatus.BAD_GATEWAY)
                return
            self._send_json(proxy_payload)
            return

        if parsed.path == "/api/proxy/usage":
            try:
                proxy_payload = self._get_proxy_control_json_or_none("/api/proxy/usage")
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_GATEWAY)
                return
            if proxy_payload is None:
                self._send_json({"error": "proxy returned empty usage payload"}, status=HTTPStatus.BAD_GATEWAY)
                return
            self._send_json(proxy_payload)
            return

        if parsed.path.startswith("/api/proxy/sessions/") and parsed.path.endswith("/usage"):
            session_id = quote(parsed.path.split("/api/proxy/sessions/", 1)[1].rsplit("/", 1)[0], safe="")
            try:
                proxy_payload = self._get_proxy_control_json_or_none(f"/api/proxy/sessions/{session_id}/usage")
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_GATEWAY)
                return
            if proxy_payload is None:
                self._send_json({"error": "session not found"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(proxy_payload)
            return

        if parsed.path.startswith("/api/proxy/sessions/"):
            session_id = quote(parsed.path.split("/api/proxy/sessions/", 1)[1].split("/", 1)[0], safe="")
            try:
                proxy_payload = self._get_proxy_control_json_or_none(f"/api/proxy/sessions/{session_id}")
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_GATEWAY)
                return
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

    def _handle_context_workbench_settings_get(self, parsed: Any) -> None:
        query = parse_qs(parsed.query)
        refresh_models = sanitize_text((query.get("refresh_models") or ["0"])[0]).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        latest_settings = load_settings()
        provider_payloads = context_workbench_provider_payloads(
            latest_settings,
            refresh_models=refresh_models,
        )
        self._send_json(
            {
                "settings": context_workbench_settings_payload(latest_settings),
                "models": context_workbench_models_payload(latest_settings, provider_payloads),
                "providers": provider_payloads,
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
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, sanitize_text(str(exc) or "service error"))

    def _post_routes(self) -> dict[str, Callable[[dict[str, object]], None]]:
        return {
            "/api/proxy-sync-session": self._handle_proxy_sync_session_post,
            "/api/codex-local-session-sync": self._handle_codex_local_session_sync_post,
            "/api/context-edit-marker-consume": self._handle_context_edit_marker_consume_post,
            "/api/context-workbench-settings": self._handle_context_workbench_settings_post,
            "/api/proxy-session-transcript": self._handle_proxy_session_transcript_post,
            "/api/proxy-session-node-lock": self._handle_proxy_session_node_lock_post,
            "/api/proxy-session-usage-reset": self._handle_proxy_session_usage_reset_post,
            "/api/context-workbench-suggestions": self._handle_context_workbench_suggestions_post,
            "/api/context-review-generate": self._handle_context_review_generate_post,
            "/api/context-review-apply": self._handle_context_review_apply_post,
            "/api/context-review-discard": self._handle_context_review_discard_post,
            "/api/cancel-request": self._handle_cancel_request_post,
            "/api/context-chat": self._handle_context_chat_post,
            "/api/context-chat-stream": self._handle_context_chat_stream_post,
            "/api/context-workbench-history-message-delete": self._handle_context_workbench_history_message_delete_post,
            "/api/context-workbench-history-clear": self._handle_context_workbench_history_clear_post,
        }

    def _handle_proxy_sync_session_post(self, payload: dict[str, object]) -> None:
        transcript = payload.get("transcript")
        if transcript is not None and not isinstance(transcript, list):
            raise ValueError("transcript must be a list")
        session = self.app_state.upsert_proxy_session(
            session_id=sanitize_text(payload.get("session_id") or "").strip(),
            title=sanitize_text(payload.get("title") or "").strip(),
            transcript=transcript,
            is_main_turn_running=bool(payload.get("is_main_turn_running")),
            main_turn_id=sanitize_text(payload.get("main_turn_id") or "").strip(),
            main_turn_started_at=sanitize_text(payload.get("main_turn_started_at") or "").strip(),
            main_turn_updated_at=sanitize_text(payload.get("main_turn_updated_at") or "").strip(),
            node_locks=payload.get("node_locks") if isinstance(payload.get("node_locks"), dict) else None,
            node_lock_revision=int(payload.get("node_lock_revision") or 0),
        )
        self._send_json(
            {
                "ok": True,
                "session_id": session.session_id,
                "context_workbench_history": sanitize_value(session.context_workbench_history),
            }
        )

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
            title=sanitize_text(payload.get("title") or "").strip(),
            transcript=transcript,
            node_locks={},
            node_lock_revision=0,
        )
        self._send_json(
            {
                "ok": True,
                "session_id": session.session_id,
                "conversation": sanitize_value(session.transcript),
                "context_workbench_history": sanitize_value(session.context_workbench_history),
            }
        )

    def _handle_context_edit_marker_consume_post(self, payload: dict[str, object]) -> None:
        session_id = sanitize_text(payload.get("session_id") or "").strip()
        marker = consume_context_edit_marker(session_id)
        self._send_json({"marker": marker})

    def _handle_context_workbench_settings_post(self, payload: dict[str, object]) -> None:
        updated_settings = save_settings(
            context_workbench_model=sanitize_text(payload.get("context_workbench_model") or "").strip()
            or None,
            context_workbench_provider_id=sanitize_text(payload.get("context_workbench_provider_id") or "").strip()
            or None,
            context_review_auto_enabled=payload.get("context_review_auto_enabled")
            if type(payload.get("context_review_auto_enabled")) is bool
            else None,
            context_review_interval_minutes=payload.get("context_review_interval_minutes")
            if type(payload.get("context_review_interval_minutes")) is int
            else None,
            response_providers=payload.get("response_providers")
            if isinstance(payload.get("response_providers"), list)
            else None,
            context_token_warning_threshold=payload.get("context_token_warning_threshold"),
            context_token_critical_threshold=payload.get("context_token_critical_threshold"),
            codex_system_prompt=payload.get("codex_system_prompt")
            if isinstance(payload.get("codex_system_prompt"), str)
            else None,
            manual_local_compact_prompt=payload.get("manual_local_compact_prompt")
            if isinstance(payload.get("manual_local_compact_prompt"), str)
            else None,
            auto_local_compact_prompt=payload.get("auto_local_compact_prompt")
            if isinstance(payload.get("auto_local_compact_prompt"), str)
            else None,
            user_locale=payload.get("user_locale") if isinstance(payload.get("user_locale"), str) else None,
            theme_mode=payload.get("theme_mode") if isinstance(payload.get("theme_mode"), str) else None,
            ui_font=payload.get("ui_font") if isinstance(payload.get("ui_font"), str) else None,
            ui_font_size=payload.get("ui_font_size") if type(payload.get("ui_font_size")) is int else None,
        )
        self.app_state.refresh_settings(updated_settings)
        should_refresh_models = bool(payload.get("refresh_models"))
        provider_payloads = context_workbench_provider_payloads(
            updated_settings,
            refresh_models=should_refresh_models,
        )
        self._send_json(
            {
                "settings": context_workbench_settings_payload(updated_settings),
                "models": context_workbench_models_payload(updated_settings, provider_payloads),
                "providers": provider_payloads,
            }
        )

    def _handle_proxy_session_transcript_post(self, payload: dict[str, object]) -> None:
        session_id = sanitize_text(payload.get("session_id") or "").strip()
        transcript = payload.get("transcript")
        if not session_id:
            raise ValueError("session_id is required")
        if not isinstance(transcript, list):
            raise ValueError("transcript must be a list")
        proxy_payload = post_codex_proxy_control_json(
            f"/api/proxy/sessions/{quote(session_id, safe='')}/transcript",
            {"transcript": transcript},
        )
        if bool(proxy_payload.get("changed")):
            visible_transcript = normalize_transcript(proxy_payload.get("transcript"))
            session = self.app_state.get_session(session_id)
            write_context_edit_marker(
                session_id,
                summary="Context has been edited.",
                edit_version=0,
                node_count=editable_context_node_count(visible_transcript, session.node_locks),
            )
        self._send_json(proxy_payload)

    def _handle_proxy_session_node_lock_post(self, payload: dict[str, object]) -> None:
        session_id = sanitize_text(payload.get("session_id") or "").strip()
        node_id = sanitize_text(payload.get("node_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        if not node_id:
            raise ValueError("node_id is required")

        session = self.app_state.get_session(session_id)
        if sanitize_text(session.active_request_mode or "").strip() == "context":
            self._send_json(
                {"error": "Cannot change node locks while the context model is running."},
                status=HTTPStatus.CONFLICT,
            )
            return

        request_payload: dict[str, object] = {
            "node_id": node_id,
            "locked": bool(payload.get("locked")),
        }
        if payload.get("expected_revision") is not None:
            request_payload["expected_revision"] = payload.get("expected_revision")

        try:
            proxy_payload = post_codex_proxy_control_json(
                f"/api/proxy/sessions/{quote(session_id, safe='')}/node-locks",
                request_payload,
            )
        except ValueError as exc:
            self._send_json({"error": sanitize_text(str(exc))}, status=HTTPStatus.CONFLICT)
            return

        updated = self.app_state.upsert_proxy_session(
            session_id=session_id,
            title=sanitize_text(proxy_payload.get("title") or session.title).strip(),
            transcript=normalize_transcript(proxy_payload.get("transcript") or session.transcript),
            is_main_turn_running=bool(proxy_payload.get("is_main_turn_running")),
            main_turn_id=sanitize_text(proxy_payload.get("main_turn_id") or "").strip(),
            main_turn_started_at=sanitize_text(proxy_payload.get("main_turn_started_at") or "").strip(),
            main_turn_updated_at=sanitize_text(proxy_payload.get("main_turn_updated_at") or "").strip(),
            node_locks=proxy_payload.get("node_locks") if isinstance(proxy_payload.get("node_locks"), dict) else {},
            node_lock_revision=int(proxy_payload.get("node_lock_revision") or 0),
        )
        self._send_json(
            {
                "ok": True,
                "session_id": updated.session_id,
                "node_locks": sanitize_value(updated.node_locks),
                "node_lock_revision": int(updated.node_lock_revision or 0),
            }
        )

    def _handle_proxy_session_usage_reset_post(self, payload: dict[str, object]) -> None:
        session_id = sanitize_text(payload.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        proxy_payload = post_codex_proxy_control_json(
            f"/api/proxy/sessions/{quote(session_id, safe='')}/usage/reset",
            {},
        )
        self._send_json(proxy_payload)

    def _handle_context_workbench_suggestions_post(self, payload: dict[str, object]) -> None:
        session_id = sanitize_text(payload.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        proxy_payload = get_codex_proxy_control_json(
            f"/api/proxy/sessions/{quote(session_id, safe='')}",
            timeout_seconds=3,
        )
        if proxy_payload is None:
            self._send_json({"error": "session not found"}, status=HTTPStatus.NOT_FOUND)
            return
        self._send_json(
            {
                "pending_review": proxy_payload.get("pending_context_review")
                if isinstance(proxy_payload.get("pending_context_review"), dict)
                else None,
                "transcript_version": int(proxy_payload.get("transcript_version") or 0),
            }
        )

    def _handle_context_review_generate_post(self, payload: dict[str, object]) -> None:
        session_id = sanitize_text(payload.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        result = generate_and_store_context_review(
            self.app_state,
            session_id,
            source=sanitize_text(payload.get("source") or "manual").strip() or "manual",
        )
        if result.get("reason") in {"main_turn_running", "context_model_running"}:
            self._send_json(result, status=HTTPStatus.CONFLICT)
            return
        self._send_json(result)

    def _handle_context_review_apply_post(self, payload: dict[str, object]) -> None:
        session_id = sanitize_text(payload.get("session_id") or "").strip()
        review_id = sanitize_text(payload.get("review_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        try:
            proxy_payload = apply_proxy_context_review(session_id, review_id)
        except ValueError as exc:
            message = sanitize_text(str(exc))
            status = HTTPStatus.CONFLICT if "stale" in message.lower() else HTTPStatus.BAD_REQUEST
            self._send_json({"error": message}, status=status)
            return

        visible_transcript = normalize_transcript(proxy_payload.get("transcript"))
        updated = self.app_state.upsert_proxy_session(
            session_id=session_id,
            title=sanitize_text(proxy_payload.get("title") or "").strip() or "Codex Context",
            transcript=visible_transcript,
            is_main_turn_running=bool(proxy_payload.get("is_main_turn_running")),
            main_turn_id=sanitize_text(proxy_payload.get("main_turn_id") or "").strip(),
            main_turn_started_at=sanitize_text(proxy_payload.get("main_turn_started_at") or "").strip(),
            main_turn_updated_at=sanitize_text(proxy_payload.get("main_turn_updated_at") or "").strip(),
            node_locks=proxy_payload.get("node_locks") if isinstance(proxy_payload.get("node_locks"), dict) else {},
            node_lock_revision=int(proxy_payload.get("node_lock_revision") or 0),
        )
        if bool(proxy_payload.get("changed")):
            write_context_edit_marker(
                session_id,
                summary="Context review applied.",
                edit_version=int(proxy_payload.get("transcript_version") or 0),
                node_count=editable_context_node_count(visible_transcript, updated.node_locks),
            )
        self._send_json(proxy_payload)

    def _handle_context_review_discard_post(self, payload: dict[str, object]) -> None:
        session_id = sanitize_text(payload.get("session_id") or "").strip()
        review_id = sanitize_text(payload.get("review_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        try:
            proxy_payload = discard_proxy_context_review(session_id, review_id)
        except ValueError as exc:
            self._send_json({"error": sanitize_text(str(exc))}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(proxy_payload)

    def _handle_cancel_request_post(self, payload: dict[str, object]) -> None:
        session = self.app_state.get_session(payload.get("session_id"))
        mode = sanitize_text(payload.get("mode") or "context").strip() or "context"
        cancelled = self.app_state.cancel_session_request(session, mode)
        self._send_json({"cancelled": cancelled})

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
        safe_set_proxy_context_run_state(session.session_id, request_id, True)
        try:
            answer, used_model, draft, tool_events = run_context_chat_turn(
                self.app_state.settings,
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
            safe_set_proxy_context_run_state(session.session_id, request_id, False)
            self.app_state.release_session_request(session, "context", request_id)

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
        safe_set_proxy_context_run_state(session.session_id, request_id, True)
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
                self.app_state.settings,
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
            if draft.has_changes:
                self._write_stream_event({"type": "finalizing", "stage": "commit", "has_changes": True})
            self._write_stream_event(sanitize_value(payload_data))
        except (ClientDisconnectedError, RequestCancelledError):
            pass
        except Exception as exc:  # noqa: BLE001
            try:
                self._write_stream_event(
                    {
                        "type": "error",
                        "error": sanitize_text(str(exc) or "service error"),
                    }
                )
            except ClientDisconnectedError:
                pass
        finally:
            safe_set_proxy_context_run_state(session.session_id, request_id, False)
            self.app_state.release_session_request(session, "context", request_id)

    def _handle_context_workbench_history_message_delete_post(self, payload: dict[str, object]) -> None:
        session = self.app_state.get_session(payload.get("session_id"))
        raw_message_index = payload.get("message_index")
        try:
            message_index = int(raw_message_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("message_index must be a number") from exc

        conversation, history = self.app_state.delete_context_workbench_history_message(
            session,
            message_index=message_index,
        )
        self._send_json(
            {
                "conversation": conversation,
                "history": history,
            }
        )

    def _handle_context_workbench_history_clear_post(self, payload: dict[str, object]) -> None:
        session = self.app_state.get_session(payload.get("session_id"))
        conversation, history = self.app_state.clear_context_workbench_history(
            session,
        )
        self._send_json(
            {
                "conversation": conversation,
                "history": history,
            }
        )

    def _serve_static(self, request_path: str) -> None:
        normalized_path = request_path or "/"
        if normalized_path == "/":
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
                self._send_error_json(HTTPStatus.FORBIDDEN, "Forbidden path")
                return
        else:
            relative_path = normalized_path.lstrip("/")
            file_path = (REPO_ROOT / relative_path).resolve()
            if REPO_ROOT not in file_path.parents and file_path != REPO_ROOT:
                self._send_error_json(HTTPStatus.FORBIDDEN, "Forbidden path")
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
            raise ValueError("Content-Length is invalid") from exc

        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Request body must be valid JSON") from exc

        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
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


class StudioHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], app_state: AppState) -> None:
        super().__init__(server_address, StudioHTTPRequestHandler)
        self.app_state = app_state


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    settings = load_settings()
    port = int(os.getenv("CODEX_CONTEXT_STUDIO_WEB_PORT", "8765"))
    host = os.getenv("CODEX_CONTEXT_STUDIO_WEB_HOST", os.getenv("CODEX_CONTEXT_STUDIO_HOST", "localhost"))
    CONTEXT_REQUEST_DEBUG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONTEXT_REQUEST_DEBUG_FILE.write_text("", encoding="utf-8")
    app_state = AppState(settings)
    review_scheduler = ContextReviewIdleScheduler(app_state)
    server = StudioHTTPServer((host, port), app_state)

    print(f"Codex Context Studio web ready: http://{host}:{port}")
    review_scheduler.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        review_scheduler.stop()
        server.server_close()


if __name__ == "__main__":
    main()
