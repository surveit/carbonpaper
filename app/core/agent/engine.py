"""What every chat engine shares: the turn contract, and how a turn's blocks become events and parts."""
from __future__ import annotations

import json
from typing import Any, Callable, Protocol

from pydantic import ValidationError

from app.core.agent.store import OFFER_NEXT_STEPS, NextSteps
from app.core.agent.usage import LlmUsage
from app.core.ids import ID
from app.core.json_types import JsonDict


class ChatEngine(Protocol):
    last_usage: LlmUsage | None

    async def stream_turn(
        self,
        prompt: str,
        *,
        message_history: Any,
        emit: Callable[[dict[str, Any]], None],
        resume: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]: ...


class TurnRecorder:
    """Each record_* emits one event to the page and stores the same block as a transcript part."""

    def __init__(
        self, *, emit: Callable[[dict[str, Any]], None], tool_labels: dict[str, str],
    ) -> None:
        self._emit = emit
        self._tool_labels = tool_labels
        self._parts: list[dict[str, Any]] = []
        self._hidden_tool_result_ids: set[ID] = set()

    def record_thinking(self, text: str) -> None:
        # A redacted or signature-only block carries no text; it would open an empty disclosure.
        if text.strip():
            self._record("thinking", {"text": text})

    def record_text(self, text: str) -> None:
        self._record("text", {"text": text})

    def record_tool_call(self, tool_use_id: ID, name: str, arguments: dict[str, Any]) -> None:
        offered = read_offered_steps(name, arguments)
        if offered is not None:
            # Its own part type, so the page draws buttons and never shows the call behind them.
            self._hidden_tool_result_ids.add(tool_use_id)
            self._record("offer", {"options": render_offered_options(offered)})
            return
        if name == "submit_answer":
            self._hidden_tool_result_ids.add(tool_use_id)
        self._record("tool_call", {
            "name": name,
            "args": json.dumps(arguments, default=str),
            "label": self._tool_labels.get(name, name),
        })

    def record_tool_result(self, tool_use_id: ID, content: str, *, is_error: bool) -> None:
        hidden = tool_use_id in self._hidden_tool_result_ids
        self._hidden_tool_result_ids.discard(tool_use_id)
        if hidden and not is_error:
            return
        self._record("tool_result", {"content": content})

    def build_transcript(self, prompt: str) -> list[dict[str, Any]]:
        return [
            {"role": "user", "parts": [{"type": "text", "text": prompt}]},
            {"role": "assistant", "parts": self._parts},
        ]

    def _record(self, kind: str, body: dict[str, Any]) -> None:
        self._emit({"kind": kind, **body})
        self._parts.append({"type": kind, **body})


def render_offered_options(offered: NextSteps) -> list[JsonDict]:
    """Both destinations are JSON: the SSE wire, and the stored transcript."""
    return [option.model_dump(mode="json") for option in offered.options]


def read_offered_steps(tool_name: str, payload: Any) -> NextSteps | None:
    """None for any other tool, and for arguments that do not validate — both draw as a tool row."""
    if tool_name != OFFER_NEXT_STEPS:
        return None
    try:
        return NextSteps.model_validate(payload)
    except ValidationError:
        return None
