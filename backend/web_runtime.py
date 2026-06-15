from __future__ import annotations

import base64
import json
import mimetypes
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import quote, urlparse, urlunparse
from simple_agent.agent import BridgedFunctionCall, SimpleAgent, ToolEvent, sanitize_text
from simple_agent.config import CODEX_PROXY_BASE_URL, CODEX_PROXY_PROVIDER_ID, Settings
from simple_agent.tools import ToolExecution
try:
    import tiktoken
except ImportError:
    tiktoken = None

from backend.web_constants import (
    ATTACHMENTS_DIR,
    DATA_URL_PATTERN,
    MAX_ATTACHMENT_BYTES,
    MAX_TOTAL_ATTACHMENT_BYTES,
    PROVIDER_MODEL_TYPES,
    SessionState,
)

from backend.web_state import AppState

from backend.web_context import (
    ContextWorkbenchDraft,
    ContextWorkbenchToolRegistry,
    attachment_url_path,
    build_attachment_input,
    build_attachment_path_note,
    build_context_workspace_snapshot,
    coerce_context_revision_number,
    context_revision_summaries,
    editable_context_node_count,
    extract_text_from_provider_message_content,
    find_active_context_revision_id,
    model_options,
    normalize_context_chat_history,
    normalize_provider_items,
    normalize_selected_node_indexes,
    normalize_transcript,
    proxy_state_sqlite_file,
    sanitize_value,
    serialize_tool_event,
    write_context_edit_marker,
    write_context_request_debug,
)

def context_workbench_settings_payload(settings: Settings) -> dict[str, object]:
    return {
        "context_workbench_model": sanitize_text(settings.context_workbench_model or settings.model).strip()
        or sanitize_text(settings.model).strip()
        or "gpt-5.5",
        "context_workbench_provider_id": CODEX_PROXY_PROVIDER_ID,
        "context_token_warning_threshold": int(settings.context_token_warning_threshold or 5000),
        "context_token_critical_threshold": int(settings.context_token_critical_threshold or 10000),
        "user_locale": sanitize_text(settings.user_locale or "").strip() or "en-US",
        "ui_font": sanitize_text(settings.ui_font or "").strip() or "Noto Serif SC",
        "ui_font_size": int(settings.ui_font_size or 15),
    }

def prepare_context_chat_history_for_model(raw_history: Any, *, limit: int = 12) -> list[dict[str, str]]:
    history = normalize_context_chat_history(raw_history)
    filtered: list[dict[str, str]] = []

    for item in history:
        if item["role"] == "assistant":
            content = sanitize_text(item["content"])
            if "我已经读完当前上下文了，但这次没能稳定产出文字答复" in content:
                continue
        filtered.append(item)

    if limit > 0:
        return filtered[-limit:]
    return filtered

def extract_response_output_text(response: Any) -> str:
    direct_text = sanitize_text(getattr(response, "output_text", "") or "").strip()
    if direct_text:
        return direct_text

    text_parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        if sanitize_text(getattr(item, "type", "")).strip() != "message":
            continue

        for content_item in getattr(item, "content", None) or []:
            if sanitize_text(getattr(content_item, "type", "")).strip() != "output_text":
                continue
            text_parts.append(sanitize_text(getattr(content_item, "text", "") or ""))

    return sanitize_text("".join(text_parts)).strip()

def response_output_to_turn_items(response: Any) -> tuple[list[dict[str, Any]], list[Any]]:
    turn_items: list[dict[str, Any]] = []
    function_calls: list[Any] = []

    for item in getattr(response, "output", []) or []:
        item_type = sanitize_text(getattr(item, "type", "")).strip()
        if item_type == "message":
            role = sanitize_text(getattr(item, "role", "")).strip() or "assistant"
            text_parts: list[str] = []
            for content_item in getattr(item, "content", None) or []:
                if sanitize_text(getattr(content_item, "type", "")).strip() != "output_text":
                    continue
                text_parts.append(sanitize_text(getattr(content_item, "text", "") or ""))

            message_text = "".join(text_parts)
            if message_text.strip():
                turn_items.append(SimpleAgent._message(role, message_text))
            continue

        if item_type == "function_call":
            function_calls.append(item)
            turn_items.append(
                {
                    "type": "function_call",
                    "call_id": sanitize_text(getattr(item, "call_id", "") or ""),
                    "name": sanitize_text(getattr(item, "name", "") or ""),
                    "arguments": sanitize_text(getattr(item, "arguments", "") or "{}") or "{}",
                }
            )

    return normalize_provider_items(turn_items), function_calls

