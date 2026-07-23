from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import web_runtime  # noqa: E402
from backend.web_handler import context_provider_updates_from_request  # noqa: E402
from simple_agent import config as config_module  # noqa: E402


@contextmanager
def isolated_settings() -> Iterator[None]:
    previous_data_dir = config_module.DATA_DIR
    previous_settings_file = config_module.SETTINGS_FILE
    with tempfile.TemporaryDirectory(prefix="studio-context-settings-") as raw_temp_dir:
        temp_dir = Path(raw_temp_dir)
        config_module.DATA_DIR = temp_dir
        config_module.SETTINGS_FILE = temp_dir / "openai_settings.json"
        try:
            yield
        finally:
            config_module.DATA_DIR = previous_data_dir
            config_module.SETTINGS_FILE = previous_settings_file


def provider(settings: config_module.Settings, provider_id: str) -> dict[str, object]:
    return next(item for item in settings.response_providers if item.get("id") == provider_id)


def configure_deepseek(*, current_model: str = "deepseek-v4-pro") -> config_module.Settings:
    return config_module.save_settings(
        context_workbench_provider_id="anthropic",
        context_workbench_model=current_model,
        response_providers=[
            {
                "id": "anthropic",
                "name": "DeepSeek",
                "provider_type": "claude",
                "api_base_url": "https://api.deepseek.com/anthropic",
                "api_key": "test-key",
                "default_model": current_model,
            }
        ],
    )


def deepseek_models() -> list[dict[str, str]]:
    return [
        {
            "id": "deepseek-v4-flash",
            "label": "deepseek-v4-flash",
            "group": "DeepSeek",
            "provider": "DeepSeek",
        },
        {
            "id": "deepseek-v4-pro",
            "label": "deepseek-v4-pro",
            "group": "DeepSeek",
            "provider": "DeepSeek",
        },
    ]


def test_refresh_persists_exact_provider_models_globally() -> None:
    with isolated_settings():
        settings = configure_deepseek()
        with patch.object(web_runtime, "fetch_models_from_provider", return_value=deepseek_models()):
            updated = web_runtime.refresh_context_workbench_provider_models(settings, "anthropic")

        persisted = config_module.load_settings()
        assert updated.context_workbench_provider_id == "anthropic"
        assert persisted.context_workbench_model == "deepseek-v4-pro"
        assert [item["id"] for item in provider(persisted, "anthropic")["models"]] == [
            "deepseek-v4-flash",
            "deepseek-v4-pro",
        ]

        response = web_runtime.context_workbench_settings_response(persisted)
        assert response["scope"] == "global"
        assert "models" not in response
        deepseek_payload = next(item for item in response["providers"] if item["id"] == "anthropic")
        assert [item["id"] for item in deepseek_payload["models"]] == [
            "deepseek-v4-flash",
            "deepseek-v4-pro",
        ]


def test_missing_current_model_is_not_injected_into_fetched_models() -> None:
    with isolated_settings():
        settings = configure_deepseek(current_model="claude-sonnet-4-5")
        with patch.object(web_runtime, "fetch_models_from_provider", return_value=deepseek_models()):
            persisted = web_runtime.refresh_context_workbench_provider_models(settings, "anthropic")

        assert persisted.context_workbench_model == "claude-sonnet-4-5"
        assert [item["id"] for item in provider(persisted, "anthropic")["models"]] == [
            "deepseek-v4-flash",
            "deepseek-v4-pro",
        ]


def test_refresh_error_is_global_and_keeps_last_successful_models() -> None:
    with isolated_settings():
        settings = configure_deepseek()
        settings = config_module.save_settings(
            response_providers=[
                {
                    "id": "anthropic",
                    "models": deepseek_models(),
                    "last_sync_at": "2026-07-23T00:00:00+00:00",
                }
            ]
        )
        with patch.object(web_runtime, "fetch_models_from_provider", side_effect=ValueError("provider unavailable")):
            persisted = web_runtime.refresh_context_workbench_provider_models(settings, "anthropic")

        deepseek = provider(persisted, "anthropic")
        assert [item["id"] for item in deepseek["models"]] == ["deepseek-v4-flash", "deepseek-v4-pro"]
        assert deepseek["last_sync_at"] == "2026-07-23T00:00:00+00:00"
        assert deepseek["last_sync_error"] == "provider unavailable"


def test_connection_change_invalidates_only_that_provider_model_cache() -> None:
    with isolated_settings():
        configure_deepseek()
        config_module.save_settings(
            response_providers=[
                {
                    "id": "anthropic",
                    "models": deepseek_models(),
                    "last_sync_at": "2026-07-23T00:00:00+00:00",
                    "last_sync_error": "old error",
                }
            ]
        )
        persisted = config_module.save_settings(
            response_providers=[
                {
                    "id": "anthropic",
                    "api_base_url": "https://new.example.com/anthropic",
                }
            ]
        )

        changed = provider(persisted, "anthropic")
        assert changed["models"] == []
        assert changed["last_sync_at"] == ""
        assert changed["last_sync_error"] == ""
        assert persisted.context_workbench_model == "deepseek-v4-pro"


def test_ordinary_settings_request_cannot_write_server_model_cache() -> None:
    updates = context_provider_updates_from_request(
        [
            {
                "id": "anthropic",
                "name": "DeepSeek",
                "api_key": "new-key",
                "models": deepseek_models(),
                "last_sync_at": "fake",
                "last_sync_error": "fake",
            }
        ]
    )
    assert updates == [{"id": "anthropic", "name": "DeepSeek", "api_key": "new-key"}]


def test_external_provider_does_not_fall_back_to_a_different_endpoint() -> None:
    with isolated_settings():
        settings = configure_deepseek()
        without_base_url = {
            **provider(settings, "anthropic"),
            "api_base_url": "",
        }
        try:
            web_runtime.build_context_provider_client(without_base_url, settings)
        except ValueError as exc:
            assert str(exc) == "Base URL is required for the selected provider."
        else:
            raise AssertionError("external provider silently fell back to a default endpoint")


def test_api_key_round_trips_as_the_real_global_value() -> None:
    with isolated_settings():
        settings = configure_deepseek()
        response = web_runtime.context_workbench_settings_response(settings)
        deepseek_payload = next(item for item in response["providers"] if item["id"] == "anthropic")
        assert deepseek_payload["api_key"] == "test-key"
        assert "has_api_key" not in deepseek_payload

        cleared = config_module.save_settings(
            response_providers=[{"id": "anthropic", "api_key": ""}]
        )
        assert "api_key" not in provider(cleared, "anthropic")
        cleared_response = web_runtime.context_workbench_settings_response(cleared)
        cleared_payload = next(item for item in cleared_response["providers"] if item["id"] == "anthropic")
        assert cleared_payload["api_key"] == ""


def main() -> None:
    tests = [
        test_refresh_persists_exact_provider_models_globally,
        test_missing_current_model_is_not_injected_into_fetched_models,
        test_refresh_error_is_global_and_keeps_last_successful_models,
        test_connection_change_invalidates_only_that_provider_model_cache,
        test_ordinary_settings_request_cannot_write_server_model_cache,
        test_external_provider_does_not_fall_back_to_a_different_endpoint,
        test_api_key_round_trips_as_the_real_global_value,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} global context workbench settings tests passed")


if __name__ == "__main__":
    main()
