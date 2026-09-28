from __future__ import annotations

from critic.backend import ModelRequest
from critic.diff import PullRequestDiff, parse_patch, render_diff, render_patch_line
from critic.matching import LINE_WINDOW
from critic.predictions import build_answer_schema
from critic.rubric import Rubric, render_rubric
from critic.themes import Theme, find_themes_outside
from critic.worked_examples import ReviewExample

REVIEWER_LOGIN = "surveit"
EXAMPLE_HUNK_LINES = 6

_ROLE = """\
YOUR PLACE
You are the critic in a review harness for the GitHub repository {repo}. Coding agents write \
its pull requests. One person reviews them: the repository owner, GitHub user {reviewer}, who \
reads each diff and leaves inline comments on the lines that need a change or an answer. You \
predict those inline comments for one pull request.

WHAT YOU ARE SHOWN
The pull request's title and description, and its diff at the commit the reviewer read. Each \
kept or added line carries its line number in the new file; a deleted line carries none. Below \
are the house rules this run supplies, which may be none, and the themes the reviewer's \
comments are sorted into.

WHAT YOU ARE NOT TOLD
The reviewer's comments on this pull request, the conversation on it, its later commits and \
its CI results. The description is the coding agent's own account of its work; the reviewer \
judges the diff.

WHAT BECOMES OF YOUR ANSWER
Your comments are scored beside the comments the reviewer left on this diff. One of yours is \
caught when a real comment sits on the same file within {window} lines and carries the same \
theme, or when a judge reads the two and finds they make the same point. A comment the \
reviewer did not make lowers precision; a point the reviewer made that you missed lowers \
recall. Later this critic gates merges: each comment you return holds the pull request until \
an agent answers it. Return the comments this reviewer would leave, not every comment a \
reviewer could leave. An empty list is a valid answer.

EACH COMMENT
- path: the file path exactly as its diff header prints it.
- line: the new-file line number printed left of the line you comment on.
- theme: one slug from THEMES below.
- rule: the general rule the comment enforces, in twelve words or fewer.
- text: the comment in the reviewer's own voice, as the examples below show it.
- severity: blocking (must change before merge), should (a change the reviewer expects), or \
nit (minor)."""

def build_review_request(
    diff: PullRequestDiff,
    rubric: Rubric,
    themes: list[Theme],
    examples: list[ReviewExample],
) -> ModelRequest:
    strays = find_themes_outside((example.answer.theme for example in examples), themes)
    if strays:
        raise ValueError(f"worked examples carry themes outside this vocabulary: {strays}")
    sections = [
        render_role(diff.repo),
        "THEMES\n" + render_themes(themes),
        f"HOUSE RULES: {rubric.name}\n{render_rubric(rubric)}",
        "WORKED EXAMPLES\n" + render_review_examples(examples),
    ]
    return ModelRequest(
        system="\n\n".join(sections),
        user=render_pull_request(diff),
        answer_schema=build_answer_schema([theme.slug for theme in themes]),
    )


def render_role(repo: str) -> str:
    return _ROLE.format(repo=repo, reviewer=REVIEWER_LOGIN, window=LINE_WINDOW)


def render_themes(themes: list[Theme]) -> str:
    return "\n".join(f"- {theme.slug}: {theme.definition}" for theme in themes)


def render_review_examples(examples: list[ReviewExample]) -> str:
    intro = (
        "Comments the reviewer left on earlier pull requests, each with the diff lines above "
        "it and the answer object it becomes."
    )
    blocks = [render_review_example(number, example) for number, example in enumerate(examples, 1)]
    return "\n\n".join([intro, *blocks])


def render_review_example(number: int, example: ReviewExample) -> str:
    hunk_lines = [render_patch_line(line) for line in parse_patch(example.diff_hunk)]
    tail = "\n".join(hunk_lines[-EXAMPLE_HUNK_LINES:])
    return (
        f"Example {number}: {example.html_url}\n"
        f"{example.lesson}\n"
        f"Pull request #{example.pr}, file {example.answer.path}:\n{tail}\n"
        f"Answer object:\n{example.answer.model_dump_json()}"
    )


def render_pull_request(diff: PullRequestDiff) -> str:
    description = diff.body.strip() if diff.body else "(no description)"
    return (
        f"Pull request #{diff.number}: {diff.title}\n"
        f"Commit: {diff.commit_sha}\n\n"
        f"DESCRIPTION\n{description}\n\n"
        f"DIFF ({len(diff.files)} files)\n{render_diff(diff)}"
    )
