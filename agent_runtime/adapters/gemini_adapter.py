from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any

from agent_runtime.adapters.base import (
    BaseAdapter,
    ProviderRequestContext,
    ToolSpec,
    reasoning_effort_token_budget,
)
from agent_runtime.core.canonical_types import CanonicalItem
from agent_runtime.core.stream_events import (
    AdapterStreamEvent,
    ProviderDoneEvent,
    ReasoningDeltaEvent,
    ReasoningDoneEvent,
    ReasoningStartEvent,
    TextDeltaEvent,
    ToolCallReadyEvent,
)


_MISSING = object()
_TOOL_CALL_ITEM_TYPES = {"tool_call", "function_call"}
_TOOL_RESULT_ITEM_TYPES = {"tool_result", "function_call_output"}


class GeminiAdapter(BaseAdapter[dict[str, Any]]):
    """Translate runtime requests and Gemini streams without importing Gemini SDKs."""

    provider_name = "gemini"

    def __init__(self, client: Any) -> None:
        self.client = client

    def build_request(self, context: ProviderRequestContext) -> dict[str, Any]:
        if context.model is None:
            raise ValueError("Gemini request requires a model")

        contents, metadata = self._compile_contents(context)
        if not contents:
            # Gemini requires at least one content item
            contents = [{"role": "user", "parts": [{"text": "..."}]}]

        request: dict[str, Any] = {
            "model": context.model,
            "contents": contents,
            "systemInstruction": self._compile_system_instruction(
                context.prompt_blocks
            ),
        }
        tools = self._normalize_tools(context.tools)
        if tools:
            request["tools"] = tools

        tool_config = _read_any(
            context.provider_config,
            ("toolConfig", "tool_config"),
            _MISSING,
        )
        if tool_config is not _MISSING:
            request["toolConfig"] = _to_plain_value(tool_config)

        generation_config = _read_any(
            context.provider_config,
            ("generationConfig", "generation_config"),
            _MISSING,
        )
        next_generation_config = _to_plain_value(generation_config) if generation_config is not _MISSING else {}
        if not isinstance(next_generation_config, dict):
            next_generation_config = {}
        if "temperature" in context.provider_config:
            next_generation_config["temperature"] = context.provider_config["temperature"]
        if "topP" in context.provider_config:
            next_generation_config["topP"] = context.provider_config["topP"]
        thinking_config = next_generation_config.get("thinkingConfig")
        if not isinstance(thinking_config, dict):
            thinking_config = {}
        if context.reasoning_effort or thinking_config:
            thinking_config["includeThoughts"] = True
            if "thinkingBudget" not in thinking_config:
                effort_budget = reasoning_effort_token_budget(context.reasoning_effort)
                if effort_budget is not None:
                    thinking_config["thinkingBudget"] = effort_budget
            next_generation_config["thinkingConfig"] = thinking_config
        if next_generation_config:
            request["generationConfig"] = next_generation_config

        request_metadata = dict(context.metadata)
        if metadata:
            request_metadata["tool_call_ids"] = metadata
        if request_metadata:
            request["metadata"] = request_metadata

        return request

    def stream_response(
        self,
        request: dict[str, Any],
        context: ProviderRequestContext | None = None,
    ) -> Iterable[AdapterStreamEvent]:
        del context

        output_chunks: list[str] = []
        tool_call_events: list[ToolCallReadyEvent] = []
        finish_reason: str | None = None
        usage: Mapping[str, Any] | None = None
        last_chunk: Any | None = None
        reasoning_active = False

        for chunk in self._iter_provider_stream(request):
            last_chunk = chunk
            finish_reason = self._extract_finish_reason(chunk) or finish_reason
            usage = self._extract_usage(chunk) or usage

            parts = self._extract_parts(chunk)
            if not parts:
                text = self._extract_chunk_text(chunk)
                if text:
                    output_chunks.append(text)
                    yield TextDeltaEvent(delta=text, provider_raw=chunk)
                continue

            for part in parts:
                text = self._extract_part_text(part)
                if text:
                    if self._is_thought_part(part):
                        if not reasoning_active:
                            reasoning_active = True
                            yield ReasoningStartEvent(provider_raw=chunk)
                        yield ReasoningDeltaEvent(delta=text, provider_raw=chunk)
                    else:
                        if reasoning_active:
                            reasoning_active = False
                            yield ReasoningDoneEvent(provider_raw=chunk)
                        output_chunks.append(text)
                        yield TextDeltaEvent(delta=text, provider_raw=chunk)

                function_call = self._extract_function_call(part)
                if function_call is None:
                    continue

                name = str(_read_any(function_call, ("name",), "") or "")
                raw_args_value = _read_any(
                    function_call,
                    ("args", "arguments"),
                    {},
                )
                arguments = _coerce_arguments(raw_args_value)
                raw_arguments = _raw_json_arguments(raw_args_value)
                call_index = len(tool_call_events)
                call_id = _stable_call_id(call_index, name, arguments)
                event = ToolCallReadyEvent(
                    name=name,
                    arguments=arguments,
                    call_id=call_id,
                    raw_arguments=raw_arguments,
                    index=call_index,
                    provider_raw=function_call,
                )
                tool_call_events.append(event)
                yield event

        if reasoning_active:
            yield ReasoningDoneEvent(provider_raw=last_chunk)

        yield ProviderDoneEvent(
            output_text="".join(output_chunks),
            finish_reason=finish_reason,
            usage=usage,
            canonical_items=self._done_canonical_items(
                "".join(output_chunks),
                tool_call_events,
            ),
            provider_raw=last_chunk,
        )

    def _compile_system_instruction(
        self,
        prompt_blocks: Sequence[Any],
    ) -> dict[str, Any]:
        texts = [
            str(text)
            for block in prompt_blocks
            if (text := _read_any(block, ("text",), "")) not in (None, "")
        ]
        if not texts:
            return {"parts": []}
        return {"parts": [{"text": "\n\n".join(texts)}]}

    def _compile_contents(
        self,
        context: ProviderRequestContext,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        contents: list[dict[str, Any]] = []
        metadata: list[dict[str, Any]] = []
        tool_names_by_call_id: dict[str, str] = {}

        for item, fallback_role in self._iter_context_items(context):
            role, parts = self._compile_canonical_item(
                item,
                fallback_role=fallback_role,
                metadata=metadata,
                tool_names_by_call_id=tool_names_by_call_id,
            )
            for part in parts:
                self._append_part(contents, role, part)

        return contents, metadata

    def _iter_context_items(
        self,
        context: ProviderRequestContext,
    ) -> Iterable[tuple[Any, str | None]]:
        for record in context.transcript:
            yield from self._iter_record_or_item(record)
        for item in context.current_turn:
            yield from self._iter_record_or_item(item)

    def _iter_record_or_item(self, value: Any) -> Iterable[tuple[Any, str | None]]:
        if _looks_like_canonical_item(value):
            yield value, None
            return

        fallback_role = _read_any(value, ("role",), None)
        canonical_items = _read_any(value, ("canonical_items",), None)
        if canonical_items:
            for item in canonical_items:
                yield item, fallback_role
            return

        text = _read_any(value, ("text",), "")
        if text not in (None, ""):
            yield {
                "type": "message",
                "role": fallback_role or "user",
                "content": text,
            }, fallback_role

    def _compile_canonical_item(
        self,
        item: Any,
        *,
        fallback_role: str | None,
        metadata: list[dict[str, Any]],
        tool_names_by_call_id: dict[str, str],
    ) -> tuple[str, list[dict[str, Any]]]:
        item_type = _read_any(item, ("type",), "message") or "message"

        if item_type in _TOOL_CALL_ITEM_TYPES:
            name = str(_read_any(item, ("name",), "") or "")
            arguments = _coerce_arguments(_read_any(item, ("arguments", "args"), {}))
            call_id = str(_read_any(item, ("call_id",), "") or "")
            if call_id and name:
                tool_names_by_call_id[call_id] = name
            if call_id:
                metadata.append(
                    {
                        "kind": "tool_call",
                        "index": len(metadata),
                        "name": name,
                        "call_id": call_id,
                    }
                )
            return "model", [{"functionCall": {"name": name, "args": arguments}}]

        if item_type in _TOOL_RESULT_ITEM_TYPES:
            call_id = str(_read_any(item, ("call_id",), "") or "")
            name = str(_read_any(item, ("name",), "") or "")
            if not name and call_id:
                name = tool_names_by_call_id.get(call_id, "")
            output = _read_any(item, ("output", "response", "content"), {})
            if call_id:
                metadata.append(
                    {
                        "kind": "tool_result",
                        "index": len(metadata),
                        "name": name,
                        "call_id": call_id,
                    }
                )
            return "user", [
                {
                    "functionResponse": {
                        "name": name,
                        "response": _normalize_function_response_payload(output),
                    }
                }
            ]

        role = _gemini_role(
            str(_read_any(item, ("role",), fallback_role or "user") or "user")
        )
        content = _read_any(item, ("content", "text"), "")
        return role, self._content_to_parts(content)

    def _content_to_parts(self, content: Any) -> list[dict[str, Any]]:
        if content is None:
            return []

        if isinstance(content, str):
            return [{"text": content}] if content else []

        if isinstance(content, Mapping):
            return [self._normalize_content_part(content)]

        if _is_sequence(content):
            parts: list[dict[str, Any]] = []
            for item in content:
                if item is None:
                    continue
                if isinstance(item, str):
                    if item:
                        parts.append({"text": item})
                    continue
                if isinstance(item, Mapping):
                    parts.append(self._normalize_content_part(item))
                    continue
                parts.append({"text": _json_text(item)})
            return parts

        return [{"text": _json_text(content)}]

    def _normalize_content_part(self, part: Mapping[str, Any]) -> dict[str, Any]:
        if "text" in part:
            return {"text": str(part.get("text") or "")}

        part_type = str(part.get("type") or "")
        if part_type in {"text", "input_text", "output_text"} and "text" in part:
            return {"text": str(part.get("text") or "")}

        function_call = part.get("functionCall", part.get("function_call"))
        if function_call is not None:
            name = str(_read_any(function_call, ("name",), "") or "")
            args = _coerce_arguments(
                _read_any(function_call, ("args", "arguments"), {})
            )
            return {"functionCall": {"name": name, "args": args}}

        function_response = part.get("functionResponse", part.get("function_response"))
        if function_response is not None:
            name = str(_read_any(function_response, ("name",), "") or "")
            response = _read_any(function_response, ("response", "output"), {})
            return {
                "functionResponse": {
                    "name": name,
                    "response": _normalize_function_response_payload(response),
                }
            }

        return {"text": _json_text(part)}

    @staticmethod
    def _append_part(
        contents: list[dict[str, Any]],
        role: str,
        part: dict[str, Any],
    ) -> None:
        if not part:
            return
        if contents and contents[-1].get("role") == role:
            contents[-1]["parts"].append(part)
            return
        contents.append({"role": role, "parts": [part]})

    def _normalize_tools(
        self,
        tools: Sequence[ToolSpec | Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        function_declarations: list[dict[str, Any]] = []
        native_tools: list[dict[str, Any]] = []

        for tool in tools:
            if isinstance(tool, ToolSpec):
                function_declarations.append(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": _to_plain_value(tool.parameters),
                    }
                )
                continue

            tool_mapping = dict(tool)
            declarations = _read_any(
                tool_mapping,
                ("functionDeclarations", "function_declarations"),
                None,
            )
            if declarations is not None:
                function_declarations.extend(
                    self._normalize_function_declaration(declaration)
                    for declaration in _as_list(declarations)
                )
                continue

            declaration = self._responses_tool_to_declaration(tool_mapping)
            if declaration is not None:
                function_declarations.append(declaration)
                continue

            native_tools.append(_to_plain_value(tool_mapping))

        gemini_tools = list(native_tools)
        if function_declarations:
            gemini_tools.append({"functionDeclarations": function_declarations})
        return gemini_tools

    def _responses_tool_to_declaration(
        self,
        tool: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        function = _read_any(tool, ("function",), None)
        source = function if isinstance(function, Mapping) else tool

        if tool.get("type") != "function" and "name" not in source:
            return None

        return self._normalize_function_declaration(source)

    @staticmethod
    def _normalize_function_declaration(declaration: Any) -> dict[str, Any]:
        return {
            "name": str(_read_any(declaration, ("name",), "") or ""),
            "description": str(
                _read_any(declaration, ("description",), "") or ""
            ),
            "parameters": _to_plain_value(
                _read_any(declaration, ("parameters",), {})
            ),
        }

    def _iter_provider_stream(self, request: Mapping[str, Any]) -> Iterable[Any]:
        stream = self._start_provider_stream(request)
        yielded_chunk = False

        if hasattr(stream, "__enter__"):
            with stream as entered_stream:
                for chunk in entered_stream:
                    yielded_chunk = True
                    yield chunk
            if not yielded_chunk:
                fallback_response = self._start_provider_generate_content(request)
                if fallback_response is not None:
                    yield fallback_response
            return

        for chunk in stream:
            yielded_chunk = True
            yield chunk
        if not yielded_chunk:
            fallback_response = self._start_provider_generate_content(request)
            if fallback_response is not None:
                yield fallback_response

    def _start_provider_stream(self, request: Mapping[str, Any]) -> Any:
        models = getattr(self.client, "models", None)
        generate_content_stream = getattr(models, "generate_content_stream", None)
        if callable(generate_content_stream):
            return generate_content_stream(
                model=request["model"],
                contents=request.get("contents", []),
                config=self._sdk_config_from_request(request),
            )

        stream_generate_content = getattr(self.client, "stream_generate_content", None)
        if callable(stream_generate_content):
            return stream_generate_content(**_provider_request_kwargs(request))

        client_generate_content_stream = getattr(
            self.client,
            "generate_content_stream",
            None,
        )
        if callable(client_generate_content_stream):
            return client_generate_content_stream(**_provider_request_kwargs(request))

        raise RuntimeError("GeminiAdapter client does not expose a stream method")

    def _start_provider_generate_content(
        self,
        request: Mapping[str, Any],
    ) -> Any | None:
        models = getattr(self.client, "models", None)
        generate_content = getattr(models, "generate_content", None)
        if callable(generate_content):
            return generate_content(
                model=request["model"],
                contents=request.get("contents", []),
                config=self._sdk_config_from_request(request),
            )

        client_generate_content = getattr(self.client, "generate_content", None)
        if callable(client_generate_content):
            return client_generate_content(**_provider_request_kwargs(request))

        return None

    @staticmethod
    def _sdk_config_from_request(request: Mapping[str, Any]) -> dict[str, Any]:
        config: dict[str, Any] = {}

        tools = request.get("tools")
        if tools:
            config["tools"] = tools

        system_instruction = request.get("systemInstruction")
        if _has_parts(system_instruction):
            config["system_instruction"] = system_instruction

        if "toolConfig" in request:
            config["tool_config"] = request["toolConfig"]

        generation_config = request.get("generationConfig")
        if isinstance(generation_config, Mapping):
            config.update(dict(generation_config))

        return config

    def _extract_parts(self, chunk: Any) -> list[Any]:
        parts: list[Any] = []
        candidates = _read_any(chunk, ("candidates",), None)
        for candidate in _as_list(candidates):
            content = _read_any(candidate, ("content",), None)
            parts.extend(_as_list(_read_any(content, ("parts",), None)))

        if parts:
            return parts

        content = _read_any(chunk, ("content",), None)
        parts.extend(_as_list(_read_any(content, ("parts",), None)))
        if parts:
            return parts

        return _as_list(_read_any(chunk, ("parts",), None))

    @staticmethod
    def _extract_part_text(part: Any) -> str:
        text = _read_any(part, ("text",), "")
        if text in (None, ""):
            return ""
        return str(text)

    @staticmethod
    def _is_thought_part(part: Any) -> bool:
        return bool(_read_any(part, ("thought",), False))

    @staticmethod
    def _extract_chunk_text(chunk: Any) -> str:
        # 1. Try candidates[0].content.parts[0].text
        candidates = _as_list(_read_any(chunk, ("candidates",), None))
        if candidates:
            content = _read_any(candidates[0], ("content",), None)
            parts = _as_list(_read_any(content, ("parts",), None))
            if parts:
                text = _read_any(parts[0], ("text",), None)
                if text:
                    return str(text)

        # 2. Try top-level text (some proxy/SSE formats)
        text = _read_any(chunk, ("text",), "")
        if text not in (None, ""):
            return str(text)

        return ""

    @staticmethod
    def _extract_function_call(part: Any) -> Any | None:
        return _read_any(part, ("functionCall", "function_call"), None)

    @staticmethod
    def _extract_finish_reason(chunk: Any) -> str | None:
        candidates = _as_list(_read_any(chunk, ("candidates",), None))
        for candidate in candidates:
            reason = _read_any(candidate, ("finishReason", "finish_reason"), None)
            if reason:
                return str(reason)

        reason = _read_any(chunk, ("finishReason", "finish_reason"), None)
        if reason:
            return str(reason)
        return None

    @staticmethod
    def _extract_usage(chunk: Any) -> Mapping[str, Any] | None:
        usage = _read_any(chunk, ("usageMetadata", "usage_metadata"), None)
        if usage is None:
            return None
        plain_usage = _to_plain_value(usage)
        if isinstance(plain_usage, Mapping):
            return dict(plain_usage)
        return None

    @staticmethod
    def _done_canonical_items(
        output_text: str,
        tool_call_events: Sequence[ToolCallReadyEvent],
    ) -> tuple[CanonicalItem, ...]:
        items: list[CanonicalItem] = []
        if output_text:
            items.append(
                CanonicalItem(
                    type="message",
                    role="assistant",
                    content=output_text,
                )
            )

        for event in tool_call_events:
            items.append(
                CanonicalItem(
                    type="tool_call",
                    role="assistant",
                    name=event.name,
                    call_id=event.call_id or "",
                    arguments=dict(event.arguments),
                )
            )

        return tuple(items)


def _read_any(value: Any, keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        result = _read(value, key, _MISSING)
        if result is not _MISSING:
            return result
    return default


def _read(value: Any, key: str, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _looks_like_canonical_item(value: Any) -> bool:
    item_type = _read_any(value, ("type",), None)
    if item_type in {"message", *_TOOL_CALL_ITEM_TYPES, *_TOOL_RESULT_ITEM_TYPES}:
        return True
    if (
        _read_any(value, ("role",), _MISSING) is not _MISSING
        and _read_any(value, ("content",), _MISSING) is not _MISSING
    ):
        return True
    return (
        _read_any(value, ("name",), _MISSING) is not _MISSING
        and _read_any(value, ("arguments", "output"), _MISSING) is not _MISSING
    )


def _gemini_role(role: str) -> str:
    return "model" if role == "assistant" else "user"


def _coerce_arguments(value: Any) -> dict[str, Any]:
    if value is None:
        return {}

    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
        return {}

    if isinstance(value, Mapping):
        return dict(_to_plain_value(value))

    return {}


def _raw_json_arguments(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(_to_plain_value(value), ensure_ascii=False, sort_keys=True)


def _stable_call_id(index: int, name: str, arguments: Mapping[str, Any]) -> str:
    digest_source = json.dumps(
        {"index": index, "name": name, "arguments": _to_plain_value(arguments)},
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:12]
    return f"gemini_call_{index}_{digest}"


def _provider_request_kwargs(request: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in request.items() if key != "metadata"}


def _has_parts(value: Any) -> bool:
    parts = _read_any(value, ("parts",), None)
    return bool(_as_list(parts))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, (str, bytes, Mapping)):
        return [value]
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def _is_sequence(value: Any) -> bool:
    return not isinstance(value, (str, bytes, Mapping)) and isinstance(
        value,
        Iterable,
    )


def _to_plain_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _to_plain_value(item) for key, item in value.items()}
    if _is_sequence(value):
        return [_to_plain_value(item) for item in value]
    return value


def _normalize_function_response_payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return {"result": value}
        return _normalize_function_response_payload(parsed)

    plain_value = _to_plain_value(value)
    if isinstance(plain_value, Mapping):
        return {str(key): _to_plain_value(item) for key, item in plain_value.items()}

    return {"result": plain_value}


def _json_text(value: Any) -> str:
    return json.dumps(_to_plain_value(value), ensure_ascii=False, default=str)


__all__ = ["GeminiAdapter"]
