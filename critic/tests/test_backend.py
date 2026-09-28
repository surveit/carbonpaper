from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from critic.backend import (
    BackendReplyError,
    BackendUnavailableError,
    ClaudeCliBackend,
    ModelReply,
    ModelRequest,
    parse_cli_result,
)
from critic.review import run_review
from critic.rubric import load_rubric
from critic.tests.fixture_data import (
    RUBRICS,
    load_diff_at_first_commit,
    load_labels,
    read_fixture_text,
)
from critic.themes import select_flag_themes


class RecordedBackend:
    """Replays one real `claude -p` stdout instead of calling the model."""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.requests: list[ModelRequest] = []

    def check_available(self) -> None:
        return None

    def ask(self, request: ModelRequest) -> ModelReply:
        self.requests.append(request)
        return parse_cli_result(self.stdout, "")


def test_a_real_review_reply_yields_its_structured_answer_and_spend() -> None:
    reply = parse_cli_result(read_fixture_text("claude-review-976.json"), "")
    assert reply.model_ids == ["claude-opus-4-8[1m]"]
    assert reply.cost_usd == pytest.approx(0.139835)
    [comment] = reply.answer["comments"]
    assert (comment["path"], comment["line"], comment["theme"]) == (
        "app/services/stage_edit.py", 126, "correctness_bug",
    )


def test_a_logged_out_cli_fails_loudly_with_its_own_message() -> None:
    with pytest.raises(BackendReplyError, match="Not logged in"):
        parse_cli_result(read_fixture_text("claude-not-logged-in.json"), "")


def test_output_that_is_not_a_result_names_stderr() -> None:
    with pytest.raises(BackendReplyError, match="stderr: segfault"):
        parse_cli_result("", "segfault")


def test_a_missing_cli_is_unavailable_not_stubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(BackendUnavailableError, match="not on PATH"):
        ClaudeCliBackend(model=None).check_available()


def test_the_command_keeps_every_claude_md_and_every_tool_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/claude")
    command = ClaudeCliBackend(model="opus")._build_command(Path("/tmp/system.txt"), {"type": "object"})
    assert command[:2] == ["/bin/claude", "-p"]
    assert "--safe-mode" in command
    assert command[command.index("--tools") + 1] == ""
    assert command[-2:] == ["--model", "opus"]


def test_a_recorded_reply_becomes_the_reviews_predictions() -> None:
    recorded = RecordedBackend(read_fixture_text("claude-review-976.json"))
    themes = select_flag_themes(load_labels().themes)
    review = run_review(load_diff_at_first_commit(), load_rubric(RUBRICS / "none"), themes, recorded)
    assert [prediction.theme for prediction in review.predictions] == ["correctness_bug"]
    assert review.rubric == "none"
    assert len(recorded.requests) == 1


def test_a_theme_outside_the_vocabulary_is_refused() -> None:
    recorded = RecordedBackend(read_fixture_text("claude-review-976.json"))
    themes = [t for t in select_flag_themes(load_labels().themes) if t.slug != "correctness_bug"]
    with pytest.raises(BackendReplyError, match="outside the vocabulary"):
        run_review(load_diff_at_first_commit(), load_rubric(RUBRICS / "none"), themes, recorded)
