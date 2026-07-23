from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_runtime.adapters.base import (  # noqa: E402
    ProviderRequestContext,
    ToolSpec,
    register_stream_cancel,
)
from agent_runtime.adapters.chat_completions_adapter import (  # noqa: E402
    ChatCompletionsAdapter,
)
from agent_runtime.adapters.claude_adapter import ClaudeAdapter  # noqa: E402
from agent_runtime.adapters.gemini_adapter import GeminiAdapter  # noqa: E402
from agent_runtime.adapters.responses_adapter import ResponsesAdapter  # noqa: E402
from agent_runtime.core.canonical_types import (  # noqa: E402
    CanonicalItem,
    PromptBlock,
    ProviderRaw,
    assert_transcript_role,
    is_transcript_role,
)
from agent_runtime.core.stream_events import ProviderDoneEvent, ToolCallReadyEvent  # noqa: E402
from simple_agent.provider_clients import SSEJSONStream  # noqa: E402
from simple_agent.config import CODEX_PROXY_PROVIDER_ID  # noqa: E402
from backend.provider_items import provider_message  # noqa: E402
from backend.web_runtime import (  # noqa: E402
    CONTEXT_CHAT_INSTRUCTIONS,
    append_context_provider_canonical_output,
    append_context_tool_round_result,
    build_context_provider_request,
    stream_context_adapter_response_with_retry,
)


PROXY_ONLY_ROLES = ("system", "developer", "context", "subagent", "compaction")


class FakeStream:
    def __init__(self, events: list[object], *, final_response: object | None = None) -> None:
        self.events = events
        self.final_response = final_response
        self.close_count = 0

    def __iter__(self):
        yield from self.events

    def close(self) -> None:
        self.close_count += 1

    def get_final_response(self) -> object | None:
        return self.final_response


class FakeStreamManager:
    def __init__(self, stream: FakeStream) -> None:
        self.stream = stream

    def __enter__(self) -> FakeStream:
        return self.stream

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.stream.close()


class ExitOnlyStream:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.exit_count = 0

    def __iter__(self):
        yield from self.events

    def __enter__(self):
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.exit_count += 1


class CancelRegistration:
    def __init__(self) -> None:
        self.history: list[object] = []

    def __call__(self, callback: object) -> None:
        self.history.append(callback)

    @property
    def callback(self):
        callbacks = [value for value in self.history if callable(value)]
        assert callbacks
        return callbacks[0]


TOOLS = (
    ToolSpec(
        name="get_nodes",
        description="Read context nodes",
        parameters={
            "type": "object",
            "properties": {"node_numbers": {"type": "array", "items": {"type": "integer"}}},
            "required": ["node_numbers"],
        },
    ),
)

WORKBENCH_HISTORY_USER = "Earlier user question."
WORKBENCH_HISTORY_ASSISTANT = "Earlier assistant answer."
WORKBENCH_SNAPSHOT = "Current Codex context snapshot. Treat this as current truth."
WORKBENCH_CURRENT_USER = "Inspect the current context."


class FakeToolRegistry:
    schemas = TOOLS


def _workbench_context_input() -> list[dict[str, object]]:
    return [
        provider_message("user", WORKBENCH_HISTORY_USER),
        provider_message("assistant", WORKBENCH_HISTORY_ASSISTANT),
        provider_message("developer", WORKBENCH_SNAPSHOT),
        provider_message("user", WORKBENCH_CURRENT_USER),
    ]


