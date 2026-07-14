from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import proxy_fastapi  # noqa: E402
from backend.compact_controller import (  # noqa: E402
    LOCAL_COMPACT_PROMPT_PREFIX,
    CompactController,
    is_local_compact_prompt_text,
    replacement_local_compact_prompt,
)
from backend.transcript_codec import input_items_to_transcript, transcript_to_input_items  # noqa: E402
from simple_agent import config as config_module  # noqa: E402


@contextmanager
def isolated_settings() -> Iterator[None]:
    previous_data_dir = config_module.DATA_DIR
    previous_settings_file = config_module.SETTINGS_FILE

    with tempfile.TemporaryDirectory(prefix="studio-prompt-settings-") as raw_temp_dir:
        temp_dir = Path(raw_temp_dir)
        config_module.DATA_DIR = temp_dir
        config_module.SETTINGS_FILE = temp_dir / "openai_settings.json"
        try:
            yield
        finally:
            config_module.DATA_DIR = previous_data_dir
            config_module.SETTINGS_FILE = previous_settings_file


def reset_first_instructions_scan() -> None:
    with proxy_fastapi._FIRST_CODEX_INSTRUCTIONS_SCAN_LOCK:
        proxy_fastapi._FIRST_CODEX_INSTRUCTIONS_SCAN_DONE = False


def message(role: str, text: str) -> dict[str, Any]:
    return {"type": "message", "role": role, "content": text}


def test_first_codex_instructions_updates_default_and_current_on_next_proxy_start() -> None:
    with isolated_settings():
        reset_first_instructions_scan()
        proxy_fastapi._capture_first_codex_instructions({"instructions": " latest official prompt "})

        settings = config_module.load_settings()
        assert settings.codex_system_prompt_default == "latest official prompt"
        assert settings.codex_system_prompt == "latest official prompt"

        proxy_fastapi._capture_first_codex_instructions({"instructions": "second prompt"})
        settings = config_module.load_settings()
        assert settings.codex_system_prompt_default == "latest official prompt"
        assert settings.codex_system_prompt == "latest official prompt"

        reset_first_instructions_scan()
        proxy_fastapi._capture_first_codex_instructions({"instructions": "new official prompt"})
        settings = config_module.load_settings()
        assert settings.codex_system_prompt_default == "new official prompt"
        assert settings.codex_system_prompt == "new official prompt"


def test_first_codex_instructions_preserves_user_custom_prompt() -> None:
    with isolated_settings():
        config_module.save_settings(
            codex_system_prompt_default="old official prompt",
            codex_system_prompt="custom user prompt",
        )
        reset_first_instructions_scan()
        proxy_fastapi._capture_first_codex_instructions({"instructions": "new official prompt"})

        settings = config_module.load_settings()
        assert settings.codex_system_prompt_default == "new official prompt"
        assert settings.codex_system_prompt == "custom user prompt"


def test_first_codex_instructions_waits_for_a_supported_prompt_location() -> None:
    with isolated_settings():
        reset_first_instructions_scan()
        proxy_fastapi._capture_first_codex_instructions(
            {"input": [{"type": "message", "instructions": "nested prompt must be ignored"}]}
        )

        settings = config_module.load_settings()
        assert settings.codex_system_prompt_default == ""
        assert settings.codex_system_prompt == ""

        proxy_fastapi._capture_first_codex_instructions({"instructions": "late prompt"})
        settings = config_module.load_settings()
        assert settings.codex_system_prompt_default == "late prompt"
        assert settings.codex_system_prompt == "late prompt"


def test_first_codex_instructions_reads_lite_base_developer_only() -> None:
    with isolated_settings():
        reset_first_instructions_scan()
        proxy_fastapi._capture_first_codex_instructions(
            {
                "input": [
                    {"type": "additional_tools", "role": "developer", "tools": []},
                    message("developer", "official Lite prompt"),
                    message("developer", "permissions and skills"),
                    message("user", "hello"),
                ]
            }
        )

        settings = config_module.load_settings()
        assert settings.codex_system_prompt_default == "official Lite prompt"
        assert settings.codex_system_prompt == "official Lite prompt"


def test_codex_system_prompt_override_replaces_forwarded_instructions() -> None:
    with isolated_settings():
        config_module.save_settings(codex_system_prompt="custom forwarded prompt")
        body = {
            "instructions": "official prompt",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "original"}],
                }
            ],
        }

        forwarded = proxy_fastapi._apply_codex_system_prompt_override(body)
        assert forwarded is not body
        assert body["instructions"] == "official prompt"
        assert forwarded["instructions"] == "custom forwarded prompt"

        forwarded["input"][0]["content"][0]["text"] = "mutated"
        assert body["input"][0]["content"][0]["text"] == "original"


