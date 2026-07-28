"""Why a headless agent run submitted no valid answer, rendered as its error message.

Carries only what the run log's detail tier does not already put in front of a
reader: the model's own tool_call count against the handler's, the tool_result
text, and the terminal error.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

_TOOL_CALL_KIND = "tool_call"
_TOOL_RESULT_KIND = "tool_result"
_ERROR_KIND = "error"

# Budgets for the rendered message. It goes into an exception a CI reader sees
# inline, so it has to stay short. A rejected call names the offending field at
# the head of its result, and the first rejection explains the run, so keeping
# the leading characters of the leading results loses least. Every cut is
# announced in the rendering.
_MAX_TOOL_RESULT_CHARS = 800
_MAX_TOOL_RESULTS = 3

_ABSENT = "(none emitted)"


class AgentRunDiagnostics(BaseModel):
    """What the engine emitted during a run that submitted no valid answer."""

    model_config = ConfigDict(frozen=True)

    target_model: str
    tool_name: str
    # calls_emitted counts tool_call events the MODEL emitted; handler_invocations
    # counts times the Python tool function actually ran. A gap between them means
    # the calls were rejected between the model and the handler.
    calls_emitted: int
    handler_invocations: int
    handler_issues: tuple[str, ...]
    tool_results: tuple[str, ...]
    tool_results_omitted: int
    terminal_error: str | None

    def render(self) -> str:
        lines = [
            f"agent submitted no valid {self.target_model}.",
            f"{self.tool_name}: model emitted {self.calls_emitted} tool_call event(s); "
            f"{self.handler_invocations} reached the handler.",
            f"handler issues: {list(self.handler_issues)}",
            f"terminal error: {self.terminal_error or _ABSENT}",
        ]
        lines.extend(self._render_tool_results())
        return "\n".join(lines)

    def _render_tool_results(self) -> list[str]:
        if not self.tool_results:
            return [f"tool results: {_ABSENT}"]
        shown = len(self.tool_results)
        total = shown + self.tool_results_omitted
        lines = [f"tool results ({shown} of {total} shown):"]
        lines.extend(f"  [{i}] {text}" for i, text in enumerate(self.tool_results, 1))
        return lines


def summarize_run(
    events: list[dict[str, Any]],
    *,
    target_model: str,
    tool_name: str,
    handler_invocations: int,
    handler_issues: list[str],
) -> AgentRunDiagnostics:
    results = [_truncate(text, _MAX_TOOL_RESULT_CHARS) for text in _find_tool_results(events)]
    return AgentRunDiagnostics(
        target_model=target_model,
        tool_name=tool_name,
        calls_emitted=_count_tool_calls(events, tool_name),
        handler_invocations=handler_invocations,
        handler_issues=tuple(handler_issues),
        tool_results=tuple(results[:_MAX_TOOL_RESULTS]),
        tool_results_omitted=max(len(results) - _MAX_TOOL_RESULTS, 0),
        terminal_error=_find_terminal_error(events),
    )


def _count_tool_calls(events: list[dict[str, Any]], tool_name: str) -> int:
    calls = _find_events(events, _TOOL_CALL_KIND)
    return sum(1 for event in calls if event.get("name") == tool_name)


def _find_tool_results(events: list[dict[str, Any]]) -> list[str]:
    # A tool_result event carries no tool name, but the run allows exactly one
    # tool, so every result here is that tool's.
    results = _find_events(events, _TOOL_RESULT_KIND)
    return [str(event.get("content", "")) for event in results]


def _find_terminal_error(events: list[dict[str, Any]]) -> str | None:
    errors = [str(event.get("text", "")) for event in _find_events(events, _ERROR_KIND)]
    return errors[-1] if errors else None


def _find_events(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event for event in events if event.get("kind") == kind]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]} …[truncated: {limit} of {len(text)} chars shown]"
