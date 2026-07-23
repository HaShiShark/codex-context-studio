from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv


MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent
DEFAULT_DATA_DIR = Path.home() / ".codex-context-studio" / "shared"


def _resolve_data_dir() -> Path:
    raw_data_dir = str(
        os.getenv("CODEX_CONTEXT_STUDIO_CONFIG_DIR")
        or os.getenv("CODEX_CONTEXT_STUDIO_DATA_DIR")
        or ""
    ).strip()
    if not raw_data_dir:
        return DEFAULT_DATA_DIR
    data_dir = Path(raw_data_dir).expanduser()
    return data_dir if data_dir.is_absolute() else (REPO_ROOT / data_dir).resolve()


DATA_DIR = _resolve_data_dir()
SETTINGS_FILE = DATA_DIR / "openai_settings.json"
CODEX_PROXY_PROVIDER_ID = "codex-proxy"
CODEX_PROXY_BASE_URL = (
    f"http://{os.getenv('CODEX_CONTEXT_STUDIO_PROXY_HOST', os.getenv('CODEX_CONTEXT_STUDIO_HOST', 'localhost'))}:"
    f"{os.getenv('CODEX_CONTEXT_STUDIO_PROXY_PORT', '8787')}/v1"
)
DEFAULT_CODEX_PROXY_MODELS: tuple[dict[str, str], ...] = (
    {"id": "gpt-5.6-sol", "label": "gpt-5.6-sol", "group": "Codex", "provider": "Codex"},
    {"id": "gpt-5.6-terra", "label": "gpt-5.6-terra", "group": "Codex", "provider": "Codex"},
    {"id": "gpt-5.6-luna", "label": "gpt-5.6-luna", "group": "Codex", "provider": "Codex"},
    {"id": "gpt-5.5", "label": "gpt-5.5", "group": "Codex", "provider": "Codex"},
    {"id": "gpt-5.4-mini", "label": "gpt-5.4-mini", "group": "Codex", "provider": "Codex"},
    {"id": "gpt-5.4", "label": "gpt-5.4", "group": "Codex", "provider": "Codex"},
    {"id": "gpt-5.2", "label": "gpt-5.2", "group": "Codex", "provider": "Codex"},
)
DEFAULT_CONTEXT_WORKBENCH_PROVIDER_ID = CODEX_PROXY_PROVIDER_ID
DEFAULT_CONTEXT_WORKBENCH_MODEL = "gpt-5.6-sol"
DEFAULT_CONTEXT_REVIEW_INTERVAL_MINUTES = 10
DEFAULT_CONTEXT_TOKEN_WARNING_THRESHOLD = 5000
DEFAULT_CONTEXT_TOKEN_CRITICAL_THRESHOLD = 10000
DEFAULT_REASONING_EFFORT = "default"
DEFAULT_THEME_MODE = "light"
DEFAULT_UI_FONT = "Noto Serif SC"
DEFAULT_UI_FONT_SIZE = 16
MIN_UI_FONT_SIZE = 10
MAX_UI_FONT_SIZE = 32

DEFAULT_RESPONSE_PROVIDERS: tuple[dict[str, object], ...] = (
    {
        "id": "openai",
        "name": "OpenAI Responses",
        "provider_type": "responses",
        "enabled": True,
        "supports_model_fetch": True,
        "supports_responses": True,
        "api_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-5.4-mini",
    },
    {
        "id": CODEX_PROXY_PROVIDER_ID,
        "name": "Codex",
        "provider_type": "responses",
        "enabled": True,
        "supports_model_fetch": True,
        "supports_responses": True,
        "api_base_url": CODEX_PROXY_BASE_URL,
        "default_model": DEFAULT_CONTEXT_WORKBENCH_MODEL,
        "models": DEFAULT_CODEX_PROXY_MODELS,
    },
    {
        "id": "openai-chat",
        "name": "OpenAI Chat Completions",
        "provider_type": "chat_completion",
        "enabled": True,
        "supports_model_fetch": True,
        "supports_responses": False,
        "api_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4.1-mini",
    },
    {
        "id": "anthropic",
        "name": "Anthropic Messages",
        "provider_type": "claude",
        "enabled": True,
        "supports_model_fetch": True,
        "supports_responses": False,
        "api_base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-sonnet-4-5",
    },
    {
        "id": "gemini",
        "name": "Gemini",
        "provider_type": "gemini",
        "enabled": True,
        "supports_model_fetch": True,
        "supports_responses": False,
        "api_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-2.5-pro",
    },
)

