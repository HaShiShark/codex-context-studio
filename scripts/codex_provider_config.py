"""Read native provider TOML once and project settings onto the Studio transport.

This module does not read credentials or write user configuration. Input/output
are piped by the launchers, never placed in process arguments or logs.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from typing import Any


TRANSPORT_OVERRIDES = frozenset({
    "name", "base_url", "wire_api", "requires_openai_auth", "supports_websockets",
    "supports_standalone_web_search", "api_key", "include_internal_metadata",
})


def toml_value(value: Any, *, cli: bool = False) -> str:
    if isinstance(value, str):
        encoded = json.dumps(value, ensure_ascii=True)
        # npm's Windows .cmd launcher adds cmd.exe between PowerShell and Codex.
        # TOML Unicode escapes carry shell metacharacters without shell evaluation.
        return re.sub(r"[ &|<>^%!()]", lambda match: f"\\u{ord(match[0]):04x}", encoded) if cli else encoded
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item, cli=cli) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{toml_value(key, cli=cli)} = {toml_value(item, cli=cli)}" for key, item in value.items()) + "}"
    raise ValueError(f"Unsupported provider setting type: {type(value).__name__}")


def provider_projection(text: str) -> dict[str, Any]:
    config = tomllib.loads(text.lstrip("\ufeff"))
    provider_id = config.get("model_provider", "openai")
    providers = config.get("model_providers", {})
    if provider_id != "openai" and provider_id not in providers:
        raise ValueError(f"Provider section [model_providers.{provider_id}] not found")
    provider = providers.get(provider_id, {})
    name = provider.get("name", "OpenAI" if provider_id == "openai" else "")
    standalone_search = name == "OpenAI" or bool(provider.get("supports_standalone_web_search", False))
    passthrough = {key: value for key, value in provider.items() if key not in TRANSPORT_OVERRIDES}
    passthrough["supports_standalone_web_search"] = standalone_search
    return {
        "provider_id": provider_id,
        "provider_name": name,
        "base_url": provider.get("base_url") or config.get("openai_base_url", ""),
        "api_key": provider.get("api_key", ""),
        "env_key": provider.get("env_key", ""),
        "bearer_token": provider.get("experimental_bearer_token", ""),
        "wire_api": provider.get("wire_api", ""),
        "requires_openai_auth": provider.get("requires_openai_auth", ""),
        "provider_options": [
            f"{key if re.fullmatch(r'[A-Za-z0-9_-]+', key) else toml_value(key)} = {toml_value(value)}"
            for key, value in passthrough.items()
        ],
        "provider_cli_options": [
            f"{key if re.fullmatch(r'[A-Za-z0-9_-]+', key) else toml_value(key, cli=True)} = {toml_value(value, cli=True)}"
            for key, value in passthrough.items()
        ],
    }


if __name__ == "__main__":
    try:
        result = provider_projection(sys.stdin.buffer.read().decode("utf-8-sig"))
        print(json.dumps(result, ensure_ascii=True))
    except (ValueError, TypeError) as exc:
        # Do not include TOML source: it may contain authentication headers.
        print(f"Cannot read Codex provider configuration ({type(exc).__name__}). Check TOML syntax and provider selection.", file=sys.stderr)
        sys.exit(1)