def build_context_chat_runtime(
    session: SessionState,
    *,
    message: str,
    selected_indexes: list[int] | None = None,
) -> tuple[str, str, ContextWorkbenchDraft, ContextWorkbenchToolRegistry, list[dict[str, Any]]]:
    safe_selected_indexes = normalize_selected_node_indexes(selected_indexes or [], len(session.transcript))
    draft = ContextWorkbenchDraft(normalize_transcript(session.transcript), safe_selected_indexes)
    snapshot = build_context_workspace_snapshot(session, selected_indexes=safe_selected_indexes)
    tool_registry = ContextWorkbenchToolRegistry(draft)
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
                    "这里是主 Codex 对话的当前上下文快照。本轮回答和编辑都以这份快照为准；前面的右侧手动页历史可能提到旧节点或旧内容。",
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

    request_model = sanitize_text(
        session.agent.settings.context_workbench_model or session.agent.settings.model
    ).strip() or "gpt-5.5"
    instructions = "\n".join(
        [
            "你是主 Codex 对话的上下文维护助手，运行在右侧手动页中。",
            "",
            "你的目标是维护、查看、压缩、删除或改写当前主 Codex 上下文。",
            "不要搞混你自己的右侧手动页聊天历史和主 Codex 的聊天历史。",
            "不要继续进行主 Codex 的任务；用户让你处理的是“当前主 Codex 上下文”。",
            "",
            "你会看到一条最新的 developer 消息，标题为：",
            "# 当前主 Codex 上下文快照",
            "",
            "这份快照是本轮唯一可信的主 Codex 上下文来源。",
            "右侧手动页历史只是你和用户关于上下文维护的对话，可能提到旧节点、旧内容或旧选择；定位节点、回答当前上下文、执行编辑时，都以最新快照为准。",
            "",
            "快照中的 Node # 只在当前这份快照中有效。",
            "user 节点通常在快照里给全文。",
            "assistant 节点通常只给首句预览；预览后面的内容你不可见。",
            "如果任务需要理解、压缩或精确修改 assistant 节点的完整内容，先调用 get_context_node_details。",
            "",
            "工具能力：",
            "",
            "- get_context_node_details(node_numbers)",
            "  展开一个或多个节点的完整详情。",
            "  返回 node_detail_list.nodes，完整内容只在每个 node 的 blocks 内出现一次；文本块用 content，工具块用 arguments/output。"
            "  block 上会给 item_number/item_ref，精细编辑时用这些引用回写当前 transcript。",
            "",
            "- compress_context_nodes(node_numbers, summary_markdown, title?, style?)",
            "  用一个摘要节点替换一个或多个完整节点。",
            "  返回 mutation_delta，只描述本次变化，不会重复返回完整快照。",
            "  适合压缩一段讨论、一个主题、一个范围内的节点，或包含工具输出的大 assistant 节点。",
            "",
            "- delete_context_nodes(node_numbers, reason?)",
            "  删除一个或多个完整节点。",
            "  返回 mutation_delta。",
            "  删除通常不需要先展开详情，除非用户要求你先核实内容。",
            "",
            "- delete_context_item(node_numbers, item_number / item_numbers, reason?)",
            "- replace_context_item(node_numbers, item_number, replacement_item, reason?)",
            "- compress_context_item(node_numbers, item_number, compressed_content, style?)",
            "  这些是精细 item 级编辑工具。",
            "  只在用户明确要求处理某个 content item、某段 assistant 文本、某个工具调用或某个工具输出时使用。",
            "  一般先 get_context_node_details，确认 item # 后再调用。",
            "  默认不要把模糊的压缩/删除请求理解成 item 级编辑。",
            "",
            "- confirm_working_snapshot()",
            "  所有计划内编辑完成后，用它确认最终 working snapshot。",
            "  返回 final_working_snapshot。",
            "",
            "- set_context_revision_summary(summary)",
            "  编辑完成并确认后，保存一句恢复页可读的变更摘要。",
            "  摘要要说明改了什么具体上下文内容，不要只说“修改了节点”。",
            "",
            "推荐工作方式：",
            "",
            "- 用户只是问“现在看到什么 / 有哪些节点 / 选中了什么”：优先基于快照直接回答，不必展开详情。",
            "- 用户说“删除 3-50”“删掉节点 3 到 50”：把它理解为 Node #3 到 Node #50 的范围，通常直接 delete_context_nodes。",
            "- 用户说“压缩 3-50”“压缩这些节点”：把它理解为节点级压缩；如果范围内包含 assistant 节点，先 get_context_node_details，再 compress_context_nodes。",
            "- 用户说“压缩有关前端的讨论”“删掉关于某主题的部分”：先根据快照定位相关节点；能判断就直接处理，范围不明确才简短确认。",
            "- 用户说“压缩这个 assistant 节点”“压缩工具输出很多的那轮”：先 get_context_node_details，再基于完整内容写 summary_markdown。",
            "- 用户说“只删某个工具输出 / 改某个工具调用 / 保留节点但缩短某个 item”：走 item 级工具。",
            "- 模糊请求默认按节点级或主题级的大方向处理，不要过早拆到 content item。",
            "- 选择最少、最直接的工具路径。不要重复展开同一个节点，不要反复确认显而易见的范围，不要啰嗦解释内部流程。",
            "- 如果工具返回 target_resolution 或 item_resolution，说明目标不明确；根据返回信息重新明确 node_numbers 或 item #，必要时再问用户。",
            "- 只要本轮做过编辑，结束前先 confirm_working_snapshot，再 set_context_revision_summary，最后用用户的语言简短说明结果。",
        ]
    )
    return instructions, request_model, draft, tool_registry, context_input

