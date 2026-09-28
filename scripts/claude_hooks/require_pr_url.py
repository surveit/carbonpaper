"""Claude Code Stop hook: a turn that changed a GitHub PR ends with that PR's URL. docs/claude-hooks.md"""
from __future__ import annotations

import json
import re
import sys
from typing import Any

# A JSON object Claude Code wrote (the hook input, or one transcript line); nothing checks its shape.
UncheckedClaudeCodeJson = dict[str, Any]

UNKNOWN_PR_URL = "https://github.com/<org>/<repo>/pull/<n>"

_GH_PR_WRITE = re.compile(
    r"\bgh\s+pr\s+(?:create|edit|comment|merge|ready|review|close|reopen)\b(?P<args>[^\n;&|]*)"
)
_GH_API_CALL = re.compile(r"\bgh\s+api\b[^\n;&|]*")
_WRITE_METHOD = re.compile(r"(?:-X|--method)[\s=]*(?:POST|PATCH|PUT|DELETE)\b", re.IGNORECASE)
_READ_METHOD = re.compile(r"(?:-X|--method)[\s=]*GET\b", re.IGNORECASE)
_FIELD_FLAG = re.compile(r"\s(?:-f|-F|--field|--raw-field|--input)[\s=]")
_PR_URL = re.compile(r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pull/(?P<number>\d+)")
_PULLS_PATH = re.compile(r"repos/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pulls/(?P<number>\d+)")
_PULLS_NUMBER = re.compile(r"pulls/(\d+)")
_POSITIONAL_NUMBER = re.compile(r"^\s+#?(\d+)\b")


def main() -> int:
    hook_input = _parse_json_object(sys.stdin.read())
    if hook_input is None or hook_input.get("stop_hook_active"):
        return 0
    entries = _read_transcript(hook_input.get("transcript_path"))
    if entries is None:
        return 0
    missing_urls = find_missing_pr_urls(entries)
    if missing_urls:
        reason = "End the turn with the PR URL: " + ", ".join(missing_urls)
        print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def find_missing_pr_urls(entries: list[UncheckedClaudeCodeJson]) -> list[str]:
    turn = _find_last_turn(entries)
    refs_per_write = _find_pr_writes(turn)
    urls = _pick_best_url_per_pr([ref for refs in refs_per_write for ref in refs])
    final_numbers = {int(match["number"]) for match in _PR_URL.finditer(_find_final_text(turn))}
    missing = [url for number, url in sorted(urls.items()) if number not in final_numbers]
    if not final_numbers and any(not refs for refs in refs_per_write):
        missing.append(UNKNOWN_PR_URL)
    return missing


def is_pr_write(command: str) -> bool:
    return bool(_GH_PR_WRITE.search(command) or _find_api_pr_writes(command))


def find_pr_refs(command: str, result_text: str) -> list[tuple[int, str]]:
    pr_call_args = [call["args"] for call in _GH_PR_WRITE.finditer(command)]
    api_calls = " ".join(_find_api_pr_writes(command))
    numbers = [match.group(1) for args in pr_call_args if (match := _POSITIONAL_NUMBER.match(args))]
    numbers += _PULLS_NUMBER.findall(api_calls)
    # A gh api reply is JSON that can quote other PRs' URLs; a gh pr command prints only its own.
    located = [*_PULLS_PATH.finditer(api_calls), *(_PR_URL.finditer(result_text) if pr_call_args else [])]
    return [(int(number), _format_pr_url("<org>", "<repo>", number)) for number in numbers] + [
        (int(match["number"]), _format_pr_url(match["owner"], match["repo"], match["number"]))
        for match in located
    ]


def _find_pr_writes(turn: list[UncheckedClaudeCodeJson]) -> list[list[tuple[int, str]]]:
    results = _collect_tool_results(turn)
    refs_per_write: list[list[tuple[int, str]]] = []
    for tool_use in _find_parts(turn, "assistant", "tool_use"):
        command = (tool_use.get("input") or {}).get("command")
        if tool_use.get("name") != "Bash" or not isinstance(command, str):
            continue
        command = command.replace("\\\n", " ")
        is_error, result_text = results.get(tool_use["id"], (False, ""))
        if is_pr_write(command) and not is_error:
            refs_per_write.append(find_pr_refs(command, result_text))
    return refs_per_write


def _find_api_pr_writes(command: str) -> list[str]:
    return [call for call in _GH_API_CALL.findall(command) if "pulls/" in call and _is_api_write(call)]


def _is_api_write(call: str) -> bool:
    if _WRITE_METHOD.search(call):
        return True
    return bool(_FIELD_FLAG.search(call)) and not _READ_METHOD.search(call)


def _pick_best_url_per_pr(refs: list[tuple[int, str]]) -> dict[int, str]:
    urls: dict[int, str] = {}
    for number, url in refs:
        if number not in urls or "<org>" in urls[number]:
            urls[number] = url
    return urls


def _format_pr_url(owner: str, repo: str, number: str) -> str:
    return f"https://github.com/{owner}/{repo}/pull/{number}"


def _find_last_turn(entries: list[UncheckedClaudeCodeJson]) -> list[UncheckedClaudeCodeJson]:
    starts = [index for index, entry in enumerate(entries) if _is_human_message(entry)]
    return entries[starts[-1] + 1 :] if starts else entries


# An isMeta line is text Claude Code injects, such as a system reminder; it starts no turn.
def _is_human_message(entry: UncheckedClaudeCodeJson) -> bool:
    if entry.get("type") != "user" or entry.get("isSidechain") or entry.get("isMeta"):
        return False
    if not isinstance((entry.get("message") or {}).get("content"), str):
        return False
    return "origin" not in entry or (entry["origin"] or {}).get("kind", "human") == "human"


def _collect_tool_results(turn: list[UncheckedClaudeCodeJson]) -> dict[str, tuple[bool, str]]:
    return {
        part["tool_use_id"]: (bool(part.get("is_error")), _read_text(part.get("content")))
        for part in _find_parts(turn, "user", "tool_result")
    }


def _find_final_text(turn: list[UncheckedClaudeCodeJson]) -> str:
    texts_since_last_tool_use: list[str] = []
    for part in _find_parts(turn, "assistant", "tool_use", "text"):
        if part.get("type") == "tool_use":
            texts_since_last_tool_use = []
        elif isinstance(part.get("text"), str):
            texts_since_last_tool_use.append(part["text"])
    return "\n".join(texts_since_last_tool_use)


def _find_parts(
    turn: list[UncheckedClaudeCodeJson], entry_type: str, *part_types: str
) -> list[UncheckedClaudeCodeJson]:
    return [
        part
        for entry in turn
        if entry.get("type") == entry_type and not entry.get("isSidechain")
        for part in _read_content_parts(entry)
        if isinstance(part, dict) and part.get("type") in part_types
    ]


def _read_content_parts(entry: UncheckedClaudeCodeJson) -> list[Any]:
    content = (entry.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def _read_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def _read_transcript(path: Any) -> list[UncheckedClaudeCodeJson] | None:
    if not isinstance(path, str):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as transcript:
            lines = transcript.readlines()
    except OSError:
        return None
    return [entry for entry in map(_parse_json_object, lines) if entry is not None]


def _parse_json_object(raw: str) -> UncheckedClaudeCodeJson | None:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


if __name__ == "__main__":
    sys.exit(main())
