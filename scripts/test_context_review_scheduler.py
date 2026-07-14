from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import context_review_scheduler as scheduler_module  # noqa: E402


SESSION_ID = "12345678-1234-1234-1234-123456789abc"


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def summary(request_at: datetime, *, revision: int = 1) -> dict[str, Any]:
    return {
        "id": SESSION_ID,
        "transcript_version": 3,
        "last_proxy_request_at": request_at.isoformat(),
        "context_review_cancel_revision": revision,
        "is_main_turn_running": False,
        "is_context_running": False,
        "pending_context_review": None,
    }


def wait_for_workers(scheduler: scheduler_module.ContextReviewIdleScheduler) -> None:
    deadline = time.monotonic() + 2
    while scheduler._workers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not scheduler._workers


def test_targets_each_session_from_real_proxy_request_time() -> None:
    process_started_at = datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc)
    request_at = process_started_at + timedelta(minutes=1)
    clock = MutableClock(request_at + timedelta(minutes=10))
    current_summary = summary(request_at)
    calls: list[str] = []
    original_get = scheduler_module.get_codex_proxy_control_json
    original_generate = scheduler_module.generate_and_store_context_review

    def fake_get(path: str, timeout_seconds: float = 0) -> dict[str, Any]:
        if path == "/api/proxy/sessions":
            return {"sessions": [dict(current_summary)]}
        return dict(current_summary)

    def fake_generate(_app_state: Any, session_id: str, **kwargs: Any) -> dict[str, Any]:
        kwargs["check_cancelled"]()
        calls.append(session_id)
        return {"status": "skipped"}

    scheduler_module.get_codex_proxy_control_json = fake_get
    scheduler_module.generate_and_store_context_review = fake_generate
    try:
        app_state = SimpleNamespace(
            settings=SimpleNamespace(
                context_review_auto_enabled=True,
                context_review_interval_minutes=10,
            )
        )
        scheduler = scheduler_module.ContextReviewIdleScheduler(
            app_state,
            now=clock,
            process_probe=lambda: scheduler_module.DesktopProcessRun("run-1", process_started_at),
            archive_probe=lambda: {SESSION_ID},
        )
        scheduler._tick()
        wait_for_workers(scheduler)
        assert calls == [SESSION_ID]

        scheduler._tick()
        assert calls == [SESSION_ID]
    finally:
        scheduler_module.get_codex_proxy_control_json = original_get
        scheduler_module.generate_and_store_context_review = original_generate


def test_same_session_request_cancels_running_review() -> None:
    process_started_at = datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc)
    request_at = process_started_at + timedelta(minutes=1)
    clock = MutableClock(request_at + timedelta(minutes=10))
    current_summary = summary(request_at)
    started = threading.Event()
    cancelled = threading.Event()
    original_get = scheduler_module.get_codex_proxy_control_json
    original_generate = scheduler_module.generate_and_store_context_review

    def fake_get(path: str, timeout_seconds: float = 0) -> dict[str, Any]:
        if path == "/api/proxy/sessions":
            return {"sessions": [dict(current_summary)]}
        return dict(current_summary)

    def fake_generate(_app_state: Any, _session_id: str, **kwargs: Any) -> dict[str, Any]:
        started.set()
        try:
            while True:
                kwargs["check_cancelled"]()
                time.sleep(0.01)
        except RuntimeError as exc:
            assert str(exc) == "context_review_cancelled"
            cancelled.set()
            raise

    scheduler_module.get_codex_proxy_control_json = fake_get
    scheduler_module.generate_and_store_context_review = fake_generate
    try:
        app_state = SimpleNamespace(
            settings=SimpleNamespace(
                context_review_auto_enabled=True,
                context_review_interval_minutes=10,
            )
        )
        scheduler = scheduler_module.ContextReviewIdleScheduler(
            app_state,
            now=clock,
            process_probe=lambda: scheduler_module.DesktopProcessRun("run-1", process_started_at),
            archive_probe=lambda: {SESSION_ID},
        )
        scheduler._tick()
        assert started.wait(1)

        next_request_at = clock.value + timedelta(seconds=1)
        current_summary.update(summary(next_request_at, revision=2))
        scheduler._tick()
        wait_for_workers(scheduler)
        assert cancelled.is_set()
    finally:
        scheduler_module.get_codex_proxy_control_json = original_get
        scheduler_module.generate_and_store_context_review = original_generate


def test_missing_active_archive_and_disabled_setting_do_not_generate() -> None:
    process_started_at = datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc)
    request_at = process_started_at + timedelta(minutes=1)
    clock = MutableClock(request_at + timedelta(minutes=30))
    current_summary = summary(request_at)
    calls: list[str] = []
    original_get = scheduler_module.get_codex_proxy_control_json
    original_generate = scheduler_module.generate_and_store_context_review

    scheduler_module.get_codex_proxy_control_json = lambda path, timeout_seconds=0: (
        {"sessions": [dict(current_summary)]} if path == "/api/proxy/sessions" else dict(current_summary)
    )
    scheduler_module.generate_and_store_context_review = lambda _state, session_id, **_kwargs: (
        calls.append(session_id) or {"status": "skipped"}
    )
    try:
        settings = SimpleNamespace(
            context_review_auto_enabled=True,
            context_review_interval_minutes=10,
        )
        scheduler = scheduler_module.ContextReviewIdleScheduler(
            SimpleNamespace(settings=settings),
            now=clock,
            process_probe=lambda: scheduler_module.DesktopProcessRun("run-1", process_started_at),
            archive_probe=lambda: set(),
        )
        scheduler._tick()
        assert calls == []

        settings.context_review_auto_enabled = False
        scheduler._tick()
        assert scheduler._targets == {}
        assert calls == []
    finally:
        scheduler_module.get_codex_proxy_control_json = original_get
        scheduler_module.generate_and_store_context_review = original_generate


def main() -> None:
    tests = [
        test_targets_each_session_from_real_proxy_request_time,
        test_same_session_request_cancels_running_review,
        test_missing_active_archive_and_disabled_setting_do_not_generate,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} context review scheduler tests passed")


if __name__ == "__main__":
    main()
