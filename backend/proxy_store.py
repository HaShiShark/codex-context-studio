from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codex_context import (
    is_context_control_command_text,
    is_contextual_user_text,
)

try:
    from .proxy_core import (
        ProxyState,
        handle_request as proxy_core_handle_request,
        handle_response_completed as proxy_core_handle_response_completed,
    )
    from .proxy_session_storage import ProxySessionStorage
    from .transcript_codec import (
        input_items_to_transcript as core_input_items_to_transcript,
        transcript_to_input_items as core_transcript_to_input_items,
    )
except ImportError:
    repo_root_for_import = Path(__file__).resolve().parents[1]
    if str(repo_root_for_import) not in sys.path:
        sys.path.insert(0, str(repo_root_for_import))
    from backend.proxy_core import (
        ProxyState,
        handle_request as proxy_core_handle_request,
        handle_response_completed as proxy_core_handle_response_completed,
    )
    from backend.proxy_session_storage import ProxySessionStorage
    from backend.transcript_codec import (
        input_items_to_transcript as core_input_items_to_transcript,
        transcript_to_input_items as core_transcript_to_input_items,
    )


DATA_DIR = Path(os.environ.get("HASH_CONTEXT_PROXY_DATA_DIR", Path.home() / ".hash-context-codex"))
STATE_PATH = DATA_DIR / "proxy_state.json"
USAGE_EVENT_LIMIT = 500


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def compact_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = (
                    item.get("text")
                    or item.get("input_text")
                    or item.get("output_text")
                    or item.get("summary_text")
                    or item.get("reasoning_text")
                )
                if text:
                    parts.append(str(text))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(value, dict):
        text = (
            value.get("text")
            or value.get("input_text")
            or value.get("output_text")
            or value.get("summary_text")
            or value.get("reasoning_text")
        )
        if text:
            return str(text)
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)
    return str(value)


def read_message_text(item: dict[str, Any]) -> str:
    if "content" in item:
        return compact_text(item.get("content"))
    return compact_text(item.get("text"))


def provider_message(role: str, text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": role,
        "content": text,
    }


def sanitize_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in value.strip())
    return cleaned[:120] or uuid.uuid4().hex


CODEX_SESSION_HEADER_NAMES = {
    "session-id",
    "session_id",
    "thread-id",
    "thread_id",
    "x-client-request-id",
    "x-codex-beta-features",
    "x-codex-installation-id",
    "x-codex-parent-thread-id",
    "x-codex-turn-metadata",
    "x-codex-turn-state",
    "x-codex-window-id",
    "x-openai-subagent",
}


def codex_session_headers_from_request(headers: dict[str, str]) -> dict[str, str]:
    return {
        key.lower(): str(value)
        for key, value in headers.items()
        if key.lower() in CODEX_SESSION_HEADER_NAMES and str(value).strip()
    }


def request_turn_metadata(headers: dict[str, str]) -> str:
    return str(headers.get("x-codex-turn-metadata") or headers.get("X-Codex-Turn-Metadata") or "")


def should_replace_transcript_from_control_intercept(
    existing_input_items: list[Any],
    candidate_input_items: list[Any],
) -> bool:
    return _conversation_input_item_count(candidate_input_items) >= _conversation_input_item_count(existing_input_items)


def _conversation_input_item_count(input_items: list[Any]) -> int:
    count = 0
    for item in input_items:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        if role == "assistant":
            count += 1
            continue
        if role != "user":
            continue
        text = read_message_text(item)
        if text.strip() and not is_contextual_user_text(text) and not is_context_control_command_text(text):
            count += 1
    return count


def usage_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(float(value.strip())))
        except ValueError:
            return 0
    return 0


def first_usage_int(record: dict[str, Any], *keys: str) -> int:
    for key in keys:
        if key in record:
            value = usage_int(record.get(key))
            if value:
                return value
    return 0


GPT55_INPUT_USD_PER_MILLION = 1.25
GPT55_CACHED_INPUT_USD_PER_MILLION = 0.125
GPT55_OUTPUT_USD_PER_MILLION = 10.0


