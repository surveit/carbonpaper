from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.claude_hooks.replay_require_pr_url import StopCounts, count_stops
from scripts.claude_hooks.require_pr_url import UNKNOWN_PR_URL, find_missing_pr_urls, is_pr_write

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "claude_hooks" / "require_pr_url.py"
PR_7 = "https://github.com/acme/widgets/pull/7"
PR_9 = "https://github.com/acme/widgets/pull/9"


def _human(text: str) -> dict[str, Any]:
    return {"type": "user", "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}


def _task_notification(text: str) -> dict[str, Any]:
    return {"type": "user", "origin": {"kind": "task-notification"}, "message": {"role": "user", "content": text}}


def _peer_message(text: str) -> dict[str, Any]:
    peer = {"type": "user", "isMeta": True, "origin": {"kind": "peer"}}
    return {**peer, "message": {"role": "user", "content": text}}


def _human_with_image(text: str) -> dict[str, Any]:
    content = [{"type": "image", "source": {"type": "base64", "data": ""}}, {"type": "text", "text": text}]
    return {"type": "user", "origin": {"kind": "human"}, "message": {"role": "user", "content": content}}


def _stop_summary() -> dict[str, Any]:
    return {"type": "system", "subtype": "stop_hook_summary", "isSidechain": False}


def _bash(tool_use_id: str, command: str) -> dict[str, Any]:
    tool_use = {"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": {"command": command}}
    return {"type": "assistant", "message": {"role": "assistant", "content": [tool_use]}}


def _result(tool_use_id: str, output: str, is_error: bool = False) -> dict[str, Any]:
    result = {"type": "tool_result", "tool_use_id": tool_use_id, "content": output, "is_error": is_error}
    return {"type": "user", "message": {"role": "user", "content": [result]}}


def _say(text: str) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _run_hook(tmp_path: Path, entries: list[dict[str, Any]], stop_hook_active: bool = False) -> str:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    hook_input = {
        "session_id": "s1",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "hook_event_name": "Stop",
        "stop_hook_active": stop_hook_active,
    }
    completed = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(hook_input), capture_output=True, text=True, cwd=tmp_path
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


CREATED_PR_7 = [
    _human("open the PR"),
    _bash("t1", 'gh pr create --base main --title "Add widgets" --body-file body.md'),
    _result("t1", PR_7 + "\n"),
]


def test_fires_when_a_created_pr_is_missing_from_the_final_text(tmp_path: Path) -> None:
    stdout = _run_hook(tmp_path, [*CREATED_PR_7, _say("Opened the PR.")])
    assert json.loads(stdout) == {"decision": "block", "reason": f"End the turn with the PR URL: {PR_7}"}


def test_does_not_fire_when_the_final_text_carries_the_url(tmp_path: Path) -> None:
    assert _run_hook(tmp_path, [*CREATED_PR_7, _say(f"Opened {PR_7}")]) == ""


def test_does_not_fire_on_a_read_only_gh_api_call() -> None:
    turn = [
        _human("what did the reviewer say?"),
        _bash("t1", "gh api repos/acme/widgets/pulls/7/comments"),
        _bash("t2", "gh api -X GET repos/acme/widgets/pulls/7/comments -f per_page=100"),
        _say("Two comments, both on the test file."),
    ]
    assert find_missing_pr_urls(turn) == []


def test_does_not_fire_when_the_stop_hook_is_already_active(tmp_path: Path) -> None:
    assert _run_hook(tmp_path, [*CREATED_PR_7, _say("Opened the PR.")], stop_hook_active=True) == ""


def test_fires_on_the_one_pr_whose_url_is_missing_when_the_turn_changed_several() -> None:
    turn = [
        *CREATED_PR_7,
        _bash("t2", "gh api repos/acme/widgets/pulls/9/comments/55/replies -f body='Fixed in abc123'"),
        _result("t2", '{"id": 56}'),
        _say(f"Opened {PR_7} and replied on the other review thread."),
    ]
    assert find_missing_pr_urls(turn) == [PR_9]


def test_a_pr_the_command_only_mentions_is_not_asked_for() -> None:
    turn = [
        _human("open the follow-up"),
        _bash("t1", f'gh pr create --title "Follow up" --body "Follows {PR_9}"'),
        _result("t1", PR_7),
        _say(f"Opened {PR_7}"),
    ]
    assert find_missing_pr_urls(turn) == []


def test_a_pr_named_only_by_number_asks_for_its_url_with_the_repo_left_open() -> None:
    turn = [_human("retitle it"), _bash("t1", 'gh pr edit 12 --title "New"'), _result("t1", ""), _say("Done.")]
    assert find_missing_pr_urls(turn) == ["https://github.com/<org>/<repo>/pull/12"]


def test_a_pr_write_that_names_no_pr_asks_for_any_pr_url() -> None:
    turn = [_human("merge it"), _bash("t1", "gh pr merge --squash"), _result("t1", ""), _say("Merged.")]
    assert find_missing_pr_urls(turn) == [UNKNOWN_PR_URL]


def test_a_pr_write_in_an_earlier_turn_or_a_failed_call_does_not_count() -> None:
    turn = [
        *CREATED_PR_7,
        _say(f"Opened {PR_7}"),
        _human("now close the other one"),
        _bash("t2", "gh pr close 9"),
        _result("t2", "GraphQL: Could not resolve to a PullRequest", is_error=True),
        _say("That PR does not exist."),
    ]
    assert find_missing_pr_urls(turn) == []


@pytest.mark.parametrize(
    "opener",
    [
        _stop_summary(),
        _task_notification("Background task finished"),
        _peer_message("Review of the PR: approve"),
        _human_with_image("what is on this screen?"),
    ],
)
def test_a_stop_a_notification_a_peer_message_or_an_image_message_starts_a_new_turn(
    opener: dict[str, Any],
) -> None:
    turn = [*CREATED_PR_7, _say("Opened the PR."), opener, _say("Nothing to change.")]
    assert find_missing_pr_urls(turn) == []


def test_a_pr_named_by_url_or_by_a_trailing_number_asks_for_that_pr() -> None:
    turn = [
        _human("merge one, label the other"),
        _bash("t1", f"gh pr merge {PR_7} --squash"),
        _result("t1", ""),
        _bash("t2", "gh pr edit --add-label ready 9 && echo ok"),
        _result("t2", ""),
        _say("Done."),
    ]
    assert find_missing_pr_urls(turn) == [PR_7, "https://github.com/<org>/<repo>/pull/9"]


def test_count_stops_replays_the_hook_at_each_recorded_stop(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    entries = [*CREATED_PR_7, _say("Opened the PR."), _stop_summary(), _say("Anything else?"), _stop_summary()]
    (project / "session.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    assert count_stops(sorted(tmp_path.glob("*/*.jsonl"))) == StopCounts(1, 2, 1, 1)


@pytest.mark.parametrize(
    ("command", "writes"),
    [
        ("gh pr create --fill", True),
        ("cd /repo && gh pr comment 7 --body ok", True),
        ("gh pr review 7 --approve", True),
        ("gh api repos/acme/widgets/pulls/7 -X PATCH -f title=x", True),
        ("gh api --method POST repos/acme/widgets/pulls/7/reviews", True),
        ("gh api repos/acme/widgets/pulls/7/comments", False),
        ("gh api repos/acme/widgets/issues/7/comments -f body=x", False),
        ("gh pr view 7 --json title", False),
        ("git push -u origin feature", False),
    ],
)
def test_is_pr_write_matches_only_the_mutating_pull_request_commands(command: str, writes: bool) -> None:
    assert is_pr_write(command) is writes


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"transcript_path": "/no/such/file.jsonl"}'])
def test_unreadable_input_exits_zero_silently(stdin: str, tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True, cwd=tmp_path
    )
    assert (completed.returncode, completed.stdout, completed.stderr) == (0, "", "")
