from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote, urlparse, urlunparse
from agent_runtime.adapters import (
    ChatCompletionsAdapter,
    ClaudeAdapter,
    GeminiAdapter,
    ProviderRequestContext,
    ResponsesAdapter,
    ResponsesStreamResult,
)
from agent_runtime.core.prompt_blocks import PromptBlock
from agent_runtime.core.stream_events import (
    ProviderDoneEvent,
    ReasoningDeltaEvent,
    ReasoningDoneEvent,
    ReasoningStartEvent,
    TextDeltaEvent,
    ToolCallReadyEvent,
)
from simple_agent.agent import BridgedFunctionCall, SimpleAgent, ToolEvent, sanitize_text
from simple_agent.config import (
    CODEX_PROXY_BASE_URL,
    CODEX_PROXY_PROVIDER_ID,
    DEFAULT_CODEX_PROXY_MODELS,
    DEFAULT_CONTEXT_WORKBENCH_MODEL,
    DEFAULT_CONTEXT_WORKBENCH_PROVIDER_ID,
    Settings,
)
from simple_agent.codex_tool_registry import ToolExecution
from simple_agent.provider_clients import ClaudeRESTClient, GeminiRESTClient
try:
    import tiktoken
except ImportError:
    tiktoken = None

from backend.web_constants import PROVIDER_MODEL_TYPES, SessionState

from backend.web_state import AppState
from backend.compact_controller import AUTO_LOCAL_COMPACT_PROMPT, MANUAL_LOCAL_COMPACT_PROMPT

from backend.web_context import (
    ContextWorkbenchDraft,
    ContextWorkbenchToolRegistry,
    build_context_workspace_snapshot,
    context_review_transcript_stats,
    editable_context_node_count,
    extract_text_from_provider_message_content,
    normalize_context_chat_history,
    normalize_selected_node_indexes,
    normalize_transcript,
    sanitize_value,
    serialize_tool_event,
    write_context_edit_marker,
    write_context_request_debug,
)

def context_workbench_settings_payload(settings: Settings) -> dict[str, object]:
    return {
        "context_workbench_model": sanitize_text(settings.context_workbench_model or "").strip()
        or DEFAULT_CONTEXT_WORKBENCH_MODEL,
        "context_workbench_provider_id": sanitize_text(
            settings.context_workbench_provider_id or ""
        ).strip()
        or DEFAULT_CONTEXT_WORKBENCH_PROVIDER_ID,
        "context_review_auto_enabled": bool(settings.context_review_auto_enabled),
        "context_review_interval_minutes": int(settings.context_review_interval_minutes or 10),
        "context_token_warning_threshold": int(settings.context_token_warning_threshold or 5000),
        "context_token_critical_threshold": int(settings.context_token_critical_threshold or 10000),
        "user_locale": sanitize_text(settings.user_locale or "").strip() or "en-US",
        "theme_mode": "dark" if sanitize_text(settings.theme_mode or "").strip() == "dark" else "light",
        "ui_font": sanitize_text(settings.ui_font or "").strip() or "Noto Serif SC",
        "ui_font_size": int(settings.ui_font_size or 15),
        "codex_system_prompt": sanitize_text(settings.codex_system_prompt or "").strip(),
        "codex_system_prompt_default": sanitize_text(settings.codex_system_prompt_default or "").strip(),
        "manual_local_compact_prompt": sanitize_text(settings.manual_local_compact_prompt or "").strip()
        or MANUAL_LOCAL_COMPACT_PROMPT,
        "manual_local_compact_prompt_default": MANUAL_LOCAL_COMPACT_PROMPT,
        "auto_local_compact_prompt": sanitize_text(settings.auto_local_compact_prompt or "").strip()
        or AUTO_LOCAL_COMPACT_PROMPT,
        "auto_local_compact_prompt_default": AUTO_LOCAL_COMPACT_PROMPT,
    }

def prepare_context_chat_history_for_model(raw_history: Any, *, limit: int = 12) -> list[dict[str, str]]:
    history = normalize_context_chat_history(raw_history)
    filtered: list[dict[str, str]] = []

    for item in history:
        role = sanitize_text(item.get("role") or "").strip()
        content = sanitize_text(item.get("content") or "")
        if role == "assistant":
            if "empty response" in content.lower():
                continue
        elif role != "user":
            continue
        filtered.append(
            {
                "role": role,
                "content": content,
            }
        )

    if limit > 0:
        return filtered[-limit:]
    return filtered

def build_context_chat_runtime(
    settings: Settings,
    session: SessionState,
    *,
    message: str,
    selected_indexes: list[int] | None = None,
) -> tuple[str, str, ContextWorkbenchDraft, ContextWorkbenchToolRegistry, list[dict[str, Any]]]:
    safe_selected_indexes = normalize_selected_node_indexes(selected_indexes or [], len(session.transcript))
    draft = ContextWorkbenchDraft(
        session.transcript,
        safe_selected_indexes,
        getattr(session, "node_locks", {}),
        int(getattr(session, "node_lock_revision", 0) or 0),
    )
    snapshot = build_context_workspace_snapshot(session, selected_indexes=safe_selected_indexes)
    tool_registry = ContextWorkbenchToolRegistry(
        draft,
        session_title=sanitize_text(session.title or ""),
    )
    history = prepare_context_chat_history_for_model(session.context_workbench_history)

    context_input: list[dict[str, Any]] = []

    for item in history:
        context_input.append(
            SimpleAgent._message(
                item["role"],
                item["content"],
            )
        )

    context_input.append(
        SimpleAgent._message(
            "developer",
            "\n\n".join(
                [
                    "Current Codex context snapshot. Treat this snapshot as the source of truth for this turn. The manual-page chat history may mention stale nodes or stale content.",
                    snapshot,
                ]
            ),
        )
    )

    context_input.append(
        SimpleAgent._message(
            "user",
            sanitize_text(message),
        )
    )

    request_model = sanitize_text(settings.context_workbench_model or "").strip() or DEFAULT_CONTEXT_WORKBENCH_MODEL
    instructions = "\n".join(
        [
            "You are a context-maintenance assistant for a Codex conversation.",
            "You run in the manual Context Workbench, not in the main Codex task.",
            "Maintain, inspect, compress, replace, or delete context nodes only when the user asks.",
            "Do not continue the main Codex task. Work only on the current context snapshot.",
            "The developer snapshot is the source of truth for node numbers and content in this turn.",
            "Locked nodes are omitted from the snapshot and cannot be read, deleted, or edited by tools.",
            "If a developer node appears in the snapshot, it is unlocked and should be treated like a normal editable node.",
            "Use get_nodes before editing assistant nodes whose full items are not visible in the snapshot.",
            "Use write_nodes for node-level delete/insert/replace work. Use write_items only after get_nodes when item-level edits are required.",
            "For pure deletion, call write_nodes directly; no extra confirmation step is needed.",
            "When write_nodes returns an updated snapshot, summarize the result to the user briefly.",
            "Prefer the shortest adequate tool path and avoid repeating read-only tool calls.",
        ]
    )
    return instructions, request_model, draft, tool_registry, context_input


