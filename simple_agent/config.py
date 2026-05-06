from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv

from simple_agent.tools import normalize_tool_settings


MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent
LEGACY_DATA_DIR = REPO_ROOT / "data"


def _resolve_data_dir() -> Path:
    raw_data_dir = str(os.getenv("HASH_CONFIG_DIR") or os.getenv("HASH_DATA_DIR") or "").strip()
    if not raw_data_dir:
        return LEGACY_DATA_DIR

    data_dir = Path(raw_data_dir).expanduser()
    return data_dir if data_dir.is_absolute() else (REPO_ROOT / data_dir).resolve()


DATA_DIR = _resolve_data_dir()
SETTINGS_FILE = DATA_DIR / "openai_settings.json"
LEGACY_SETTINGS_FILE = LEGACY_DATA_DIR / "openai_settings.json"
CODEX_PROXY_PROVIDER_ID = "codex-proxy"
CODEX_PROXY_BASE_URL = (
    f"http://{os.getenv('HASH_CONTEXT_PROXY_HOST', '127.0.0.1')}:"
    f"{os.getenv('HASH_CONTEXT_PROXY_PORT', '8787')}/v1"
)
DEFAULT_CODEX_PROXY_MODELS: tuple[dict[str, str], ...] = (
    {"id": "gpt-5.4-mini", "label": "gpt-5.4-mini", "group": "Codex", "provider": "Codex"},
    {"id": "gpt-5.4", "label": "gpt-5.4", "group": "Codex", "provider": "Codex"},
    {"id": "gpt-5.2", "label": "gpt-5.2", "group": "Codex", "provider": "Codex"},
)

