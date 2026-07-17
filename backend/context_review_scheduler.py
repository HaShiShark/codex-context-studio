from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from simple_agent.agent import sanitize_text

from backend import proxy_routes_support
from backend.web_runtime import generate_and_store_context_review, get_codex_proxy_control_json
from backend.web_state import AppState

DEFAULT_POLL_SECONDS = 30.0
CANCEL_CHECK_SECONDS = 0.5
DESKTOP_PROCESS_NAMES = {"codex", "chatgpt"}


def _env_float(name: str) -> float | None:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return None
    return value if value > 0 else None


def _env_allows_auto_review() -> bool:
    raw_value = sanitize_text(os.environ.get("CODEX_CONTEXT_STUDIO_REVIEW_AUTO", "1")).strip().lower()
    return raw_value not in {"0", "false", "no", "off"}


def _parse_utc_timestamp(value: Any) -> datetime | None:
    cleaned = sanitize_text(value or "").strip()
    if not cleaned:
        return None
    normalized = cleaned.replace("Z", "+00:00")
    normalized = re.sub(r"(\.\d{6})\d+(?=[+-]\d{2}:\d{2}$)", r"\1", normalized)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class DesktopProcessRun:
    run_id: str
    started_at: datetime


def desktop_process_run() -> DesktopProcessRun | None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name != "nt":
        try:
            result = subprocess.run(
                ["pgrep", "-fo", "codex|chatgpt"],
                capture_output=True,
                check=False,
                text=True,
                timeout=4,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        pid = sanitize_text(result.stdout).strip()
        if result.returncode != 0 or not pid:
            return None
        return DesktopProcessRun(run_id=f"desktop:{pid}", started_at=datetime.now(timezone.utc))

    command = (
        "$items = Get-Process Codex,ChatGPT -ErrorAction SilentlyContinue | "
        "ForEach-Object { [pscustomobject]@{ Name=$_.ProcessName; Id=$_.Id; "
        "StartTime=$_.StartTime.ToUniversalTime().ToString('o') } }; "
        "$items | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            check=False,
            creationflags=creationflags,
            text=True,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw_output = sanitize_text(result.stdout).strip()
    if result.returncode != 0 or not raw_output:
        return None
    try:
        decoded = json.loads(raw_output)
    except json.JSONDecodeError:
        return None
    records = decoded if isinstance(decoded, list) else [decoded]
    candidates: list[tuple[datetime, str, int]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        name = sanitize_text(record.get("Name") or "").strip().lower()
        started_at = _parse_utc_timestamp(record.get("StartTime"))
        try:
            pid = int(record.get("Id") or 0)
        except (TypeError, ValueError):
            pid = 0
        if name not in DESKTOP_PROCESS_NAMES or started_at is None or pid <= 0:
            continue
        candidates.append((started_at, name, pid))
    if not candidates:
        return None
    started_at, name, pid = min(candidates, key=lambda item: item[0])
    return DesktopProcessRun(
        run_id=f"{name}:{pid}:{started_at.isoformat()}",
        started_at=started_at,
    )


def active_codex_session_ids() -> set[str] | None:
    session_ids, _scan_info = proxy_routes_support.codex_active_thread_ids()
    if session_ids is None:
        return None
    return {sanitize_text(session_id).strip().lower() for session_id in session_ids if session_id}


@dataclass
class ReviewWorker:
    thread: threading.Thread
    cancel_event: threading.Event
    request_timestamp: str
    cancel_revision: int


class ContextReviewIdleScheduler:
    def __init__(
        self,
        app_state: AppState,
        *,
        idle_seconds: float | None = None,
        poll_seconds: float | None = None,
        process_probe: Callable[[], DesktopProcessRun | None] = desktop_process_run,
        archive_probe: Callable[[], set[str] | None] = active_codex_session_ids,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.app_state = app_state
        self.idle_seconds_override = idle_seconds
        self.poll_seconds = poll_seconds or _env_float("CODEX_CONTEXT_STUDIO_REVIEW_POLL_SECONDS") or DEFAULT_POLL_SECONDS
        self.process_probe = process_probe
        self.archive_probe = archive_probe
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._process_run: DesktopProcessRun | None = None
        self._enabled_since: datetime | None = None
        self._was_enabled = False
        self._targets: dict[str, str] = {}
        self._analyzed_keys: set[tuple[str, str, int]] = set()
        self._workers: dict[str, ReviewWorker] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="context-review-idle-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._cancel_all_workers()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.wait(self.poll_seconds):
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                print(f"context review scheduler error: {type(exc).__name__}: {exc}")

    def _settings_enabled(self) -> bool:
        return _env_allows_auto_review() and bool(self.app_state.settings.context_review_auto_enabled)

    def _idle_seconds(self) -> float:
        return (
            self.idle_seconds_override
            or _env_float("CODEX_CONTEXT_STUDIO_REVIEW_IDLE_SECONDS")
            or max(1, int(self.app_state.settings.context_review_interval_minutes or 10)) * 60.0
        )

    def _tick(self) -> None:
        now = self.now().astimezone(timezone.utc)
        process_run = self.process_probe()
        if process_run is None:
            self._reset_process_run(None)
            return
        if self._process_run is None or self._process_run.run_id != process_run.run_id:
            self._reset_process_run(process_run)

        enabled = self._settings_enabled()
        if not enabled:
            self._cancel_all_workers()
            self._targets.clear()
            self._was_enabled = False
            self._enabled_since = None
            return
        if not self._was_enabled:
            self._enabled_since = now if self._process_run else None
            self._was_enabled = True

        payload = get_codex_proxy_control_json("/api/proxy/sessions", timeout_seconds=3)
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(sessions, list):
            return
        summaries = {
            sanitize_text(item.get("id") or "").strip(): item
            for item in sessions
            if isinstance(item, dict) and sanitize_text(item.get("id") or "").strip()
        }
        self._refresh_targets(summaries)
        self._cancel_stale_workers(summaries)

        due: list[tuple[str, dict[str, Any], str, int]] = []
        for session_id, request_timestamp in self._targets.items():
            summary = summaries.get(session_id)
            if not isinstance(summary, dict) or not self._eligible(summary, request_timestamp):
                continue
            request_at = _parse_utc_timestamp(request_timestamp)
            if request_at is None or (now - request_at).total_seconds() < self._idle_seconds():
                continue
            version = int(summary.get("transcript_version") or 0)
            analyzed_key = (session_id, request_timestamp, version)
            if analyzed_key in self._analyzed_keys or session_id in self._workers:
                continue
            due.append(
                (
                    session_id,
                    summary,
                    request_timestamp,
                    int(summary.get("context_review_cancel_revision") or 0),
                )
            )
        if not due:
            return
        archived_ids = self.archive_probe()
        if archived_ids is None:
            return
        for session_id, summary, request_timestamp, cancel_revision in due:
            if session_id.lower() not in archived_ids:
                continue
            self._start_worker(session_id, summary, request_timestamp, cancel_revision)

    def _reset_process_run(self, process_run: DesktopProcessRun | None) -> None:
        self._cancel_all_workers()
        self._process_run = process_run
        self._enabled_since = process_run.started_at if process_run else None
        self._was_enabled = bool(process_run and self._settings_enabled())
        self._targets.clear()
        self._analyzed_keys.clear()

    def _refresh_targets(self, summaries: dict[str, dict[str, Any]]) -> None:
        process_started_at = self._process_run.started_at if self._process_run else None
        enabled_since = self._enabled_since
        for session_id, summary in summaries.items():
            request_timestamp = sanitize_text(summary.get("last_proxy_request_at") or "").strip()
            request_at = _parse_utc_timestamp(request_timestamp)
            if request_at is None or process_started_at is None:
                continue
            if request_at < process_started_at or (enabled_since is not None and request_at < enabled_since):
                continue
            self._targets[session_id] = request_timestamp

    @staticmethod
    def _eligible(summary: dict[str, Any], request_timestamp: str) -> bool:
        if int(summary.get("transcript_version") or 0) <= 0:
            return False
        if sanitize_text(summary.get("last_proxy_request_at") or "").strip() != request_timestamp:
            return False
        if bool(summary.get("is_main_turn_running")) or bool(summary.get("is_context_running")):
            return False
        return not isinstance(summary.get("pending_context_review"), dict)

    def _cancel_stale_workers(self, summaries: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            workers = list(self._workers.items())
        for session_id, worker in workers:
            summary = summaries.get(session_id)
            if not isinstance(summary, dict):
                worker.cancel_event.set()
                continue
            if (
                sanitize_text(summary.get("last_proxy_request_at") or "").strip() != worker.request_timestamp
                or int(summary.get("context_review_cancel_revision") or 0) != worker.cancel_revision
                or bool(summary.get("is_main_turn_running"))
            ):
                worker.cancel_event.set()

    def _start_worker(
        self,
        session_id: str,
        summary: dict[str, Any],
        request_timestamp: str,
        cancel_revision: int,
    ) -> None:
        cancel_event = threading.Event()
        thread = threading.Thread(
            target=self._generate_worker,
            args=(session_id, request_timestamp, int(summary.get("transcript_version") or 0), cancel_revision, cancel_event),
            name=f"context-review-{session_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._workers[session_id] = ReviewWorker(
                thread=thread,
                cancel_event=cancel_event,
                request_timestamp=request_timestamp,
                cancel_revision=cancel_revision,
            )
        thread.start()

    def _generate_worker(
        self,
        session_id: str,
        request_timestamp: str,
        transcript_version: int,
        cancel_revision: int,
        cancel_event: threading.Event,
    ) -> None:
        last_remote_check = 0.0

        def raise_if_cancelled() -> None:
            nonlocal last_remote_check
            if cancel_event.is_set() or self._stop_event.is_set() or not self._settings_enabled():
                raise RuntimeError("context_review_cancelled")
            now_monotonic = time.monotonic()
            if now_monotonic - last_remote_check < CANCEL_CHECK_SECONDS:
                return
            last_remote_check = now_monotonic
            payload = get_codex_proxy_control_json(
                f"/api/proxy/sessions/{session_id}",
                timeout_seconds=2,
            )
            if not isinstance(payload, dict):
                raise RuntimeError("context_review_cancelled")
            if (
                int(payload.get("context_review_cancel_revision") or 0) != cancel_revision
                or sanitize_text(payload.get("last_proxy_request_at") or "").strip() != request_timestamp
                or bool(payload.get("is_main_turn_running"))
            ):
                raise RuntimeError("context_review_cancelled")

        try:
            result = generate_and_store_context_review(
                self.app_state,
                session_id,
                source="auto_idle",
                expected_cancel_revision=cancel_revision,
                check_cancelled=raise_if_cancelled,
            )
            if result.get("status") in {"pending", "skipped"}:
                self._analyzed_keys.add((session_id, request_timestamp, transcript_version))
        except Exception as exc:  # noqa: BLE001
            if sanitize_text(str(exc)).strip() != "context_review_cancelled":
                print(f"context review generation error: {type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                current = self._workers.get(session_id)
                if current and current.cancel_event is cancel_event:
                    self._workers.pop(session_id, None)

    def _cancel_all_workers(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            worker.cancel_event.set()