def test_codex_system_prompt_override_replaces_only_lite_base_developer() -> None:
    with isolated_settings():
        config_module.save_settings(codex_system_prompt="custom Lite prompt")
        body = {
            "input": [
                {"type": "additional_tools", "role": "developer", "tools": []},
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "official Lite prompt"}],
                },
                message("developer", "permissions and skills"),
                message("developer", "multi-agent policy"),
                message("user", "hello"),
            ]
        }

        forwarded = proxy_fastapi._apply_codex_system_prompt_override(body)

        assert "instructions" not in forwarded
        assert forwarded["input"][1]["content"][0]["text"] == "custom Lite prompt"
        assert forwarded["input"][2] == body["input"][2]
        assert forwarded["input"][3] == body["input"][3]
        assert body["input"][1]["content"][0]["text"] == "official Lite prompt"


def test_codex_system_prompt_override_does_not_synthesize_missing_prompt_fields() -> None:
    with isolated_settings():
        config_module.save_settings(codex_system_prompt="custom prompt")
        body = {"input": [message("user", "hello")]}

        forwarded = proxy_fastapi._apply_codex_system_prompt_override(body)

        assert forwarded == body
        assert "instructions" not in forwarded


def test_compact_prompt_uses_configured_manual_and_auto_defaults() -> None:
    with isolated_settings():
        config_module.save_settings(
            manual_local_compact_prompt="custom manual compact",
            auto_local_compact_prompt="custom auto compact",
        )
        transcript = input_items_to_transcript(
            [message("user", f"{LOCAL_COMPACT_PROMPT_PREFIX}\nSummarize this conversation.")]
        )

        result = CompactController.replace_last_compact_prompt(transcript, "manual")
        output_items = transcript_to_input_items(result.new_transcript)

        assert result.replaced is True
        assert result.replacement_prompt == "custom manual compact"
        assert output_items[-1]["content"] == "custom manual compact"
        assert replacement_local_compact_prompt("auto") == "custom auto compact"
        assert is_local_compact_prompt_text("custom manual compact") is True
        assert is_local_compact_prompt_text("custom auto compact") is True


def test_context_workbench_defaults_to_codex_55() -> None:
    with isolated_settings():
        settings = config_module.load_settings()
        assert settings.context_workbench_provider_id == config_module.DEFAULT_CONTEXT_WORKBENCH_PROVIDER_ID
        assert settings.context_workbench_model == config_module.DEFAULT_CONTEXT_WORKBENCH_MODEL


def test_context_review_trigger_settings_persist() -> None:
    with isolated_settings():
        settings = config_module.load_settings()
        assert settings.context_review_auto_enabled is True
        assert settings.context_review_interval_minutes == 10

        config_module.save_settings(
            context_review_auto_enabled=False,
            context_review_interval_minutes=27,
        )
        settings = config_module.load_settings()
        assert settings.context_review_auto_enabled is False
        assert settings.context_review_interval_minutes == 27

        config_module.save_settings(codex_system_prompt="custom context prompt")
        settings = config_module.load_settings()
        assert settings.context_workbench_provider_id == config_module.DEFAULT_CONTEXT_WORKBENCH_PROVIDER_ID
        assert settings.context_workbench_model == config_module.DEFAULT_CONTEXT_WORKBENCH_MODEL


def main() -> None:
    tests = [
        test_first_codex_instructions_updates_default_and_current_on_next_proxy_start,
        test_first_codex_instructions_preserves_user_custom_prompt,
        test_first_codex_instructions_waits_for_a_supported_prompt_location,
        test_first_codex_instructions_reads_lite_base_developer_only,
        test_codex_system_prompt_override_replaces_forwarded_instructions,
        test_codex_system_prompt_override_replaces_only_lite_base_developer,
        test_codex_system_prompt_override_does_not_synthesize_missing_prompt_fields,
        test_compact_prompt_uses_configured_manual_and_auto_defaults,
        test_context_workbench_defaults_to_codex_55,
        test_context_review_trigger_settings_persist,
    ]
    for test in tests:
        test()
    print(f"OK: {len(tests)} prompt settings tests passed")


if __name__ == "__main__":
    main()