DEFAULT_RESPONSE_PROVIDERS: tuple[dict[str, object], ...] = (
    {
        "id": "openai",
        "name": "OpenAI",
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
        "default_model": "gpt-5.4-mini",
        "models": DEFAULT_CODEX_PROXY_MODELS,
    },
    {
        "id": "anthropic",
        "name": "Claude",
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
PROVIDER_IDS = {str(spec["id"]) for spec in DEFAULT_RESPONSE_PROVIDERS}
LEGACY_DEFAULT_PROVIDER_IDS = {"openrouter", "newapi", "siliconflow", "lmstudio"}
PROVIDER_TYPES = {"chat_completion", "responses", "gemini", "claude"}
REASONING_EFFORTS = {"default", "none", "low", "medium", "high"}
DEFAULT_REASONING_EFFORT = "default"
DEFAULT_CONTEXT_TOKEN_WARNING_THRESHOLD = 5000
DEFAULT_CONTEXT_TOKEN_CRITICAL_THRESHOLD = 10000
DEFAULT_ASSISTANT_NAME = "Hanako"
DEFAULT_ASSISTANT_GREETING = "对话开始时先接住情绪，再推进任务，不要一上来就像客服一样念模板。"
DEFAULT_ASSISTANT_PROMPT = "你是一个温柔、可靠、说人话的助手。先理解我的真实意图，再给出清晰直接的建议；少一些官话，多一些陪我一起把事情做完的感觉。"
DEFAULT_USER_NAME = "小宝"
DEFAULT_USER_LOCALE = "en-US"
DEFAULT_USER_TIMEZONE = "Asia/Shanghai"
DEFAULT_USER_PROFILE = "希望它更像一个陪我做事的搭档，不要太客服腔。帮我收住情绪，也帮我推进执行。"
DEFAULT_THEME_COLOR = "paper-ink-white"
DEFAULT_THEME_MODE = "dark"
DEFAULT_BACKGROUND_COLOR = "#111111"
DEFAULT_UI_FONT = "Noto Serif SC"
DEFAULT_CODE_FONT = "JetBrains Mono"
DEFAULT_UI_FONT_SIZE = 16
DEFAULT_CODE_FONT_SIZE = 14
DEFAULT_APPEARANCE_CONTRAST = 45
_UNSET = object()


def _provider_spec(provider_id: str) -> dict[str, object]:
    for spec in DEFAULT_RESPONSE_PROVIDERS:
        if spec["id"] == provider_id:
            return dict(spec)
    return dict(DEFAULT_RESPONSE_PROVIDERS[0])


def _normalize_provider_type(value: Any, provider_id: str = "") -> str:
    cleaned = _clean_string(value)
    if cleaned in PROVIDER_TYPES:
        return cleaned
    if provider_id == "gemini":
        return "gemini"
    if provider_id in {"anthropic", "claude"}:
        return "claude"
    return "responses"


def _provider_type_defaults(provider_type: str) -> dict[str, str]:
    if provider_type == "gemini":
        return {
            "name": "Gemini",
            "api_base_url": "https://generativelanguage.googleapis.com/v1beta",
            "default_model": "gemini-2.5-pro",
        }
    if provider_type == "claude":
        return {
            "name": "Claude",
            "api_base_url": "https://api.anthropic.com/v1",
            "default_model": "claude-sonnet-4-5",
        }
    if provider_type == "chat_completion":
        return {
            "name": "Chat Completion",
            "api_base_url": "https://api.example.com/v1",
            "default_model": "gpt-4.1-mini",
        }
    return {
        "name": "OpenAI",
        "api_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-5.4-mini",
    }


def _normalize_provider_api_base_url(raw_url: Any, provider_type: str = "responses") -> str:
    cleaned_url = _clean_string(raw_url).rstrip("/")
    if not cleaned_url:
        return ""

    parsed = urlparse(cleaned_url)
    if not parsed.scheme or not parsed.netloc:
        return cleaned_url

    path = parsed.path.rstrip("/")
    suffixes_by_type = {
        "responses": ("/responses", "/chat/completions", "/completions", "/models"),
        "chat_completion": ("/chat/completions", "/completions", "/models"),
        "gemini": ("/models",),
        "claude": ("/messages", "/models"),
    }
    for suffix in suffixes_by_type.get(provider_type, suffixes_by_type["responses"]):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break

    return urlunparse((parsed.scheme, parsed.netloc, path or "", "", "", "")).rstrip("/")


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _read_settings_file() -> dict[str, Any]:
    candidates = [SETTINGS_FILE]
    allow_legacy_settings = os.getenv("HASH_ALLOW_LEGACY_SETTINGS", "1").strip().lower()
    if allow_legacy_settings not in {"0", "false", "no"} and LEGACY_SETTINGS_FILE.resolve() != SETTINGS_FILE.resolve():
        candidates.append(LEGACY_SETTINGS_FILE)

    for settings_path in candidates:
        if not settings_path.exists():
            continue

        try:
            raw_value = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        return raw_value if isinstance(raw_value, dict) else {}

    return {}



def _normalize_optional_float(value: Any, *, min_value: float, max_value: float) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not min_value <= parsed <= max_value:
        return None
    return parsed


def _normalize_optional_int(value: Any, *, min_value: int, max_value: int | None = None) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < min_value:
        return None
    if max_value is not None and parsed > max_value:
        return None
    return parsed


def _normalize_bounded_int(value: Any, *, min_value: int, max_value: int, fallback: int) -> int:
    parsed = _normalize_optional_int(value, min_value=min_value, max_value=max_value)
    return parsed if parsed is not None else fallback


def _normalize_hex_color(value: Any, fallback: str) -> str:
    cleaned = _clean_string(value)
    if re.fullmatch(r"#[0-9a-fA-F]{6}", cleaned):
        return cleaned
    return fallback


def _normalize_theme_color(value: Any, fallback: str = DEFAULT_THEME_COLOR) -> str:
    cleaned = _clean_string(value)
    if cleaned == DEFAULT_THEME_COLOR or re.fullmatch(r"#[0-9a-fA-F]{6}", cleaned):
        return cleaned
    return fallback


def _normalize_theme_mode(value: Any, fallback: str = DEFAULT_THEME_MODE) -> str:
    cleaned = _clean_string(value)
    return cleaned if cleaned in {"light", "dark", "system"} else fallback


def _normalize_reasoning_effort(value: Any) -> str:
    cleaned = _clean_string(value)
    return cleaned if cleaned in REASONING_EFFORTS else DEFAULT_REASONING_EFFORT


def _normalize_context_token_thresholds(
    warning_threshold: Any,
    critical_threshold: Any,
) -> tuple[int, int]:
    try:
        warning = max(0, int(warning_threshold))
    except (TypeError, ValueError):
        warning = DEFAULT_CONTEXT_TOKEN_WARNING_THRESHOLD

    try:
        critical = max(1, int(critical_threshold))
    except (TypeError, ValueError):
        critical = DEFAULT_CONTEXT_TOKEN_CRITICAL_THRESHOLD

    return warning, max(warning + 1, critical)


def _mask_secret(secret: str | None) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:6]}...{secret[-4:]}"


def _normalize_provider_models(raw_models: Any) -> list[dict[str, str]]:
    if not isinstance(raw_models, (list, tuple)):
        return []

    normalized_models: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for item in raw_models:
        if not isinstance(item, dict):
            continue

        model_id = _clean_string(item.get("id"))
        if not model_id or model_id in seen_ids:
            continue

        seen_ids.add(model_id)
        normalized_models.append(
            {
                "id": model_id,
                "label": _clean_string(item.get("label")) or model_id,
                "group": _clean_string(item.get("group")) or "Models",
                "provider": _clean_string(item.get("provider")),
            }
        )

    return normalized_models