PROVIDER_TYPES = {"chat_completion", "responses", "gemini", "claude"}
REASONING_EFFORTS = {"default", "none", "low", "medium", "high"}
_UNSET = object()


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _read_settings_file() -> dict[str, Any]:
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _provider_type_defaults(provider_type: str) -> dict[str, str]:
    defaults = {
        "gemini": ("Gemini", "https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-pro"),
        "claude": ("Claude", "https://api.anthropic.com/v1", "claude-sonnet-4-5"),
        "chat_completion": ("Chat Completion", "https://api.openai.com/v1", "gpt-4.1-mini"),
        "responses": ("OpenAI", "https://api.openai.com/v1", "gpt-5.4-mini"),
    }
    name, base_url, model = defaults.get(provider_type, defaults["responses"])
    return {"name": name, "api_base_url": base_url, "default_model": model}


def _normalize_provider_type(value: Any, provider_id: str = "") -> str:
    cleaned = _clean_string(value)
    if cleaned in PROVIDER_TYPES:
        return cleaned
    if provider_id == "gemini":
        return "gemini"
    if provider_id in {"anthropic", "claude"}:
        return "claude"
    if provider_id == "openai-chat":
        return "chat_completion"
    return "responses"


def _normalize_provider_api_base_url(raw_url: Any, provider_type: str = "responses") -> str:
    cleaned_url = _clean_string(raw_url).rstrip("/")
    if not cleaned_url:
        return ""
    parsed = urlparse(cleaned_url)
    if not parsed.scheme or not parsed.netloc:
        return cleaned_url
    path = parsed.path.rstrip("/")
    if provider_type == "claude" and path.endswith("/messages"):
        path = path[: -len("/messages")]
    if provider_type == "chat_completion" and path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]
    if provider_type == "responses" and path.endswith("/responses"):
        path = path[: -len("/responses")]
    return urlunparse(parsed._replace(path=path, params="", query="", fragment="")).rstrip("/")


def _normalize_optional_float(value: Any, *, min_value: float, max_value: float) -> float | None:
    if value in {None, ""}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if min_value <= parsed <= max_value else None


