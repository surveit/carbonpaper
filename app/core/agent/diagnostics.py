"""Why a headless agent run submitted no valid answer, rendered as its error message.

Carries only what the run log's detail tier does not already put in front of a
reader: the model's own tool_call count against the handler's, the tool_result
text, and the terminal error.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

_INIT_KIND = "init"
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
    # Read off the CLI's opening inventory: was this run's tool actually in the
    # turn's tool list, and how did each MCP server report itself. None where no
    # init arrived (an engine that reports none, or a turn that died first) —
    # distinct from False, which is the CLI saying the tool was not there.
    tool_advertised: bool | None = None
    mcp_servers: tuple[str, ...] = ()

    def render(self) -> str:
        lines = [
            f"agent submitted no valid {self.target_model}.",
            f"{self.tool_name}: model emitted {self.calls_emitted} tool_call event(s); "
            f"{self.handler_invocations} reached the handler.",
            f"handler issues: {list(self.handler_issues)}",
            f"terminal error: {self.terminal_error or _ABSENT}",
            self._render_availability(),
        ]
        lines.extend(self._render_tool_results())
        return "\n".join(lines)

    def _render_availability(self) -> str:
        """Whether the tool was on offer at all — the first thing to read when a
        run emitted no call: `advertised=no` is an environment fault (the MCP
        server did not connect), not the model declining to call it."""
        if self.tool_advertised is None:
            return "tool availability: (no init reported)"
        offered = "yes" if self.tool_advertised else "NO"
        servers = ", ".join(self.mcp_servers) if self.mcp_servers else _ABSENT
        return f"tool availability: {self.tool_name} advertised={offered}; mcp servers: {servers}"

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
        tool_advertised=_tool_advertised(events, tool_name),
        mcp_servers=tuple(_mcp_server_statuses(events)),
    )


def _tool_advertised(events: list[dict[str, Any]], tool_name: str) -> bool | None:
    """True/False from the init inventory, or None where none arrived. Matched on
    the BARE name: the CLI advertises the namespaced `mcp__<server>__<tool>`."""
    inits = _find_events(events, _INIT_KIND)
    if not inits:
        return None
    return any(
        str(advertised).rsplit("__", 1)[-1] == tool_name
        for init in inits
        for advertised in (init.get("tools") or [])
    )


def _mcp_server_statuses(events: list[dict[str, Any]]) -> list[str]:
    """`name=status` per MCP server the init inventory listed."""
    return [
        f"{server.get('name', '?')}={server.get('status', '?')}"
        for init in _find_events(events, _INIT_KIND)
        for server in (init.get("mcp_servers") or [])
        if isinstance(server, dict)
    ]


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