def resolve_context_workbench_provider_id(settings: Settings, model_id: str) -> str:
    requested_provider_id = sanitize_text(
        settings.context_workbench_provider_id or settings.active_provider_id
    ).strip()
    enabled_providers = [
        provider
        for provider in settings.response_providers
        if bool(provider.get("enabled"))
    ]
    enabled_provider_ids = {
        sanitize_text(provider.get("id") or "").strip()
        for provider in enabled_providers
        if sanitize_text(provider.get("id") or "").strip()
    }
    if CODEX_PROXY_PROVIDER_ID in enabled_provider_ids:
        return CODEX_PROXY_PROVIDER_ID

    cleaned_model_id = sanitize_text(model_id).strip()
    if cleaned_model_id:
        if requested_provider_id and requested_provider_id in enabled_provider_ids:
            requested_provider = next(
                (
                    provider
                    for provider in enabled_providers
                    if sanitize_text(provider.get("id") or "").strip() == requested_provider_id
                ),
                None,
            )
            requested_provider_model_ids = {
                sanitize_text(model.get("id") or "").strip()
                for model in (requested_provider or {}).get("models") or []
                if sanitize_text(model.get("id") or "").strip()
            }
            if cleaned_model_id in requested_provider_model_ids:
                return requested_provider_id

        for provider in enabled_providers:
            provider_id = sanitize_text(provider.get("id") or "").strip()
            if not provider_id:
                continue
            provider_model_ids = {
                sanitize_text(model.get("id") or "").strip()
                for model in provider.get("models") or []
                if sanitize_text(model.get("id") or "").strip()
            }
            if cleaned_model_id in provider_model_ids:
                return provider_id

    if requested_provider_id and requested_provider_id in enabled_provider_ids:
        return requested_provider_id

    if CODEX_PROXY_PROVIDER_ID in enabled_provider_ids:
        return CODEX_PROXY_PROVIDER_ID

    active_provider_id = sanitize_text(settings.active_provider_id or "").strip()
    if active_provider_id in enabled_provider_ids:
        return active_provider_id

    return next(iter(enabled_provider_ids), active_provider_id or "openai")

def context_workbench_provider(settings: Settings, provider_id: str) -> dict[str, Any]:
    cleaned_provider_id = sanitize_text(provider_id).strip()
    return next(
        (
            item
            for item in settings.response_providers
            if sanitize_text(item.get("id") or "").strip() == cleaned_provider_id
        ),
        settings.active_provider(),
    )

def model_supports_minimal_reasoning(model_id: str) -> bool:
    cleaned_model_id = sanitize_text(model_id).strip().lower()
    return cleaned_model_id.startswith("gpt-5") or cleaned_model_id.startswith("gpt-oss")