def _normalize_provider_records(
    raw_providers: Any,
    *,
    fallback_model: str,
    fallback_base_url: str,
    fallback_api_key: str,
) -> list[dict[str, Any]]:
    raw_by_id: dict[str, dict[str, Any]] = {}
    if isinstance(raw_providers, list):
        for item in raw_providers:
            if not isinstance(item, dict):
                continue

            provider_id = _clean_string(item.get("id"))
            if provider_id:
                raw_by_id[provider_id] = item

    normalized_records: list[dict[str, Any]] = []

    for spec in DEFAULT_RESPONSE_PROVIDERS:
        provider_id = str(spec["id"])
        raw = raw_by_id.get(provider_id, {})
        provider_type = _normalize_provider_type(raw.get("provider_type") or spec.get("provider_type"), provider_id)
        type_defaults = _provider_type_defaults(provider_type)
        name = _clean_string(raw.get("name")) or _clean_string(spec.get("name")) or type_defaults["name"]
        base_url = _normalize_provider_api_base_url(raw.get("api_base_url"), provider_type)
        default_model = _clean_string(raw.get("default_model"))
        api_key = _clean_string(raw.get("api_key"))

        if provider_id == "openai":
            base_url = (
                base_url
                or _normalize_provider_api_base_url(fallback_base_url, provider_type)
                or _normalize_provider_api_base_url(spec.get("api_base_url"), provider_type)
                or type_defaults["api_base_url"]
            )
            default_model = default_model or fallback_model or _clean_string(spec.get("default_model")) or type_defaults["default_model"]
            api_key = api_key or fallback_api_key
        else:
            base_url = (
                base_url
                or _normalize_provider_api_base_url(spec.get("api_base_url"), provider_type)
                or type_defaults["api_base_url"]
            )
            default_model = default_model or _clean_string(spec.get("default_model")) or type_defaults["default_model"] or fallback_model

        enabled_value = raw.get("enabled")
        record: dict[str, Any] = {
            "id": provider_id,
            "name": name,
            "provider_type": provider_type,
            "enabled": bool(enabled_value) if enabled_value is not None else bool(spec.get("enabled")),
            "supports_model_fetch": True,
            "supports_responses": provider_type == "responses",
            "api_base_url": base_url,
            "default_model": default_model,
            "models": _normalize_provider_models(raw.get("models") or spec.get("models")),
            "last_sync_at": _clean_string(raw.get("last_sync_at")),
            "last_sync_error": _clean_string(raw.get("last_sync_error")),
        }
        if api_key:
            record["api_key"] = api_key
        normalized_records.append(record)

    for provider_id, raw in raw_by_id.items():
        if provider_id in PROVIDER_IDS:
            continue
        provider_type = _normalize_provider_type(raw.get("provider_type"), provider_id)
        type_defaults = _provider_type_defaults(provider_type)
        name = _clean_string(raw.get("name")) or provider_id
        base_url = _normalize_provider_api_base_url(raw.get("api_base_url"), provider_type) or type_defaults["api_base_url"]
        default_model = _clean_string(raw.get("default_model")) or type_defaults["default_model"] or fallback_model
        api_key = _clean_string(raw.get("api_key"))

        record = {
            "id": provider_id,
            "name": name,
            "provider_type": provider_type,
            "enabled": bool(raw.get("enabled")) if raw.get("enabled") is not None else True,
            "supports_model_fetch": True,
            "supports_responses": provider_type == "responses",
            "api_base_url": base_url,
            "default_model": default_model,
            "models": _normalize_provider_models(raw.get("models")),
            "last_sync_at": _clean_string(raw.get("last_sync_at")),
            "last_sync_error": _clean_string(raw.get("last_sync_error")),
        }
        if api_key:
            record["api_key"] = api_key
        normalized_records.append(record)

    return normalized_records


def _normalize_active_provider_id(raw_provider_id: Any, providers: list[dict[str, Any]]) -> str:
    candidate = _clean_string(raw_provider_id) or "openai"
    enabled_ids = {
        _clean_string(provider.get("id"))
        for provider in providers
        if bool(provider.get("enabled"))
    }
    if candidate in enabled_ids:
        return candidate
    if "openai" in enabled_ids:
        return "openai"
    if enabled_ids:
        return next(iter(enabled_ids))
    all_ids = [
        _clean_string(provider.get("id"))
        for provider in providers
        if _clean_string(provider.get("id"))
    ]
    if candidate in all_ids:
        return candidate
    if "openai" in all_ids:
        return "openai"
    return all_ids[0] if all_ids else "openai"


