from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_runtime.adapters.base import ProviderRequestContext  # noqa: E402
from agent_runtime.adapters.chat_completions_adapter import (  # noqa: E402
    ChatCompletionsAdapter,
)
from agent_runtime.core.canonical_types import (  # noqa: E402
    CanonicalItem,
    PromptBlock,
    ProviderRaw,
    assert_transcript_role,
    is_transcript_role,
)
from agent_runtime.core.transcript_contract import TranscriptRecord  # noqa: E402


PROXY_ONLY_ROLES = ("system", "developer", "context", "subagent", "compaction")


def test_agent_runtime_transcript_role_scope_is_explicit() -> None:
    assert is_transcript_role("user")
    assert is_transcript_role("assistant")
    assert assert_transcript_role("user") == "user"
    assert assert_transcript_role("assistant") == "assistant"

    for role in PROXY_ONLY_ROLES:
        assert not is_transcript_role(role)
        try:
            assert_transcript_role(role)
        except ValueError as exc:
            assert "user or assistant" in str(exc)
        else:
            raise AssertionError(f"agent_runtime accepted proxy-only role: {role}")


def test_transcript_record_does_not_claim_proxy_node_roles() -> None:
    assert TranscriptRecord(role="user", text="hello").role == "user"
    assert TranscriptRecord(role="assistant", text="hi").role == "assistant"

    for role in PROXY_ONLY_ROLES:
        try:
            TranscriptRecord(role=role, text="proxy-only role")  # type: ignore[arg-type]
        except ValueError:
            continue
        raise AssertionError(f"TranscriptRecord accepted proxy-only role: {role}")


def test_canonical_items_keep_tools_and_opaque_provider_payloads() -> None:
    tool_call = CanonicalItem(
        type="tool_call",
        name="lookup",
        call_id="call_1",
        arguments={"query": "hash context"},
    )
    tool_result = CanonicalItem(
        type="tool_result",
        call_id="call_1",
        output={"ok": True},
        provider_raw=ProviderRaw(
            provider_id="test-provider",
            event_type="context_blob",
            payload={"role": "context", "items": [{"type": "context_compaction"}]},
        ),
    )

    assert tool_call.role is None
    assert tool_call.arguments == {"query": "hash context"}
    assert tool_result.output == {"ok": True}
    assert tool_result.provider_raw is not None
    assert tool_result.provider_raw.payload == {
        "role": "context",
        "items": [{"type": "context_compaction"}],
    }


def test_chat_completions_keeps_prompt_roles_out_of_transcript_path() -> None:
    adapter = ChatCompletionsAdapter(client=None)
    context = ProviderRequestContext(
        model="test-model",
        prompt_blocks=(
            PromptBlock(kind="system", text="system prompt"),
            PromptBlock(kind="developer", text="developer prompt"),
        ),
        transcript=(
            {"role": "system", "text": "must not come from transcript"},
            {"role": "developer", "text": "must not come from transcript"},
            {"role": "user", "text": "visible user"},
            {
                "role": "assistant",
                "canonical_items": [
                    {"type": "message", "role": "developer", "content": "skip me"},
                    {"type": "message", "role": "assistant", "content": "visible assistant"},
                    {"type": "tool_call", "call_id": "call_2", "name": "lookup"},
                    {"type": "tool_result", "call_id": "call_2", "output": "done"},
                ],
            },
        ),
    )

    request = adapter.build_request(context)
    messages = request["messages"]

    assert messages[:2] == [
        {"role": "system", "content": "system prompt"},
        {"role": "developer", "content": "developer prompt"},
    ]
    assert {"role": "system", "content": "must not come from transcript"} not in messages
    assert {
        "role": "developer",
        "content": "must not come from transcript",
    } not in messages
    assert {"role": "developer", "content": "skip me"} not in messages
    assert {"role": "user", "content": "visible user"} in messages
    assistant_messages = [
        message
        for message in messages
        if message.get("role") == "assistant"
        and message.get("content") == "visible assistant"
    ]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["tool_calls"][0]["id"] == "call_2"
    assert messages[-1] == {"role": "tool", "tool_call_id": "call_2", "content": "done"}


def main() -> None:
    tests = [
        test_agent_runtime_transcript_role_scope_is_explicit,
        test_transcript_record_does_not_claim_proxy_node_roles,
        test_canonical_items_keep_tools_and_opaque_provider_payloads,
        test_chat_completions_keeps_prompt_roles_out_of_transcript_path,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} agent_runtime contract tests passed")


if __name__ == "__main__":
    main()
