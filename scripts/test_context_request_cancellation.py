from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _settings(project_root: Path) -> Any:
    from simple_agent.config import (
        CODEX_PROXY_PROVIDER_ID,
        DEFAULT_RESPONSE_PROVIDERS,
        Settings,
    )

    return Settings(
        default_reasoning_effort="default",
        context_workbench_model="gpt-5.6",
        context_workbench_provider_id=CODEX_PROXY_PROVIDER_ID,
        project_root=project_root,
        response_providers=[dict(provider) for provider in DEFAULT_RESPONSE_PROVIDERS],
    )


class _BlockingResponse:
    def __init__(self) -> None:
        self.read_started = threading.Event()
        self.close_called = threading.Event()
        self.allow_read_to_finish = threading.Event()

    def __enter__(self) -> _BlockingResponse:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def read1(self, _size: int) -> bytes:
        self.read_started.set()
        if not self.close_called.wait(timeout=2):
            raise AssertionError("provider read was not interrupted by response.close()")
        if not self.allow_read_to_finish.wait(timeout=2):
            raise AssertionError("test did not release the provider read")
        return b""

    def read(self, size: int) -> bytes:
        return self.read1(size)

    def close(self) -> None:
        self.close_called.set()


def _new_session(app_state: Any, session_id: str) -> Any:
    return app_state.upsert_proxy_session(
        session_id=session_id,
        title="Cancellation test",
        transcript=[
            {
                "id": f"{session_id}-user",
                "role": "user",
                "providerItems": [{"type": "message", "role": "user", "content": "keep"}],
            }
        ],
    )


def _test_transport_close_and_confirmed_ack(app_state: Any) -> None:
    from backend import web_runtime
    from backend.web_handler import StudioHTTPRequestHandler

    session = _new_session(app_state, "cancel-transport")
    request_id = app_state.acquire_session_request(session, "context")
    control = session.active_request_control
    if control is None:
        raise AssertionError("active request should own a cancellation control")

    response = _BlockingResponse()
    original_urlopen = web_runtime.urllib_request.urlopen
    web_runtime.urllib_request.urlopen = lambda *_args, **_kwargs: response
    worker_errors: list[BaseException] = []

    def check_cancelled() -> None:
        if app_state.is_session_request_cancelled(session, request_id):
            raise RuntimeError("cancelled")

    def register_cancel(callback: Any | None) -> None:
        app_state.register_session_request_cancel_callback(session, request_id, callback)

    def run_provider() -> None:
        try:
            web_runtime.stream_context_codex_proxy_response(
                {"model": "gpt-5.6", "input": []},
                check_cancelled=check_cancelled,
                register_cancel=register_cancel,
            )
        except BaseException as exc:  # noqa: BLE001
            worker_errors.append(exc)
        finally:
            app_state.release_session_request(session, "context", request_id)

    worker = threading.Thread(target=run_provider)
    worker.start()
    if not response.read_started.wait(timeout=2):
        raise AssertionError("provider stream did not enter its blocking read")

    captured: dict[str, Any] = {}
    cancel_handler = StudioHTTPRequestHandler.__new__(StudioHTTPRequestHandler)
    cancel_handler.server = SimpleNamespace(app_state=app_state)

    def capture_json(payload: dict[str, object], *, status: Any = None) -> None:
        captured["payload"] = payload
        captured["status"] = status

    cancel_handler._send_json = capture_json
    cancel_thread = threading.Thread(
        target=lambda: cancel_handler._handle_cancel_request_post(
            {
                "session_id": session.session_id,
                "mode": "context",
                "request_id": request_id,
            }
        )
    )
    cancel_thread.start()

    if not response.close_called.wait(timeout=2):
        raise AssertionError("cancel endpoint did not actively close the provider response")
    if not cancel_thread.is_alive():
        raise AssertionError("cancel endpoint acknowledged before the request released its run state")
    try:
        app_state.acquire_session_request(session, "context")
    except ValueError:
        pass
    else:
        raise AssertionError("a cancelled request must remain active until its worker actually exits")

    response.allow_read_to_finish.set()
    worker.join(timeout=2)
    cancel_thread.join(timeout=2)
    web_runtime.urllib_request.urlopen = original_urlopen

    if worker.is_alive() or cancel_thread.is_alive():
        raise AssertionError("provider worker and cancel acknowledgement should both finish")
    payload = captured.get("payload")
    if not isinstance(payload, dict) or payload.get("completed") is not True:
        raise AssertionError("cancel endpoint must confirm completion only after request release")
    if payload.get("request_id") != request_id:
        raise AssertionError("cancel acknowledgement should identify the exact request")
    if session.active_request_mode is not None or session.active_request_control is not None:
        raise AssertionError("completed cancellation should release the session request state")
    if worker_errors:
        raise AssertionError(f"provider worker failed unexpectedly: {worker_errors[0]}")