def _bounded_int(value: Any, *, minimum: int, maximum: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return min(maximum, max(minimum, parsed))


def _normalize_reasoning_effort(value: Any) -> str:
    cleaned = _clean_string(value).lower()
    return cleaned if cleaned in REASONING_EFFORTS else DEFAULT_REASONING_EFFORT


def _normalize_context_token_thresholds(warning: Any, critical: Any) -> tuple[int, int]:
    warning_value = _bounded_int(
        warning,
        minimum=0,
        maximum=1_000_000,
        fallback=DEFAULT_CONTEXT_TOKEN_WARNING_THRESHOLD,
    )
    critical_value = _bounded_int(
        critical,
        minimum=warning_value + 1,
        maximum=2_000_000,
        fallback=max(DEFAULT_CONTEXT_TOKEN_CRITICAL_THRESHOLD, warning_value + 1),
    )
    return warning_value, critical_value


def _normalize_provider_models(raw_models: Any) -> list[dict[str, str]]:
    if not isinstance(raw_models, (list, tuple)):
        return []
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_models:
        if isinstance(raw, str):
            model_id = raw.strip()
            record: dict[str, Any] = {"id": model_id}
        elif isinstance(raw, dict):
            record = raw
            model_id = _clean_string(record.get("id") or record.get("name"))
        else:
            continue
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append(
            {
                "id": model_id,
                "label": _clean_string(record.get("label")) or model_id,
                "group": _clean_string(record.get("group") or record.get("provider")),
                "provider": _clean_string(record.get("provider") or record.get("group")),
            }
        )
    return models


def _normalize_provider(raw: Any, default: dict[str, object] | None = None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    provider_id = _clean_string(raw.get("id") or (default or {}).get("id"))
    if not provider_id:
        return None
    provider_type = _normalize_provider_type(raw.get("provider_type"), provider_id)
    type_defaults = _provider_type_defaults(provider_type)
    base = dict(default or {})
    raw_api_base_url = (
        raw.get("api_base_url")
        if "api_base_url" in raw
        else base.get("api_base_url", type_defaults["api_base_url"])
    )
    record: dict[str, Any] = {
        "id": provider_id,
        "name": _clean_string(raw.get("name") or base.get("name")) or type_defaults["name"],
        "provider_type": provider_type,
        "enabled": bool(raw.get("enabled", base.get("enabled", True))),
        "supports_model_fetch": True,
        "supports_responses": provider_type == "responses",
        "api_base_url": _normalize_provider_api_base_url(
            raw_api_base_url,
            provider_type,
        ),
        "default_model": _clean_string(
            raw.get("default_model") or base.get("default_model") or type_defaults["default_model"]
        ),
        "models": _normalize_provider_models(raw.get("models", base.get("models"))),
        "last_sync_at": _clean_string(raw.get("last_sync_at")),
        "last_sync_error": _clean_string(raw.get("last_sync_error")),
    }
    api_key = _clean_string(raw.get("api_key"))
    if api_key:
        record["api_key"] = api_key
    return record


def _normalize_provider_records(raw_providers: Any) -> list[dict[str, Any]]:
    incoming = {
        _clean_string(item.get("id")): item
        for item in raw_providers
        if isinstance(raw_providers, list) and isinstance(item, dict) and _clean_string(item.get("id"))
    } if isinstance(raw_providers, list) else {}

    records: list[dict[str, Any]] = []
    built_in_ids: set[str] = set()
    for default in DEFAULT_RESPONSE_PROVIDERS:
        provider_id = _clean_string(default.get("id"))
        built_in_ids.add(provider_id)
        normalized = _normalize_provider(incoming.get(provider_id, default), default)
        if normalized:
            records.append(normalized)
    for provider_id, raw in incoming.items():
        if provider_id in built_in_ids:
            continue
        normalized = _normalize_provider(raw)
        if normalized:
            records.append(normalized)
    return records


def _normalize_provider_id(raw_provider_id: Any, providers: list[dict[str, Any]]) -> str:
    enabled_ids = {
        _clean_string(provider.get("id"))
        for provider in providers
        if provider.get("enabled", True)
    }
    candidate = _clean_string(raw_provider_id)
    if candidate in enabled_ids:
        return candidate
    if DEFAULT_CONTEXT_WORKBENCH_PROVIDER_ID in enabled_ids:
        return DEFAULT_CONTEXT_WORKBENCH_PROVIDER_ID
    return next(iter(enabled_ids), DEFAULT_CONTEXT_WORKBENCH_PROVIDER_ID)


@dataclass(slots=True)
class Settings:
    default_reasoning_effort: str
    context_workbench_model: str
    context_workbench_provider_id: str
    project_root: Path
    response_providers: list[dict[str, Any]]
    context_review_auto_enabled: bool = True
    context_review_interval_minutes: int = DEFAULT_CONTEXT_REVIEW_INTERVAL_MINUTES
    codex_system_prompt: str = ""
    codex_system_prompt_default: str = ""
    manual_local_compact_prompt: str = ""
    auto_local_compact_prompt: str = ""
    temperature: float | None = None
    top_p: float | None = None
    user_locale: str = "en-US"
    context_token_warning_threshold: int = DEFAULT_CONTEXT_TOKEN_WARNING_THRESHOLD
    context_token_critical_threshold: int = DEFAULT_CONTEXT_TOKEN_CRITICAL_THRESHOLD
    theme_mode: str = DEFAULT_THEME_MODE
    ui_font: str = DEFAULT_UI_FONT
    ui_font_size: int = DEFAULT_UI_FONT_SIZE

    def context_workbench_provider(self) -> dict[str, Any]:
        for provider in self.response_providers:
            if _clean_string(provider.get("id")) == self.context_workbench_provider_id:
                return provider
        return self.response_providers[0] if self.response_providers else {}


def load_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env")
    stored = _read_settings_file()
    providers = _normalize_provider_records(stored.get("response_providers"))
    provider_id = _normalize_provider_id(stored.get("context_workbench_provider_id"), providers)
    provider = next((item for item in providers if item.get("id") == provider_id), providers[0])
    warning, critical = _normalize_context_token_thresholds(
        stored.get("context_token_warning_threshold"),
        stored.get("context_token_critical_threshold"),
    )

    project_root = Path(stored.get("project_root") or os.getenv("AGENT_PROJECT_ROOT") or REPO_ROOT).expanduser()
    if not project_root.is_absolute():
        project_root = (REPO_ROOT / project_root).resolve()

    default_prompt = _clean_string(stored.get("codex_system_prompt_default"))
    return Settings(
        default_reasoning_effort=_normalize_reasoning_effort(stored.get("default_reasoning_effort")),
        context_workbench_model=(
            _clean_string(stored.get("context_workbench_model"))
            or _clean_string(provider.get("default_model"))
            or DEFAULT_CONTEXT_WORKBENCH_MODEL
        ),
        context_workbench_provider_id=provider_id,
        project_root=project_root,
        response_providers=providers,
        context_review_auto_enabled=bool(stored.get("context_review_auto_enabled", True)),
        context_review_interval_minutes=_bounded_int(
            stored.get("context_review_interval_minutes"),
            minimum=1,
            maximum=1440,
            fallback=DEFAULT_CONTEXT_REVIEW_INTERVAL_MINUTES,
        ),
        codex_system_prompt=_clean_string(stored.get("codex_system_prompt")) or default_prompt,
        codex_system_prompt_default=default_prompt,
        manual_local_compact_prompt=_clean_string(stored.get("manual_local_compact_prompt")),
        auto_local_compact_prompt=_clean_string(stored.get("auto_local_compact_prompt")),
        temperature=_normalize_optional_float(stored.get("temperature"), min_value=0, max_value=2),
        top_p=_normalize_optional_float(stored.get("top_p"), min_value=0, max_value=1),
        user_locale=_clean_string(stored.get("user_locale")) or "en-US",
        context_token_warning_threshold=warning,
        context_token_critical_threshold=critical,
        theme_mode="dark" if _clean_string(stored.get("theme_mode")) == "dark" else "light",
        ui_font=_clean_string(stored.get("ui_font")) or DEFAULT_UI_FONT,
        ui_font_size=_bounded_int(
            stored.get("ui_font_size"),
            minimum=MIN_UI_FONT_SIZE,
            maximum=MAX_UI_FONT_SIZE,
            fallback=DEFAULT_UI_FONT_SIZE,
        ),
    )


def _merge_provider_updates(
    providers: list[dict[str, Any]],
    updates: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if updates is None:
        return providers
    by_id = {_clean_string(provider.get("id")): dict(provider) for provider in providers}
    order = list(by_id)
    for update in updates:
        if not isinstance(update, dict):
            continue
        provider_id = _clean_string(update.get("id"))
        if not provider_id:
            continue
        current = by_id.get(provider_id, {"id": provider_id})
        merged = {**current, **update}
        if provider_id in by_id:
            current_type = _normalize_provider_type(current.get("provider_type"), provider_id)
            next_type = _normalize_provider_type(merged.get("provider_type"), provider_id)
            current_base_url = _normalize_provider_api_base_url(current.get("api_base_url"), current_type)
            next_base_url = _normalize_provider_api_base_url(merged.get("api_base_url"), next_type)
            if current_type != next_type or current_base_url != next_base_url:
                merged["models"] = []
                merged["last_sync_at"] = ""
                merged["last_sync_error"] = ""
        normalized = _normalize_provider(merged, current)
        if normalized:
            by_id[provider_id] = normalized
            if provider_id not in order:
                order.append(provider_id)
    return [by_id[provider_id] for provider_id in order]


def save_settings(
    *,
    default_reasoning_effort: str | None = None,
    context_workbench_model: str | None = None,
    context_workbench_provider_id: str | None = None,
    context_review_auto_enabled: bool | None = None,
    context_review_interval_minutes: int | None = None,
    context_token_warning_threshold: int | None = None,
    context_token_critical_threshold: int | None = None,
    response_providers: list[dict[str, Any]] | None = None,
    codex_system_prompt: str | None = None,
    codex_system_prompt_default: str | None = None,
    manual_local_compact_prompt: str | None = None,
    auto_local_compact_prompt: str | None = None,
    temperature: float | None | object = _UNSET,
    top_p: float | None | object = _UNSET,
    user_locale: str | None = None,
    theme_mode: str | None = None,
    ui_font: str | None = None,
    ui_font_size: int | None = None,
) -> Settings:
    current = _read_settings_file()
    loaded = load_settings()
    providers = _merge_provider_updates(loaded.response_providers, response_providers)
    provider_id = _normalize_provider_id(
        context_workbench_provider_id or loaded.context_workbench_provider_id,
        providers,
    )
    provider = next((item for item in providers if item.get("id") == provider_id), providers[0])

    current.update(
        {
            "response_providers": providers,
            "context_workbench_provider_id": provider_id,
            "context_workbench_model": (
                _clean_string(context_workbench_model)
                if context_workbench_model is not None
                else loaded.context_workbench_model
            ) or _clean_string(provider.get("default_model")) or DEFAULT_CONTEXT_WORKBENCH_MODEL,
            "default_reasoning_effort": (
                _normalize_reasoning_effort(default_reasoning_effort)
                if default_reasoning_effort is not None
                else loaded.default_reasoning_effort
            ),
        }
    )

    direct_updates = {
        "context_review_auto_enabled": context_review_auto_enabled,
        "codex_system_prompt": codex_system_prompt,
        "codex_system_prompt_default": codex_system_prompt_default,
        "manual_local_compact_prompt": manual_local_compact_prompt,
        "auto_local_compact_prompt": auto_local_compact_prompt,
        "user_locale": user_locale,
        "ui_font": ui_font,
    }
    for key, value in direct_updates.items():
        if value is not None:
            current[key] = bool(value) if key == "context_review_auto_enabled" else _clean_string(value)

    if context_review_interval_minutes is not None:
        current["context_review_interval_minutes"] = _bounded_int(
            context_review_interval_minutes,
            minimum=1,
            maximum=1440,
            fallback=loaded.context_review_interval_minutes,
        )
    warning, critical = _normalize_context_token_thresholds(
        context_token_warning_threshold if context_token_warning_threshold is not None else loaded.context_token_warning_threshold,
        context_token_critical_threshold if context_token_critical_threshold is not None else loaded.context_token_critical_threshold,
    )
    current["context_token_warning_threshold"] = warning
    current["context_token_critical_threshold"] = critical
    if temperature is not _UNSET:
        current["temperature"] = _normalize_optional_float(temperature, min_value=0, max_value=2)
    if top_p is not _UNSET:
        current["top_p"] = _normalize_optional_float(top_p, min_value=0, max_value=1)
    if theme_mode is not None:
        current["theme_mode"] = "dark" if _clean_string(theme_mode) == "dark" else "light"
    if ui_font_size is not None:
        current["ui_font_size"] = _bounded_int(
            ui_font_size,
            minimum=MIN_UI_FONT_SIZE,
            maximum=MAX_UI_FONT_SIZE,
            fallback=loaded.ui_font_size,
        )

    allowed_keys = {
        "response_providers",
        "context_workbench_provider_id",
        "context_workbench_model",
        "default_reasoning_effort",
        "context_review_auto_enabled",
        "context_review_interval_minutes",
        "context_token_warning_threshold",
        "context_token_critical_threshold",
        "codex_system_prompt",
        "codex_system_prompt_default",
        "manual_local_compact_prompt",
        "auto_local_compact_prompt",
        "temperature",
        "top_p",
        "user_locale",
        "theme_mode",
        "ui_font",
        "ui_font_size",
        "project_root",
    }
    payload = {key: value for key, value in current.items() if key in allowed_keys}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_settings()