def estimate_gpt55_cost_usd(input_tokens: int, cached_input_tokens: int, output_tokens: int) -> float:
    cached_tokens = min(max(0, cached_input_tokens), max(0, input_tokens))
    non_cached_tokens = max(0, input_tokens - cached_tokens)
    return (
        (non_cached_tokens * GPT55_INPUT_USD_PER_MILLION)
        + (cached_tokens * GPT55_CACHED_INPUT_USD_PER_MILLION)
        + (max(0, output_tokens) * GPT55_OUTPUT_USD_PER_MILLION)
    ) / 1_000_000


def empty_usage_bucket() -> dict[str, Any]:
    return {
        "request_count": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "non_cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "known_cost_usd": 0,
        "unknown_cost_request_count": 0,
        "cache_hit_rate": 0,
    }


def normalize_usage_payload(raw_usage: Any) -> dict[str, Any] | None:
    if not isinstance(raw_usage, dict):
        return None

    input_details = raw_usage.get("input_tokens_details") or raw_usage.get("prompt_tokens_details")
    if not isinstance(input_details, dict):
        input_details = {}
    output_details = raw_usage.get("output_tokens_details") or raw_usage.get("completion_tokens_details")
    if not isinstance(output_details, dict):
        output_details = {}

    input_tokens = first_usage_int(raw_usage, "input_tokens", "prompt_tokens")
    output_tokens = first_usage_int(raw_usage, "output_tokens", "completion_tokens")
    cached_input_tokens = first_usage_int(
        raw_usage,
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cached_tokens",
    ) or first_usage_int(
        input_details,
        "cached_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
    )
    reasoning_tokens = first_usage_int(
        raw_usage,
        "reasoning_tokens",
        "reasoning_output_tokens",
    ) or first_usage_int(
        output_details,
        "reasoning_tokens",
        "reasoning_output_tokens",
    )
    total_tokens = first_usage_int(raw_usage, "total_tokens") or input_tokens + output_tokens
    if not any((input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, total_tokens)):
        return None

    bucket = empty_usage_bucket()
    bucket.update(
        {
            "input_tokens": input_tokens,
            "cached_input_tokens": min(cached_input_tokens, input_tokens) if input_tokens else cached_input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": total_tokens,
            "unknown_cost_request_count": 0,
        }
    )
    bucket["non_cached_input_tokens"] = max(0, bucket["input_tokens"] - bucket["cached_input_tokens"])
    bucket["known_cost_usd"] = estimate_gpt55_cost_usd(
        bucket["input_tokens"],
        bucket["cached_input_tokens"],
        bucket["output_tokens"],
    )
    bucket["cache_hit_rate"] = (
        bucket["cached_input_tokens"] / bucket["input_tokens"] if bucket["input_tokens"] else 0
    )
    return bucket


def usage_event(kind: str, model: str, raw_usage: Any) -> dict[str, Any] | None:
    usage = normalize_usage_payload(raw_usage)
    if usage is None:
        return None
    return {
        "created_at": utc_timestamp(),
        "kind": kind,
        "model": model.strip() or "unknown",
        "usage": usage,
    }


def add_usage_to_bucket(bucket: dict[str, Any], usage: dict[str, Any], created_at: str) -> None:
    bucket["request_count"] += 1
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "non_cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    ):
        bucket[key] += usage_int(usage.get(key))
    bucket["known_cost_usd"] += estimate_gpt55_cost_usd(
        usage_int(usage.get("input_tokens")),
        usage_int(usage.get("cached_input_tokens")),
        usage_int(usage.get("output_tokens")),
    )
    bucket["unknown_cost_request_count"] = 0
    bucket["cache_hit_rate"] = (
        bucket["cached_input_tokens"] / bucket["input_tokens"] if bucket["input_tokens"] else 0
    )
    if created_at and (not bucket.get("latest_at") or created_at > str(bucket.get("latest_at") or "")):
        bucket["latest_at"] = created_at


def usage_summary_from_events(session_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    summary = empty_usage_bucket()
    summary["session_id"] = session_id
    by_kind: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}

    for event in events:
        if not isinstance(event, dict):
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        created_at = str(event.get("created_at") or "")
        kind = str(event.get("kind") or "unknown").strip() or "unknown"
        model = str(event.get("model") or "unknown").strip() or "unknown"
        add_usage_to_bucket(summary, usage, created_at)
        kind_bucket = by_kind.setdefault(kind, empty_usage_bucket())
        add_usage_to_bucket(kind_bucket, usage, created_at)
        model_bucket = by_model.setdefault(model, empty_usage_bucket())
        add_usage_to_bucket(model_bucket, usage, created_at)

    summary["by_kind"] = by_kind
    summary["by_model"] = by_model
    return summary


