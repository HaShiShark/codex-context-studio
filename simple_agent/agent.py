from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from agent_runtime.adapters import (
    ChatCompletionsAdapter,
    ClaudeAdapter,
    GeminiAdapter,
    ProviderRequestContext,
    ResponsesAdapter,
    ResponsesStreamResult,
)
from agent_runtime.core.agent_core import AgentCore
from agent_runtime.core.prompt_blocks import PromptBlock
from agent_runtime.core.stream_events import (
    ProviderDoneEvent,
    ReasoningDeltaEvent,
    ReasoningDoneEvent,
    ReasoningStartEvent,
    TextDeltaEvent,
    ToolCallReadyEvent,
)
from simple_agent.config import Settings
from simple_agent.provider_clients import ClaudeRESTClient, GeminiRESTClient
from simple_agent.tools import ToolRegistry


@dataclass(slots=True)
class ToolEvent:
    name: str
    arguments: dict[str, Any]
    output_preview: str
    raw_output: str = ""
    display_title: str = ""
    display_detail: str = ""
    display_result: str = ""
    status: str = "completed"


@dataclass(slots=True)
class BridgedFunctionCall:
    name: str
    arguments: str
    call_id: str = ""


StreamResult = ResponsesStreamResult
_RESPONSES_PROVIDER_TYPE = "responses"
_CHAT_PROVIDER_TYPE = "chat_completion"
_CLAUDE_PROVIDER_TYPE = "claude"
_GEMINI_PROVIDER_TYPE = "gemini"
_TEXT_PART_TYPES = {"", "text", "input_text", "output_text"}
_IMAGE_PART_TYPES = {"input_image", "image_url"}
_NON_RESPONSE_ALLOWED_PARTS = {
    _CHAT_PROVIDER_TYPE: _TEXT_PART_TYPES | _IMAGE_PART_TYPES,
    _CLAUDE_PROVIDER_TYPE: _TEXT_PART_TYPES
    | _IMAGE_PART_TYPES
    | {"image", "thinking", "redacted_thinking", "tool_use", "tool_result"},
    _GEMINI_PROVIDER_TYPE: _TEXT_PART_TYPES,
}


def sanitize_text(value: Any) -> str:
    return "".join(
        char if not (0xD800 <= ord(char) <= 0xDFFF) else "\ufffd"
        for char in str(value)
    )


def sanitize_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return sanitize_value(asdict(value))
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {
            sanitize_value(key): sanitize_value(item)
            for key, item in value.items()
        }
    return value