def _infer_provider_id_for_model(model_id: Any, providers: list[dict[str, Any]], fallback_provider_id: str = "") -> str:
    matched_provider_id = _find_provider_id_for_model(model_id, providers)
    if matched_provider_id:
        return matched_provider_id

    return _normalize_active_provider_id(fallback_provider_id, providers)


def _find_provider_id_for_model(model_id: Any, providers: list[dict[str, Any]]) -> str:
    cleaned_model_id = _clean_string(model_id)
    if cleaned_model_id:
        for provider in providers:
            provider_id = _clean_string(provider.get("id"))
            if not provider_id:
                continue
            for model in _normalize_provider_models(provider.get("models")):
                if _clean_string(model.get("id")) == cleaned_model_id:
                    return provider_id

    return ""


def _public_provider_payload(record: dict[str, Any]) -> dict[str, object]:
    api_key = _clean_string(record.get("api_key")) or None
    return {
        "id": _clean_string(record.get("id")),
        "name": _clean_string(record.get("name")),
        "provider_type": _normalize_provider_type(record.get("provider_type"), _clean_string(record.get("id"))),
        "enabled": bool(record.get("enabled")),
        "supports_model_fetch": bool(record.get("supports_model_fetch")),
        "supports_responses": _normalize_provider_type(record.get("provider_type"), _clean_string(record.get("id"))) == "responses",
        "api_base_url": _clean_string(record.get("api_base_url")),
        "default_model": _clean_string(record.get("default_model")),
        "has_api_key": bool(api_key),
        "api_key_preview": _mask_secret(api_key),
        "api_key": api_key or "",
        "models": _normalize_provider_models(record.get("models")),
        "last_sync_at": _clean_string(record.get("last_sync_at")),
        "last_sync_error": _clean_string(record.get("last_sync_error")),
    }


@dataclass(slots=True)
class Settings:
    model: str
    default_reasoning_effort: str
    context_workbench_model: str
    context_workbench_provider_id: str
    project_root: Path
    max_tool_rounds: int
    tool_settings: list[dict[str, Any]]
    response_providers: list[dict[str, Any]]
    active_provider_id: str
    assistant_name: str = DEFAULT_ASSISTANT_NAME
    assistant_greeting: str = DEFAULT_ASSISTANT_GREETING
    assistant_prompt: str = DEFAULT_ASSISTANT_PROMPT
    temperature: float | None = None
    top_p: float | None = None
    context_message_limit: int | None = None
    streaming: bool = True
    user_name: str = DEFAULT_USER_NAME
    user_locale: str = DEFAULT_USER_LOCALE
    user_timezone: str = DEFAULT_USER_TIMEZONE
    user_profile: str = DEFAULT_USER_PROFILE
    context_token_warning_threshold: int = DEFAULT_CONTEXT_TOKEN_WARNING_THRESHOLD
    context_token_critical_threshold: int = DEFAULT_CONTEXT_TOKEN_CRITICAL_THRESHOLD
    theme_color: str = DEFAULT_THEME_COLOR
    theme_mode: str = DEFAULT_THEME_MODE
    background_color: str = DEFAULT_BACKGROUND_COLOR
    ui_font: str = DEFAULT_UI_FONT
    code_font: str = DEFAULT_CODE_FONT
    ui_font_size: int = DEFAULT_UI_FONT_SIZE
    code_font_size: int = DEFAULT_CODE_FONT_SIZE
    appearance_contrast: int = DEFAULT_APPEARANCE_CONTRAST
    service_hints_enabled: bool = True
    openai_api_key: str | None = None
    openai_base_url: str | None = None

    def active_provider(self) -> dict[str, Any]:
        for provider in self.response_providers:
            if _clean_string(provider.get("id")) == self.active_provider_id:
                return provider
        return self.response_providers[0] if self.response_providers else {}

    def active_provider_model_ids(self) -> list[str]:
        return [
            _clean_string(model.get("id"))
            for model in _normalize_provider_models(self.active_provider().get("models"))
            if _clean_string(model.get("id"))
        ]

    def public_payload(self) -> dict[str, object]:
        return {
            "default_model": self.model,
            "default_reasoning_effort": self.default_reasoning_effort,
            "context_workbench_model": self.context_workbench_model,
            "context_workbench_provider_id": self.context_workbench_provider_id,
            "context_token_warning_threshold": self.context_token_warning_threshold,
            "context_token_critical_threshold": self.context_token_critical_threshold,
            "openai_base_url": self.openai_base_url or "",
            "max_tool_rounds": self.max_tool_rounds,
            "assistant_name": self.assistant_name,
            "assistant_greeting": self.assistant_greeting,
            "assistant_prompt": self.assistant_prompt,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "context_message_limit": self.context_message_limit,
            "streaming": self.streaming,
            "user_name": self.user_name,
            "user_locale": self.user_locale,
            "user_timezone": self.user_timezone,
            "user_profile": self.user_profile,
            "theme_color": self.theme_color,
            "theme_mode": self.theme_mode,
            "background_color": self.background_color,
            "ui_font": self.ui_font,
            "code_font": self.code_font,
            "ui_font_size": self.ui_font_size,
            "code_font_size": self.code_font_size,
            "appearance_contrast": self.appearance_contrast,
            "service_hints_enabled": self.service_hints_enabled,
            "tool_settings": normalize_tool_settings(self.tool_settings),
            "has_api_key": bool(self.openai_api_key),
            "api_key_preview": _mask_secret(self.openai_api_key),
            "openai_api_key": self.openai_api_key or "",
            "project_root": str(self.project_root),
            "active_provider_id": self.active_provider_id,
            "response_providers": [
                _public_provider_payload(provider)
                for provider in self.response_providers
            ],
        }