def usage_summary_with_event(session_id: str, current_summary: dict[str, Any] | None, event: dict[str, Any]) -> dict[str, Any]:
    summary = copy.deepcopy(current_summary) if isinstance(current_summary, dict) else usage_summary_from_events(session_id, [])
    summary["session_id"] = session_id
    usage = event.get("usage") if isinstance(event, dict) else None
    if not isinstance(usage, dict):
        return summary
    created_at = str(event.get("created_at") or "")
    kind = str(event.get("kind") or "unknown").strip() or "unknown"
    model = str(event.get("model") or "unknown").strip() or "unknown"
    by_kind = summary.setdefault("by_kind", {})
    if not isinstance(by_kind, dict):
        by_kind = {}
        summary["by_kind"] = by_kind
    by_model = summary.setdefault("by_model", {})
    if not isinstance(by_model, dict):
        by_model = {}
        summary["by_model"] = by_model
    add_usage_to_bucket(summary, usage, created_at)
    kind_bucket = by_kind.setdefault(kind, empty_usage_bucket())
    add_usage_to_bucket(kind_bucket, usage, created_at)
    model_bucket = by_model.setdefault(model, empty_usage_bucket())
    add_usage_to_bucket(model_bucket, usage, created_at)
    return summary


@dataclass
class ProxySession:
    id: str
    title: str
    status: str = "mirror"
    transcript: list[dict[str, Any]] = field(default_factory=list)
    proxy_state: ProxyState = field(default_factory=ProxyState)
    transcript_version: int = 0
    workbench_history: list[dict[str, Any]] = field(default_factory=list)
    request_log: list[dict[str, Any]] = field(default_factory=list)
    usage_events: list[dict[str, Any]] = field(default_factory=list)
    usage_summary_cache: dict[str, Any] | None = None
    last_codex_session_headers: dict[str, str] = field(default_factory=dict)
    last_turn_metadata_header: str = ""
    last_error: str = ""
    created_at: str = field(default_factory=utc_timestamp)
    updated_at: str = field(default_factory=utc_timestamp)
    payloads_loaded: bool = True
    inflight_before_request: ProxyState | None = None

    def visible_transcript(self) -> list[dict[str, Any]]:
        return self.transcript

    def usage_summary(self) -> dict[str, Any]:
        if isinstance(self.usage_summary_cache, dict):
            summary = copy.deepcopy(self.usage_summary_cache)
            summary["session_id"] = self.id
            summary.setdefault("by_kind", {})
            summary.setdefault("by_model", {})
            return summary
        return usage_summary_from_events(self.id, self.usage_events)

    def metadata_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "is_running": self.status in {"running", "compacting"},
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "transcript_version": self.transcript_version,
            "usage_summary": self.usage_summary(),
        }

    def to_payload(self) -> dict[str, Any]:
        _ensure_session_proxy_state(self)
        visible_transcript = self.visible_transcript()
        return {
            **self.metadata_payload(),
            "transcript": visible_transcript,
            "tail_conflict": self.proxy_state.tail_conflict,
            "compact_pending": self.proxy_state.compact_pending,
            "compact_kind": self.proxy_state.compact_kind,
        }

    def should_expose(self) -> bool:
        return not self.id.startswith("session-")


def _looks_like_proxy_core_transcript(transcript: Any) -> bool:
    if not isinstance(transcript, list):
        return False
    return all(
        isinstance(node, dict)
        and isinstance(node.get("items"), list)
        and "providerItems" not in node
        for node in transcript
    )


def _core_transcript_from_input_items(input_items: Any) -> list[dict[str, Any]]:
    if isinstance(input_items, str):
        return core_input_items_to_transcript([provider_message("user", input_items)])
    if isinstance(input_items, list):
        return core_input_items_to_transcript(input_items)
    return []