def _build_workbench_provider_request(
    provider: dict[str, object],
    context_input: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    settings = SimpleNamespace(temperature=None, top_p=None)
    provider_config = dict(provider)
    provider_type = str(provider_config.get("provider_type") or "responses")
    provider_config.setdefault(
        "api_base_url",
        {
            "claude": "https://api.anthropic.com/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta",
        }.get(provider_type, "https://api.openai.com/v1"),
    )
    if provider_config.get("id") != CODEX_PROXY_PROVIDER_ID:
        provider_config.setdefault("api_key", "test-key")
    _, request, _ = build_context_provider_request(
        settings,  # type: ignore[arg-type]
        provider_config,
        instructions=CONTEXT_CHAT_INSTRUCTIONS,
        request_model="context-model",
        request_reasoning_effort=None,
        tool_registry=FakeToolRegistry(),  # type: ignore[arg-type]
        context_input=context_input or _workbench_context_input(),  # type: ignore[arg-type]
        session_id="session-1",
    )
    return request


def _message_text(message: dict[str, object]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        )
    return ""


def _done_and_tools(events: list[object]) -> tuple[ProviderDoneEvent, list[ToolCallReadyEvent]]:
    done = next(event for event in events if isinstance(event, ProviderDoneEvent))
    tool_calls = [event for event in events if isinstance(event, ToolCallReadyEvent)]
    return done, tool_calls


def _plain_canonical_items(done: ProviderDoneEvent) -> tuple[dict[str, object], ...]:
    return tuple(asdict(item) if not isinstance(item, dict) else item for item in done.canonical_items)


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


def test_canonical_items_keep_tools_and_opaque_provider_payloads() -> None:
    tool_call = CanonicalItem(
        type="tool_call",
        name="lookup",
        call_id="call_1",
        arguments={"query": "context studio"},
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
    assert tool_call.arguments == {"query": "context studio"}
    assert tool_result.output == {"ok": True}
    assert tool_result.provider_raw is not None
    assert tool_result.provider_raw.payload == {
        "role": "context",
        "items": [{"type": "context_compaction"}],
    }


def test_workbench_prompt_and_turn_order_is_exact_for_all_providers() -> None:
    responses_request = _build_workbench_provider_request(
        {"id": "custom-responses", "provider_type": "responses"}
    )
    assert responses_request["instructions"] == CONTEXT_CHAT_INSTRUCTIONS
    assert [item["role"] for item in responses_request["input"]] == [
        "user",
        "assistant",
        "developer",
        "user",
    ]
    assert _message_text(responses_request["input"][2]) == WORKBENCH_SNAPSHOT
    assert _message_text(responses_request["input"][3]) == WORKBENCH_CURRENT_USER

    chat_request = _build_workbench_provider_request(
        {"id": "custom-chat", "provider_type": "chat_completion"}
    )
    assert chat_request["messages"][0] == {
        "role": "system",
        "content": CONTEXT_CHAT_INSTRUCTIONS,
    }
    assert [message["role"] for message in chat_request["messages"]] == [
        "system",
        "user",
        "assistant",
        "system",
        "user",
    ]
    assert _message_text(chat_request["messages"][3]) == WORKBENCH_SNAPSHOT
    assert _message_text(chat_request["messages"][4]) == WORKBENCH_CURRENT_USER

    claude_request = _build_workbench_provider_request(
        {"id": "claude", "provider_type": "claude"}
    )
    assert claude_request["system"] == CONTEXT_CHAT_INSTRUCTIONS
    assert [message["role"] for message in claude_request["messages"]] == [
        "user",
        "assistant",
        "user",
        "user",
    ]
    assert _message_text(claude_request["messages"][2]) == WORKBENCH_SNAPSHOT
    assert _message_text(claude_request["messages"][3]) == WORKBENCH_CURRENT_USER
    assert claude_request["messages"][2] is not claude_request["messages"][3]

    gemini_request = _build_workbench_provider_request(
        {"id": "gemini", "provider_type": "gemini"}
    )
    assert gemini_request["systemInstruction"] == {
        "parts": [{"text": CONTEXT_CHAT_INSTRUCTIONS}]
    }
    assert [content["role"] for content in gemini_request["contents"]] == [
        "user",
        "model",
        "user",
        "user",
    ]
    assert gemini_request["contents"][2]["parts"] == [{"text": WORKBENCH_SNAPSHOT}]
    assert gemini_request["contents"][3]["parts"] == [{"text": WORKBENCH_CURRENT_USER}]


def test_builtin_and_custom_responses_are_isomorphic_through_tool_rounds() -> None:
    first_round_input = _workbench_context_input()
    reasoning_item = {
        "id": "rs_round_1",
        "type": "reasoning",
        "encrypted_content": "opaque-round-state",
    }
    function_call_item = {
        "id": "fc_round_1",
        "type": "function_call",
        "call_id": "call_round_1",
        "name": "get_nodes",
        "arguments": '{"node_numbers":[1]}',
    }
    second_round_input = [
        *first_round_input,
        asdict(
            CanonicalItem(
                type="reasoning",
                provider_raw=ProviderRaw(
                    provider_id="openai_responses",
                    payload=reasoning_item,
                ),
            )
        ),
        asdict(
            CanonicalItem(
                type="tool_call",
                name="get_nodes",
                call_id="call_round_1",
                arguments={"node_numbers": [1]},
                provider_raw=ProviderRaw(
                    provider_id="openai_responses",
                    payload=function_call_item,
                ),
            )
        ),
        {
            "type": "tool_result",
            "call_id": "call_round_1",
            "name": "get_nodes",
            "output": '{"node":1}',
        },
    ]

    builtin = _build_workbench_provider_request(
        {
            "id": CODEX_PROXY_PROVIDER_ID,
            "provider_type": "responses",
            "api_base_url": "http://127.0.0.1:8787/v1",
        },
        second_round_input,
    )
    custom = _build_workbench_provider_request(
        {"id": "custom-responses", "provider_type": "responses"},
        second_round_input,
    )
    builtin_without_auth = {
        key: value for key, value in builtin.items() if key != "extra_headers"
    }
    assert builtin_without_auth == custom
    assert builtin["instructions"] == CONTEXT_CHAT_INSTRUCTIONS
    assert builtin["input"][:4] == first_round_input
    assert _message_text(builtin["input"][2]) == WORKBENCH_SNAPSHOT
    assert _message_text(builtin["input"][3]) == WORKBENCH_CURRENT_USER
    assert builtin["input"][4:7] == [
        reasoning_item,
        function_call_item,
        {
            "type": "function_call_output",
            "call_id": "call_round_1",
            "output": '{"node":1}',
        },
    ]


def test_responses_two_round_tool_replay_keeps_reasoning_and_usage() -> None:
    reasoning_item = {
        "id": "rs_1",
        "type": "reasoning",
        "encrypted_content": "opaque-reasoning",
        "summary": [{"type": "summary_text", "text": "Checked the map."}],
    }
    message_item = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "Inspecting node 1."}],
    }
    tool_item = {
        "id": "fc_1",
        "type": "function_call",
        "call_id": "call_1",
        "name": "get_nodes",
        "arguments": '{"node_numbers":[1]}',
        "status": "completed",
    }
    final_response = {
        "status": "completed",
        "output": [reasoning_item, message_item, tool_item],
        "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
    }
    stream = FakeStream(
        [
            SimpleNamespace(type="response.output_text.delta", delta="Inspecting node 1."),
            SimpleNamespace(type="response.output_item.done", item=reasoning_item),
            SimpleNamespace(type="response.output_item.done", item=message_item),
            SimpleNamespace(type="response.output_item.done", item=tool_item),
            SimpleNamespace(type="response.completed", response=final_response),
        ],
        final_response=final_response,
    )
    manager = FakeStreamManager(stream)
    client = SimpleNamespace(
        responses=SimpleNamespace(stream=lambda **_request: manager)
    )
    registration = CancelRegistration()
    adapter = ResponsesAdapter(client, instructions="")
    first_context = ProviderRequestContext(
        model="gpt-test",
        prompt_blocks=(PromptBlock(kind="developer", text="Maintain context."),),
        transcript=tuple(_workbench_context_input()),
        tools=TOOLS,
        register_cancel=registration,  # type: ignore[arg-type]
    )

    events = list(adapter.stream_response(adapter.build_request(first_context), first_context))
    done, tool_calls = _done_and_tools(events)

    assert len(tool_calls) == 1
    assert done.usage == {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
    assert len(done.canonical_items) == 3
    assert done.canonical_items[0].type == "reasoning"
    assert done.canonical_items[0].provider_raw is not None
    assert done.canonical_items[0].provider_raw.payload == reasoning_item
    assert registration.history[-1] is None
    registration.callback()
    assert stream.close_count >= 2

    second_context = ProviderRequestContext(
        model="gpt-test",
        prompt_blocks=first_context.prompt_blocks,
        transcript=first_context.transcript,
        current_turn=(
            *_plain_canonical_items(done),
            CanonicalItem(type="tool_result", call_id="call_1", output={"node": 1}),
        ),
        tools=TOOLS,
    )
    second_request = adapter.build_request(second_context)
    assert second_request["instructions"] == "Maintain context."
    assert second_request["input"][:4] == _workbench_context_input()
    assert second_request["input"][4:7] == [reasoning_item, message_item, tool_item]
    assert second_request["input"][7] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": {"node": 1},
    }