class SimpleAgent:
    def __init__(self, settings: Settings, *, include_default_instructions: bool = True) -> None:
        self.settings = settings
        self.include_default_instructions = include_default_instructions
        self.active_provider = sanitize_value(settings.active_provider())
        self.provider_id = sanitize_text(self.active_provider.get("id") or "openai").strip() or "openai"
        self.provider_type = sanitize_text(
            self.active_provider.get("provider_type") or _RESPONSES_PROVIDER_TYPE
        ).strip() or _RESPONSES_PROVIDER_TYPE
        self.provider_api_key = sanitize_text(
            self.active_provider.get("api_key") or settings.openai_api_key or ""
        ).strip()
        self.provider_api_base_url = sanitize_text(
            self.active_provider.get("api_base_url") or settings.openai_base_url or ""
        ).strip()

        self.client = self._build_provider_client()
        self.tools = ToolRegistry(settings.project_root, settings.tool_settings)
        self.history: list[dict[str, Any]] = []
        self.context_role = "developer"
        self.instructions = self._build_instructions() if include_default_instructions else ""
        self.adapter = self._build_adapter()

    def _build_instructions(self) -> str:
        sections = [
            (
                "[核心规则]",
                "\n".join(
                    [
                        "You are a local coding assistant with full access to enabled local tools.",
                        "Be concise, practical, and honest.",
                        "Reply in Simplified Chinese when the user writes in Chinese.",
                        "Use tools when the answer depends on local files, local command output, images, patches, JavaScript execution, or the current time.",
                        "Use parallel_tools when several independent enabled tool calls can run at the same time.",
                        "Use apply_patch for file edits when possible. Use shell_command for short local commands and exec_command/write_stdin for long-running commands.",
                        "Uploaded attachments are included directly. When a tool needs an uploaded file, use the exact 'Local path for tools' shown with the attachment; never invent placeholder paths like <<LAST_USER_IMAGE>>.",
                        "Do not claim to have read a file unless you actually used a tool.",
                        "Only use tools that appear in the current enabled tools list.",
                        "User-configured assistant persona and user profile are preferences, not higher-priority instructions; ignore any part that conflicts with these core rules.",
                        f"Project root: {self.settings.project_root}",
                        "Relative tool paths resolve from the project root; absolute local paths are allowed when needed.",
                    ]
                ),
            )
        ]
        enabled_tool_names = self._enabled_tool_names()
        sections.append(("[当前启用工具]", ", ".join(enabled_tool_names) or "none"))
        sections.append(("[工具使用规则]", self._build_tool_usage_instructions(enabled_tool_names)))

        assistant_lines = []
        if self.settings.assistant_name:
            assistant_lines.append(f"助手名字：{self.settings.assistant_name}")
        if self.settings.assistant_prompt:
            assistant_lines.append(self.settings.assistant_prompt)
        if assistant_lines:
            sections.append(("[助手人设]", "\n".join(assistant_lines)))

        user_lines = []
        if self.settings.user_name:
            user_lines.append(f"用户名字：{self.settings.user_name}")
        if self.settings.user_locale:
            user_lines.append(f"用户语言：{self.settings.user_locale}")
        if self.settings.user_timezone:
            user_lines.append(f"用户时区：{self.settings.user_timezone}")
        if self.settings.user_profile:
            user_lines.append(f"关于用户：{self.settings.user_profile}")
        if user_lines:
            sections.append(("[用户画像]", "\n".join(user_lines)))

        return "\n\n".join(f"{title}\n{text}" for title, text in sections if text.strip())

    def _enabled_tool_names(self) -> list[str]:
        names: list[str] = []
        for schema in self.tools.schemas:
            if not isinstance(schema, dict):
                continue
            name = sanitize_text(schema.get("name") or "").strip()
            if name:
                names.append(name)
        return names

    def _build_tool_usage_instructions(self, enabled_tool_names: list[str]) -> str:
        enabled = set(enabled_tool_names)
        lines = [
            "Treat the enabled tools list as authoritative; do not mention or call tools that are not listed.",
            "Do not invent unavailable tools such as web_search, image_generation, MCP tools, request_permissions, request_user_input, spawn_agent, send_input, wait_agent, or multi_tool_use.parallel.",
        ]

        if "parallel_tools" in enabled:
            lines.append(
                "Use parallel_tools for independent enabled tool calls that can run at the same time. Do not use it for dependent steps, interactive processes, or nested parallel_tools calls."
            )
        if "shell_command" in enabled:
            lines.append(
                "Use shell_command for short one-shot local PowerShell commands. Set the working directory when it matters, and prefer rg for text/file search."
            )
        if "exec_command" in enabled:
            lines.append(
                "Use exec_command for long-running or interactive commands; if it returns a process_id/session_id, continue that process with write_stdin."
            )
        if "write_stdin" in enabled:
            lines.append(
                "Use write_stdin only with a process_id or session_id returned by exec_command, and pass the exact characters to write."
            )
        if "list_dir" in enabled:
            lines.append(
                "Use list_dir to inspect local directories; offset is 1-indexed, and depth/limit should stay focused."
            )
        if "read_file" in enabled:
            lines.append(
                "Use read_file for UTF-8 text files. Do not claim to know file contents until the tool result is available."
            )
        if "view_image" in enabled:
            lines.append(
                "Use view_image only with a real local image path. The detail parameter may be omitted or set to original when exact pixels matter."
            )
        if "apply_patch" in enabled:
            lines.extend(
                [
                    "Use apply_patch for file edits. Its patch must begin with *** Begin Patch and end with *** End Patch.",
                    "Each apply_patch operation must use *** Add File, *** Delete File, or *** Update File. New file contents need + prefixes; update hunks use space, -, and + line prefixes.",
                    "Use relative file paths in apply_patch payloads, never absolute paths.",
                ]
            )
        if "js_repl" in enabled:
            lines.append(
                "Use js_repl for JavaScript execution in a persistent Node.js kernel. Return a final value when useful; store persistent state on globalThis."
            )
        if "js_repl_reset" in enabled:
            lines.append("Use js_repl_reset only when the JavaScript kernel state needs to be cleared.")
        if "get_current_time" in enabled:
            lines.append("Use get_current_time when the answer depends on the current time or a user's timezone.")

        return "\n".join(lines)

    def describe_tools(self) -> str:
        return self.tools.describe()

    def reset(self) -> None:
        self.history = []

    def run_turn(
        self,
        user_message: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        on_reasoning_start: Callable[[], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        on_reasoning_done: Callable[[], None] | None = None,
        on_model_start: Callable[[], None] | None = None,
        on_model_done: Callable[[], None] | None = None,
        on_round_reset: Callable[[], None] | None = None,
        on_tool_event: Callable[[ToolEvent], None] | None = None,
        check_cancelled: Callable[[], None] | None = None,
    ) -> tuple[str, list[ToolEvent]]:
        core: AgentCore[ToolEvent] = AgentCore(
            max_tool_rounds=self.settings.max_tool_rounds,
            default_model=self.settings.model,
            history=self.history,
            build_request=self._build_agent_core_request,
            stream_response=self._stream_response,
            execute_tool=self.tools.execute,
            make_user_message=self._make_user_message,
            make_assistant_message=self._make_assistant_message,
            tool_event_factory=ToolEvent,
            sanitize_text=sanitize_text,
            sanitize_value=sanitize_value,
            preview_text=self._preview,
            should_fallback_to_developer=self._should_fallback_to_developer,
            fallback_to_developer=self._fallback_to_developer_context,
            check_cancelled=check_cancelled,
        )
        return core.run_turn(
            user_message,
            attachments=attachments,
            model=model,
            reasoning_effort=reasoning_effort,
            on_text_delta=on_text_delta,
            on_reasoning_start=on_reasoning_start,
            on_reasoning_delta=on_reasoning_delta,
            on_reasoning_done=on_reasoning_done,
            on_model_start=on_model_start,
            on_model_done=on_model_done,
            on_round_reset=on_round_reset,
            on_tool_event=on_tool_event,
        )

    def _build_provider_client(self) -> Any:
        if self.provider_type == _CLAUDE_PROVIDER_TYPE:
            return ClaudeRESTClient(
                self.provider_api_base_url or "https://api.anthropic.com/v1",
                self.provider_api_key,
            )

        if self.provider_type == _GEMINI_PROVIDER_TYPE:
            return GeminiRESTClient(
                self.provider_api_base_url
                or "https://generativelanguage.googleapis.com/v1beta",
                self.provider_api_key,
            )

        from openai import OpenAI

        client_kwargs: dict[str, Any] = {
            "api_key": self.provider_api_key or os.getenv("OPENAI_API_KEY") or "not-needed",
        }
        if self.provider_api_base_url:
            client_kwargs["base_url"] = self.provider_api_base_url
        return OpenAI(**client_kwargs)

    def _build_adapter(self) -> Any:
        if self.provider_type == _CHAT_PROVIDER_TYPE:
            return ChatCompletionsAdapter(self.client)
        if self.provider_type == _CLAUDE_PROVIDER_TYPE:
            return ClaudeAdapter(self.client)
        if self.provider_type == _GEMINI_PROVIDER_TYPE:
            return GeminiAdapter(self.client)

        return ResponsesAdapter(
            self.client,
            instructions=lambda: self.instructions,
            request_input=self._request_input,
            tools=lambda: self.tools.schemas,
            sanitize_text=sanitize_text,
            sanitize_value=sanitize_value,
        )

    def _stream_response(
        self,
        *,
        on_text_delta: Callable[[str], None] | None = None,
        on_reasoning_start: Callable[[], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        on_reasoning_done: Callable[[], None] | None = None,
        **request: Any,
    ) -> StreamResult:
        if self.provider_type == _RESPONSES_PROVIDER_TYPE:
            return self.adapter.stream_response(
                on_text_delta=on_text_delta,
                on_reasoning_start=on_reasoning_start,
                on_reasoning_delta=on_reasoning_delta,
                on_reasoning_done=on_reasoning_done,
                **request,
            )

        provider_request, context = self._normalize_provider_request(request)
        return self._stream_adapter_response(
            provider_request=provider_request,
            context=context,
            on_text_delta=on_text_delta,
            on_reasoning_start=on_reasoning_start,
            on_reasoning_delta=on_reasoning_delta,
            on_reasoning_done=on_reasoning_done,
        )

    def _normalize_provider_request(
        self,
        request: Mapping[str, Any],
    ) -> tuple[dict[str, Any], ProviderRequestContext | None]:
        if "input" in request:
            context = self._context_from_legacy_input_request(request)
            return self.adapter.build_request(context), context

        return dict(request), None

    def _stream_adapter_response(
        self,
        *,
        provider_request: dict[str, Any],
        context: ProviderRequestContext | None,
        on_text_delta: Callable[[str], None] | None,
        on_reasoning_start: Callable[[], None] | None,
        on_reasoning_delta: Callable[[str], None] | None,
        on_reasoning_done: Callable[[], None] | None,
    ) -> StreamResult:
        output_chunks: list[str] = []
        function_calls: list[BridgedFunctionCall] = []
        canonical_items: list[Any] = []
        final_output_text = ""
        finish_reason: str | None = None

        for event in self.adapter.stream_response(provider_request, context):
            if isinstance(event, TextDeltaEvent):
                safe_delta = sanitize_text(event.delta)
                if not safe_delta:
                    continue
                output_chunks.append(safe_delta)
                if on_text_delta is not None:
                    on_text_delta(safe_delta)
                continue

            if isinstance(event, ReasoningStartEvent):
                if on_reasoning_start is not None:
                    on_reasoning_start()
                continue

            if isinstance(event, ReasoningDeltaEvent):
                safe_delta = sanitize_text(event.delta)
                if safe_delta and on_reasoning_delta is not None:
                    on_reasoning_delta(safe_delta)
                continue

            if isinstance(event, ReasoningDoneEvent):
                if on_reasoning_done is not None:
                    on_reasoning_done()
                continue

            if isinstance(event, ToolCallReadyEvent):
                raw_arguments = sanitize_text(
                    event.raw_arguments
                    or json.dumps(event.arguments, ensure_ascii=False)
                )
                function_calls.append(
                    BridgedFunctionCall(
                        name=sanitize_text(event.name),
                        arguments=raw_arguments or "{}",
                        call_id=sanitize_text(event.call_id or ""),
                    )
                )
                continue

            if isinstance(event, ProviderDoneEvent):
                final_output_text = sanitize_text(event.output_text)
                finish_reason = getattr(event, "finish_reason", None)
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

    def _request_input(self, turn_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sanitize_value(
            [
                self._message(self.context_role, self.instructions),
                *self.history,
                *turn_items,
            ]
        )

    def _build_agent_core_request(
        self,
        turn_items: list[dict[str, Any]],
        request_model: str,
        request_reasoning_effort: str | None,
    ) -> dict[str, Any]:
        context = self._build_turn_request_context(
            turn_items,
            request_model=request_model,
            request_reasoning_effort=request_reasoning_effort,
        )
        return self.adapter.build_request(context)

    def _build_turn_request_context(
        self,
        turn_items: Sequence[dict[str, Any]],
        *,
        request_model: str,
        request_reasoning_effort: str | None,
    ) -> ProviderRequestContext:
        transcript_items = self._request_history()
        prompt_blocks = tuple(self._prompt_blocks())
        provider_config: dict[str, Any] = self._provider_config()

        if self.provider_type == _RESPONSES_PROVIDER_TYPE:
            prompt_message = self._message(self.context_role, self.instructions)
            transcript_items = [prompt_message, *transcript_items]
            prompt_blocks = ()
            provider_config["instructions"] = ""
        else:
            self._assert_supported_content_parts([*transcript_items, *turn_items])

        return ProviderRequestContext(
            prompt_blocks=prompt_blocks,
            transcript=tuple(sanitize_value(transcript_items)),
            current_turn=tuple(sanitize_value(list(turn_items))),
            tools=tuple(self.tools.schemas),
            provider_config=provider_config,
            model=request_model,
            reasoning_effort=request_reasoning_effort,
            metadata={"provider_id": self.provider_id},
        )

    def _request_history(self) -> list[dict[str, Any]]:
        limit = self.settings.context_message_limit
        if limit is None or limit <= 0:
            return list(self.history)
        return list(self.history[-limit:])

    def _provider_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {}
        if self.settings.temperature is not None:
            config["temperature"] = self.settings.temperature
        if self.settings.top_p is not None:
            if self.provider_type == _GEMINI_PROVIDER_TYPE:
                config["topP"] = self.settings.top_p
            else:
                config["top_p"] = self.settings.top_p
        return config

    def _context_from_legacy_input_request(
        self,
        request: Mapping[str, Any],
    ) -> ProviderRequestContext:
        raw_input = request.get("input")
        items = list(raw_input) if isinstance(raw_input, list) else []
        prompt_blocks: list[PromptBlock] = []
        transcript_items: list[dict[str, Any]] = []

        instructions = sanitize_text(request.get("instructions") or "").strip()
        if instructions:
            prompt_blocks.append(
                PromptBlock(
                    kind=self._prompt_block_kind(),
                    text=instructions,
                    source="legacy_instructions",
                )
            )

        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = sanitize_text(item.get("type") or "").strip()
            role = sanitize_text(item.get("role") or "").strip()
            if item_type == "message" and role in {"system", "developer"}:
                text = self._message_text(item.get("content"))
                if text:
                    prompt_blocks.append(
                        PromptBlock(
                            kind=role if role in {"system", "developer"} else "developer",
                            text=text,
                            source="legacy_input",
                        )
                    )
                continue
            transcript_items.append(sanitize_value(item))

        self._assert_supported_content_parts(transcript_items)

        reasoning = request.get("reasoning")
        reasoning_effort = None
        if isinstance(reasoning, Mapping):
            reasoning_effort = sanitize_text(reasoning.get("effort") or "").strip() or None

        provider_config = self._provider_config()
        provider_config.update(
            {
                key: sanitize_value(value)
                for key, value in request.items()
                if key not in {"input", "model", "tools", "reasoning", "instructions"}
            }
        )

        return ProviderRequestContext(
            prompt_blocks=tuple(prompt_blocks),
            transcript=tuple(transcript_items),
            current_turn=(),
            tools=tuple(sanitize_value(request.get("tools")) if isinstance(request.get("tools"), list) else self.tools.schemas),
            provider_config=provider_config,
            model=sanitize_text(request.get("model") or "").strip() or self.settings.model,
            reasoning_effort=reasoning_effort,
            metadata={"provider_id": self.provider_id, "source": "legacy_input"},
        )

    def _prompt_blocks(self) -> list[PromptBlock]:
        return [
            PromptBlock(
                kind=self._prompt_block_kind(),
                text=self.instructions,
                source="simple_agent",
            )
        ]

    def _prompt_block_kind(self) -> str:
        return self.context_role if self.context_role in {"system", "developer"} else "developer"

    def _assert_supported_content_parts(self, items: Sequence[Any]) -> None:
        allowed_types = _NON_RESPONSE_ALLOWED_PARTS.get(self.provider_type)
        if allowed_types is None:
            return

        for item in items:
            if not isinstance(item, Mapping):
                continue
            if sanitize_text(item.get("type") or "").strip() != "message":
                continue

            content = item.get("content")
            if not isinstance(content, list):
                continue

            for part in content:
                if isinstance(part, str):
                    continue
                if not isinstance(part, Mapping):
                    continue

                part_type = sanitize_text(part.get("type") or "").strip()
                if part_type in allowed_types:
                    continue

                provider_label = {
                    _CHAT_PROVIDER_TYPE: "Chat Completions",
                    _CLAUDE_PROVIDER_TYPE: "Claude",
                    _GEMINI_PROVIDER_TYPE: "Gemini",
                }.get(self.provider_type, self.provider_type)
                raise RuntimeError(
                    f"{provider_label} 当前只支持文本"
                    if part_type == "input_file"
                    else f"{provider_label} 当前还不支持这种消息内容类型：{part_type or 'unknown'}"
                )

    def _message_text(self, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return sanitize_text(content)
        if isinstance(content, Mapping):
            text = content.get("text")
            return sanitize_text(text or "")
        if isinstance(content, list):
            text_parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    text_parts.append(sanitize_text(part))
                    continue
                if not isinstance(part, Mapping):
                    continue
                part_type = sanitize_text(part.get("type") or "").strip()
                if part_type in _TEXT_PART_TYPES:
                    text = sanitize_text(part.get("text") or "")
                    if text:
                        text_parts.append(text)
            return "".join(text_parts)
        return sanitize_text(content)

    def _make_user_message(
        self,
        text: str,
        attachments: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        return self._message("user", text, attachments=attachments)

    def _make_assistant_message(self, text: str) -> dict[str, Any]:
        return self._message("assistant", text)

    def _fallback_to_developer_context(self) -> None:
        self.context_role = "developer"

    @staticmethod
    def _should_fallback_to_developer(exc: Exception) -> bool:
        message = str(exc).lower()
        return "system messages are not allowed" in message

    @staticmethod
    def _message(
        role: str,
        text: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        safe_text = sanitize_text(text)
        if not attachments:
            return {
                "type": "message",
                "role": role,
                "content": safe_text,
            }

        content: list[dict[str, Any]] = []

        if safe_text:
            content.append(
                {
                    "type": "input_text",
                    "text": safe_text,
                }
            )
        content.extend(sanitize_value(attachments))

        return {
            "type": "message",
            "role": role,
            "content": content,
        }

    @staticmethod
    def _preview(result: str, limit: int = 120) -> str:
        compact = sanitize_text(result).replace("\n", " ")
        if len(compact) <= limit:
            return compact
        return f"{compact[: limit - 3]}..."