CONTEXT_REVIEW_PROPOSAL_INSTRUCTIONS = """
You are preparing a context-compression proposal for human review in a Codex conversation.
You are preparing a draft for human review; never continue the user's task and never edit the live transcript.

Analyze the complete transcript conservatively and decide whether selective compression is genuinely useful.
- Image payloads are intentionally omitted and represented by placeholders. Treat a placeholder only as evidence that an image exists; never infer its visual contents.
- A clean, coherent conversation with no meaningful pollution should remain unchanged. In that case, do not call tools.
- Preserve the current topic and recent working set exactly unless there is overwhelming evidence that a recent node is obsolete.
- Preserve requirements, constraints, decisions, file paths, verified facts, unresolved work, and evidence needed for future implementation.
- Good compression targets include completed older phases, duplicated tool output, corrected mistakes, abandoned approaches, superseded plans, and stale exploration.
- Topic change alone is not sufficient. Compress an older phase only when its detailed discussion is unlikely to affect the current phase.
- When confidence is low, leave the nodes untouched.
- Never collapse the whole transcript into one summary by default. Compress only the specific ranges that benefit from it.
- Locked records are visible for context but have no node_number and must remain untouched.

When compression is useful, call write_nodes exactly once with the complete edit plan.
- Delete only the selected source nodes and insert one or more replacement summary nodes at appropriate anchors.
- The replacement content must retain all useful information from the removed range.
- Summary-node length must scale with useful source material. Long source ranges often require long summaries; brevity is not the objective.
- Put the detailed retained context inside inserted node content, not in review_rationale.

review_rationale is product copy shown directly to the user before anything is applied.
- Write it in the user's language.
- Describe a proposal, not a completed operation. Use wording like "建议整理", "建议合并", or "将保留". Never say "已压缩", "已删除", "已完成", "compressed", "deleted", or "completed" as an accomplished action.
- Explain what older topic or low-value material is proposed for consolidation and why it no longer belongs to the current working set.
- Identify the current task and explain concretely why the proposal should not affect it.
- State which important requirements, decisions, constraints, and unresolved work will remain available.
- Mention material uncertainty or risk when present. Do not claim zero impact without evidence.
- Never mention node numbers, token counts, write_nodes, tools, transcript internals, or implementation mechanics.
- Keep this rationale focused and readable; the inserted replacement node, not the rationale, carries the detailed retained context.
""".strip()


CONTEXT_REVIEW_IMAGE_PLACEHOLDER = {
    "type": "image_placeholder",
    "image_present": True,
    "note": "The original transcript contains an image. Its visual content is intentionally omitted from automatic context review.",
}
CONTEXT_REVIEW_IMAGE_TYPES = {"image", "image_url", "input_image", "output_image"}
CONTEXT_REVIEW_IMAGE_DATA_URL_RE = re.compile(
    r"data:image/[^\s\"'\\]+",
    flags=re.IGNORECASE,
)