def load_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env")
    stored = _read_settings_file()

    env_project_root = _clean_string(os.getenv("AGENT_PROJECT_ROOT")) or "."
    project_root = Path(stored.get("project_root") or env_project_root).expanduser()
    if not project_root.is_absolute():
        project_root = (REPO_ROOT / project_root).resolve()

    model = (
        _clean_string(stored.get("model"))
        or _clean_string(os.getenv("OPENAI_MODEL"))
        or "gpt-5.4-mini"
    )
    default_reasoning_effort = _normalize_reasoning_effort(stored.get("default_reasoning_effort"))
    context_workbench_model = _clean_string(stored.get("context_workbench_model")) or model
    context_token_warning_threshold, context_token_critical_threshold = _normalize_context_token_thresholds(
        stored.get("context_token_warning_threshold"),
        stored.get("context_token_critical_threshold"),
    )

    raw_rounds = stored.get("max_tool_rounds", os.getenv("MAX_TOOL_ROUNDS", "999999"))
    try:
        max_tool_rounds = max(1, int(raw_rounds))
    except (TypeError, ValueError):
        max_tool_rounds = 999999

    env_api_key = _clean_string(os.getenv("OPENAI_API_KEY"))
    env_base_url = _clean_string(os.getenv("OPENAI_BASE_URL"))

    stored_api_key = _clean_string(stored.get("openai_api_key")) or env_api_key
    stored_base_url = _clean_string(stored.get("openai_base_url")) or env_base_url

    response_providers = _normalize_provider_records(
        stored.get("response_providers"),
        fallback_model=model,
        fallback_base_url=stored_base_url,
        fallback_api_key=stored_api_key,
    )
    active_provider_id = _normalize_active_provider_id(
        stored.get("active_provider_id"),
        response_providers,
    )

    active_provider = next(
        (
            provider
            for provider in response_providers
            if _clean_string(provider.get("id")) == active_provider_id
        ),
        response_providers[0],
    )
    model = _clean_string(active_provider.get("default_model")) or model
    openai_base_url = _clean_string(active_provider.get("api_base_url")) or None
    openai_api_key = _clean_string(active_provider.get("api_key")) or None
    context_workbench_provider_id = _normalize_active_provider_id(
        _find_provider_id_for_model(context_workbench_model, response_providers)
        or stored.get("context_workbench_provider_id")
        or active_provider_id,
        response_providers,
    )
    tool_settings = normalize_tool_settings(stored.get("tool_settings"))

    assistant_name = _clean_string(stored.get("assistant_name")) or DEFAULT_ASSISTANT_NAME
    assistant_greeting = _clean_string(stored.get("assistant_greeting")) or DEFAULT_ASSISTANT_GREETING
    assistant_prompt = _clean_string(stored.get("assistant_prompt")) or DEFAULT_ASSISTANT_PROMPT
    temperature = _normalize_optional_float(stored.get("temperature"), min_value=0, max_value=2)
    top_p = _normalize_optional_float(stored.get("top_p"), min_value=0, max_value=1)
    context_message_limit = _normalize_optional_int(stored.get("context_message_limit"), min_value=1)
    streaming = bool(stored.get("streaming", True))
    user_name = _clean_string(stored.get("user_name")) or DEFAULT_USER_NAME
    user_locale = _clean_string(stored.get("user_locale")) or DEFAULT_USER_LOCALE
    user_timezone = _clean_string(stored.get("user_timezone")) or DEFAULT_USER_TIMEZONE
    user_profile = _clean_string(stored.get("user_profile")) or DEFAULT_USER_PROFILE
    theme_color = _normalize_theme_color(stored.get("theme_color"))
    theme_mode = _normalize_theme_mode(stored.get("theme_mode"))
    background_color = _normalize_hex_color(stored.get("background_color"), DEFAULT_BACKGROUND_COLOR)
    ui_font = _clean_string(stored.get("ui_font")) or DEFAULT_UI_FONT
    code_font = _clean_string(stored.get("code_font")) or DEFAULT_CODE_FONT
    ui_font_size = _normalize_bounded_int(
        stored.get("ui_font_size"),
        min_value=12,
        max_value=22,
        fallback=DEFAULT_UI_FONT_SIZE,
    )
    code_font_size = _normalize_bounded_int(
        stored.get("code_font_size"),
        min_value=11,
        max_value=20,
        fallback=DEFAULT_CODE_FONT_SIZE,
    )
    appearance_contrast = _normalize_bounded_int(
        stored.get("appearance_contrast"),
        min_value=30,
        max_value=80,
        fallback=DEFAULT_APPEARANCE_CONTRAST,
    )
    service_hints_enabled = bool(stored.get("service_hints_enabled", True))

    return Settings(
        model=model,
        default_reasoning_effort=default_reasoning_effort,
        context_workbench_model=context_workbench_model,
        context_workbench_provider_id=context_workbench_provider_id,
        project_root=project_root,
        max_tool_rounds=max_tool_rounds,
        tool_settings=tool_settings,
        response_providers=response_providers,
        active_provider_id=active_provider_id,
        assistant_name=assistant_name,
        assistant_greeting=assistant_greeting,
        assistant_prompt=assistant_prompt,
        temperature=temperature,
        top_p=top_p,
        context_message_limit=context_message_limit,
        streaming=streaming,
        user_name=user_name,
        user_locale=user_locale,
        user_timezone=user_timezone,
        user_profile=user_profile,
        context_token_warning_threshold=context_token_warning_threshold,
        context_token_critical_threshold=context_token_critical_threshold,
        theme_color=theme_color,
        theme_mode=theme_mode,
        background_color=background_color,
        ui_font=ui_font,
        code_font=code_font,
        ui_font_size=ui_font_size,
        code_font_size=code_font_size,
        appearance_contrast=appearance_contrast,
        service_hints_enabled=service_hints_enabled,
        openai_api_key=openai_api_key,
        openai_base_url=openai_base_url,
    )


