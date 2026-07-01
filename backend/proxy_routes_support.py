from __future__ import annotations

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
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from codex_context import is_context_control_command_text

try:
    from .codex_input_cursor import fingerprint_provider_item
    from .proxy_store import DATA_DIR, compact_text, read_message_text, utc_timestamp
except ImportError:
    repo_root_for_import = Path(__file__).resolve().parents[1]
    if str(repo_root_for_import) not in sys.path:
        sys.path.insert(0, str(repo_root_for_import))
    from backend.codex_input_cursor import fingerprint_provider_item
    from backend.proxy_store import DATA_DIR, compact_text, read_message_text, utc_timestamp


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
LOG_PATH = DATA_DIR / "proxy.log"
INTERNAL_CONTEXT_HEADER = "x-hash-context-internal"
INTERNAL_CONTEXT_VALUE = "context-workbench"
CONTEXT_CONTROL_NOTICE_TEXT = "Hash Context: opened workbench."
CONTROL_PORT = int(os.environ.get("HASH_CONTEXT_CONTROL_PORT", "8790"))
CODEX_AUTH_PATH = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
CODEX_HOME = CODEX_AUTH_PATH.parent
DEFAULT_MODEL_CONTEXT_WINDOW = 200000
CODEX_PASSTHROUGH_REQUEST_KINDS = {"prewarm", "memory"}
CODEX_THREAD_ID_PATTERN = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

_UPSTREAM_AUTH_LOCK = threading.Lock()
_UPSTREAM_AUTH_HEADERS: dict[str, str] = {}