def _test_cancelled_stream_never_commits_draft(app_state: Any) -> None:
    from backend import web_handler
    from backend.web_constants import RequestCancelledError
    from backend.web_handler import StudioHTTPRequestHandler

    session = _new_session(app_state, "cancel-no-commit")
    original_transcript = list(session.transcript)
    provider_started = threading.Event()
    provider_closed = threading.Event()
    commit_calls: list[bool] = []
    stream_events: list[dict[str, object]] = []
    run_states: list[bool] = []

    originals = {
        "refresh": web_handler.refresh_session_from_proxy_active_context_if_known,
        "run": web_handler.run_context_chat_turn,
        "build": web_handler.build_context_chat_response_payload,
        "run_state": web_handler.safe_set_proxy_context_run_state,
    }

    def fake_run(*_args: Any, **kwargs: Any) -> Any:
        register_cancel = kwargs["register_cancel"]
        check_cancelled = kwargs["check_cancelled"]
        register_cancel(provider_closed.set)
        provider_started.set()
        if not provider_closed.wait(timeout=2):
            raise AssertionError("manual provider was not closed")
        check_cancelled()
        raise AssertionError("cancelled provider should not continue")

    web_handler.refresh_session_from_proxy_active_context_if_known = lambda _state, target: target
    web_handler.run_context_chat_turn = fake_run
    web_handler.build_context_chat_response_payload = lambda *_args, **_kwargs: commit_calls.append(True)
    web_handler.safe_set_proxy_context_run_state = (
        lambda _session_id, _request_id, running: run_states.append(bool(running)) or {}
    )

    handler = StudioHTTPRequestHandler.__new__(StudioHTTPRequestHandler)
    handler.server = SimpleNamespace(app_state=app_state)
    handler._start_stream_response = lambda: None
    handler._write_stream_event = lambda payload: stream_events.append(payload)

    worker_errors: list[BaseException] = []

    def run_stream_handler() -> None:
        try:
            handler._handle_context_chat_stream_post(
                {
                    "session_id": session.session_id,
                    "message": "please edit the context",
                    "selected_node_indexes": [],
                }
            )
        except BaseException as exc:  # noqa: BLE001
            worker_errors.append(exc)

    worker = threading.Thread(target=run_stream_handler)
    worker.start()
    if not provider_started.wait(timeout=2):
        raise AssertionError("manual context provider did not start")
    started_event = next((event for event in stream_events if event.get("type") == "started"), None)
    if not isinstance(started_event, dict):
        raise AssertionError("manual stream must publish its server request id before model output")
    request_id = str(started_event.get("request_id") or "")
    control = app_state.cancel_session_request(session, "context", request_id)
    if control is None or not control.completed_event.wait(timeout=2):
        raise AssertionError("cancelled manual request did not complete")
    worker.join(timeout=2)

    web_handler.refresh_session_from_proxy_active_context_if_known = originals["refresh"]
    web_handler.run_context_chat_turn = originals["run"]
    web_handler.build_context_chat_response_payload = originals["build"]
    web_handler.safe_set_proxy_context_run_state = originals["run_state"]

    if worker.is_alive():
        raise AssertionError("cancelled manual stream handler did not exit")
    if worker_errors and not isinstance(worker_errors[0], RequestCancelledError):
        raise AssertionError(f"manual stream cancellation failed: {worker_errors[0]}")
    if commit_calls:
        raise AssertionError("cancelled manual draft must never reach the commit payload builder")
    if session.transcript != original_transcript or session.context_workbench_history:
        raise AssertionError("cancelled manual draft must not change transcript or chat history")
    if run_states != [True, False]:
        raise AssertionError("cancelled manual request must release the proxy context-run state")


def _test_cancel_and_commit_are_atomic(app_state: Any) -> None:
    session = _new_session(app_state, "cancel-commit-gate")

    commit_request_id = app_state.acquire_session_request(session, "context")
    if not app_state.begin_session_request_commit(session, commit_request_id):
        raise AssertionError("active request should be able to enter its commit phase")
    if app_state.cancel_session_request(session, "context", commit_request_id) is not None:
        raise AssertionError("a completed model turn already committing must not report a false cancellation")
    app_state.release_session_request(session, "context", commit_request_id)

    cancel_request_id = app_state.acquire_session_request(session, "context")
    control = app_state.cancel_session_request(session, "context", cancel_request_id)
    if control is None:
        raise AssertionError("active request cancellation should win before commit starts")
    if app_state.begin_session_request_commit(session, cancel_request_id):
        raise AssertionError("a cancelled request must not cross the draft commit gate")
    app_state.release_session_request(session, "context", cancel_request_id)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="studio-context-cancel-") as raw_tmp_dir:
        tmp_dir = Path(raw_tmp_dir)
        os.environ["CODEX_CONTEXT_STUDIO_DATA_DIR"] = str(tmp_dir / "state")

        from backend.web_state import AppState

        app_state = AppState(_settings(tmp_dir))
        _test_transport_close_and_confirmed_ack(app_state)
        _test_cancelled_stream_never_commits_draft(app_state)
        _test_cancel_and_commit_are_atomic(app_state)

    print("context request cancellation checks passed")


if __name__ == "__main__":
    main()