def test_chat_completions_two_round_tool_replay_keeps_assistant_message() -> None:
    stream = FakeStream(
        [
            {
                "choices": [
                    {
                        "delta": {"role": "assistant", "reasoning_content": "plan "},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "content": "Inspecting.",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_chat_1",
                                    "function": {
                                        "name": "get_nodes",
                                        "arguments": '{"node_',
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_content": "then inspect",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": 'numbers":[1]}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {
                "choices": [],
                "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
            },
        ]
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_request: stream)
        )
    )
    registration = CancelRegistration()
    adapter = ChatCompletionsAdapter(client)
    first_context = ProviderRequestContext(
        model="chat-test",
        prompt_blocks=(PromptBlock(kind="developer", text="Maintain context."),),
        transcript=tuple(_workbench_context_input()),
        tools=TOOLS,
        register_cancel=registration,  # type: ignore[arg-type]
    )
    first_request = adapter.build_request(first_context)
    assert first_request["stream_options"] == {"include_usage": True}

    events = list(adapter.stream_response(first_request, first_context))
    done, tool_calls = _done_and_tools(events)
    assert len(tool_calls) == 1
    assert done.usage == {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}
    canonical = _plain_canonical_items(done)[0]
    raw_message = canonical["provider_raw"]["payload"]  # type: ignore[index]
    assert raw_message["reasoning_content"] == "plan then inspect"  # type: ignore[index]
    assert raw_message["content"] == "Inspecting."  # type: ignore[index]
    assert raw_message["tool_calls"][0]["id"] == "call_chat_1"  # type: ignore[index]
    assert registration.history[-1] is None

    second_request = adapter.build_request(
        ProviderRequestContext(
            model="chat-test",
            prompt_blocks=first_context.prompt_blocks,
            transcript=first_context.transcript,
            current_turn=(
                canonical,
                CanonicalItem(
                    type="tool_result",
                    call_id="call_chat_1",
                    output={"node": 1},
                ),
            ),
            tools=TOOLS,
        )
    )
    assert [message["role"] for message in second_request["messages"]] == [
        "system",
        "user",
        "assistant",
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert _message_text(second_request["messages"][3]) == WORKBENCH_SNAPSHOT
    assert _message_text(second_request["messages"][4]) == WORKBENCH_CURRENT_USER
    assert second_request["messages"][5] == raw_message
    assert second_request["messages"][6] == {
        "role": "tool",
        "tool_call_id": "call_chat_1",
        "content": '{"node":1}',
    }


def test_claude_two_round_tool_replay_keeps_thinking_signature_and_usage() -> None:
    stream = ExitOnlyStream(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 90}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "plan "},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "carefully"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "opaque-signature"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": "Inspecting."},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_nodes",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"node_numbers":[1]}'},
            },
            {"type": "content_block_stop", "index": 2},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 25},
            },
            {"type": "message_stop"},
        ]
    )
    client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **_request: stream))
    registration = CancelRegistration()
    adapter = ClaudeAdapter(client)
    first_context = ProviderRequestContext(
        model="claude-test",
        prompt_blocks=(PromptBlock(kind="developer", text="Maintain context."),),
        transcript=tuple(_workbench_context_input()),
        tools=TOOLS,
        register_cancel=registration,  # type: ignore[arg-type]
    )
    events = list(adapter.stream_response(adapter.build_request(first_context), first_context))
    done, tool_calls = _done_and_tools(events)
    assert len(tool_calls) == 1
    assert done.output_text == "Inspecting."
    assert done.usage == {"input_tokens": 90, "output_tokens": 25}
    canonical = _plain_canonical_items(done)[0]
    raw_message = canonical["provider_raw"]["payload"]  # type: ignore[index]
    assert raw_message["content"][0] == {  # type: ignore[index]
        "type": "thinking",
        "thinking": "plan carefully",
        "signature": "opaque-signature",
    }
    assert registration.history[-1] is None
    registration.callback()
    assert stream.exit_count >= 2

    second_request = adapter.build_request(
        ProviderRequestContext(
            model="claude-test",
            prompt_blocks=first_context.prompt_blocks,
            transcript=first_context.transcript,
            current_turn=(
                canonical,
                CanonicalItem(type="tool_result", call_id="toolu_1", output={"node": 1}),
            ),
            tools=TOOLS,
        )
    )
    assert second_request["system"] == "Maintain context."
    assert [message["role"] for message in second_request["messages"]] == [
        "user",
        "assistant",
        "user",
        "user",
        "assistant",
        "user",
    ]
    assert _message_text(second_request["messages"][2]) == WORKBENCH_SNAPSHOT
    assert _message_text(second_request["messages"][3]) == WORKBENCH_CURRENT_USER
    assert second_request["messages"][4] == raw_message
    assert second_request["messages"][5] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": '{"node": 1}',
            }
        ],
    }


