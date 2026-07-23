from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.proxy_store import (  # noqa: E402
    estimate_gpt56_sol_cost_usd,
    normalize_usage_payload,
    ProxyStore,
)


def test_gpt56_sol_reference_price() -> None:
    assert estimate_gpt56_sol_cost_usd(200_000, 0, 0) == 1.0
    assert estimate_gpt56_sol_cost_usd(200_000, 200_000, 0) == 0.1
    assert estimate_gpt56_sol_cost_usd(0, 0, 1_000_000) == 30.0
    assert estimate_gpt56_sol_cost_usd(272_000, 0, 100_000) == 4.36
    assert estimate_gpt56_sol_cost_usd(300_000, 100_000, 100_000) == 6.6


def test_openai_responses_usage_normalization() -> None:
    usage = normalize_usage_payload(
        {
            "input_tokens": 120,
            "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 15},
            "output_tokens": 40,
            "output_tokens_details": {"reasoning_tokens": 10},
            "total_tokens": 160,
        },
        provider_type="responses",
    )
    assert usage is not None
    assert usage["input_tokens"] == 120
    assert usage["cached_input_tokens"] == 20
    assert usage["cache_write_tokens"] == 15
    assert usage["output_tokens"] == 40
    assert usage["reasoning_tokens"] == 10
    assert usage["total_tokens"] == 160
    assert usage["known_cost_usd"] == estimate_gpt56_sol_cost_usd(120, 20, 40)


def test_chat_completions_usage_normalization() -> None:
    usage = normalize_usage_payload(
        {
            "prompt_tokens": 80,
            "prompt_tokens_details": {"cached_tokens": 30},
            "completion_tokens": 25,
            "completion_tokens_details": {"reasoning_tokens": 5},
            "total_tokens": 105,
        },
        provider_type="chat_completion",
    )
    assert usage is not None
    assert usage["input_tokens"] == 80
    assert usage["cached_input_tokens"] == 30
    assert usage["output_tokens"] == 25
    assert usage["reasoning_tokens"] == 5
    assert usage["total_tokens"] == 105


def test_claude_usage_combines_all_input_classes() -> None:
    usage = normalize_usage_payload(
        {
            "input_tokens": 12,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 40,
            "output_tokens": 20,
            "output_tokens_details": {"thinking_tokens": 4},
        },
        provider_type="claude",
    )
    assert usage is not None
    assert usage["input_tokens"] == 152
    assert usage["cached_input_tokens"] == 40
    assert usage["cache_write_tokens"] == 100
    assert usage["non_cached_input_tokens"] == 112
    assert usage["output_tokens"] == 20
    assert usage["reasoning_tokens"] == 4
    assert usage["total_tokens"] == 172


def test_gemini_usage_includes_thoughts_and_tool_prompt_tokens() -> None:
    usage = normalize_usage_payload(
        {
            "promptTokenCount": 100,
            "candidatesTokenCount": 20,
            "thoughtsTokenCount": 5,
            "cachedContentTokenCount": 30,
            "toolUsePromptTokenCount": 7,
            "totalTokenCount": 125,
        },
        provider_type="gemini",
    )
    assert usage is not None
    assert usage["input_tokens"] == 100
    assert usage["cached_input_tokens"] == 30
    assert usage["output_tokens"] == 25
    assert usage["reasoning_tokens"] == 5
    assert usage["total_tokens"] == 125


def test_provider_usage_recorder_persists_provider_identity_and_raw_usage() -> None:
    raw_usage = {
        "promptTokenCount": 100,
        "candidatesTokenCount": 20,
        "thoughtsTokenCount": 5,
        "cachedContentTokenCount": 30,
        "totalTokenCount": 125,
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        store = ProxyStore(Path(temp_dir) / "proxy-state.json")
        assert store.record_usage(
            "session-1",
            "context_workbench",
            "gemini-test",
            raw_usage,
            provider_id="google-custom",
            provider_type="gemini",
        )
        usage_payload = store.session_usage("session-1")
        assert usage_payload is not None
        summary = usage_payload["summary"]
        assert summary["request_count"] == 1
        assert summary["input_tokens"] == 100
        assert summary["output_tokens"] == 25
        assert summary["known_cost_usd"] == estimate_gpt56_sol_cost_usd(100, 30, 25)

        event = store.sessions["session-1"].usage_events[0]
        assert event["provider_id"] == "google-custom"
        assert event["provider_type"] == "gemini"
        assert event["provider_raw"] == raw_usage


def main() -> None:
    tests = [
        test_gpt56_sol_reference_price,
        test_openai_responses_usage_normalization,
        test_chat_completions_usage_normalization,
        test_claude_usage_combines_all_input_classes,
        test_gemini_usage_includes_thoughts_and_tool_prompt_tokens,
        test_provider_usage_recorder_persists_provider_identity_and_raw_usage,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} provider usage and GPT-5.6 Sol pricing tests passed")


if __name__ == "__main__":
    main()
