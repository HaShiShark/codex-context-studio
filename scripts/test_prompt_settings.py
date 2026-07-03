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

    with tempfile.TemporaryDirectory(prefix="hash-prompt-settings-") as raw_temp_dir:
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


def test_first_codex_instructions_only_scans_top_level_instructions() -> None:
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
        assert settings.codex_system_prompt_default == ""
        assert settings.codex_system_prompt == ""


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


def main() -> None:
    tests = [
        test_first_codex_instructions_updates_default_and_current_on_next_proxy_start,
        test_first_codex_instructions_preserves_user_custom_prompt,
        test_first_codex_instructions_only_scans_top_level_instructions,
        test_codex_system_prompt_override_replaces_forwarded_instructions,
        test_compact_prompt_uses_configured_manual_and_auto_defaults,
    ]
    for test in tests:
        test()
    print(f"OK: {len(tests)} prompt settings tests passed")


if __name__ == "__main__":
    main()