def test_gemini_two_round_tool_replay_keeps_thought_signatures_and_ids() -> None:
    stream = ExitOnlyStream(
        [
            {
                "candidates": [
                    {"content": {"parts": [{"text": "plan", "thought": True}]}}
                ],
                "usageMetadata": {"promptTokenCount": 70},
            },
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": "",
                                    "thought": True,
                                    "thoughtSignature": "opaque-thought",
                                },
                                {"text": "Inspecting."},
                                {
                                    "functionCall": {
                                        "id": "gemini-fc-1",
                                        "name": "get_nodes",
                                        "args": {"node_numbers": [1]},
                                    },
                                    "thoughtSignature": "opaque-function-call",
                                },
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 70,
                    "candidatesTokenCount": 18,
                    "totalTokenCount": 88,
                },
            },
        ]
    )
    client = SimpleNamespace(stream_generate_content=lambda **_request: stream)
    registration = CancelRegistration()
    adapter = GeminiAdapter(client)
    first_context = ProviderRequestContext(
        model="gemini-test",
        prompt_blocks=(PromptBlock(kind="developer", text="Maintain context."),),
        transcript=tuple(_workbench_context_input()),
        tools=TOOLS,
        register_cancel=registration,  # type: ignore[arg-type]
    )
    events = list(adapter.stream_response(adapter.build_request(first_context), first_context))
    done, tool_calls = _done_and_tools(events)
    assert len(tool_calls) == 1
    assert tool_calls[0].call_id == "gemini-fc-1"
    assert done.usage == {
        "promptTokenCount": 70,
        "candidatesTokenCount": 18,
        "totalTokenCount": 88,
    }
    canonical = _plain_canonical_items(done)[0]
    raw_content = canonical["provider_raw"]["payload"]  # type: ignore[index]
    assert raw_content["parts"][0] == {"text": "plan", "thought": True}  # type: ignore[index]
    assert raw_content["parts"][1] == {  # type: ignore[index]
        "text": "",
        "thought": True,
        "thoughtSignature": "opaque-thought",
    }
    assert raw_content["parts"][3]["thoughtSignature"] == "opaque-function-call"  # type: ignore[index]
    assert registration.history[-1] is None

    second_request = adapter.build_request(
        ProviderRequestContext(
            model="gemini-test",
            prompt_blocks=first_context.prompt_blocks,
            transcript=first_context.transcript,
            current_turn=(
                canonical,
                CanonicalItem(
                    type="tool_result",
                    call_id="gemini-fc-1",
                    output={"node": 1},
                ),
            ),
            tools=TOOLS,
        )
    )
    assert second_request["systemInstruction"] == {
        "parts": [{"text": "Maintain context."}]
    }
    assert [content["role"] for content in second_request["contents"]] == [
        "user",
        "model",
        "user",
        "user",
        "model",
        "user",
    ]
    assert second_request["contents"][2]["parts"] == [{"text": WORKBENCH_SNAPSHOT}]
    assert second_request["contents"][3]["parts"] == [{"text": WORKBENCH_CURRENT_USER}]
    assert second_request["contents"][4] == raw_content
    assert second_request["contents"][5] == {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "id": "gemini-fc-1",
                    "name": "get_nodes",
                    "response": {"node": 1},
                }
            }
        ],
    }