def resolve_context_reasoning_effort(
    settings: Settings,
    *,
    provider_id: str,
    model_id: str,
    requested_effort: str | None,
) -> str | None:
    cleaned_effort = sanitize_text(requested_effort or "").strip()
    if cleaned_effort == "default":
        cleaned_effort = sanitize_text(settings.default_reasoning_effort).strip()

    if cleaned_effort in {"", "default"}:
        return None

    if cleaned_effort == "none":
        provider = context_workbench_provider(settings, provider_id)
        provider_type = sanitize_text(provider.get("provider_type") or "").strip()
        if (
            provider_type in {"responses", "chat_completion"}
            and model_supports_minimal_reasoning(model_id)
        ):
            return "minimal"
        return None

    if cleaned_effort in {"minimal", "low", "medium", "high", "xhigh"}:
        return cleaned_effort

    return None

def build_context_workbench_agent(settings: Settings, provider_id: str) -> SimpleAgent:
    resolved_provider_id = sanitize_text(provider_id).strip() or sanitize_text(settings.active_provider_id).strip() or "openai"
    provider = context_workbench_provider(settings, resolved_provider_id)
    if resolved_provider_id == CODEX_PROXY_PROVIDER_ID:
        provider_api_key = "not-needed"
        provider_base_url = CODEX_PROXY_BASE_URL
    else:
        provider_api_key = sanitize_text(provider.get("api_key") or "").strip() or settings.openai_api_key
        provider_base_url = sanitize_text(provider.get("api_base_url") or "").strip() or settings.openai_base_url
    scoped_settings = Settings(
        model=settings.model,
        default_reasoning_effort=settings.default_reasoning_effort,
        context_workbench_model=settings.context_workbench_model,
        context_workbench_provider_id=resolved_provider_id,
        project_root=settings.project_root,
        max_tool_rounds=settings.max_tool_rounds,
        tool_settings=settings.tool_settings,
        response_providers=settings.response_providers,
        active_provider_id=resolved_provider_id,
        context_token_warning_threshold=settings.context_token_warning_threshold,
        context_token_critical_threshold=settings.context_token_critical_threshold,
        openai_api_key=provider_api_key,
        openai_base_url=provider_base_url,
        assistant_name="",
        assistant_greeting="",
        assistant_prompt="",
        user_name="",
        user_locale="",
        user_timezone="",
        user_profile="",
    )
    return SimpleAgent(scoped_settings, include_default_instructions=False)

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
        return "hash-context-workbench"
    return f"hash-context:{safe_session_id[:48]}"

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

            chunk = response.read(4096)
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
    session: SessionState,
    *,
    message: str,
    selected_indexes: list[int] | None = None,
    reasoning_effort: str | None = None,
    on_text_delta: Callable[[str], None] | None = None,
    on_round_reset: Callable[[], None] | None = None,
    on_tool_event: Callable[[ToolEvent], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[str, str, ContextWorkbenchDraft, list[ToolEvent]]:
    instructions, request_model, draft, tool_registry, context_input = build_context_chat_runtime(
        session,
        message=message,
        selected_indexes=selected_indexes,
    )
    context_provider_id = resolve_context_workbench_provider_id(session.agent.settings, request_model)
    request_reasoning_effort = resolve_context_reasoning_effort(
        session.agent.settings,
        provider_id=context_provider_id,
        model_id=request_model,
        requested_effort=reasoning_effort,
    )
    context_agent = build_context_workbench_agent(session.agent.settings, context_provider_id)
    tool_events: list[ToolEvent] = []
    readonly_tool_result_cache: dict[str, str] = {}
    readonly_tool_cache_names = {"get_context_node_details", "confirm_working_snapshot"}

    round_count = 0
    while True:
        round_count += 1

        if check_cancelled is not None:
            check_cancelled()

        def build_request() -> dict[str, Any]:
            request = {
                "model": request_model,
                "instructions": instructions,
                "input": sanitize_value(context_input),
                "tools": tool_registry.schemas,
                "store": False,
                "prompt_cache_key": context_workbench_prompt_cache_key(session.session_id),
                "extra_headers": {
                    "x-hash-context-internal": "context-workbench",
                    "x-hash-context-session-id": session.session_id,
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
            request = build_request()
            if context_provider_id == CODEX_PROXY_PROVIDER_ID:
                response = stream_context_codex_proxy_response_with_retry(
                    request,
                    on_text_delta=on_text_delta,
                    check_cancelled=check_cancelled,
                )
            else:
                response = context_agent._stream_response(
                    **request,
                    on_text_delta=on_text_delta,
                )
        except Exception as exc:
            if (
                context_provider_id == CODEX_PROXY_PROVIDER_ID
                or not context_agent._should_fallback_to_developer(exc)
            ):
                raise

            context_agent._fallback_to_developer_context()
            response = context_agent._stream_response(
                **build_request(),
                on_text_delta=on_text_delta,
            )
        if check_cancelled is not None:
            check_cancelled()

        if not response.function_calls:
            final_answer = sanitize_text(response.output_text).strip()
            if not final_answer:
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
                output_preview=session.agent._preview(result),
                raw_output=result,
                display_title=execution.display_title,
                display_detail=execution.display_detail,
                display_result=execution.display_result,
                status=execution.status,
            )
            tool_events.append(tool_event)
            if on_tool_event is not None:
                on_tool_event(tool_event)

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

def create_context_chat_answer(
    session: SessionState,
    *,
    message: str,
    selected_indexes: list[int] | None = None,
    reasoning_effort: str | None = None,
) -> tuple[str, str, ContextWorkbenchDraft]:
    answer, request_model, draft, _tool_events = run_context_chat_turn(
        session,
        message=message,
        selected_indexes=selected_indexes,
        reasoning_effort=reasoning_effort,
    )
    return answer, request_model, draft

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
    proxy_override: dict[str, object] | None = None
    if draft.has_changes:
        conversation, revisions, pending_restore = app_state.apply_context_workbench_mutation(
            session,
            transcript=draft.committed_transcript(),
            revision_label=draft.revision_label(),
            revision_summary=draft.revision_summary(),
            operations=draft.operations,
        )
        proxy_override = safe_sync_proxy_session_override_if_known(session, conversation)
        if sanitize_text(proxy_override.get("status") or "") == "error":
            answer = append_proxy_override_warning(answer, sanitize_text(proxy_override.get("error") or ""))
    else:
        conversation = sanitize_value(session.transcript)
        revisions = context_revision_summaries(session.context_revisions)
        pending_restore = None

    history = app_state.append_context_workbench_turn(
        session,
        user_message=user_message,
        answer=answer,
    )
    payload: dict[str, object] = {
        "answer": answer,
        "used_model": used_model,
        "history": history,
        "conversation": conversation,
        "revisions": revisions,
        "pending_restore": pending_restore,
    }
    if tool_events is not None:
        payload["tool_events"] = [serialize_tool_event(event) for event in tool_events]
    if proxy_override is not None:
        payload["proxy_override"] = proxy_override
    return payload

def parse_data_url(data_url: str) -> tuple[str, bytes]:
    match = DATA_URL_PATTERN.match(sanitize_text(data_url))
    if not match:
        raise ValueError("attachment data_url is invalid")

    mime_type = sanitize_text(match.group("mime") or "").strip() or "application/octet-stream"
    try:
        raw_bytes = base64.b64decode(match.group("data"), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("附件编码解析失败") from exc

    if not raw_bytes:
        raise ValueError("附件内容为空")

    return mime_type, raw_bytes

def persist_request_attachments(raw_attachments: Any) -> tuple[list[dict[str, object]], list[dict[str, Any]]]:
    if raw_attachments in (None, ""):
        return [], []
    if not isinstance(raw_attachments, list):
        raise ValueError("attachments must be a list")

    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    transcript_attachments: list[dict[str, object]] = []
    agent_inputs: list[dict[str, Any]] = []
    total_size = 0

    for raw_item in raw_attachments:
        if not isinstance(raw_item, dict):
            continue

        original_name = sanitize_text(raw_item.get("name") or "").strip() or "upload"
        data_url = sanitize_text(raw_item.get("data_url") or "")
        payload_mime_type = sanitize_text(raw_item.get("mime_type") or "").strip()
        parsed_mime_type, raw_bytes = parse_data_url(data_url)
        mime_type = payload_mime_type or parsed_mime_type or "application/octet-stream"
        total_size += len(raw_bytes)

        if len(raw_bytes) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"附件 {original_name} 超过 50 MB")
        if total_size > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError("本轮附件总大小超过 50 MB")

        suffix = Path(original_name).suffix
        if not suffix:
            guessed_extension = mimetypes.guess_extension(mime_type or "") or ""
            suffix = guessed_extension

        attachment_id = uuid.uuid4().hex
        stored_name = f"{attachment_id}{suffix}"
        stored_path = ATTACHMENTS_DIR / stored_name
        stored_path.write_bytes(raw_bytes)

        relative_path = attachment_url_path(stored_name)
        kind = "image" if mime_type.startswith("image/") else "file"

        transcript_attachments.append(
            {
                "id": attachment_id,
                "name": original_name,
                "mime_type": mime_type,
                "kind": kind,
                "size_bytes": len(raw_bytes),
                "relative_path": relative_path,
                "url": f"/{relative_path}",
            }
        )
        agent_inputs.append(build_attachment_path_note(original_name, mime_type, stored_path.resolve()))
        agent_inputs.append(build_attachment_input(original_name, mime_type, data_url))

    return transcript_attachments, agent_inputs

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
        raise ValueError("请先填写有效的 API 地址")

    headers = {
        "Accept": "application/json",
        "User-Agent": "hash-code/0.2",
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
            raise ValueError("模型接口返回的不是合法 JSON") from exc

        models = normalize_fetched_provider_models(payload, safe_provider_type)
        if models:
            return models
        last_error = ValueError("这个供应商没有返回可用模型")

    raise last_error or ValueError("这个供应商没有返回可用模型")

def clone_provider_settings_payloads(settings: Settings) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for provider in settings.response_providers:
        payloads.append(
            {
                "id": sanitize_text(provider.get("id") or "").strip(),
                "enabled": bool(provider.get("enabled")),
                "supports_model_fetch": bool(provider.get("supports_model_fetch")),
                "supports_responses": bool(provider.get("supports_responses")),
                "api_base_url": sanitize_text(provider.get("api_base_url") or "").strip(),
                "default_model": sanitize_text(provider.get("default_model") or "").strip(),
                "models": sanitize_value(provider.get("models") or []),
                "last_sync_at": sanitize_text(provider.get("last_sync_at") or "").strip(),
                "last_sync_error": sanitize_text(provider.get("last_sync_error") or "").strip(),
            }
        )
    return payloads

def provider_model_ids_from_payloads(provider_payloads: list[dict[str, Any]], provider_id: str) -> list[str]:
    cleaned_provider_id = sanitize_text(provider_id).strip()
    provider = next(
        (
            item
            for item in provider_payloads
            if sanitize_text(item.get("id") or "").strip() == cleaned_provider_id
        ),
        None,
    )
    if provider is None:
        return []
    model_ids: list[str] = []
    for model in provider.get("models") or []:
        if not isinstance(model, dict):
            continue
        model_id = sanitize_text(model.get("id") or "").strip()
        if model_id and model_id not in model_ids:
            model_ids.append(model_id)
    return model_ids

def context_workbench_provider_payloads(settings: Settings, *, refresh_codex_proxy_models: bool = False) -> list[dict[str, Any]]:
    payload = settings.public_payload()
    raw_providers = payload.get("response_providers")
    provider_payloads = [dict(item) for item in raw_providers if isinstance(item, dict)] if isinstance(raw_providers, list) else []
    for provider in provider_payloads:
        provider_id = sanitize_text(provider.get("id") or "").strip()
        if provider_id != CODEX_PROXY_PROVIDER_ID:
            continue
        provider["api_base_url"] = CODEX_PROXY_BASE_URL
        context_model = sanitize_text(settings.context_workbench_model or "").strip()
        if context_model:
            provider["default_model"] = context_model
        if not refresh_codex_proxy_models:
            continue
        try:
            fetched_models = fetch_models_from_provider(
                CODEX_PROXY_BASE_URL,
                "not-needed",
                "responses",
                timeout_seconds=4,
            )
        except Exception as exc:
            provider["last_sync_error"] = "" if provider.get("models") else sanitize_text(str(exc))
            continue
        if fetched_models:
            if context_model and not any(
                sanitize_text(item.get("id") or "").strip() == context_model
                for item in fetched_models
                if isinstance(item, dict)
            ):
                fetched_models = [
                    {
                        "id": context_model,
                        "label": context_model,
                        "group": "Codex",
                        "provider": "Codex",
                    },
                    *fetched_models,
                ]
            provider["models"] = fetched_models
            provider["last_sync_error"] = ""
            provider["last_sync_at"] = datetime.now(timezone.utc).isoformat()
        break
    return provider_payloads

def context_workbench_models_payload(settings: Settings, provider_payloads: list[dict[str, Any]]) -> list[str]:
    settings_data = context_workbench_settings_payload(settings)
    context_model = sanitize_text(settings_data.get("context_workbench_model") or "").strip()
    provider_id = sanitize_text(settings_data.get("context_workbench_provider_id") or "").strip()
    return model_options(context_model, provider_model_ids_from_payloads(provider_payloads, provider_id))

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

def proxy_state_contains_session(session_id: str) -> bool:
    safe_session_id = sanitize_text(session_id or "").strip()
    if not safe_session_id:
        return False
    sqlite_file = proxy_state_sqlite_file()
    if not sqlite_file.exists():
        return False
    try:
        with closing(sqlite3.connect(sqlite_file)) as conn:
            row = conn.execute("SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (safe_session_id,)).fetchone()
    except sqlite3.Error:
        return False
    return row is not None

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

def sync_proxy_session_override_if_known(
    session: SessionState,
    transcript: list[dict[str, object]],
) -> dict[str, object]:
    session_id = sanitize_text(session.session_id or "").strip()
    if not session_id:
        return {"status": "skipped", "reason": "missing_session_id"}
    if not codex_proxy_session_exists(session_id):
        return {"status": "skipped", "reason": "not_proxy_session"}

    proxy_payload = post_codex_proxy_control_json(
        f"/api/proxy/sessions/{quote(session_id, safe='')}/override",
        {"transcript": transcript},
    )
    if bool(proxy_payload.get("changed")):
        summary, revision_number = active_context_revision_marker(session)
        visible_transcript = normalize_transcript(proxy_payload.get("transcript"))
        write_context_edit_marker(
            session_id,
            summary=summary,
            revision_number=revision_number,
            node_count=editable_context_node_count(visible_transcript),
        )
    return {
        "status": "synced",
        "changed": bool(proxy_payload.get("changed")),
        "has_override": bool(proxy_payload.get("has_override")),
    }

def safe_sync_proxy_session_override_if_known(
    session: SessionState,
    transcript: list[dict[str, object]],
) -> dict[str, object]:
    try:
        return sync_proxy_session_override_if_known(session, transcript)
    except ValueError as exc:
        return {
            "status": "error",
            "error": sanitize_text(str(exc) or "proxy override sync failed"),
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

    active_transcript = normalize_transcript(
        proxy_payload.get("active_transcript") or proxy_payload.get("transcript")
    )
    if not active_transcript:
        return session

    return app_state.upsert_proxy_session(
        session_id=session_id,
        title=sanitize_text(proxy_payload.get("title") or "").strip() or session.title,
        transcript=active_transcript,
        is_running=bool(proxy_payload.get("is_running")),
    )

def append_proxy_override_warning(answer: str, error_message: str) -> str:
    warning = (
        "注意：这次上下文编辑已经写入本地视图，但同步到 Codex 代理 override 失败："
        f"{sanitize_text(error_message)}。下一轮主模型可能仍会看到旧上下文。"
    )
    safe_answer = sanitize_text(answer).rstrip()
    if not safe_answer:
        return warning
    return f"{safe_answer}\n\n{warning}"

def active_context_revision_marker(session: SessionState) -> tuple[str, int]:
    active_revision_id = find_active_context_revision_id(session.context_revisions)
    active_revision = next(
        (
            revision
            for revision in reversed(session.context_revisions)
            if sanitize_text(revision.get("id") or "").strip() == active_revision_id
        ),
        None,
    )
    if active_revision is None and session.context_revisions:
        active_revision = session.context_revisions[-1]
    if not isinstance(active_revision, dict):
        return "Context has been edited.", 0
    summary = sanitize_text(active_revision.get("summary") or active_revision.get("label") or "").strip()
    revision_number = coerce_context_revision_number(active_revision.get("revision_number"), 0)
    return summary or "Context has been edited.", revision_number