def proxy_log(message: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text("", encoding="utf-8") if not LOG_PATH.exists() else None
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{utc_timestamp()} {message}\n")
    except Exception:
        pass


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
    "最大 18 个中文字符或 8 个英文单词。",
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
    hash_context_session_id = headers.get("x-hash-context-session-id")
    if hash_context_session_id:
        return sanitize_id(hash_context_session_id)

    metadata_session_id = session_id_from_codex_metadata(headers, body)
    if metadata_session_id:
        return sanitize_id(metadata_session_id)

    for key in (
        "x-codex-conversation-id",
        "x-codex-thread-id",
        "thread-id",
        "thread_id",
        "x-codex-session-id",
        "session-id",
        "session_id",
    ):
        value = headers.get(key)
        if value:
            return sanitize_id(value)
    prompt_cache_key = body.get("prompt_cache_key")
    if isinstance(prompt_cache_key, str) and prompt_cache_key.strip():
        return sanitize_id(prompt_cache_key)
    metadata = body.get("client_metadata")
    if isinstance(metadata, dict):
        metadata_session_id = session_id_from_client_metadata(metadata)
        if metadata_session_id:
            return sanitize_id(metadata_session_id)
    digest = hashlib.sha1(json.dumps(body.get("input", []), sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return f"session-{digest[:16]}"


SESSION_ID_METADATA_KEYS = (
    "thread_id",
    "codex_thread_id",
    "conversation_id",
    "codex_conversation_id",
    "session_id",
    "codex_session_id",
)
SESSION_CONTAINER_KEY_PARTS = ("session", "conversation", "thread")


def normalized_metadata_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def session_id_from_mapping_keys(mapping: dict[Any, Any], allowed_keys: tuple[str, ...]) -> str:
    for allowed_key in allowed_keys:
        for key, candidate in mapping.items():
            if normalized_metadata_key(key) != allowed_key:
                continue
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return ""


def session_id_from_client_metadata(metadata: dict[str, Any]) -> str:
    return session_id_from_mapping_keys(metadata, SESSION_ID_METADATA_KEYS)


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
    metadata_session_id = session_id_from_codex_metadata(headers, body)
    if metadata_session_id:
        return sanitize_id(metadata_session_id)
    if active_session_id:
        return active_session_id
    return session_id_for_request(body, headers)


def parse_metadata_value(raw_value: Any) -> dict[str, Any] | None:
    if isinstance(raw_value, dict):
        return copy.deepcopy(raw_value)
    if isinstance(raw_value, str) and raw_value.strip():
        try:
            parsed = json.loads(raw_value)
        except (TypeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def client_metadata_value(body: dict[str, Any] | None, desired_key: str) -> Any:
    metadata = body.get("client_metadata") if isinstance(body, dict) else None
    if not isinstance(metadata, dict):
        return None
    normalized_desired_key = normalized_metadata_key(desired_key)
    for key, value in metadata.items():
        if normalized_metadata_key(key) == normalized_desired_key:
            return value
    return None


def codex_turn_metadata(headers: dict[str, str], body: dict[str, Any] | None = None) -> dict[str, Any]:
    for raw_value in (
        client_metadata_value(body, "x-codex-turn-metadata"),
        client_metadata_value(body, "x-codex-turn-state"),
        headers.get("x-codex-turn-metadata"),
        headers.get("x-codex-turn-state"),
    ):
        metadata = parse_metadata_value(raw_value)
        if metadata is not None:
            return metadata
    return {}


def codex_request_kind(headers: dict[str, str], body: dict[str, Any] | None = None) -> str:
    metadata = codex_turn_metadata(headers, body)
    request_kind = str(metadata.get("request_kind") or "").strip().lower()
    if request_kind:
        return request_kind
    raw_kind = client_metadata_value(body, "request_kind")
    return str(raw_kind or "").strip().lower()


def is_codex_subagent_request(headers: dict[str, str], body: dict[str, Any] | None = None) -> bool:
    if str(headers.get("x-openai-subagent") or "").strip():
        return True
    if str(client_metadata_value(body, "x-openai-subagent") or "").strip():
        return True
    metadata = codex_turn_metadata(headers, body)
    return bool(str(metadata.get("subagent_kind") or "").strip())


def codex_passthrough_reason(headers: dict[str, str], body: dict[str, Any] | None = None) -> str:
    if is_codex_subagent_request(headers, body):
        return "subagent"
    request_kind = codex_request_kind(headers, body)
    if request_kind in CODEX_PASSTHROUGH_REQUEST_KINDS:
        return request_kind
    return ""


def session_id_from_codex_metadata(headers: dict[str, str], body: dict[str, Any] | None = None) -> str:
    for parsed in (
        codex_turn_metadata(headers, body),
    ):
        session_id = find_session_id_in_value(parsed)
        if session_id:
            return session_id
    return ""


def find_session_id_in_value(value: Any) -> str:
    if isinstance(value, dict):
        found = session_id_from_mapping_keys(value, SESSION_ID_METADATA_KEYS)
        if found:
            return found
        for key, nested_value in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in SESSION_CONTAINER_KEY_PARTS) and isinstance(nested_value, (dict, list)):
                found = find_session_id_in_session_container(nested_value)
                if found:
                    return found
    elif isinstance(value, list):
        for item in value:
            found = find_session_id_in_value(item)
            if found:
                return found
    return ""


def find_session_id_in_session_container(value: Any) -> str:
    if isinstance(value, dict):
        found = session_id_from_mapping_keys(value, SESSION_ID_METADATA_KEYS + ("id",))
        if found:
            return found
        for key, nested_value in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in SESSION_CONTAINER_KEY_PARTS) and isinstance(nested_value, (dict, list)):
                found = find_session_id_in_session_container(nested_value)
                if found:
                    return found
    elif isinstance(value, list):
        for item in value:
            found = find_session_id_in_session_container(item)
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


def codex_rollout_thread_id_from_path(path: Path) -> str:
    match = CODEX_THREAD_ID_PATTERN.search(path.stem)
    if match:
        return match.group(1).lower()

    try:
        with path.open("r", encoding="utf-8") as handle:
            for _ in range(5):
                line = handle.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
                for container in (payload, record):
                    candidate = container.get("id") if isinstance(container, dict) else None
                    if isinstance(candidate, str) and CODEX_THREAD_ID_PATTERN.fullmatch(candidate.strip()):
                        return candidate.strip().lower()
    except OSError:
        return ""

    return ""


def codex_existing_thread_ids(codex_home: Path | None = None) -> tuple[set[str] | None, dict[str, Any]]:
    home = Path(codex_home or CODEX_HOME).expanduser()
    roots = [home / "sessions", home / "archived_sessions"]
    existing_roots = [root for root in roots if root.exists() and root.is_dir()]
    if not existing_roots:
        return None, {
            "codex_home": str(home),
            "roots": [str(root) for root in roots],
            "scanned_files": 0,
            "reason": "codex_session_roots_not_found",
        }

    thread_ids: set[str] = set()
    scanned_files = 0
    unreadable_files = 0
    for root in existing_roots:
        try:
            rollout_paths = root.rglob("*.jsonl")
            for path in rollout_paths:
                if not path.is_file():
                    continue
                scanned_files += 1
                thread_id = codex_rollout_thread_id_from_path(path)
                if thread_id:
                    thread_ids.add(thread_id)
                else:
                    unreadable_files += 1
        except OSError:
            unreadable_files += 1

    return thread_ids, {
        "codex_home": str(home),
        "roots": [str(root) for root in existing_roots],
        "scanned_files": scanned_files,
        "unreadable_files": unreadable_files,
    }


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