def context_review_model_value(value: Any) -> Any:
    """Build a review-only copy that records image presence without image payloads."""
    if isinstance(value, dict):
        value_type = sanitize_text(value.get("type") or "").strip().lower()
        if value_type in CONTEXT_REVIEW_IMAGE_TYPES:
            return dict(CONTEXT_REVIEW_IMAGE_PLACEHOLDER)

        mime_type = sanitize_text(
            value.get("mime_type")
            or value.get("mimeType")
            or value.get("media_type")
            or ""
        ).strip().lower()
        if mime_type.startswith("image/") and any(
            key in value for key in ("data", "inline_data", "inlineData", "source")
        ):
            return dict(CONTEXT_REVIEW_IMAGE_PLACEHOLDER)

        return {
            sanitize_text(key): context_review_model_value(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [context_review_model_value(item) for item in value]

    if isinstance(value, str):
        return CONTEXT_REVIEW_IMAGE_DATA_URL_RE.sub(
            CONTEXT_REVIEW_IMAGE_PLACEHOLDER["note"],
            value,
        )

    return sanitize_value(value)


def context_review_model_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Keep one canonical record representation instead of repeated UI derivatives."""
    provider_items = record.get("providerItems")
    if isinstance(provider_items, list) and provider_items:
        return {
            "role": sanitize_text(record.get("role") or "").strip() or "unknown",
            "providerItems": context_review_model_value(provider_items),
        }
    return context_review_model_value(dict(record))


def build_context_review_proposal_runtime(
    settings: Settings,
    session: SessionState,
) -> tuple[str, str, ContextWorkbenchDraft, ContextWorkbenchToolRegistry, list[dict[str, Any]]]:
    draft = ContextWorkbenchDraft(
        session.transcript,
        [],
        getattr(session, "node_locks", {}),
        int(getattr(session, "node_lock_revision", 0) or 0),
    )
    tool_registry = ContextWorkbenchToolRegistry(
        draft,
        session_title=sanitize_text(session.title or ""),
        review_mode=True,
    )
    complete_transcript = [
        {
            "node_number": node.source_node_number,
            "locked": not node.editable,
            "record": context_review_model_record(node.record),
        }
        for node in sorted(draft.nodes, key=lambda item: item.order)
    ]
    context_input = [
        SimpleAgent._message(
            "developer",
            "Complete transcript with stable editable node numbers:\n"
            + json.dumps(complete_transcript, ensure_ascii=False, indent=2),
        ),
        SimpleAgent._message(
            "user",
            "Perform one conservative automatic context review now. Submit one complete selective edit only when it is clearly beneficial.",
        ),
    ]
    request_model = sanitize_text(settings.context_workbench_model or "").strip() or DEFAULT_CONTEXT_WORKBENCH_MODEL
    return CONTEXT_REVIEW_PROPOSAL_INSTRUCTIONS, request_model, draft, tool_registry, context_input

def model_supports_minimal_reasoning(model_id: str) -> bool:
    cleaned_model_id = sanitize_text(model_id).strip().lower()
    return cleaned_model_id.startswith("gpt-5") or cleaned_model_id.startswith("gpt-oss")

def resolve_context_reasoning_effort(
    settings: Settings,
    *,
    model_id: str,
    requested_effort: str | None,
) -> str | None:
    cleaned_effort = sanitize_text(requested_effort or "").strip()
    if cleaned_effort == "default":
        cleaned_effort = sanitize_text(settings.default_reasoning_effort).strip()

    if cleaned_effort in {"", "default"}:
        return None

    if cleaned_effort == "none":
        if model_supports_minimal_reasoning(model_id):
            return "minimal"
        return None

    if cleaned_effort in {"minimal", "low", "medium", "high", "xhigh"}:
        return cleaned_effort

    return None

def context_workbench_fallback_answer_for_changes(
    draft: ContextWorkbenchDraft,
    tool_events: list[ToolEvent],
) -> str:
    for event in reversed(tool_events):
        if sanitize_text(event.name).strip() not in {"write_nodes", "write_items"}:
            continue
        if sanitize_text(event.status).strip() == "error":
            continue
        summary = sanitize_text(
            event.display_result
            or event.display_detail
            or event.output_preview
        ).strip()
        if summary:
            return f"Context edit applied: {summary}"

    for operation in reversed(draft.operations):
        if not isinstance(operation, dict):
            continue
        summary = sanitize_text(
            operation.get("summary")
            or operation.get("label")
            or operation.get("operation_type")
            or ""
        ).strip()
        if summary:
            return f"Context edit applied: {summary}"

    return "Context edit applied."

def extract_context_proxy_message_text(item: dict[str, Any]) -> str:
    if sanitize_text(item.get("type") or "").strip() != "message":
        return ""
    return extract_text_from_provider_message_content(item.get("content"))

def append_context_proxy_function_call(
    function_calls_by_id: dict[str, BridgedFunctionCall],
    item: dict[str, Any],
) -> None:
    if sanitize_text(item.get("type") or "").strip() != "function_call":
        return

    name = sanitize_text(item.get("name") or "").strip()
    if not name:
        return

    call_id = sanitize_text(item.get("call_id") or item.get("id") or "").strip()
    if not call_id:
        call_id = uuid.uuid4().hex

    arguments = sanitize_text(item.get("arguments") or "{}") or "{}"
    function_calls_by_id[call_id] = BridgedFunctionCall(
        name=name,
        arguments=arguments,
        call_id=call_id,
    )

def parse_context_proxy_sse_event(
    raw_event: str,
    *,
    text_parts: list[str],
    function_calls_by_id: dict[str, BridgedFunctionCall],
    saw_text_delta: list[bool],
    on_text_delta: Callable[[str], None] | None = None,
) -> None:
    if raw_event == "[DONE]":
        return

    try:
        event = json.loads(raw_event)
    except json.JSONDecodeError:
        return

    if not isinstance(event, dict):
        return

    event_type = sanitize_text(event.get("type") or "").strip()
    if event_type in {"response.output_text.delta", "response.refusal.delta"}:
        delta = sanitize_text(event.get("delta") or "")
        if delta:
            saw_text_delta[0] = True
            text_parts.append(delta)
            if on_text_delta is not None:
                on_text_delta(delta)
        return

    if event_type == "response.output_text.done" and not saw_text_delta[0]:
        text = sanitize_text(event.get("text") or "")
        if text:
            text_parts.append(text)
            if on_text_delta is not None:
                on_text_delta(text)
        return

    if event_type in {"response.output_item.done", "response.output_item.added"}:
        item = event.get("item")
        if isinstance(item, dict):
            append_context_proxy_function_call(function_calls_by_id, item)
        return

    if event_type == "response.completed":
        response = event.get("response")
        output = response.get("output") if isinstance(response, dict) else None
        if not isinstance(output, list):
            return
        fallback_text_parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            append_context_proxy_function_call(function_calls_by_id, item)
            if not saw_text_delta[0]:
                item_text = extract_context_proxy_message_text(item)
                if item_text:
                    fallback_text_parts.append(item_text)
        if fallback_text_parts and not text_parts:
            text = sanitize_text("".join(fallback_text_parts))
            if text:
                text_parts.append(text)
                if on_text_delta is not None:
                    on_text_delta(text)
        return

    if event_type == "response.failed":
        response = event.get("response")
        error = response.get("error") if isinstance(response, dict) else None
        if isinstance(error, dict):
            message = sanitize_text(error.get("message") or error.get("code") or "")
            if message:
                raise RuntimeError(f"response failed: {message}")
        raise RuntimeError("response failed")

    if event_type == "error":
        message = sanitize_text(event.get("message") or event.get("error") or "")
        raise RuntimeError(f"response stream error: {message or 'unknown error'}")

def context_workbench_prompt_cache_key(session_id: str) -> str:
    safe_session_id = "".join(
        ch if ch.isalnum() or ch in "-_." else "-"
        for ch in sanitize_text(session_id).strip()
    ).strip("-_.")
    if not safe_session_id:
        return "codex-context-studio-workbench"
    return f"codex-context-studio:{safe_session_id[:48]}"


def context_workbench_provider(settings: Settings) -> dict[str, Any]:
    provider = sanitize_value(settings.context_workbench_provider())
    if not isinstance(provider, dict):
        provider = {}

    provider_id = sanitize_text(provider.get("id") or "").strip()
    if provider_id:
        return provider

    return {
        "id": DEFAULT_CONTEXT_WORKBENCH_PROVIDER_ID,
        "name": "Codex",
        "provider_type": "responses",
        "api_base_url": CODEX_PROXY_BASE_URL,
        "default_model": DEFAULT_CONTEXT_WORKBENCH_MODEL,
    }


def context_provider_type(provider: Mapping[str, Any]) -> str:
    return normalize_provider_type(
        provider.get("provider_type"),
        sanitize_text(provider.get("id") or "").strip(),
    )


def context_provider_api_base_url(provider: Mapping[str, Any]) -> str:
    provider_type = context_provider_type(provider)
    provider_id = sanitize_text(provider.get("id") or "").strip()
    raw_base_url = sanitize_text(provider.get("api_base_url") or "").strip()
    if provider_id == CODEX_PROXY_PROVIDER_ID:
        raw_base_url = raw_base_url or CODEX_PROXY_BASE_URL
    return normalize_provider_api_base_url(raw_base_url, provider_type)


def context_provider_api_key(provider: Mapping[str, Any], settings: Settings) -> str:
    provider_id = sanitize_text(provider.get("id") or "").strip()
    api_key = sanitize_text(provider.get("api_key") or "").strip()
    if api_key:
        return api_key
    if provider_id == CODEX_PROXY_PROVIDER_ID:
        return "not-needed"
    return ""


def build_context_provider_client(provider: Mapping[str, Any], settings: Settings) -> Any:
    provider_type = context_provider_type(provider)
    base_url = context_provider_api_base_url(provider)
    api_key = context_provider_api_key(provider, settings)

    if provider_type == "claude":
        return ClaudeRESTClient(
            base_url or "https://api.anthropic.com/v1",
            api_key,
        )

    if provider_type == "gemini":
        return GeminiRESTClient(
            base_url or "https://generativelanguage.googleapis.com/v1beta",
            api_key,
        )

    from openai import OpenAI

    client_kwargs: dict[str, Any] = {
        "api_key": api_key or "not-needed",
    }
    if base_url:
        client_kwargs["base_url"] = base_url
    return OpenAI(**client_kwargs)


def build_context_provider_adapter(provider: Mapping[str, Any], settings: Settings) -> Any:
    provider_type = context_provider_type(provider)
    client = build_context_provider_client(provider, settings)

    if provider_type == "chat_completion":
        return ChatCompletionsAdapter(client)
    if provider_type == "claude":
        return ClaudeAdapter(client)
    if provider_type == "gemini":
        return GeminiAdapter(client)

    return ResponsesAdapter(
        client,
        instructions="",
        tools=(),
        sanitize_text=sanitize_text,
        sanitize_value=sanitize_value,
    )


def prompt_block_text(prompt_blocks: Iterable[PromptBlock]) -> str:
    labels = {
        "system": "System",
        "developer": "Developer",
        "memory": "Memory",
        "summary": "Summary",
    }
    sections: list[str] = []
    for block in prompt_blocks:
        text = sanitize_text(block.text or "").strip()
        if not text:
            continue
        label = labels.get(block.kind, block.kind.title() or "Prompt")
        sections.append(f"[{label}]\n{text}")
    return "\n\n".join(sections)


def context_input_to_provider_parts(
    instructions: str,
    context_input: list[dict[str, Any]],
) -> tuple[list[PromptBlock], list[dict[str, Any]]]:
    prompt_blocks = [
        PromptBlock(
            kind="developer",
            text=sanitize_text(instructions),
            source="context_workbench",
        )
    ]
    transcript_items: list[dict[str, Any]] = []

    for item in context_input:
        if not isinstance(item, dict):
            continue
        item_type = sanitize_text(item.get("type") or "").strip()
        role = sanitize_text(item.get("role") or "").strip()
        if item_type == "message" and role in {"system", "developer"}:
            text = extract_text_from_provider_message_content(item.get("content"))
            if text:
                prompt_blocks.append(
                    PromptBlock(
                        kind="system" if role == "system" else "developer",
                        text=text,
                        source="context_workbench_input",
                    )
                )
            continue
        transcript_items.append(sanitize_value(item))

    return prompt_blocks, transcript_items


def context_provider_config(
    settings: Settings,
    provider: Mapping[str, Any],
    *,
    prompt_blocks: list[PromptBlock],
    session_id: str,
) -> dict[str, Any]:
    provider_type = context_provider_type(provider)
    provider_id = sanitize_text(provider.get("id") or "").strip()
    config: dict[str, Any] = {}

    if settings.temperature is not None:
        config["temperature"] = settings.temperature
    if settings.top_p is not None:
        config["topP" if provider_type == "gemini" else "top_p"] = settings.top_p

    if provider_type == "responses":
        config["instructions"] = prompt_block_text(prompt_blocks)
        config["store"] = False
        config["prompt_cache_key"] = context_workbench_prompt_cache_key(session_id)
        if provider_id == CODEX_PROXY_PROVIDER_ID:
            config["extra_headers"] = {
                "x-codex-context-studio-internal": "context-workbench",
                "x-codex-context-studio-session-id": session_id,
            }

    return config


def build_context_provider_request(
    settings: Settings,
    provider: Mapping[str, Any],
    *,
    instructions: str,
    request_model: str,
    request_reasoning_effort: str | None,
    tool_registry: ContextWorkbenchToolRegistry,
    context_input: list[dict[str, Any]],
    session_id: str,
) -> tuple[Any, dict[str, Any], ProviderRequestContext]:
    prompt_blocks, transcript_items = context_input_to_provider_parts(
        instructions,
        context_input,
    )
    context = ProviderRequestContext(
        prompt_blocks=tuple(prompt_blocks),
        transcript=tuple(transcript_items),
        current_turn=(),
        tools=tuple(tool_registry.schemas),
        provider_config=context_provider_config(
            settings,
            provider,
            prompt_blocks=prompt_blocks,
            session_id=session_id,
        ),
        model=request_model,
        reasoning_effort=request_reasoning_effort,
        metadata={
            "provider_id": sanitize_text(provider.get("id") or "").strip(),
        },
    )
    adapter = build_context_provider_adapter(provider, settings)
    return adapter, adapter.build_request(context), context


def stream_context_adapter_response(
    adapter: Any,
    request: dict[str, Any],
    context: ProviderRequestContext,
    *,
    on_text_delta: Callable[[str], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> ResponsesStreamResult:
    output_chunks: list[str] = []
    function_calls: list[BridgedFunctionCall] = []
    canonical_items: list[Any] = []
    final_output_text = ""
    finish_reason: str | None = None

    for event in adapter.stream_response(request, context):
        if check_cancelled is not None:
            check_cancelled()

        if isinstance(event, TextDeltaEvent):
            safe_delta = sanitize_text(event.delta)
            if safe_delta:
                output_chunks.append(safe_delta)
                if on_text_delta is not None:
                    on_text_delta(safe_delta)
            continue

        if isinstance(event, (ReasoningStartEvent, ReasoningDeltaEvent, ReasoningDoneEvent)):
            continue

        if isinstance(event, ToolCallReadyEvent):
            raw_arguments = sanitize_text(
                event.raw_arguments
                or json.dumps(event.arguments, ensure_ascii=False)
            ) or "{}"
            function_calls.append(
                BridgedFunctionCall(
                    name=sanitize_text(event.name),
                    arguments=raw_arguments,
                    call_id=sanitize_text(event.call_id or ""),
                )
            )
            continue

        if isinstance(event, ProviderDoneEvent):
            final_output_text = sanitize_text(event.output_text)
            finish_reason = event.finish_reason
            canonical_items = list(sanitize_value(event.canonical_items or ()))

    output_text = "".join(output_chunks) or final_output_text
    if not output_chunks and final_output_text and on_text_delta is not None:
        on_text_delta(final_output_text)

    return ResponsesStreamResult(
        output_text=output_text,
        function_calls=function_calls,
        finish_reason=finish_reason,
        canonical_items=canonical_items,
    )


def stream_context_adapter_response_with_retry(
    adapter: Any,
    request: dict[str, Any],
    context: ProviderRequestContext,
    *,
    on_text_delta: Callable[[str], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
    max_attempts: int = 3,
) -> ResponsesStreamResult:
    last_response: ResponsesStreamResult | None = None
    last_error: Exception | None = None

    for attempt in range(max(1, max_attempts)):
        if check_cancelled is not None:
            check_cancelled()

        try:
            response = stream_context_adapter_response(
                adapter,
                request,
                context,
                on_text_delta=on_text_delta,
                check_cancelled=check_cancelled,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= max_attempts - 1:
                raise
        else:
            last_response = response
            output_text = sanitize_text(getattr(response, "output_text", "") or "")
            function_calls = getattr(response, "function_calls", None) or []
            if output_text or function_calls:
                return response
            last_error = RuntimeError("Context provider stream returned no events")

        if attempt < max_attempts - 1:
            time.sleep(0.5 * (attempt + 1))

    if last_response is not None:
        return last_response
    if last_error is not None:
        raise last_error
    raise RuntimeError("Context provider stream returned no response")


def stream_context_codex_proxy_response(
    request: dict[str, Any],
    *,
    on_text_delta: Callable[[str], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> object:
    request_body = {
        key: value
        for key, value in request.items()
        if key != "extra_headers"
    }
    request_body["stream"] = True
    extra_headers = request.get("extra_headers")
    headers = {
        "Authorization": "Bearer not-needed",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }
    if isinstance(extra_headers, dict):
        for key, value in extra_headers.items():
            safe_key = sanitize_text(key).strip()
            safe_value = sanitize_text(value).strip()
            if safe_key and safe_value:
                headers[safe_key] = safe_value

    proxy_url = f"{CODEX_PROXY_BASE_URL.rstrip('/')}/responses"
    payload = json.dumps(sanitize_value(request_body), ensure_ascii=False).encode("utf-8")
    http_request = urllib_request.Request(proxy_url, data=payload, headers=headers, method="POST")

    text_parts: list[str] = []
    function_calls_by_id: dict[str, BridgedFunctionCall] = {}
    saw_text_delta = [False]
    buffer = ""

    try:
        response = urllib_request.urlopen(http_request, timeout=600)
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(detail or f"Context proxy request failed with HTTP {exc.code}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"Context proxy request failed: {exc.reason}") from exc

    with response:
        while True:
            if check_cancelled is not None:
                check_cancelled()

            read_available = getattr(response, "read1", response.read)
            chunk = read_available(4096)
            if not chunk:
                break

            buffer += chunk.decode("utf-8", errors="ignore")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                data_lines = [
                    line[5:].strip()
                    for line in block.splitlines()
                    if line.startswith("data:")
                ]
                if not data_lines:
                    continue
                parse_context_proxy_sse_event(
                    "\n".join(data_lines),
                    text_parts=text_parts,
                    function_calls_by_id=function_calls_by_id,
                    saw_text_delta=saw_text_delta,
                    on_text_delta=on_text_delta,
                )

        if buffer.strip():
            data_lines = [
                line[5:].strip()
                for line in buffer.splitlines()
                if line.startswith("data:")
            ]
            if data_lines:
                parse_context_proxy_sse_event(
                    "\n".join(data_lines),
                    text_parts=text_parts,
                    function_calls_by_id=function_calls_by_id,
                    saw_text_delta=saw_text_delta,
                    on_text_delta=on_text_delta,
                )

    return type(
        "ContextProxyStreamResult",
        (),
        {
            "output_text": "".join(text_parts),
            "function_calls": list(function_calls_by_id.values()),
            "finish_reason": None,
        },
    )()

def stream_context_codex_proxy_response_with_retry(
    request: dict[str, Any],
    *,
    on_text_delta: Callable[[str], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
    max_attempts: int = 3,
) -> object:
    last_response: object | None = None
    last_error: Exception | None = None

    for attempt in range(max(1, max_attempts)):
        if check_cancelled is not None:
            check_cancelled()

        try:
            response = stream_context_codex_proxy_response(
                request,
                on_text_delta=on_text_delta,
                check_cancelled=check_cancelled,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= max_attempts - 1:
                raise
        else:
            last_response = response
            output_text = sanitize_text(getattr(response, "output_text", "") or "")
            function_calls = getattr(response, "function_calls", None) or []
            if output_text or function_calls:
                return response
            last_error = RuntimeError("Context proxy stream returned no events")

        if attempt < max_attempts - 1:
            time.sleep(0.5 * (attempt + 1))

    if last_response is not None:
        return last_response
    if last_error is not None:
        raise last_error
    raise RuntimeError("Context proxy stream returned no response")

def run_context_chat_turn(
    settings: Settings,
    session: SessionState,
    *,
    message: str,
    selected_indexes: list[int] | None = None,
    reasoning_effort: str | None = None,
    on_text_delta: Callable[[str], None] | None = None,
    on_round_reset: Callable[[], None] | None = None,
    on_tool_event: Callable[[ToolEvent], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
    context_review: bool = False,
) -> tuple[str, str, ContextWorkbenchDraft, list[ToolEvent]]:
    if context_review:
        instructions, request_model, draft, tool_registry, context_input = build_context_review_proposal_runtime(
            settings,
            session,
        )
    else:
        instructions, request_model, draft, tool_registry, context_input = build_context_chat_runtime(
            settings,
            session,
            message=message,
            selected_indexes=selected_indexes,
        )
    request_reasoning_effort = resolve_context_reasoning_effort(
        settings,
        model_id=request_model,
        requested_effort=reasoning_effort,
    )
    provider = context_workbench_provider(settings)
    provider_id = sanitize_text(provider.get("id") or "").strip() or CODEX_PROXY_PROVIDER_ID
    provider_type = context_provider_type(provider)
    tool_events: list[ToolEvent] = []
    readonly_tool_result_cache: dict[str, str] = {}
    readonly_tool_cache_names = {"get_nodes"}

    round_count = 0
    while True:
        round_count += 1

        if check_cancelled is not None:
            check_cancelled()

        def build_codex_proxy_request() -> dict[str, Any]:
            request = {
                "model": request_model,
                "instructions": instructions,
                "input": sanitize_value(context_input),
                "tools": tool_registry.schemas,
                "store": False,
                "prompt_cache_key": context_workbench_prompt_cache_key(session.session_id),
                "extra_headers": {
                    "x-codex-context-studio-internal": "context-workbench",
                    "x-codex-context-studio-session-id": session.session_id,
                },
            }
            if request_reasoning_effort:
                request["reasoning"] = {"effort": request_reasoning_effort}
            write_context_request_debug(
                session_id=session.session_id,
                request_model=request_model,
                round_count=round_count,
                request=request,
                note="context_workbench_request",
            )
            return request

        try:
            if provider_id == CODEX_PROXY_PROVIDER_ID:
                request = build_codex_proxy_request()
                response = stream_context_codex_proxy_response_with_retry(
                    request,
                    on_text_delta=on_text_delta,
                    check_cancelled=check_cancelled,
                )
            else:
                adapter, request, provider_context = build_context_provider_request(
                    settings,
                    provider,
                    instructions=instructions,
                    request_model=request_model,
                    request_reasoning_effort=request_reasoning_effort,
                    tool_registry=tool_registry,
                    context_input=context_input,
                    session_id=session.session_id,
                )
                write_context_request_debug(
                    session_id=session.session_id,
                    request_model=request_model,
                    round_count=round_count,
                    request=request,
                    note=f"context_workbench_request:{provider_id}:{provider_type}",
                )
                response = stream_context_adapter_response_with_retry(
                    adapter,
                    request,
                    provider_context,
                    on_text_delta=on_text_delta,
                    check_cancelled=check_cancelled,
                )
        except Exception:
            if check_cancelled is not None:
                check_cancelled()
            if draft.has_changes:
                return (
                    context_workbench_fallback_answer_for_changes(draft, tool_events),
                    request_model,
                    draft,
                    tool_events,
                )
            raise
        if check_cancelled is not None:
            check_cancelled()

        if not response.function_calls:
            final_answer = sanitize_text(response.output_text).strip()
            if not final_answer:
                if draft.has_changes:
                    return (
                        context_workbench_fallback_answer_for_changes(draft, tool_events),
                        request_model,
                        draft,
                        tool_events,
                    )
                error_msg = "Model returned empty response"
                if response.finish_reason:
                    error_msg += f" (Finish reason: {response.finish_reason})"
                raise RuntimeError(error_msg)
            if check_cancelled is not None:
                check_cancelled()
            return final_answer, request_model, draft, tool_events

        if response.output_text and on_round_reset is not None:
            if check_cancelled is not None:
                check_cancelled()
            on_round_reset()

        for call in response.function_calls:
            if check_cancelled is not None:
                check_cancelled()
            safe_call_name = sanitize_text(getattr(call, "name", "") or "")
            safe_call_id = sanitize_text(getattr(call, "call_id", "") or "")
            safe_call_arguments = sanitize_text(getattr(call, "arguments", "") or "{}") or "{}"

            try:
                raw_arguments = json.loads(safe_call_arguments)
                arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
                cache_key = ""
                if safe_call_name in readonly_tool_cache_names:
                    cache_key = json.dumps(
                        {
                            "name": safe_call_name,
                            "arguments": sanitize_value(arguments),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )

                if cache_key and cache_key in readonly_tool_result_cache:
                    result = json.dumps(
                        {
                            "payload_kind": "cached_tool_result",
                            "tool_name": safe_call_name,
                            "message": "This exact read-only context tool call already ran in this workbench turn. Use the previous function_call_output result instead of requesting it again.",
                        },
                        ensure_ascii=False,
                    )
                    execution = ToolExecution(
                        output_text=result,
                        display_title=safe_call_name,
                        display_detail="cached duplicate tool call",
                        display_result="Duplicate read-only tool call skipped; use the previous result.",
                        status="completed",
                    )
                else:
                    execution = tool_registry.execute(safe_call_name, arguments)
                    if cache_key:
                        readonly_tool_result_cache[cache_key] = sanitize_text(execution.output_text)
                    else:
                        readonly_tool_result_cache.clear()
                result = sanitize_text(execution.output_text)
            except json.JSONDecodeError as exc:
                arguments = {}
                result = json.dumps(
                    {"error": f"invalid tool arguments: {exc.msg}"},
                    ensure_ascii=False,
                )
                execution = ToolExecution(
                    output_text=result,
                    display_title=safe_call_name or "context_workbench_tool",
                    display_detail="tool arguments invalid",
                    display_result=f"Tool arguments are not valid JSON: {exc.msg}",
                    status="error",
                )
            else:
                result = sanitize_text(execution.output_text)

            if check_cancelled is not None:
                check_cancelled()
            safe_arguments = sanitize_value(arguments)
            tool_event = ToolEvent(
                name=safe_call_name,
                arguments=safe_arguments,
                output_preview=SimpleAgent._preview(result),
                raw_output=result,
                display_title=execution.display_title,
                display_detail=execution.display_detail,
                display_result=execution.display_result,
                status=execution.status,
            )
            tool_events.append(tool_event)
            if on_tool_event is not None:
                on_tool_event(tool_event)

            if context_review and safe_call_name == "write_nodes" and draft.has_changes:
                if check_cancelled is not None:
                    check_cancelled()
                return (
                    tool_registry.review_rationale or context_workbench_fallback_answer_for_changes(draft, tool_events),
                    request_model,
                    draft,
                    tool_events,
                )

            context_input.append(
                {
                    "type": "function_call",
                    "call_id": safe_call_id,
                    "name": safe_call_name,
                    "arguments": safe_call_arguments,
                }
            )
            context_input.append(
                {
                    "type": "function_call_output",
                    "call_id": safe_call_id,
                    "output": result,
                }
            )

    # Note: Loop continues until returns or error inside

def build_context_chat_response_payload(
    app_state: AppState,
    session: SessionState,
    *,
    user_message: str,
    answer: str,
    used_model: str,
    draft: ContextWorkbenchDraft,
    tool_events: list[ToolEvent] | None = None,
) -> dict[str, object]:
    proxy_transcript_sync: dict[str, object] | None = None
    if draft.has_changes:
        if int(getattr(session, "node_lock_revision", 0) or 0) != int(getattr(draft, "node_lock_revision", 0) or 0):
            raise ValueError("Node lock state changed while the context model was running. Please retry.")
        conversation = app_state.apply_context_workbench_mutation(
            session,
            transcript=draft.committed_transcript(),
        )
        proxy_transcript_sync = safe_sync_proxy_session_transcript_if_known(session, conversation)
        if sanitize_text(proxy_transcript_sync.get("status") or "") == "error":
            answer = append_proxy_transcript_sync_warning(
                answer,
                sanitize_text(proxy_transcript_sync.get("error") or ""),
            )
    else:
        conversation = sanitize_value(session.transcript)

    serialized_tool_events = [serialize_tool_event(event) for event in tool_events] if tool_events is not None else None
    history = app_state.append_context_workbench_turn(
        session,
        user_message=user_message,
        answer=answer,
        tool_events=serialized_tool_events,
    )
    payload: dict[str, object] = {
        "answer": answer,
        "used_model": used_model,
        "history": history,
        "conversation": conversation,
    }
    if serialized_tool_events is not None:
        payload["tool_events"] = serialized_tool_events
    if proxy_transcript_sync is not None:
        payload["proxy_transcript_sync"] = proxy_transcript_sync
    return payload

def build_context_review_payload(
    *,
    session: SessionState,
    base_transcript_version: int,
    base_context_review_cancel_revision: int,
    proposed_transcript: list[dict[str, object]],
    summary: str,
    model: str,
    source: str,
) -> dict[str, object]:
    current_transcript = normalize_transcript(session.transcript)
    proposal_transcript = normalize_transcript(proposed_transcript)
    return {
        "review_schema_version": 2,
        "id": uuid.uuid4().hex,
        "session_id": sanitize_text(session.session_id).strip(),
        "status": "pending",
        "source": sanitize_text(source).strip() or "manual",
        "base_transcript_version": max(0, int(base_transcript_version or 0)),
        "base_context_review_cancel_revision": max(0, int(base_context_review_cancel_revision or 0)),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "summary": sanitize_text(summary).strip() or "Context compression proposal generated.",
        "model": sanitize_text(model).strip(),
        "before": context_review_transcript_stats(current_transcript),
        "after": context_review_transcript_stats(proposal_transcript),
        "proposed_transcript": proposal_transcript,
    }


def run_context_review_generation(
    settings: Settings,
    session: SessionState,
    *,
    base_transcript_version: int,
    base_context_review_cancel_revision: int,
    source: str = "manual",
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, object] | None:
    current_transcript = normalize_transcript(session.transcript)
    if not current_transcript:
        return None

    answer, used_model, draft, _tool_events = run_context_chat_turn(
        settings,
        session,
        message="",
        selected_indexes=[],
        reasoning_effort="default",
        check_cancelled=check_cancelled,
        context_review=True,
    )
    if not draft.has_changes:
        return None

    proposed_transcript = draft.committed_transcript()
    if proposed_transcript == current_transcript:
        return None

    return build_context_review_payload(
        session=session,
        base_transcript_version=base_transcript_version,
        base_context_review_cancel_revision=base_context_review_cancel_revision,
        proposed_transcript=proposed_transcript,
        summary=answer,
        model=used_model,
        source=source,
    )


def refresh_session_from_proxy_for_review(
    app_state: AppState,
    session_id: str,
) -> tuple[SessionState, dict[str, Any]]:
    safe_session_id = sanitize_text(session_id or "").strip()
    if not safe_session_id:
        raise ValueError("session_id is required")
    proxy_payload = get_codex_proxy_control_json(
        f"/api/proxy/sessions/{quote(safe_session_id, safe='')}",
        timeout_seconds=3,
    )
    if not proxy_payload:
        raise ValueError("session not found")

    session = app_state.upsert_proxy_session(
        session_id=safe_session_id,
        title=sanitize_text(proxy_payload.get("title") or "").strip() or "Codex Context",
        transcript=normalize_transcript(proxy_payload.get("transcript")),
        is_main_turn_running=bool(proxy_payload.get("is_main_turn_running")),
        main_turn_id=sanitize_text(proxy_payload.get("main_turn_id") or "").strip(),
        main_turn_started_at=sanitize_text(proxy_payload.get("main_turn_started_at") or "").strip(),
        main_turn_updated_at=sanitize_text(proxy_payload.get("main_turn_updated_at") or "").strip(),
        node_locks=proxy_payload.get("node_locks") if isinstance(proxy_payload.get("node_locks"), dict) else {},
        node_lock_revision=int(proxy_payload.get("node_lock_revision") or 0),
    )
    return session, proxy_payload


def store_proxy_context_review(session_id: str, review: dict[str, object]) -> dict[str, Any]:
    return post_codex_proxy_control_json(
        f"/api/proxy/sessions/{quote(sanitize_text(session_id).strip(), safe='')}/context-review",
        {"review": review},
        timeout_seconds=8,
    )


def apply_proxy_context_review(session_id: str, review_id: str) -> dict[str, Any]:
    return post_codex_proxy_control_json(
        f"/api/proxy/sessions/{quote(sanitize_text(session_id).strip(), safe='')}/context-review/apply",
        {"review_id": sanitize_text(review_id).strip()},
        timeout_seconds=8,
    )


def discard_proxy_context_review(session_id: str, review_id: str = "") -> dict[str, Any]:
    return post_codex_proxy_control_json(
        f"/api/proxy/sessions/{quote(sanitize_text(session_id).strip(), safe='')}/context-review/discard",
        {"review_id": sanitize_text(review_id).strip()},
        timeout_seconds=8,
    )


def generate_and_store_context_review(
    app_state: AppState,
    session_id: str,
    *,
    source: str = "manual",
    expected_cancel_revision: int | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    safe_source = sanitize_text(source or "manual").strip() or "manual"
    session, proxy_payload = refresh_session_from_proxy_for_review(app_state, session_id)
    pending_review = proxy_payload.get("pending_context_review")
    if isinstance(pending_review, dict):
        return {
            "pending_review": pending_review,
            "status": "pending",
            "session": proxy_payload,
        }
    if bool(proxy_payload.get("is_main_turn_running")):
        return {
            "error": "main Codex turn is still running",
            "status": "blocked",
            "reason": "main_turn_running",
        }
    if bool(proxy_payload.get("is_context_running")):
        return {
            "error": "context model is still running",
            "status": "blocked",
            "reason": "context_model_running",
        }
    current_cancel_revision = int(proxy_payload.get("context_review_cancel_revision") or 0)
    if expected_cancel_revision is not None and current_cancel_revision != int(expected_cancel_revision):
        raise RuntimeError("context_review_cancelled")

    request_id = app_state.acquire_session_request(session, "context")
    safe_set_proxy_context_run_state(session.session_id, request_id, True)

    def raise_if_cancelled() -> None:
        if check_cancelled is not None:
            check_cancelled()
        if app_state.is_session_request_cancelled(session, request_id):
            raise RuntimeError("context_review_cancelled")

    try:
        raise_if_cancelled()
        review = run_context_review_generation(
            app_state.settings,
            session,
            base_transcript_version=int(proxy_payload.get("transcript_version") or 0),
            base_context_review_cancel_revision=current_cancel_revision,
            source=safe_source,
            check_cancelled=raise_if_cancelled,
        )
        if review is None:
            return {
                "pending_review": None,
                "status": "skipped",
                "reason": "no_compression_proposal",
            }
        raise_if_cancelled()
        proxy_session = store_proxy_context_review(session.session_id, review)
        return {
            "pending_review": proxy_session.get("pending_context_review")
            if isinstance(proxy_session.get("pending_context_review"), dict)
            else None,
            "status": "pending",
            "session": proxy_session,
        }
    finally:
        safe_set_proxy_context_run_state(session.session_id, request_id, False)
        app_state.release_session_request(session, "context", request_id)


def normalize_provider_type(raw_type: Any, provider_id: str = "") -> str:
    cleaned_type = sanitize_text(raw_type or "").strip()
    if cleaned_type in PROVIDER_MODEL_TYPES:
        return cleaned_type
    if provider_id == "gemini":
        return "gemini"
    if provider_id in {"anthropic", "claude"}:
        return "claude"
    return "responses"

def normalize_provider_api_base_url(raw_url: str, provider_type: str = "responses") -> str:
    cleaned_url = sanitize_text(raw_url).strip().rstrip("/")
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
    suffixes = suffixes_by_type.get(provider_type, suffixes_by_type["responses"])
    for suffix in suffixes:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break

    return urlunparse((parsed.scheme, parsed.netloc, path or "", "", "", "")).rstrip("/")

def build_provider_models_url(api_base_url: str, provider_type: str = "responses") -> str:
    normalized_base_url = normalize_provider_api_base_url(api_base_url, provider_type)
    if not normalized_base_url:
        return ""
    return f"{normalized_base_url}/models"

def build_provider_models_url_candidates(api_base_url: str, provider_type: str = "responses") -> list[str]:
    primary_url = build_provider_models_url(api_base_url, provider_type)
    if not primary_url:
        return []

    urls = [primary_url]
    parsed = urlparse(primary_url)
    if parsed.scheme and parsed.netloc and parsed.path not in {"", "/models"}:
        root_models_url = urlunparse((parsed.scheme, parsed.netloc, "/models", "", "", ""))
        if root_models_url not in urls:
            urls.append(root_models_url)
    return urls

def normalize_fetched_provider_models(raw_payload: Any, provider_type: str = "responses") -> list[dict[str, str]]:
    if not isinstance(raw_payload, dict):
        return []

    raw_models = raw_payload.get("models") if provider_type == "gemini" else raw_payload.get("data")
    if not isinstance(raw_models, list):
        return []

    normalized_models: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for item in raw_models:
        if not isinstance(item, dict):
            continue

        if provider_type == "gemini":
            raw_model_id = sanitize_text(item.get("name") or item.get("id") or "").strip()
            model_id = raw_model_id.removeprefix("models/")
            label = sanitize_text(item.get("displayName") or model_id).strip() or model_id
            group = "Gemini"
        elif provider_type == "claude":
            model_id = sanitize_text(item.get("id") or "").strip()
            label = sanitize_text(item.get("display_name") or item.get("displayName") or model_id).strip() or model_id
            group = "Claude"
        else:
            model_id = sanitize_text(item.get("id") or "").strip()
            label = model_id
            group = sanitize_text(item.get("owned_by") or item.get("object") or "Models").strip() or "Models"

        if not model_id or model_id in seen_ids:
            continue

        seen_ids.add(model_id)
        normalized_models.append(
            {
                "id": model_id,
                "label": label,
                "group": group,
                "provider": group,
            }
        )

    normalized_models.sort(key=lambda item: item["id"].lower())
    return normalized_models

def fetch_models_from_provider(
    api_base_url: str,
    api_key: str | None,
    provider_type: str = "responses",
    timeout_seconds: float = 18,
) -> list[dict[str, str]]:
    safe_provider_type = normalize_provider_type(provider_type)
    models_urls = build_provider_models_url_candidates(api_base_url, safe_provider_type)
    if not models_urls:
        raise ValueError("A valid API base URL is required")

    headers = {
        "Accept": "application/json",
        "User-Agent": "codex-context-studio/0.2",
    }
    safe_api_key = sanitize_text(api_key or "").strip()
    if safe_provider_type == "gemini" and safe_api_key:
        headers["x-goog-api-key"] = safe_api_key
    elif safe_provider_type == "claude" and safe_api_key:
        headers["x-api-key"] = safe_api_key
        headers["anthropic-version"] = "2023-06-01"
    elif safe_api_key:
        headers["Authorization"] = f"Bearer {safe_api_key}"

    last_error: ValueError | None = None

    for models_url in models_urls:
        request = urllib_request.Request(models_url, headers=headers, method="GET")

        try:
            with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore").strip()
            message = sanitize_text(detail or exc.reason or f"HTTP {exc.code}")
            if exc.code in {404, 405} and models_url != models_urls[-1]:
                last_error = ValueError(message)
                continue
            raise ValueError(message) from exc
        except urllib_error.URLError as exc:
            raise ValueError(sanitize_text(exc.reason or str(exc))) from exc
        except json.JSONDecodeError as exc:
            raise ValueError("Model endpoint returned invalid JSON") from exc

        models = normalize_fetched_provider_models(payload, safe_provider_type)
        if models:
            return models
        last_error = ValueError("Provider returned no usable models")

    raise last_error or ValueError("Provider returned no usable models")

def normalize_provider_models_for_payload(
    raw_models: Any,
    *,
    provider_label: str,
) -> list[dict[str, str]]:
    if not isinstance(raw_models, (list, tuple)):
        return []

    models: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for item in raw_models:
        if isinstance(item, str):
            model_id = sanitize_text(item).strip()
            label = model_id
            group = provider_label
        elif isinstance(item, dict):
            model_id = sanitize_text(item.get("id") or item.get("name") or "").strip()
            label = sanitize_text(item.get("label") or item.get("displayName") or model_id).strip() or model_id
            group = sanitize_text(item.get("group") or item.get("provider") or provider_label).strip() or provider_label
        else:
            continue

        if not model_id or model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        models.append(
            {
                "id": model_id,
                "label": label,
                "group": group,
                "provider": provider_label,
            }
        )
    return models


def sanitize_context_workbench_provider_payload(
    provider: Mapping[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    provider_id = sanitize_text(provider.get("id") or "").strip()
    provider_type = context_provider_type(provider)
    name = sanitize_text(provider.get("name") or provider_id).strip() or provider_id
    api_base_url = context_provider_api_base_url(provider)
    if provider_id == CODEX_PROXY_PROVIDER_ID:
        api_base_url = api_base_url or CODEX_PROXY_BASE_URL

    default_model = sanitize_text(provider.get("default_model") or "").strip()
    models = normalize_provider_models_for_payload(
        provider.get("models"),
        provider_label=name,
    )
    if provider_id == CODEX_PROXY_PROVIDER_ID and not models:
        models = normalize_provider_models_for_payload(
            DEFAULT_CODEX_PROXY_MODELS,
            provider_label=name or "Codex",
        )

    return {
        "id": provider_id,
        "name": name,
        "provider_type": provider_type,
        "enabled": bool(provider.get("enabled", True)),
        "supports_model_fetch": bool(provider.get("supports_model_fetch", True)),
        "supports_responses": provider_type == "responses",
        "api_base_url": api_base_url,
        "default_model": default_model,
        "models": models,
        "last_sync_at": sanitize_text(provider.get("last_sync_at") or "").strip(),
        "last_sync_error": sanitize_text(provider.get("last_sync_error") or "").strip(),
        "has_api_key": bool(context_provider_api_key(provider, settings))
        and provider_id != CODEX_PROXY_PROVIDER_ID,
    }


def context_workbench_provider_payloads(settings: Settings, *, refresh_models: bool = False) -> list[dict[str, Any]]:
    context_provider_id = sanitize_text(settings.context_workbench_provider_id or "").strip() or CODEX_PROXY_PROVIDER_ID
    context_model = sanitize_text(settings.context_workbench_model or "").strip()
    providers = [
        sanitize_context_workbench_provider_payload(provider, settings)
        for provider in settings.response_providers
        if sanitize_text(provider.get("id") or "").strip()
    ]

    if not any(sanitize_text(provider.get("id") or "").strip() == CODEX_PROXY_PROVIDER_ID for provider in providers):
        providers.insert(
            0,
            sanitize_context_workbench_provider_payload(
                {
                    "id": CODEX_PROXY_PROVIDER_ID,
                    "name": "Codex",
                    "provider_type": "responses",
                    "enabled": True,
                    "api_base_url": CODEX_PROXY_BASE_URL,
                    "default_model": DEFAULT_CONTEXT_WORKBENCH_MODEL,
                    "models": DEFAULT_CODEX_PROXY_MODELS,
                },
                settings,
            ),
        )

    if refresh_models:
        for provider in providers:
            provider_id = sanitize_text(provider.get("id") or "").strip()
            if provider_id != context_provider_id:
                continue
            source_provider = next(
                (
                    item
                    for item in settings.response_providers
                    if sanitize_text(item.get("id") or "").strip() == provider_id
                ),
                provider,
            )
            try:
                provider_base_url = sanitize_text(provider.get("api_base_url") or "").strip()
                provider_api_key = context_provider_api_key(source_provider, settings)
                if provider_id != CODEX_PROXY_PROVIDER_ID and not provider_base_url:
                    raise ValueError("Base URL is required before getting models.")
                if provider_id != CODEX_PROXY_PROVIDER_ID and not provider_api_key:
                    raise ValueError("API Key is required before getting models.")
                fetched_models = fetch_models_from_provider(
                    provider_base_url,
                    provider_api_key,
                    sanitize_text(provider.get("provider_type") or "").strip(),
                    timeout_seconds=8 if provider_id != CODEX_PROXY_PROVIDER_ID else 4,
                )
            except Exception as exc:  # noqa: BLE001
                provider["last_sync_error"] = sanitize_text(str(exc))
            else:
                if context_model and not any(
                    sanitize_text(item.get("id") or "").strip() == context_model
                    for item in fetched_models
                    if isinstance(item, dict)
                ):
                    fetched_models = [
                        {
                            "id": context_model,
                            "label": context_model,
                            "group": sanitize_text(provider.get("name") or "Models"),
                            "provider": sanitize_text(provider.get("name") or "Models"),
                        },
                        *fetched_models,
                    ]
                provider["models"] = normalize_provider_models_for_payload(
                    fetched_models,
                    provider_label=sanitize_text(provider.get("name") or "Models"),
                )
                provider["last_sync_error"] = ""
                provider["last_sync_at"] = datetime.now(timezone.utc).isoformat()
            break

    return providers


def context_workbench_models_payload(settings: Settings, provider_payloads: list[dict[str, Any]]) -> list[dict[str, str]]:
    settings_data = context_workbench_settings_payload(settings)
    context_model = sanitize_text(settings_data.get("context_workbench_model") or "").strip()
    context_provider_id = sanitize_text(settings_data.get("context_workbench_provider_id") or "").strip()
    selected_provider = next(
        (
            provider
            for provider in provider_payloads
            if sanitize_text(provider.get("id") or "").strip() == context_provider_id
        ),
        provider_payloads[0] if provider_payloads else {},
    )
    provider_label = sanitize_text(selected_provider.get("name") or "Models")
    models = normalize_provider_models_for_payload(
        selected_provider.get("models"),
        provider_label=provider_label,
    )
    if context_model and not any(model.get("id") == context_model for model in models):
        models.insert(
            0,
            {
                "id": context_model,
                "label": context_model,
                "group": provider_label,
                "provider": provider_label,
            },
        )
    return models

def codex_proxy_control_url(path: str) -> str:
    control_base = CODEX_PROXY_BASE_URL.rstrip("/")
    if control_base.endswith("/v1"):
        control_base = control_base[:-3]
    return f"{control_base}{path}"

def post_codex_proxy_control_json(path: str, payload: dict[str, Any], timeout_seconds: float = 8) -> dict[str, Any]:
    request = urllib_request.Request(
        codex_proxy_control_url(path),
        data=json.dumps(sanitize_value(payload), ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise ValueError(sanitize_text(detail or exc.reason or f"HTTP {exc.code}")) from exc
    except urllib_error.URLError as exc:
        raise ValueError(sanitize_text(exc.reason or str(exc))) from exc

    try:
        result = json.loads(raw_body or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Proxy returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("Proxy returned invalid payload")
    return sanitize_value(result)

def get_codex_proxy_control_json(path: str, timeout_seconds: float = 3) -> dict[str, Any] | None:
    request = urllib_request.Request(
        codex_proxy_control_url(path),
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if exc.code == HTTPStatus.NOT_FOUND:
            return None
        raise ValueError(sanitize_text(detail or exc.reason or f"HTTP {exc.code}")) from exc
    except urllib_error.URLError as exc:
        raise ValueError(sanitize_text(exc.reason or str(exc))) from exc

    try:
        result = json.loads(raw_body or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Proxy returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("Proxy returned invalid payload")
    return sanitize_value(result)


def set_proxy_context_run_state(session_id: str, request_id: str, running: bool) -> dict[str, Any]:
    safe_session_id = sanitize_text(session_id or "").strip()
    safe_request_id = sanitize_text(request_id or "").strip()
    if not safe_session_id or not safe_request_id:
        return {"status": "skipped", "reason": "missing_session_or_request"}
    return post_codex_proxy_control_json(
        f"/api/proxy/sessions/{quote(safe_session_id, safe='')}/context-run",
        {
            "request_id": safe_request_id,
            "running": bool(running),
        },
        timeout_seconds=3,
    )


def safe_set_proxy_context_run_state(session_id: str, request_id: str, running: bool) -> dict[str, Any]:
    try:
        return set_proxy_context_run_state(session_id, request_id, running)
    except ValueError as exc:
        return {
            "status": "error",
            "error": sanitize_text(str(exc) or "proxy context run state update failed"),
        }

def proxy_state_contains_session(session_id: str) -> bool:
    safe_session_id = sanitize_text(session_id or "").strip()
    if not safe_session_id:
        return False
    try:
        from backend.proxy_session_storage import ProxySessionStorage
        from backend.web_constants import STATE_DIR
    except ImportError:
        return False
    return ProxySessionStorage(STATE_DIR).session_exists(safe_session_id)

def codex_proxy_session_exists(session_id: str) -> bool:
    safe_session_id = sanitize_text(session_id or "").strip()
    if not safe_session_id:
        return False
    if proxy_state_contains_session(safe_session_id):
        return True
    try:
        return get_codex_proxy_control_json(
            f"/api/proxy/sessions/{quote(safe_session_id, safe='')}",
            timeout_seconds=1.5,
        ) is not None
    except ValueError:
        return False

def sync_proxy_session_transcript_if_known(
    session: SessionState,
    transcript: list[dict[str, object]],
) -> dict[str, object]:
    session_id = sanitize_text(session.session_id or "").strip()
    if not session_id:
        return {"status": "skipped", "reason": "missing_session_id"}
    if not codex_proxy_session_exists(session_id):
        return {"status": "skipped", "reason": "not_proxy_session"}

    proxy_payload = post_codex_proxy_control_json(
        f"/api/proxy/sessions/{quote(session_id, safe='')}/transcript",
        {"transcript": transcript},
    )
    if bool(proxy_payload.get("changed")):
        visible_transcript = normalize_transcript(proxy_payload.get("transcript"))
        write_context_edit_marker(
            session_id,
            summary="Context has been edited.",
            edit_version=0,
            node_count=editable_context_node_count(
                visible_transcript,
                getattr(session, "node_locks", {}),
            ),
        )
    return {
        "status": "synced",
        "changed": bool(proxy_payload.get("changed")),
    }

def safe_sync_proxy_session_transcript_if_known(
    session: SessionState,
    transcript: list[dict[str, object]],
) -> dict[str, object]:
    try:
        return sync_proxy_session_transcript_if_known(session, transcript)
    except ValueError as exc:
        return {
            "status": "error",
            "error": sanitize_text(str(exc) or "proxy transcript sync failed"),
        }


def refresh_session_from_proxy_active_context_if_known(
    app_state: AppState,
    session: SessionState,
) -> SessionState:
    session_id = sanitize_text(session.session_id or "").strip()
    if not session_id:
        return session

    try:
        proxy_payload = get_codex_proxy_control_json(
            f"/api/proxy/sessions/{quote(session_id, safe='')}",
            timeout_seconds=2,
        )
    except ValueError:
        return session

    if not proxy_payload:
        return session

    transcript = normalize_transcript(proxy_payload.get("transcript"))
    if not transcript:
        return session

    return app_state.upsert_proxy_session(
        session_id=session_id,
        title=sanitize_text(proxy_payload.get("title") or "").strip() or session.title,
        transcript=transcript,
        is_main_turn_running=bool(proxy_payload.get("is_main_turn_running")),
        main_turn_id=sanitize_text(proxy_payload.get("main_turn_id") or "").strip(),
        main_turn_started_at=sanitize_text(proxy_payload.get("main_turn_started_at") or "").strip(),
        main_turn_updated_at=sanitize_text(proxy_payload.get("main_turn_updated_at") or "").strip(),
        node_locks=proxy_payload.get("node_locks") if isinstance(proxy_payload.get("node_locks"), dict) else {},
        node_lock_revision=int(proxy_payload.get("node_lock_revision") or 0),
    )

def append_proxy_transcript_sync_warning(answer: str, error_message: str) -> str:
    warning = (
        "Note: this context edit was written to the local workbench view, "
        "but syncing it back to the Codex proxy transcript failed: "
        f"{sanitize_text(error_message)}. The next main Codex turn may still see the previous context."
    )
    safe_answer = sanitize_text(answer).rstrip()
    if not safe_answer:
        return warning
    return f"{safe_answer}\n\n{warning}"
