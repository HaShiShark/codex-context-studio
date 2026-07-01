from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import http.client
import io
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    from .codex_input_cursor import fingerprint_provider_item
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
    from backend.codex_input_cursor import fingerprint_provider_item
    from backend.transcript_codec import (
        input_items_to_transcript as core_input_items_to_transcript,
        transcript_to_input_items as core_transcript_to_input_items,
    )



HOST = os.environ.get("HASH_CONTEXT_PROXY_HOST", os.environ.get("HASH_CONTEXT_HOST", "localhost"))
PORT = int(os.environ.get("HASH_CONTEXT_PROXY_PORT", "8787"))
OPENAI_UPSTREAM_BASE_URL = os.environ.get(
    "HASH_CONTEXT_OPENAI_UPSTREAM_BASE_URL",
    os.environ.get("HASH_CONTEXT_UPSTREAM_BASE_URL", "https://api.openai.com/v1"),
)
CHATGPT_UPSTREAM_BASE_URL = os.environ.get(
    "HASH_CONTEXT_CHATGPT_UPSTREAM_BASE_URL",
    "https://chatgpt.com/backend-api/codex",
)
FORCE_UPSTREAM_BASE_URL = os.environ.get("HASH_CONTEXT_FORCE_UPSTREAM_BASE_URL", "").strip()
FORCE_UPSTREAM_API_KEY = os.environ.get("HASH_CONTEXT_FORCE_UPSTREAM_API_KEY", "").strip()
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("HASH_CONTEXT_PROXY_DATA_DIR", Path.home() / ".hash-context-codex"))
STATE_PATH = DATA_DIR / "proxy_state.json"
LOG_PATH = DATA_DIR / "proxy.log"
CODEX_PROXY_PROVIDER_ID = "codex-proxy"
CODEX_PROXY_BASE_URL = f"http://{HOST}:{PORT}/v1"
INTERNAL_CONTEXT_HEADER = "x-hash-context-internal"
INTERNAL_CONTEXT_VALUE = "context-workbench"
CONTEXT_CONTROL_NOTICE_TEXT = "Hash Context: opened workbench."
CONTROL_PORT = int(os.environ.get("HASH_CONTEXT_CONTROL_PORT", "8790"))
_UPSTREAM_AUTH_LOCK = threading.Lock()
_UPSTREAM_AUTH_HEADERS: dict[str, str] = {}
CODEX_AUTH_PATH = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def proxy_log(message: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text("", encoding="utf-8") if not LOG_PATH.exists() else None
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{utc_timestamp()} {message}\n")
    except Exception:
        pass


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


def context_control_command_from_input(input_items: Any) -> str:
    if not isinstance(input_items, list):
        return ""
    for item in reversed(input_items):
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "message" or str(item.get("role") or "") != "user":
            continue
        text = read_message_text(item).strip()
        return text if is_context_control_command_text(text) else ""
    return ""


CODEX_UI_TITLE_GENERATION_MARKERS = (
    "you are a helpful assistant. you will be presented with a user prompt",
    "provide a short title for a task that will be created from that prompt",
    "generate a concise ui title",
    "fill the structured title field with plain text",
    "user prompt:",
)
HASH_CONTEXT_TITLE_PROMPT_MARKER = "请根据下面这条新对话的第一条用户消息，生成一个对话标题。"
HASH_CONTEXT_TITLE_INSTRUCTION_MARKERS = (
    "你只负责给一段新对话起标题。",
    "标题要短、具体、自然，优先使用用户的语言。",
    "最多 18 个中文字符或 8 个英文单词。",
)


def normalize_detection_text(value: str) -> str:
    return " ".join(value.lower().split())


def is_title_generation_request(body: dict[str, Any]) -> bool:
    input_items = body.get("input") if isinstance(body, dict) else None
    input_items = input_items if isinstance(input_items, list) else []
    user_texts = [
        read_message_text(item)
        for item in input_items
        if isinstance(item, dict)
        and str(item.get("type") or "") == "message"
        and str(item.get("role") or "").strip() == "user"
    ]
    if not user_texts:
        return False

    for text in user_texts:
        normalized = normalize_detection_text(text)
        if all(marker in normalized for marker in CODEX_UI_TITLE_GENERATION_MARKERS):
            return True

    instructions = compact_text(body.get("instructions") if isinstance(body, dict) else "")
    if any(HASH_CONTEXT_TITLE_PROMPT_MARKER in text for text in user_texts) and all(
        marker in instructions for marker in HASH_CONTEXT_TITLE_INSTRUCTION_MARKERS
    ):
        return True

    return False


def session_id_for_request(body: dict[str, Any], headers: dict[str, str]) -> str:
    for key in ("x-hash-context-session-id", "x-codex-conversation-id", "x-codex-session-id"):
        value = headers.get(key)
        if value:
            return sanitize_id(value)
    metadata_session_id = session_id_from_codex_metadata(headers)
    if metadata_session_id:
        return sanitize_id(metadata_session_id)
    prompt_cache_key = body.get("prompt_cache_key")
    if isinstance(prompt_cache_key, str) and prompt_cache_key.strip():
        return sanitize_id(prompt_cache_key)
    metadata = body.get("client_metadata")
    if isinstance(metadata, dict):
        for value in metadata.values():
            if isinstance(value, str) and value.strip():
                return sanitize_id(value)
    digest = hashlib.sha1(json.dumps(body.get("input", []), sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return f"session-{digest[:16]}"


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


def fallback_codex_session_headers(session_id: str) -> dict[str, str]:
    safe_session_id = sanitize_id(session_id)
    if not safe_session_id:
        return {}
    return {
        "session-id": safe_session_id,
        "session_id": safe_session_id,
        "thread-id": safe_session_id,
        "thread_id": safe_session_id,
        "x-client-request-id": safe_session_id,
        "x-codex-window-id": f"{safe_session_id}:0",
        "x-codex-turn-metadata": json.dumps(
            {
                "session_id": safe_session_id,
                "thread_id": safe_session_id,
                "thread_source": "hash_context_workbench",
                "turn_id": f"hash-context-{safe_session_id}",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def merge_codex_session_headers(
    headers: dict[str, str],
    session_headers: dict[str, str],
    *,
    session_id: str = "",
) -> dict[str, str]:
    merged = dict(headers)
    effective_session_headers = dict(session_headers)
    for key, value in fallback_codex_session_headers(session_id).items():
        effective_session_headers.setdefault(key, value)
    lowered_existing = {key.lower() for key in merged}
    for key, value in effective_session_headers.items():
        lower_key = key.lower()
        if lower_key in lowered_existing or not str(value).strip():
            continue
        merged[lower_key] = str(value)
    return merged


def session_id_for_compact_request(body: dict[str, Any], headers: dict[str, str], active_session_id: str) -> str:
    for key in ("x-hash-context-session-id", "x-codex-conversation-id", "x-codex-session-id"):
        value = headers.get(key)
        if value:
            return sanitize_id(value)
    metadata_session_id = session_id_from_codex_metadata(headers)
    if metadata_session_id:
        return sanitize_id(metadata_session_id)
    if active_session_id:
        return active_session_id
    return session_id_for_request(body, headers)


def session_id_from_codex_metadata(headers: dict[str, str]) -> str:
    for key in ("x-codex-turn-metadata", "x-codex-turn-state"):
        raw_value = headers.get(key)
        if not raw_value:
            continue
        try:
            parsed = json.loads(raw_value)
        except (TypeError, json.JSONDecodeError):
            continue
        session_id = find_session_id_in_value(parsed)
        if session_id:
            return session_id
    return ""


def find_session_id_in_value(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("session_id", "conversation_id", "thread_id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for key, nested_value in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in ("session", "conversation", "thread")):
                found = find_session_id_in_value(nested_value)
                if found:
                    return found
        for nested_value in value.values():
            found = find_session_id_in_value(nested_value)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_session_id_in_value(item)
            if found:
                return found
    return ""


def sanitize_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in value.strip())
    return cleaned[:120] or uuid.uuid4().hex


def response_headers_for_upstream(headers: dict[str, str]) -> dict[str, str]:
    hop_by_hop = {
        "host",
        "content-length",
        "content-encoding",
        "connection",
        "accept-encoding",
        "transfer-encoding",
        INTERNAL_CONTEXT_HEADER,
        "x-hash-context-session-id",
    }
    canonical_names = {
        "accept": "Accept",
        "authorization": "Authorization",
        "chatgpt-account-id": "ChatGPT-Account-ID",
        "content-type": "Content-Type",
        "cookie": "Cookie",
        "openai-beta": "OpenAI-Beta",
        "originator": "originator",
        "user-agent": "User-Agent",
        "x-client-request-id": "x-client-request-id",
        "x-codex-beta-features": "x-codex-beta-features",
        "x-codex-installation-id": "x-codex-installation-id",
        "x-codex-parent-thread-id": "x-codex-parent-thread-id",
        "x-codex-turn-metadata": "x-codex-turn-metadata",
        "x-codex-turn-state": "x-codex-turn-state",
        "x-codex-window-id": "x-codex-window-id",
        "x-openai-subagent": "x-openai-subagent",
        "x-responsesapi-include-timing-metrics": "x-responsesapi-include-timing-metrics",
    }
    next_headers: dict[str, str] = {}
    for key, value in headers.items():
        lower_key = key.lower()
        if lower_key in hop_by_hop:
            continue
        next_headers[canonical_names.get(lower_key, key)] = value

    # Codex sends these through reqwest, but be explicit here because the
    # proxy reserializes the JSON body before forwarding it.
    next_headers["Content-Type"] = "application/json"
    next_headers["Accept"] = "text/event-stream"
    next_headers.setdefault("User-Agent", "codex_cli_rs")
    return next_headers


def _has_usable_auth(headers: dict[str, str]) -> bool:
    authorization = str(headers.get("Authorization") or headers.get("authorization") or "").strip()
    if authorization and authorization.lower() not in {"bearer not-needed", "bearer dummy", "bearer fake"}:
        return True
    return bool(
        str(headers.get("ChatGPT-Account-ID") or headers.get("chatgpt-account-id") or "").strip()
        or str(headers.get("Cookie") or headers.get("cookie") or "").strip()
    )


def remember_upstream_auth(headers: dict[str, str]) -> None:
    candidate = response_headers_for_upstream(headers)
    if not _has_usable_auth(candidate):
        return
    cached = {
        key: value
        for key, value in candidate.items()
        if key.lower()
        not in {
            "accept",
            "content-type",
            "content-length",
            "host",
            "connection",
            "transfer-encoding",
            INTERNAL_CONTEXT_HEADER,
        }
    }
    with _UPSTREAM_AUTH_LOCK:
        _UPSTREAM_AUTH_HEADERS.clear()
        _UPSTREAM_AUTH_HEADERS.update(cached)


def preload_codex_subscription_auth() -> bool:
    try:
        raw = json.loads(CODEX_AUTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    tokens = raw.get("tokens") if isinstance(raw, dict) else None
    if not isinstance(tokens, dict):
        return False

    access_token = str(tokens.get("access_token") or "").strip()
    account_id = str(tokens.get("account_id") or "").strip()
    if not access_token or not account_id:
        return False

    headers = {
        "Authorization": f"Bearer {access_token}",
        "ChatGPT-Account-ID": account_id,
        "User-Agent": "codex_cli_rs",
        "originator": "codex-tui",
    }
    remember_upstream_auth(headers)
    return bool(cached_upstream_auth_headers())


def preload_openai_api_auth() -> bool:
    api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return False
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "codex_cli_rs",
    }
    remember_upstream_auth(headers)
    return bool(cached_upstream_auth_headers())


def preload_force_upstream_auth() -> bool:
    if not FORCE_UPSTREAM_API_KEY:
        return False
    headers = {
        "Authorization": f"Bearer {FORCE_UPSTREAM_API_KEY}",
        "User-Agent": "codex_cli_rs",
    }
    remember_upstream_auth(headers)
    return bool(cached_upstream_auth_headers())


def cached_upstream_auth_headers() -> dict[str, str]:
    with _UPSTREAM_AUTH_LOCK:
        return dict(_UPSTREAM_AUTH_HEADERS)


def apply_cached_upstream_auth(headers: dict[str, str]) -> dict[str, str]:
    next_headers = dict(headers)
    cached = cached_upstream_auth_headers()
    if not cached:
        return next_headers

    if not _has_usable_auth(next_headers):
        for key, value in cached.items():
            if key.lower() == "accept":
                continue
            next_headers[key] = value
        return next_headers

    return next_headers


def upstream_headers_for_request(headers: dict[str, str], *, accept: str) -> dict[str, str]:
    remember_upstream_auth(headers)
    next_headers = apply_cached_upstream_auth(response_headers_for_upstream(headers))
    next_headers["Accept"] = accept
    next_headers["Content-Type"] = "application/json"
    return next_headers


def json_headers_for_upstream(headers: dict[str, str]) -> dict[str, str]:
    return upstream_headers_for_request(headers, accept="application/json")


def safe_headers_for_log(headers: dict[str, str]) -> dict[str, str]:
    redacted = {"authorization", "cookie", "set-cookie"}
    return {
        key: ("<redacted>" if key.lower() in redacted else value)
        for key, value in headers.items()
        if key.lower() not in {"host", "content-length"}
    }


def decode_request_body(raw_body: bytes, content_encoding: str | None) -> bytes:
    encoding = (content_encoding or "").strip().lower()
    if not encoding or encoding == "identity":
        return raw_body
    if encoding in {"gzip", "x-gzip"}:
        return gzip.decompress(raw_body)
    if encoding == "br":
        import brotli

        return brotli.decompress(raw_body)
    if encoding in {"zstd", "zstandard"}:
        import zstandard

        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw_body)) as reader:
            return reader.read()
    raise ValueError(f"unsupported request content-encoding: {content_encoding}")


def parse_json_request_body(raw_body: bytes, content_encoding: str | None) -> Any:
    decoded = decode_request_body(raw_body, content_encoding)
    return json.loads(decoded.decode("utf-8"))


def upstream_base_url_for_request(headers: dict[str, str]) -> str:
    if FORCE_UPSTREAM_BASE_URL:
        return FORCE_UPSTREAM_BASE_URL
    effective_headers = apply_cached_upstream_auth(headers)
    lowered = {key.lower(): value for key, value in effective_headers.items()}
    if lowered.get("chatgpt-account-id"):
        return CHATGPT_UPSTREAM_BASE_URL
    return OPENAI_UPSTREAM_BASE_URL


def has_effective_chatgpt_auth(headers: dict[str, str]) -> bool:
    effective_headers = apply_cached_upstream_auth(headers)
    lowered = {key.lower(): value for key, value in effective_headers.items()}
    return bool(str(lowered.get("chatgpt-account-id") or "").strip())


def has_effective_upstream_auth(headers: dict[str, str]) -> bool:
    return _has_usable_auth(apply_cached_upstream_auth(headers))


def open_context_workbench(session_id: str) -> tuple[bool, str]:
    path = "/show"
    if session_id:
        path = f"{path}?session_id={urllib.parse.quote(session_id, safe='')}"
    conn = http.client.HTTPConnection(os.environ.get("HASH_CONTEXT_HOST", "localhost"), CONTROL_PORT, timeout=2)
    try:
        conn.request("POST", path, body=b"", headers={"Content-Length": "0"})
        response = conn.getresponse()
        preview = response.read(1000).decode("utf-8", errors="replace")
        if 200 <= response.status < 300:
            return True, ""
        return False, f"window-control returned {response.status}: {preview}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


def is_internal_context_request(headers: dict[str, str], body: dict[str, Any] | None = None) -> bool:
    if str(headers.get(INTERNAL_CONTEXT_HEADER) or "").strip() == INTERNAL_CONTEXT_VALUE:
        return True
    for metadata_key in ("metadata", "client_metadata"):
        metadata = (body or {}).get(metadata_key) if isinstance(body, dict) else None
        if not isinstance(metadata, dict):
            continue
        if str(metadata.get("hash_context_internal") or "").strip() == INTERNAL_CONTEXT_VALUE:
            return True
    return False


def normalize_model_record(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        model_id = item.strip()
        if not model_id:
            return None
        return {"id": model_id, "object": "model", "owned_by": "codex"}

    if not isinstance(item, dict):
        return None

    model_id = str(item.get("id") or item.get("name") or item.get("slug") or "").strip()
    if not model_id:
        return None

    normalized = copy.deepcopy(item)
    normalized["id"] = model_id.removeprefix("models/")
    normalized.setdefault("object", "model")
    normalized.setdefault("owned_by", str(item.get("owned_by") or item.get("provider") or "codex"))

    ctx = _extract_context_window(item)
    if ctx is not None:
        normalized["context_window"] = ctx

    return normalized


def _extract_context_window(item: dict[str, Any]) -> int | None:
    if isinstance(item.get("context_window"), (int, float)):
        return int(item["context_window"])
    if isinstance(item.get("max_context_length"), (int, float)):
        return int(item["max_context_length"])
    if isinstance(item.get("max_context_window"), (int, float)):
        return int(item["max_context_window"])
    meta = item.get("meta") or item.get("metadata")
    if isinstance(meta, dict):
        for key in ("context_window", "max_context_length", "max_context_window"):
            if isinstance(meta.get(key), (int, float)):
                return int(meta[key])
    return None


DEFAULT_MODEL_CONTEXT_WINDOW = 200000


def normalize_models_payload(payload: Any) -> dict[str, Any] | None:
    raw_models: Any = None
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            raw_models = payload.get("data")
        elif isinstance(payload.get("models"), list):
            raw_models = payload.get("models")
        elif isinstance(payload.get("items"), list):
            raw_models = payload.get("items")
        elif isinstance(payload.get("model_slugs"), list):
            raw_models = payload.get("model_slugs")
    elif isinstance(payload, list):
        raw_models = payload

    if not isinstance(raw_models, list):
        return None

    normalized_models: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in raw_models:
        record = normalize_model_record(item)
        if record is None:
            continue
        model_id = str(record.get("id") or "").strip()
        if not model_id or model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        if "context_window" not in record or not isinstance(record.get("context_window"), (int, float)):
            record["context_window"] = DEFAULT_MODEL_CONTEXT_WINDOW
        normalized_models.append(record)

    return {"object": "list", "data": normalized_models}


def normalize_models_response_body(response_body: bytes) -> bytes:
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return response_body

    normalized = normalize_models_payload(payload)
    if normalized is None:
        return response_body
    return json.dumps(normalized, ensure_ascii=False).encode("utf-8")


def fallback_models_response_body() -> bytes:
    configured = os.environ.get("HASH_CONTEXT_FALLBACK_MODELS", "")
    model_ids = [
        item.strip()
        for item in configured.split(",")
        if item.strip()
    ] or [
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex",
        "gpt-5.2",
    ]
    payload = {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "owned_by": "codex",
                "context_window": DEFAULT_MODEL_CONTEXT_WINDOW,
            }
            for model_id in model_ids
        ],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


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


def request_turn_metadata(headers: dict[str, str]) -> str:
    return str(headers.get("x-codex-turn-metadata") or headers.get("X-Codex-Turn-Metadata") or "")


USAGE_EVENT_LIMIT = 500


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


def usage_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str):
        try:
            return max(0.0, float(value.strip()))
        except ValueError:
            return None
    return None


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


def json_dumps_compact(value: Any) -> str:
    return json.dumps(sanitize_json_value(value), ensure_ascii=False, separators=(",", ":"))


def json_loads_value(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return copy.deepcopy(default)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return copy.deepcopy(default)


def sanitize_json_value(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def safe_session_path_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return safe or uuid.uuid4().hex


def session_date_parts(created_at: str) -> tuple[str, str, str]:
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(created_at or ""))
    if match:
        return match.group(1), match.group(2), match.group(3)
    fallback = utc_timestamp()
    return fallback[:4], fallback[5:7], fallback[8:10]


def append_jsonl_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json_dumps_compact(payload))
        handle.write("\n")


def load_jsonl_state(path: Path, default: Any) -> Any:
    state = copy.deepcopy(default)
    if not path.exists():
        return state
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return state
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "")
            if event_type == "clear":
                state = None
            elif event_type == "set":
                state = copy.deepcopy(event.get("records", []))
            elif event_type == "append":
                records = event.get("records")
                if isinstance(state, list) and isinstance(records, list):
                    state = [*state, *copy.deepcopy(records)]
                elif isinstance(records, list):
                    state = copy.deepcopy(records)
            elif event_type == "replace_from":
                records = event.get("records")
                index = event.get("index")
                if isinstance(records, list):
                    try:
                        safe_index = int(index)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(state, list):
                        state = []
                    safe_index = max(0, min(safe_index, len(state)))
                    state = [*state[:safe_index], *copy.deepcopy(records)]
    return copy.deepcopy(default) if state is None and default is not None else state


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


class Handler(BaseHTTPRequestHandler):
    server_version = "HashContextProxy/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[proxy] {self.address_string()} - {format % args}")

    def do_OPTIONS(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        proxy_log(
            "incoming options "
            f"path={parsed.path} "
            f"access_control_request_method={self.headers.get('access-control-request-method', '')}"
        )
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/proxy/health":
            self._send_json({"ok": True})
            return

        proxy_log(
            "incoming get "
            f"path={parsed.path} "
            f"upgrade={self.headers.get('upgrade', '')} "
            f"accept={self.headers.get('accept', '')} "
            f"user_agent={self.headers.get('user-agent', '')}"
        )
        if parsed.path == "/api/proxy/sessions":
            self._send_json(STORE.list_sessions())
            return
        if parsed.path == "/api/proxy/usage":
            self._send_json(STORE.all_usage())
            return
        if parsed.path.startswith("/api/proxy/sessions/") and parsed.path.endswith("/usage"):
            session_id = urllib.parse.unquote(parsed.path.split("/api/proxy/sessions/", 1)[1].rsplit("/", 1)[0])
            usage = STORE.session_usage(session_id)
            if usage is None:
                self._send_json({"error": "session not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(usage)
            return
        if parsed.path.startswith("/api/proxy/sessions/"):
            session_id = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
            session = STORE.get_session(session_id)
            if session is None:
                self._send_json({"error": "session not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(session)
            return
        if parsed.path == "/v1/models":
            self._handle_models()
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _handle_models(self) -> None:
        headers = {key.lower(): value for key, value in self.headers.items()}
        upstream_base_url = upstream_base_url_for_request(headers)
        upstream = urllib.parse.urlparse(upstream_base_url.rstrip("/") + "/models")
        proxy_log(f"models upstream={upstream.geturl()}")
        connection_cls = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
        conn = connection_cls(upstream.hostname, upstream.port, timeout=60)
        path = upstream.path or "/v1/models"
        if upstream.query:
            path = f"{path}?{upstream.query}"

        try:
            upstream_headers = json_headers_for_upstream(headers)
            proxy_log(
                "models upstream headers "
                f"{json.dumps(safe_headers_for_log(upstream_headers), ensure_ascii=False, sort_keys=True)}"
            )
            conn.request("GET", path, headers=upstream_headers)
            upstream_response = conn.getresponse()
            response_body = upstream_response.read()
            proxy_log(
                "models upstream status "
                f"status={upstream_response.status} reason={upstream_response.reason} bytes={len(response_body)}"
            )

            status = upstream_response.status
            body = normalize_models_response_body(response_body) if status < 400 else response_body
            if status >= 400 and has_effective_chatgpt_auth(headers):
                proxy_log("models upstream failed for ChatGPT auth; serving local fallback model list")
                status = HTTPStatus.OK
                body = fallback_models_response_body()

            self.send_response(status)
            self._send_cors_headers()
            for key, value in upstream_response.getheaders():
                if key.lower() in {"transfer-encoding", "connection", "content-encoding", "content-length", "content-type"}:
                    continue
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            proxy_log(f"models error error={type(exc).__name__}: {exc}")
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        finally:
            conn.close()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        proxy_log(
            "incoming post "
            f"path={parsed.path} "
            f"content_type={self.headers.get('content-type', '')} "
            f"content_encoding={self.headers.get('content-encoding', '')} "
            f"content_length={self.headers.get('content-length', '')} "
            f"expect={self.headers.get('expect', '')}"
        )
        if parsed.path == "/v1/responses":
            self._handle_responses()
            return
        if parsed.path == "/v1/responses/compact":
            self._handle_compact()
            return
        if parsed.path.endswith("/transcript") and parsed.path.startswith("/api/proxy/sessions/"):
            session_id = urllib.parse.unquote(parsed.path.split("/api/proxy/sessions/", 1)[1].rsplit("/", 1)[0])
            payload = self._read_json()
            transcript = payload.get("transcript")
            if not isinstance(transcript, list):
                self._send_json({"error": "transcript must be a list"}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(STORE.replace_transcript(session_id, transcript))
            return
        if parsed.path.endswith("/usage/reset") and parsed.path.startswith("/api/proxy/sessions/"):
            session_id = urllib.parse.unquote(parsed.path.split("/api/proxy/sessions/", 1)[1].rsplit("/usage/", 1)[0])
            try:
                self._send_json(STORE.reset_usage(session_id))
            except KeyError:
                self._send_json({"error": "session not found"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _send_sse_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "message")
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"event: {event_type}\n".encode("utf-8"))
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _send_context_control_response(self, body: dict[str, Any], opened: bool, error: str = "") -> None:
        response_id = f"resp_hash_context_{uuid.uuid4().hex}"
        message_id = f"msg_hash_context_{uuid.uuid4().hex}"
        model = str(body.get("model") or "gpt-5.5")
        text = CONTEXT_CONTROL_NOTICE_TEXT if opened else f"Hash Context: workbench unavailable. {error}".strip()
        item = {
            "type": "message",
            "role": "assistant",
            "id": message_id,
            "content": [{"type": "output_text", "text": text}],
        }
        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self._send_sse_event({"type": "response.created", "response": {"id": response_id, "model": model}})
        self._send_sse_event({"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": [{"type": "output_text", "text": ""}]}})
        self._send_sse_event({"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "item_id": message_id, "delta": text})
        self._send_sse_event({"type": "response.output_item.done", "output_index": 0, "item": item})
        self._send_sse_event({"type": "response.completed", "response": {"id": response_id, "model": model, "output": [item]}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _handle_responses(self) -> None:
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        proxy_log(f"responses body bytes={len(raw_body)} prefix={raw_body[:80]!r}")
        try:
            body = parse_json_request_body(raw_body, self.headers.get("content-encoding"))
            if not isinstance(body, dict):
                raise json.JSONDecodeError("request body must be an object", "", 0)
        except json.JSONDecodeError:
            self._send_json({"error": "request body must be JSON"}, HTTPStatus.BAD_REQUEST)
            return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return

        headers = {key.lower(): value for key, value in self.headers.items()}
        is_internal_context = is_internal_context_request(headers, body)
        is_title_generation = is_title_generation_request(body)
        capture_proxy_session = not is_internal_context and not is_title_generation
        if capture_proxy_session:
            control_command = context_control_command_from_input(body.get("input"))
            if control_command:
                session_id = session_id_for_request(body, headers)
                STORE.record_control_intercept(session_id, body, headers, control_command)
                opened, error = open_context_workbench(session_id)
                proxy_log(
                    f"context control intercepted session={session_id} command={control_command!r} "
                    f"opened={opened} error={error!r}"
                )
                self._send_context_control_response(body, opened, error)
                return
        if is_internal_context and not has_effective_upstream_auth(headers):
            proxy_log("internal context request missing cached upstream auth")
            self._send_json(
                {
                    "error": {
                        "message": (
                            "Codex auth has not been captured by the local proxy yet. "
                            "Send one normal Codex message through this proxy first, then retry the context workbench."
                        ),
                        "type": "hash_context_auth_unavailable",
                        "code": "codex_auth_not_captured",
                    }
                },
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if is_internal_context:
            session_id = session_id_for_request(body, headers)
            headers_for_upstream = merge_codex_session_headers(
                headers,
                STORE.codex_session_headers(session_id),
                session_id=session_id,
            )
            forwarded_body = copy.deepcopy(body)
        elif capture_proxy_session:
            session_id = session_id_for_request(body, headers)
            headers_for_upstream = headers
            _session, forwarded_body = STORE.begin_request(session_id, body, headers)
        else:
            session_id = session_id_for_request(body, headers)
            headers_for_upstream = headers
            forwarded_body = copy.deepcopy(body)
        upstream_base_url = upstream_base_url_for_request(headers_for_upstream)
        upstream = urllib.parse.urlparse(upstream_base_url.rstrip("/") + "/responses")
        effective_headers = apply_cached_upstream_auth(headers_for_upstream)
        effective_lowered = {key.lower(): value for key, value in effective_headers.items()}
        auth_kind = "chatgpt" if effective_lowered.get("chatgpt-account-id") else "api-key-or-bearer"
        proxy_log(
            f"request session={session_id} auth={auth_kind} "
            f"internal={is_internal_context} title_generation={is_title_generation} "
            f"capture={capture_proxy_session} upstream={upstream.geturl()}"
        )
        connection_cls = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
        conn = connection_cls(upstream.hostname, upstream.port, timeout=120)
        path = upstream.path or "/v1/responses"
        if upstream.query:
            path = f"{path}?{upstream.query}"

        try:
            payload = json.dumps(forwarded_body, ensure_ascii=False).encode("utf-8")
            upstream_headers = upstream_headers_for_request(headers_for_upstream, accept="text/event-stream")
            proxy_log(
                f"upstream headers session={session_id} "
                f"{json.dumps(safe_headers_for_log(upstream_headers), ensure_ascii=False, sort_keys=True)}"
            )
            conn.request("POST", path, body=payload, headers=upstream_headers)
            upstream_response = conn.getresponse()
            proxy_log(f"upstream status session={session_id} status={upstream_response.status} reason={upstream_response.reason}")
            self.send_response(upstream_response.status)
            self._send_cors_headers()
            for key, value in upstream_response.getheaders():
                if key.lower() in {"transfer-encoding", "connection", "content-encoding", "content-length"}:
                    continue
                self.send_header(key, value)
            self.end_headers()

            response_items: list[dict[str, Any]] = []
            text_parts: list[str] = []
            completed_responses: list[dict[str, Any]] = []
            buffer = ""
            error_preview = bytearray()
            while True:
                chunk = upstream_response.read(4096)
                if not chunk:
                    break
                if upstream_response.status >= 400 and len(error_preview) < 4000:
                    error_preview.extend(chunk[: 4000 - len(error_preview)])
                self.wfile.write(chunk)
                self.wfile.flush()
                buffer += chunk.decode("utf-8", errors="ignore")
                buffer = parse_sse_buffer(buffer, response_items, text_parts, completed_responses)

            if upstream_response.status >= 400:
                preview = error_preview.decode("utf-8", errors="replace")
                proxy_log(f"upstream error session={session_id} body={preview[:1000]!r}")
                if capture_proxy_session:
                    STORE.fail_response(session_id, preview[:1000] or upstream_response.reason)
            else:
                if is_internal_context or capture_proxy_session:
                    usage_kind = "context_workbench" if is_internal_context else "main"
                    fallback_model = str(forwarded_body.get("model") or body.get("model") or "")
                    for completed_response in completed_responses:
                        STORE.record_usage(
                            session_id,
                            usage_kind,
                            str(completed_response.get("model") or fallback_model),
                            completed_response.get("usage"),
                        )
                if capture_proxy_session:
                    STORE.complete_response(session_id, response_items, "".join(text_parts))
        except Exception as exc:
            proxy_log(f"error session={session_id} error={type(exc).__name__}: {exc}")
            if capture_proxy_session:
                STORE.fail_response(session_id, str(exc))
            if not self.wfile.closed:
                try:
                    self._send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
                except Exception:
                    pass
        finally:
            conn.close()

    def _handle_compact(self) -> None:
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        proxy_log(f"compact body bytes={len(raw_body)} prefix={raw_body[:80]!r}")
        self._send_json(
            {
                "error": {
                    "message": (
                        "remote compact disabled; local compact is handled "
                        "through /v1/responses metadata"
                    ),
                    "type": "remote_compact_disabled",
                    "code": "remote_compact_disabled",
                }
            },
            HTTPStatus.GONE,
        )

    def _read_json(self) -> dict[str, Any]:
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        if not raw_body:
            return {}
        data = parse_json_request_body(raw_body, self.headers.get("content-encoding"))
        return data if isinstance(data, dict) else {}

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type, x-hash-context-internal, x-hash-context-session-id")

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def parse_sse_buffer(
    buffer: str,
    response_items: list[dict[str, Any]],
    text_parts: list[str],
    completed_responses: list[dict[str, Any]] | None = None,
) -> str:
    def replace_or_append_item(next_item: dict[str, Any]) -> None:
        item_id = str(next_item.get("id") or "").strip()
        call_id = str(next_item.get("call_id") or "").strip()
        next_fingerprint = fingerprint_provider_item(next_item)
        for index, existing in enumerate(response_items):
            if not isinstance(existing, dict):
                continue
            existing_id = str(existing.get("id") or "").strip()
            existing_call_id = str(existing.get("call_id") or "").strip()
            same_identity = (item_id and existing_id == item_id) or (call_id and existing_call_id == call_id)
            same_item = next_fingerprint == fingerprint_provider_item(existing)
            if same_identity or same_item:
                response_items[index] = next_item
                return
        response_items.append(next_item)

    def item_for_delta(event: dict[str, Any]) -> dict[str, Any] | None:
        item_id = str(event.get("item_id") or "").strip()
        output_index = event.get("output_index")
        if item_id:
            for item in response_items:
                if isinstance(item, dict) and str(item.get("id") or "").strip() == item_id:
                    return item
        if isinstance(output_index, int) and 0 <= output_index < len(response_items):
            item = response_items[output_index]
            return item if isinstance(item, dict) else None
        for item in reversed(response_items):
            if isinstance(item, dict) and str(item.get("type") or "") == "function_call":
                return item
        return None

    while "\n\n" in buffer:
        block, buffer = buffer.split("\n\n", 1)
        data_lines = [line[5:].strip() for line in block.splitlines() if line.startswith("data:")]
        if not data_lines:
            continue
        raw = "\n".join(data_lines)
        if raw == "[DONE]":
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type in {"response.output_text.delta", "response.refusal.delta"}:
            delta = event.get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)
        if event_type == "response.output_item.added" and isinstance(event.get("item"), dict):
            replace_or_append_item(event["item"])
        if event_type == "response.output_item.done" and isinstance(event.get("item"), dict):
            replace_or_append_item(event["item"])
        if event_type == "response.function_call_arguments.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                target_item = item_for_delta(event)
                if target_item is not None and str(target_item.get("type") or "") == "function_call":
                    target_item["arguments"] = f"{target_item.get('arguments') or ''}{delta}"
        response = event.get("response")
        if event_type == "response.completed" and isinstance(response, dict):
            if completed_responses is not None:
                completed_responses.append(response)
            output = response.get("output")
            if isinstance(output, list):
                for item in output:
                    if isinstance(item, dict):
                        replace_or_append_item(item)
    return buffer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")
    if preload_force_upstream_auth():
        proxy_log(f"preloaded force upstream auth for {FORCE_UPSTREAM_BASE_URL}")
    elif preload_codex_subscription_auth():
        proxy_log("preloaded Codex subscription auth from local auth.json")
    elif preload_openai_api_auth():
        proxy_log("preloaded OpenAI API auth from OPENAI_API_KEY")
    else:
        proxy_log("local Codex auth was not available at proxy startup")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Hash Context proxy listening on http://{args.host}:{args.port}")
    if FORCE_UPSTREAM_BASE_URL:
        print(f"Force upstream: {FORCE_UPSTREAM_BASE_URL.rstrip('/')}/responses")
    else:
        print(f"OpenAI API upstream: {OPENAI_UPSTREAM_BASE_URL.rstrip()}/responses")
        print(f"ChatGPT upstream: {CHATGPT_UPSTREAM_BASE_URL.rstrip()}/responses")
    server.serve_forever()


if __name__ == "__main__":
    main()