def save_settings(
    *,
    default_model: str | None = None,
    default_reasoning_effort: str | None = None,
    context_workbench_model: str | None = None,
    context_workbench_provider_id: str | None = None,
    context_token_warning_threshold: int | None = None,
    context_token_critical_threshold: int | None = None,
    openai_base_url: str | None = None,
    max_tool_rounds: int | None = None,
    assistant_name: str | None = None,
    assistant_greeting: str | None = None,
    assistant_prompt: str | None = None,
    temperature: float | None | object = _UNSET,
    top_p: float | None | object = _UNSET,
    context_message_limit: int | None | object = _UNSET,
    streaming: bool | None = None,
    user_name: str | None = None,
    user_locale: str | None = None,
    user_timezone: str | None = None,
    user_profile: str | None = None,
    theme_color: str | None = None,
    theme_mode: str | None = None,
    background_color: str | None = None,
    ui_font: str | None = None,
    code_font: str | None = None,
    ui_font_size: int | None = None,
    code_font_size: int | None = None,
    appearance_contrast: int | None = None,
    service_hints_enabled: bool | None = None,
    openai_api_key: str | None = None,
    clear_api_key: bool = False,
    active_provider_id: str | None = None,
    response_providers: list[dict[str, Any]] | None = None,
    deleted_provider_ids: list[str] | None = None,
    tool_settings: list[dict[str, Any]] | None = None,
) -> Settings:
    current = _read_settings_file()
    loaded = load_settings()

    current_records = _normalize_provider_records(
        current.get("response_providers"),
        fallback_model=_clean_string(current.get("model")) or loaded.model,
        fallback_base_url=_clean_string(current.get("openai_base_url")) or (loaded.openai_base_url or ""),
        fallback_api_key=_clean_string(current.get("openai_api_key")) or (loaded.openai_api_key or ""),
    )
    current_by_id = {
        _clean_string(provider.get("id")): dict(provider)
        for provider in current_records
    }

    if deleted_provider_ids:
        for raw_provider_id in deleted_provider_ids:
            provider_id = _clean_string(raw_provider_id)
            if not provider_id or provider_id in PROVIDER_IDS:
                continue
            current_by_id.pop(provider_id, None)

    if response_providers is not None:
        incoming_by_id: dict[str, dict[str, Any]] = {}
        for item in response_providers:
            if not isinstance(item, dict):
                continue
            provider_id = _clean_string(item.get("id"))
            if provider_id:
                incoming_by_id[provider_id] = item

        for provider_id, incoming in incoming_by_id.items():
            if provider_id in current_by_id:
                continue
            provider_type = _normalize_provider_type(incoming.get("provider_type"), provider_id)
            type_defaults = _provider_type_defaults(provider_type)
            current_by_id[provider_id] = {
                "id": provider_id,
                "name": _clean_string(incoming.get("name")) or provider_id,
                "provider_type": provider_type,
                "enabled": bool(incoming.get("enabled")) if incoming.get("enabled") is not None else True,
                "supports_model_fetch": True,
                "supports_responses": provider_type == "responses",
                "api_base_url": _clean_string(incoming.get("api_base_url")) or type_defaults["api_base_url"],
                "default_model": _clean_string(incoming.get("default_model")) or type_defaults["default_model"] or loaded.model,
                "models": _normalize_provider_models(incoming.get("models")),
                "last_sync_at": _clean_string(incoming.get("last_sync_at")),
                "last_sync_error": _clean_string(incoming.get("last_sync_error")),
            }

        for provider_id, provider in current_by_id.items():
            incoming = incoming_by_id.get(provider_id)
            if incoming is None:
                continue

            if "name" in incoming:
                provider["name"] = _clean_string(incoming.get("name")) or provider_id

            if "provider_type" in incoming:
                provider_type = _normalize_provider_type(incoming.get("provider_type"), provider_id)
                provider["provider_type"] = provider_type
                provider["supports_model_fetch"] = True
                provider["supports_responses"] = provider_type == "responses"

            if "enabled" in incoming:
                provider["enabled"] = bool(incoming.get("enabled"))

            if "api_base_url" in incoming:
                provider_type = _normalize_provider_type(provider.get("provider_type"), provider_id)
                cleaned_base_url = _normalize_provider_api_base_url(
                    incoming.get("api_base_url"),
                    provider_type,
                )
                provider["api_base_url"] = (
                    cleaned_base_url
                    or _clean_string(_provider_spec(provider_id).get("api_base_url"))
                    or _provider_type_defaults(provider_type)["api_base_url"]
                )

            if "default_model" in incoming:
                cleaned_model = _clean_string(incoming.get("default_model"))
                provider["default_model"] = cleaned_model or _clean_string(provider.get("default_model")) or loaded.model

            if "models" in incoming:
                provider["models"] = _normalize_provider_models(incoming.get("models"))

            if "last_sync_at" in incoming:
                provider["last_sync_at"] = _clean_string(incoming.get("last_sync_at"))

            if "last_sync_error" in incoming:
                provider["last_sync_error"] = _clean_string(incoming.get("last_sync_error"))

            if bool(incoming.get("clear_api_key")):
                provider.pop("api_key", None)
            elif isinstance(incoming.get("api_key"), str):
                cleaned_api_key = _clean_string(incoming.get("api_key"))
                if cleaned_api_key:
                    provider["api_key"] = cleaned_api_key

    next_active_provider_id = _normalize_active_provider_id(
        active_provider_id or current.get("active_provider_id") or loaded.active_provider_id,
        list(current_by_id.values()),
    )
    active_provider = current_by_id[next_active_provider_id]

    if default_model is not None:
        active_provider["default_model"] = _clean_string(default_model) or loaded.model

    if openai_base_url is not None:
        active_provider_type = _normalize_provider_type(
            active_provider.get("provider_type"),
            next_active_provider_id,
        )
        cleaned_base_url = _normalize_provider_api_base_url(
            openai_base_url,
            active_provider_type,
        )
        active_provider["api_base_url"] = cleaned_base_url or _clean_string(_provider_spec(next_active_provider_id).get("api_base_url"))

    if clear_api_key:
        active_provider.pop("api_key", None)
    elif openai_api_key is not None:
        cleaned_api_key = _clean_string(openai_api_key)
        if cleaned_api_key:
            active_provider["api_key"] = cleaned_api_key

    ordered_records = [
        current_by_id[_clean_string(spec.get("id"))]
        for spec in DEFAULT_RESPONSE_PROVIDERS
    ] + [
        provider
        for provider_id, provider in current_by_id.items()
        if provider_id not in PROVIDER_IDS
    ]

    current["active_provider_id"] = next_active_provider_id
    current["response_providers"] = ordered_records
    current["model"] = _clean_string(active_provider.get("default_model")) or loaded.model

    if default_reasoning_effort is not None:
        current["default_reasoning_effort"] = _normalize_reasoning_effort(default_reasoning_effort)
    elif "default_reasoning_effort" not in current:
        current["default_reasoning_effort"] = loaded.default_reasoning_effort

    next_context_workbench_model = _clean_string(current.get("context_workbench_model")) or loaded.context_workbench_model
    if context_workbench_model is not None:
        cleaned_context_workbench_model = _clean_string(context_workbench_model)
        if cleaned_context_workbench_model:
            next_context_workbench_model = cleaned_context_workbench_model
        else:
            next_context_workbench_model = _clean_string(current.get("model")) or loaded.model
    current["context_workbench_model"] = next_context_workbench_model

    next_context_workbench_provider_id = _normalize_active_provider_id(
        context_workbench_provider_id
        or _find_provider_id_for_model(next_context_workbench_model, ordered_records)
        or current.get("context_workbench_provider_id")
        or loaded.context_workbench_provider_id
        or next_active_provider_id,
        ordered_records,
    )
    current["context_workbench_provider_id"] = next_context_workbench_provider_id

    next_context_token_warning_threshold, next_context_token_critical_threshold = _normalize_context_token_thresholds(
        (
            context_token_warning_threshold
            if context_token_warning_threshold is not None
            else current.get("context_token_warning_threshold", loaded.context_token_warning_threshold)
        ),
        (
            context_token_critical_threshold
            if context_token_critical_threshold is not None
            else current.get("context_token_critical_threshold", loaded.context_token_critical_threshold)
        ),
    )
    current["context_token_warning_threshold"] = next_context_token_warning_threshold
    current["context_token_critical_threshold"] = next_context_token_critical_threshold

    if max_tool_rounds is not None:
        current["max_tool_rounds"] = max(1, int(max_tool_rounds))

    if assistant_name is not None:
        current["assistant_name"] = _clean_string(assistant_name) or DEFAULT_ASSISTANT_NAME
    if assistant_greeting is not None:
        current["assistant_greeting"] = _clean_string(assistant_greeting)
    if assistant_prompt is not None:
        current["assistant_prompt"] = _clean_string(assistant_prompt)
    if temperature is not _UNSET:
        current["temperature"] = _normalize_optional_float(temperature, min_value=0, max_value=2)
    if top_p is not _UNSET:
        current["top_p"] = _normalize_optional_float(top_p, min_value=0, max_value=1)
    if context_message_limit is not _UNSET:
        current["context_message_limit"] = _normalize_optional_int(context_message_limit, min_value=1)
    current.pop("thinking_budget", None)
    if streaming is not None:
        current["streaming"] = bool(streaming)
    if user_name is not None:
        current["user_name"] = _clean_string(user_name) or DEFAULT_USER_NAME
    if user_locale is not None:
        current["user_locale"] = _clean_string(user_locale) or DEFAULT_USER_LOCALE
    if user_timezone is not None:
        current["user_timezone"] = _clean_string(user_timezone) or DEFAULT_USER_TIMEZONE
    if user_profile is not None:
        current["user_profile"] = _clean_string(user_profile)
    if theme_color is not None:
        current["theme_color"] = _normalize_theme_color(theme_color)
    if theme_mode is not None:
        current["theme_mode"] = _normalize_theme_mode(theme_mode)
    if background_color is not None:
        current["background_color"] = _normalize_hex_color(background_color, DEFAULT_BACKGROUND_COLOR)
    if ui_font is not None:
        current["ui_font"] = _clean_string(ui_font) or DEFAULT_UI_FONT
    if code_font is not None:
        current["code_font"] = _clean_string(code_font) or DEFAULT_CODE_FONT
    if ui_font_size is not None:
        current["ui_font_size"] = _normalize_bounded_int(
            ui_font_size,
            min_value=12,
            max_value=22,
            fallback=DEFAULT_UI_FONT_SIZE,
        )
    if code_font_size is not None:
        current["code_font_size"] = _normalize_bounded_int(
            code_font_size,
            min_value=11,
            max_value=20,
            fallback=DEFAULT_CODE_FONT_SIZE,
        )
    if appearance_contrast is not None:
        current["appearance_contrast"] = _normalize_bounded_int(
            appearance_contrast,
            min_value=30,
            max_value=80,
            fallback=DEFAULT_APPEARANCE_CONTRAST,
        )
    if service_hints_enabled is not None:
        current["service_hints_enabled"] = bool(service_hints_enabled)

    if tool_settings is not None:
        current["tool_settings"] = normalize_tool_settings(tool_settings)
    elif "tool_settings" not in current:
        current["tool_settings"] = normalize_tool_settings(loaded.tool_settings)

    active_base_url = _clean_string(active_provider.get("api_base_url"))
    if active_base_url:
        current["openai_base_url"] = active_base_url
    else:
        current.pop("openai_base_url", None)

    active_api_key = _clean_string(active_provider.get("api_key"))
    if active_api_key:
        current["openai_api_key"] = active_api_key
    else:
        current.pop("openai_api_key", None)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return load_settings()