def test_cancel_registration_rejects_python_generator_close() -> None:
    def provider_generator():
        yield {"chunk": True}

    registration = CancelRegistration()
    context = ProviderRequestContext(
        model="test",
        register_cancel=registration,  # type: ignore[arg-type]
    )
    try:
        register_stream_cancel(context, provider_generator())
    except RuntimeError as exc:
        assert "cannot be guaranteed" in str(exc)
    else:
        raise AssertionError("plain generator.close was accepted as a transport abort")
    assert registration.history == []


def test_rest_sse_exit_cancel_closes_underlying_transport() -> None:
    class FakeTransportResponse:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    response = FakeTransportResponse()
    stream = SSEJSONStream(
        "https://example.invalid/stream",
        headers={},
        payload={},
    )
    stream._response = response  # type: ignore[attr-defined]
    registration = CancelRegistration()
    context = ProviderRequestContext(
        model="test",
        register_cancel=registration,  # type: ignore[arg-type]
    )

    assert register_stream_cancel(context, stream)
    registration.callback()
    assert response.closed
    assert stream._response is None  # type: ignore[attr-defined]


def test_web_runtime_replays_canonical_output_and_records_each_round_usage() -> None:
    usage = {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}
    canonical = CanonicalItem(
        type="reasoning",
        content={"encrypted_content": "opaque"},
        provider_raw=ProviderRaw(
            provider_id="openai_responses",
            payload={"type": "reasoning", "encrypted_content": "opaque"},
        ),
    )

    class DoneAdapter:
        def stream_response(self, _request: object, _context: object):
            yield ProviderDoneEvent(
                output_text="done",
                usage=usage,
                canonical_items=(canonical,),
            )

    recorded_usage: list[dict[str, object]] = []
    response = stream_context_adapter_response_with_retry(
        DoneAdapter(),
        {},
        ProviderRequestContext(model="test"),
        on_usage=lambda value: recorded_usage.append(dict(value)),
        max_attempts=1,
    )
    assert recorded_usage == [usage]
    assert response.usage == usage

    context_input: list[dict[str, object]] = []
    assert append_context_provider_canonical_output(context_input, response)
    append_context_tool_round_result(
        context_input,  # type: ignore[arg-type]
        has_provider_canonical_output=True,
        call_id="call_1",
        name="get_nodes",
        raw_arguments='{"node_numbers":[1]}',
        output='{"node":1}',
    )
    assert [item["type"] for item in context_input] == ["reasoning", "tool_result"]
    assert context_input[0]["provider_raw"] == {
        "provider_id": "openai_responses",
        "model": "",
        "request_id": "",
        "event_type": "",
        "payload": {"type": "reasoning", "encrypted_content": "opaque"},
        "notes": [],
    }