def _core_transcript_from_session_transcript(
    transcript: Any,
    *,
    strict: bool = False,
) -> list[dict[str, Any]]:
    if _looks_like_proxy_core_transcript(transcript):
        return copy.deepcopy(transcript)
    if strict and transcript:
        raise ValueError("transcript must be proxy core transcript nodes")
    return []


def _sync_session_from_proxy_state(session: ProxySession) -> None:
    session.transcript = copy.deepcopy(session.proxy_state.transcript)


def _ensure_session_proxy_state(session: ProxySession) -> None:
    if not isinstance(session.proxy_state, ProxyState):
        session.proxy_state = ProxyState()
    if not session.proxy_state.transcript and session.transcript:
        session.proxy_state.transcript = _core_transcript_from_session_transcript(session.transcript)
    _sync_session_from_proxy_state(session)


def _replace_session_proxy_state_from_input(session: ProxySession, input_items: Any) -> None:
    session.proxy_state = ProxyState(transcript=_core_transcript_from_input_items(input_items))
    session.proxy_state.codex_input_cursor = core_transcript_to_input_items(session.proxy_state.transcript)
    _sync_session_from_proxy_state(session)


class ProxyStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        storage_root = path if path.suffix == "" else path.parent
        self.storage = ProxySessionStorage(storage_root)
        self.lock = threading.RLock()
        self.sessions: dict[str, ProxySession] = {}
        self.active_session_id = ""
        self.load()

    def _sessions_root(self) -> Path:
        return self.storage.sessions_root

    def _session_dir(self, session: ProxySession) -> Path:
        return self.storage.session_dir(session.id)

    def _delete_session_files(self, session_id: str) -> bool:
        session_dir = self.storage.session_dir(session_id)
        try:
            resolved_root = self.storage.sessions_root.resolve()
            resolved_session_dir = session_dir.resolve()
        except OSError:
            return False
        if resolved_session_dir == resolved_root or resolved_root not in resolved_session_dir.parents:
            return False
        if not resolved_session_dir.exists():
            return False
        shutil.rmtree(resolved_session_dir)
        return True

    def _session_from_payload(self, payload: Any) -> ProxySession:
        metadata = payload.metadata
        usage_events = [item for item in metadata.get("usage_events", []) if isinstance(item, dict)]
        usage_summary_cache = metadata.get("usage_summary") if isinstance(metadata.get("usage_summary"), dict) else None
        proxy_state = ProxyState(
            transcript=copy.deepcopy(payload.transcript),
            codex_input_cursor=copy.deepcopy(payload.cursor),
            tail_conflict=bool(metadata.get("tail_conflict")),
            compact_pending=bool(metadata.get("compact_pending")),
            compact_kind=str(metadata.get("compact_kind") or ""),
            compact_error=str(metadata.get("last_error") or "") or None,
        )
        session = ProxySession(
            id=str(metadata.get("id") or ""),
            title=str(metadata.get("title") or "Codex Session"),
            status=str(metadata.get("status") or "mirror"),
            proxy_state=proxy_state,
            transcript=copy.deepcopy(proxy_state.transcript),
            transcript_version=int(metadata.get("transcript_version") or 0),
            workbench_history=copy.deepcopy(payload.workbench_history),
            request_log=[],
            usage_events=usage_events,
            usage_summary_cache=usage_summary_cache,
            last_codex_session_headers={
                str(key): str(value)
                for key, value in (metadata.get("last_codex_session_headers") or {}).items()
            }
            if isinstance(metadata.get("last_codex_session_headers"), dict)
            else {},
            last_turn_metadata_header=str(metadata.get("last_turn_metadata_header") or ""),
            last_error=str(metadata.get("last_error") or ""),
            created_at=str(metadata.get("created_at") or utc_timestamp()),
            updated_at=str(metadata.get("updated_at") or utc_timestamp()),
            payloads_loaded=True,
        )
        _sync_session_from_proxy_state(session)
        return session

    def _ensure_session_payloads_loaded(self, session: ProxySession) -> None:
        if session.payloads_loaded:
            _ensure_session_proxy_state(session)
            return
        payload = self.storage.load_session(session.id)
        if payload is not None:
            loaded = self._session_from_payload(payload)
            session.title = loaded.title
            session.status = loaded.status
            session.transcript = loaded.transcript
            session.proxy_state = loaded.proxy_state
            session.transcript_version = loaded.transcript_version
            session.workbench_history = loaded.workbench_history
            session.usage_events = loaded.usage_events
            session.usage_summary_cache = loaded.usage_summary_cache
            session.last_codex_session_headers = loaded.last_codex_session_headers
            session.last_turn_metadata_header = loaded.last_turn_metadata_header
            session.last_error = loaded.last_error
            session.created_at = loaded.created_at
            session.updated_at = loaded.updated_at
        _ensure_session_proxy_state(session)
        session.payloads_loaded = True

    def _persist_session(self, session: ProxySession) -> None:
        _ensure_session_proxy_state(session)
        session.request_log = session.request_log[-20:]
        session.usage_events = session.usage_events[-USAGE_EVENT_LIMIT:]
        session.usage_summary_cache = session.usage_summary()
        self.storage.save_session(session, usage_summary=lambda item: item.usage_summary())

    def load(self) -> None:
        active_session_id, payloads = self.storage.load_all_sessions()
        self.active_session_id = active_session_id
        self.sessions = {}
        for payload in payloads:
            session = self._session_from_payload(payload)
            if session.id:
                self.sessions[session.id] = session

    def save(self, session_ids: str | set[str] | list[str] | tuple[str, ...] | None = None) -> None:
        with self.lock:
            if session_ids is None:
                target_ids = set(self.sessions.keys())
            elif isinstance(session_ids, str):
                target_ids = {session_ids}
            else:
                target_ids = {str(session_id) for session_id in session_ids if str(session_id)}

            for session_id in target_ids:
                session = self.sessions.get(session_id)
                if session is not None:
                    self._persist_session(session)
            self.storage.update_index(self.sessions.values(), active_session_id=self.active_session_id)

    def prune_sessions_missing_from_codex(self, codex_thread_ids: set[str]) -> dict[str, Any]:
        normalized_codex_ids = {sanitize_id(session_id).lower() for session_id in codex_thread_ids if session_id}
        with self.lock:
            deleted_session_ids: list[str] = []
            kept_session_ids: list[str] = []
            for session_id in sorted(list(self.sessions)):
                normalized_session_id = sanitize_id(session_id).lower()
                if not normalized_session_id or normalized_session_id.startswith("session-"):
                    kept_session_ids.append(session_id)
                    continue
                if normalized_session_id in normalized_codex_ids:
                    kept_session_ids.append(session_id)
                    continue
                self.sessions.pop(session_id, None)
                self._delete_session_files(session_id)
                deleted_session_ids.append(session_id)

            if self.active_session_id in deleted_session_ids:
                self.active_session_id = ""
            self.storage.update_index(self.sessions.values(), active_session_id=self.active_session_id)
            return {
                "status": "ok",
                "deleted_session_ids": deleted_session_ids,
                "kept_session_ids": kept_session_ids,
            }

    def begin_request(self, session_id: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[ProxySession, dict[str, Any]]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                session = ProxySession(id=session_id, title=f"Codex {session_id[:8]}")
                self.sessions[session_id] = session
            elif not session.payloads_loaded:
                self._ensure_session_payloads_loaded(session)
            else:
                _ensure_session_proxy_state(session)

            previous_transcript = copy.deepcopy(session.proxy_state.transcript)
            self.active_session_id = session_id
            current_turn_metadata = request_turn_metadata(headers)
            current_codex_session_headers = codex_session_headers_from_request(headers)
            if current_codex_session_headers:
                session.last_codex_session_headers = current_codex_session_headers

            session.inflight_before_request = ProxyState(
                transcript=copy.deepcopy(session.proxy_state.transcript),
                codex_input_cursor=copy.deepcopy(session.proxy_state.codex_input_cursor),
                tail_conflict=session.proxy_state.tail_conflict,
                compact_pending=session.proxy_state.compact_pending,
                compact_kind=session.proxy_state.compact_kind,
                compact_error=session.proxy_state.compact_error,
            )
            draft_state = ProxyState(
                transcript=copy.deepcopy(session.proxy_state.transcript),
                codex_input_cursor=copy.deepcopy(session.proxy_state.codex_input_cursor),
                tail_conflict=session.proxy_state.tail_conflict,
                compact_pending=session.proxy_state.compact_pending,
                compact_kind=session.proxy_state.compact_kind,
                compact_error=session.proxy_state.compact_error,
            )
            forwarded_body = proxy_core_handle_request(draft_state, body)
            session.proxy_state = draft_state
            _sync_session_from_proxy_state(session)
            if session.proxy_state.transcript != previous_transcript:
                session.transcript_version += 1
            session.status = "compacting" if session.proxy_state.compact_pending else "running"
            session.payloads_loaded = True
            if current_turn_metadata:
                session.last_turn_metadata_header = current_turn_metadata
            session.last_error = ""
            session.updated_at = utc_timestamp()
            session.request_log.append(
                {
                    "created_at": session.updated_at,
                    "kind": "proxy_core_compact" if session.proxy_state.compact_pending else "proxy_core_request",
                    "headers": {key: value for key, value in headers.items() if key.lower().startswith("x-")},
                    "body": body,
                    "forwarded_body": forwarded_body,
                    "tail_conflict": session.proxy_state.tail_conflict,
                    "compact_kind": session.proxy_state.compact_kind,
                }
            )
            session.request_log = session.request_log[-20:]
            self.save(session.id)
            return session, forwarded_body

    def codex_session_headers(self, session_id: str) -> dict[str, str]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                return {}
            self._ensure_session_payloads_loaded(session)
            if session.last_codex_session_headers:
                return dict(session.last_codex_session_headers)
            for entry in reversed(session.request_log):
                if not isinstance(entry, dict):
                    continue
                entry_headers = entry.get("headers")
                if not isinstance(entry_headers, dict):
                    continue
                session_headers = codex_session_headers_from_request(
                    {str(key).lower(): str(value) for key, value in entry_headers.items()}
                )
                if session_headers:
                    return session_headers
            return {}

    def record_control_intercept(self, session_id: str, body: dict[str, Any], headers: dict[str, str], command: str) -> ProxySession:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                session = ProxySession(id=session_id, title=f"Codex {session_id[:8]}")
                self.sessions[session_id] = session
            else:
                self._ensure_session_payloads_loaded(session)
            source_input_items = body.get("input") if isinstance(body.get("input"), list) else []
            existing_input_items = core_transcript_to_input_items(session.proxy_state.transcript)
            if should_replace_transcript_from_control_intercept(existing_input_items, source_input_items):
                _replace_session_proxy_state_from_input(session, source_input_items)
                session.transcript_version += 1
            session.status = "mirror"
            current_codex_session_headers = codex_session_headers_from_request(headers)
            if current_codex_session_headers:
                session.last_codex_session_headers = current_codex_session_headers
            current_turn_metadata = request_turn_metadata(headers)
            if current_turn_metadata:
                session.last_turn_metadata_header = current_turn_metadata
            session.last_error = ""
            session.updated_at = utc_timestamp()
            self.active_session_id = session_id
            session.request_log.append(
                {
                    "created_at": session.updated_at,
                    "kind": "context_control_intercept",
                    "command": command,
                    "headers": {key: value for key, value in headers.items() if key.lower().startswith("x-")},
                    "body": body,
                }
            )
            session.request_log = session.request_log[-20:]
            self.save(session.id)
            return session

    def complete_response(self, session_id: str, items: list[dict[str, Any]], text: str) -> None:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                return
            if not session.payloads_loaded:
                self._ensure_session_payloads_loaded(session)
            else:
                _ensure_session_proxy_state(session)
            response_items = items or ([{"type": "message", "role": "assistant", "content": text}] if text else [])
            previous_transcript = copy.deepcopy(session.proxy_state.transcript)
            draft_state = ProxyState(
                transcript=copy.deepcopy(session.proxy_state.transcript),
                codex_input_cursor=copy.deepcopy(session.proxy_state.codex_input_cursor),
                tail_conflict=session.proxy_state.tail_conflict,
                compact_pending=session.proxy_state.compact_pending,
                compact_kind=session.proxy_state.compact_kind,
                compact_error=session.proxy_state.compact_error,
            )
            result = proxy_core_handle_response_completed(draft_state, response_items, text)
            session.proxy_state = draft_state
            _sync_session_from_proxy_state(session)
            if session.proxy_state.transcript != previous_transcript:
                session.transcript_version += 1
            session.status = "mirror"
            if result.compact_handled and session.proxy_state.compact_error:
                session.status = "error"
                session.last_error = session.proxy_state.compact_error
            else:
                session.last_error = ""
            session.payloads_loaded = True
            session.updated_at = utc_timestamp()
            session.inflight_before_request = None
            self.save(session.id)

    def fail_response(self, session_id: str, message: str) -> None:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                return
            self._ensure_session_payloads_loaded(session)
            previous_transcript = copy.deepcopy(session.proxy_state.transcript)
            if session.proxy_state.compact_pending and session.inflight_before_request is not None:
                session.proxy_state = session.inflight_before_request
                session.inflight_before_request = None
            session.status = "error"
            session.last_error = message
            session.proxy_state.compact_pending = False
            session.proxy_state.compact_kind = ""
            session.proxy_state.compact_error = message
            _sync_session_from_proxy_state(session)
            if session.proxy_state.transcript != previous_transcript:
                session.transcript_version += 1
            session.updated_at = utc_timestamp()
            self.save(session.id)

    def record_usage(self, session_id: str, kind: str, model: str, raw_usage: Any) -> None:
        event = usage_event(kind, model, raw_usage)
        if event is None:
            return
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                session = ProxySession(id=session_id, title=f"Codex {session_id[:8]}")
                self.sessions[session_id] = session
            else:
                self._ensure_session_payloads_loaded(session)
            session.usage_events.append(event)
            session.usage_events = session.usage_events[-USAGE_EVENT_LIMIT:]
            session.usage_summary_cache = usage_summary_with_event(session.id, session.usage_summary_cache, event)
            session.updated_at = utc_timestamp()
            self.save(session.id)

    def all_usage(self) -> dict[str, Any]:
        with self.lock:
            exposed_sessions = [session for session in self.sessions.values() if session.should_expose()]
            sessions: dict[str, dict[str, Any]] = {}
            overall_events: list[dict[str, Any]] = []
            for session in exposed_sessions:
                self._ensure_session_payloads_loaded(session)
                sessions[session.id] = session.usage_summary()
                overall_events.extend(session.usage_events)
            return {
                "overall": usage_summary_from_events("overall", overall_events),
                "sessions": sessions,
            }

    def session_usage(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                return None
            self._ensure_session_payloads_loaded(session)
            return {"summary": session.usage_summary()}

    def reset_usage(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            self._ensure_session_payloads_loaded(session)
            cleared_count = len(session.usage_events)
            session.usage_events = []
            session.usage_summary_cache = usage_summary_from_events(session.id, [])
            session.updated_at = utc_timestamp()
            self.save(session.id)
            return {"cleared_count": cleared_count, "summary": session.usage_summary()}

    def list_sessions(self) -> dict[str, Any]:
        with self.lock:
            sessions = [
                session
                for session in sorted(self.sessions.values(), key=lambda item: item.updated_at, reverse=True)
                if session.should_expose()
            ]
            active_session_id = self.active_session_id
            if active_session_id not in {session.id for session in sessions}:
                active_session_id = sessions[0].id if sessions else ""
            return {
                "active_session_id": active_session_id,
                "sessions": [session.metadata_payload() for session in sessions],
            }

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is not None:
                self._ensure_session_payloads_loaded(session)
            return session.to_payload() if session else None

    def replace_transcript(self, session_id: str, transcript: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                session = ProxySession(id=session_id, title=f"Codex {session_id[:8]}")
                self.sessions[session_id] = session
            else:
                self._ensure_session_payloads_loaded(session)
            _ensure_session_proxy_state(session)
            previous_input = core_transcript_to_input_items(session.proxy_state.transcript)
            next_core_transcript = _core_transcript_from_session_transcript(transcript, strict=True)
            next_input = core_transcript_to_input_items(next_core_transcript)
            session.proxy_state.transcript = copy.deepcopy(next_core_transcript)
            session.proxy_state.tail_conflict = False
            session.transcript_version += 1
            _sync_session_from_proxy_state(session)
            session.status = "mirror"
            session.updated_at = utc_timestamp()
            self.active_session_id = session_id
            self.save(session.id)
            payload = session.to_payload()
            payload["changed"] = previous_input != next_input
            return payload


STORE = ProxyStore(STATE_PATH)
