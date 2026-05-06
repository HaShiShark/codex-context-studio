from __future__ import annotations

import json
from collections.abc import Callable, MutableSequence, Sequence
from typing import Any, Generic, Protocol, TypeVar


class StreamResultLike(Protocol):
    output_text: str
    function_calls: list[Any]
    canonical_items: Sequence[Any]


class ToolExecutionLike(Protocol):
    output_text: str
    display_title: str
    display_detail: str
    display_result: str
    status: str


ToolEventT = TypeVar("ToolEventT")


class AgentCore(Generic[ToolEventT]):
    """Owns the provider-agnostic tool loop for one agent turn.

    The request and stream callbacks are deliberately injected so the current
    Responses implementation keeps its wire format while future adapters can
    plug in at the same boundary.
    """

    def __init__(
        self,
        *,
        max_tool_rounds: int,
        default_model: str,
        history: MutableSequence[dict[str, Any]],
        build_request: Callable[[list[dict[str, Any]], str, str | None], dict[str, Any]],
        stream_response: Callable[..., StreamResultLike],
        execute_tool: Callable[[str, Any], ToolExecutionLike],
        make_user_message: Callable[[str, list[dict[str, Any]] | None], dict[str, Any]],
        make_assistant_message: Callable[[str], dict[str, Any]],
        tool_event_factory: Callable[..., ToolEventT],
        sanitize_text: Callable[[Any], str],
        sanitize_value: Callable[[Any], Any],
        preview_text: Callable[[str], str],
        should_fallback_to_developer: Callable[[Exception], bool],
        fallback_to_developer: Callable[[], None],
        check_cancelled: Callable[[], None] | None = None,
    ) -> None:
        self.max_tool_rounds = max_tool_rounds
        self.default_model = default_model
        self.history = history
        self.build_request = build_request
        self.stream_response = stream_response
        self.execute_tool = execute_tool
        self.make_user_message = make_user_message
        self.make_assistant_message = make_assistant_message
        self.tool_event_factory = tool_event_factory
        self.sanitize_text = sanitize_text
        self.sanitize_value = sanitize_value
        self.preview_text = preview_text
        self.should_fallback_to_developer = should_fallback_to_developer
        self.fallback_to_developer = fallback_to_developer
        self.check_cancelled = check_cancelled

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
        on_tool_event: Callable[[ToolEventT], None] | None = None,
    ) -> tuple[str, list[ToolEventT]]:
        tool_events: list[ToolEventT] = []
        turn_items: list[dict[str, Any]] = [
            self.make_user_message(
                self.sanitize_text(user_message),
                attachments,
            )
        ]
        request_model = self.sanitize_text(model or self.default_model)
        request_reasoning_effort = (
            self.sanitize_text(reasoning_effort).strip()
            if reasoning_effort
            else None
        )

        while True:
            self._check_cancelled()
            if on_model_start is not None:
                on_model_start()
            try:
                response = self._stream_next_response(
                    turn_items=turn_items,
                    request_model=request_model,
                    request_reasoning_effort=request_reasoning_effort,
                    on_text_delta=on_text_delta,
                    on_reasoning_start=on_reasoning_start,
                    on_reasoning_delta=on_reasoning_delta,
                    on_reasoning_done=on_reasoning_done,
                )
            finally:
                if on_model_done is not None:
                    on_model_done()
            self._check_cancelled()

            if not response.function_calls:
                safe_output_text = self.sanitize_text(response.output_text)
                self._check_cancelled()
                self.history.extend(self.sanitize_value(turn_items))
                self.history.append(self.make_assistant_message(safe_output_text))
                return safe_output_text, tool_events

            if response.output_text and on_round_reset is not None:
                self._check_cancelled()
                on_round_reset()

            provider_items = list(getattr(response, "canonical_items", ()) or ())
            if provider_items:
                turn_items.extend(self.sanitize_value(provider_items))
            else:
                for call in response.function_calls:
                    turn_items.append(self._function_call_item(call))

            for call in response.function_calls:
                self._check_cancelled()
                tool_event = self._execute_tool_call(call, turn_items)
                self._check_cancelled()
                tool_events.append(tool_event)
                if on_tool_event is not None:
                    on_tool_event(tool_event)

    def _stream_next_response(
        self,
        *,
        turn_items: list[dict[str, Any]],
        request_model: str,
        request_reasoning_effort: str | None,
        on_text_delta: Callable[[str], None] | None,
        on_reasoning_start: Callable[[], None] | None,
        on_reasoning_delta: Callable[[str], None] | None,
        on_reasoning_done: Callable[[], None] | None,
    ) -> StreamResultLike:
        try:
            return self.stream_response(
                **self.build_request(
                    turn_items,
                    request_model,
                    request_reasoning_effort,
                ),
                on_text_delta=on_text_delta,
                on_reasoning_start=on_reasoning_start,
                on_reasoning_delta=on_reasoning_delta,
                on_reasoning_done=on_reasoning_done,
            )
        except Exception as exc:
            if not self.should_fallback_to_developer(exc):
                raise

            self.fallback_to_developer()
            return self.stream_response(
                **self.build_request(
                    turn_items,
                    request_model,
                    request_reasoning_effort,
                ),
                on_text_delta=on_text_delta,
                on_reasoning_start=on_reasoning_start,
                on_reasoning_delta=on_reasoning_delta,
                on_reasoning_done=on_reasoning_done,
            )

    def _execute_tool_call(
        self,
        call: Any,
        turn_items: list[dict[str, Any]],
    ) -> ToolEventT:
        safe_call_name = self.sanitize_text(getattr(call, "name", ""))
        safe_call_id = self.sanitize_text(getattr(call, "call_id", ""))
        safe_call_arguments = self.sanitize_text(
            getattr(call, "arguments", "") or "{}"
        )

        arguments: Any = {}
        try:
            arguments = json.loads(safe_call_arguments)
            execution = self.execute_tool(safe_call_name, arguments)
            result = self.sanitize_text(execution.output_text)
        except json.JSONDecodeError as exc:
            arguments = {}
            result = json.dumps(
                {"error": f"invalid tool arguments: {exc.msg}"},
                ensure_ascii=False,
            )
            execution = None

        safe_arguments = self.sanitize_value(arguments)
        tool_event = self.tool_event_factory(
            name=safe_call_name,
            arguments=safe_arguments,
            output_preview=self.preview_text(result),
            raw_output=result,
            display_title=execution.display_title if execution else safe_call_name,
            display_detail=execution.display_detail if execution else "",
            display_result=execution.display_result if execution else "",
            status=execution.status if execution else "error",
        )

        turn_items.append(
            {
                "type": "function_call_output",
                "call_id": safe_call_id,
                "output": result,
            }
        )

        return tool_event

    def _function_call_item(self, call: Any) -> dict[str, Any]:
        return {
            "type": "function_call",
            "call_id": self.sanitize_text(getattr(call, "call_id", "")),
            "name": self.sanitize_text(getattr(call, "name", "")),
            "arguments": self.sanitize_text(getattr(call, "arguments", "") or "{}"),
        }

    def _check_cancelled(self) -> None:
        if self.check_cancelled is not None:
            self.check_cancelled()


__all__ = [
    "AgentCore",
    "StreamResultLike",
    "ToolExecutionLike",
]