def test_web_runtime_keeps_generic_call_only_for_codex_proxy_bypass() -> None:
    context_input: list[dict[str, object]] = []
    append_context_tool_round_result(
        context_input,  # type: ignore[arg-type]
        has_provider_canonical_output=False,
        call_id="call_legacy",
        name="get_nodes",
        raw_arguments="{}",
        output='{"nodes":[]}',
    )
    assert [item["type"] for item in context_input] == [
        "function_call",
        "function_call_output",
    ]


def main() -> None:
    tests = [
        test_agent_runtime_transcript_role_scope_is_explicit,
        test_canonical_items_keep_tools_and_opaque_provider_payloads,
        test_workbench_prompt_and_turn_order_is_exact_for_all_providers,
        test_builtin_and_custom_responses_are_isomorphic_through_tool_rounds,
        test_responses_two_round_tool_replay_keeps_reasoning_and_usage,
        test_chat_completions_two_round_tool_replay_keeps_assistant_message,
        test_claude_two_round_tool_replay_keeps_thinking_signature_and_usage,
        test_gemini_two_round_tool_replay_keeps_thought_signatures_and_ids,
        test_cancel_registration_rejects_python_generator_close,
        test_rest_sse_exit_cancel_closes_underlying_transport,
        test_web_runtime_replays_canonical_output_and_records_each_round_usage,
        test_web_runtime_keeps_generic_call_only_for_codex_proxy_bypass,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} agent_runtime contract tests passed")


if __name__ == "__main__":
    main()
