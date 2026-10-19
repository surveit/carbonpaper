from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from critic.gh import read_api_list, read_api_object
from critic.labels import ReviewerComment
from critic.records import CriticRecord, ForeignRecord

# GitHub's documented ceilings: at either one the file list may be cut short.
PULL_FILES_CAP = 3000
COMPARE_FILES_CAP = 300

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
PatchMarker = Literal["@", " ", "+", "-", "\\"]
_LINE_MARKERS: dict[str, PatchMarker] = {" ": " ", "+": "+", "-": "-", "\\": "\\"}


class DiffTooLargeError(RuntimeError):
    pass


class ChangedFile(ForeignRecord):
    filename: str
    status: str
    additions: int
    deletions: int
    # GitHub omits the patch of a binary file and of one too large to show.
    patch: str | None = None
    previous_filename: str | None = None


class PullRequestDiff(CriticRecord):
    repo: str
    number: int
    title: str
    body: str | None
    base_sha: str
    commit_sha: str
    files: list[ChangedFile]


class PatchLine(CriticRecord):
    marker: PatchMarker
    new_line: int | None
    text: str


class _BranchTip(ForeignRecord):
    sha: str


class _PullRequestMeta(ForeignRecord):
    title: str
    body: str | None
    base: _BranchTip
    head: _BranchTip


class _Comparison(ForeignRecord):
    files: list[ChangedFile]


def fetch_pull_request_diff(repo: str, number: int, commit_sha: str | None) -> PullRequestDiff:
    meta = _PullRequestMeta.model_validate(read_api_object(f"repos/{repo}/pulls/{number}"))
    if commit_sha is None:
        files = _fetch_head_files(repo, number)
    else:
        files = _fetch_files_at_commit(repo, meta.base.sha, commit_sha)
    return PullRequestDiff(
        repo=repo,
        number=number,
        title=meta.title,
        body=meta.body,
        base_sha=meta.base.sha,
        commit_sha=meta.head.sha if commit_sha is None else commit_sha,
        files=files,
    )


def load_or_fetch_diff(repo: str, number: int, commit_sha: str, cache_dir: Path) -> PullRequestDiff:
    path = find_cached_diff_path(cache_dir, number, commit_sha)
    if path.is_file():
        return PullRequestDiff.model_validate_json(path.read_text(encoding="utf-8"))
    diff = fetch_pull_request_diff(repo, number, commit_sha)
    save_diff(diff, cache_dir)
    return diff


def save_diff(diff: PullRequestDiff, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = find_cached_diff_path(cache_dir, diff.number, diff.commit_sha)
    path.write_text(diff.model_dump_json(indent=2), encoding="utf-8")
    return path


def find_cached_diff_path(cache_dir: Path, number: int, commit_sha: str) -> Path:
    return cache_dir / f"pr-{number}-{commit_sha}.json"


def parse_patch(patch: str) -> list[PatchLine]:
    parsed: list[PatchLine] = []
    next_new_line: int | None = None
    for raw in patch.splitlines():
        header = _HUNK_HEADER.match(raw)
        if header:
            next_new_line = int(header.group(1))
            parsed.append(PatchLine(marker="@", new_line=None, text=raw))
            continue
        if next_new_line is None:
            raise ValueError(f"patch line before any hunk header: {raw!r}")
        marker = _read_marker(raw)
        new_line = None if marker in ("-", "\\") else next_new_line
        if new_line is not None:
            next_new_line += 1
        parsed.append(PatchLine(marker=marker, new_line=new_line, text=raw[1:]))
    return parsed


def find_new_side_lines(file: ChangedFile) -> set[int]:
    if file.patch is None:
        return set()
    return {line.new_line for line in parse_patch(file.patch) if line.new_line is not None}


def find_lines_ending_at(diff: PullRequestDiff, path: str, line: int, count: int) -> list[PatchLine]:
    file = next((file for file in diff.files if file.filename == path), None)
    parsed = [] if file is None or file.patch is None else parse_patch(file.patch)
    ends = [index for index, patch_line in enumerate(parsed) if patch_line.new_line == line]
    return [] if not ends else parsed[max(0, ends[0] - count + 1) : ends[0] + 1]


def find_unanchored_comments(reals: list[ReviewerComment], diff: PullRequestDiff) -> list[str]:
    lines_by_path = {file.filename: find_new_side_lines(file) for file in diff.files}
    return [real.html_url for real in reals if real.line not in lines_by_path.get(real.path, set())]


def render_diff(diff: PullRequestDiff) -> str:
    return "\n\n".join(render_file(file) for file in diff.files)


def render_file(file: ChangedFile) -> str:
    header = f"### {file.filename} ({file.status}, +{file.additions} -{file.deletions})"
    if file.previous_filename is not None:
        header += f", renamed from {file.previous_filename}"
    if file.patch is None:
        return f"{header}\n(GitHub shows no patch for this file: binary, or too large to show)"
    body = "\n".join(render_patch_line(line) for line in parse_patch(file.patch))
    return f"{header}\n{body}"


def render_patch_line(line: PatchLine) -> str:
    if line.marker == "@":
        return line.text
    number = "" if line.new_line is None else str(line.new_line)
    return f"{number:>6} {line.marker}{line.text}"


def _fetch_head_files(repo: str, number: int) -> list[ChangedFile]:
    payload = read_api_list(f"repos/{repo}/pulls/{number}/files?per_page=100")
    if len(payload) >= PULL_FILES_CAP:
        raise DiffTooLargeError(f"PR {number} lists {len(payload)} files, GitHub's cap")
    return [ChangedFile.model_validate(item) for item in payload]


def _fetch_files_at_commit(repo: str, base_sha: str, commit_sha: str) -> list[ChangedFile]:
    # GitHub lists every changed file on a comparison's first page, whatever its per_page.
    path = f"repos/{repo}/compare/{base_sha}...{commit_sha}?per_page=1"
    comparison = _Comparison.model_validate(read_api_object(path))
    if len(comparison.files) >= COMPARE_FILES_CAP:
        raise DiffTooLargeError(f"{base_sha}...{commit_sha} lists {COMPARE_FILES_CAP} files, GitHub's cap")
    return comparison.files


def _read_marker(raw: str) -> PatchMarker:
    first = raw[:1]
    if first not in _LINE_MARKERS:
        raise ValueError(f"unrecognised patch line: {raw!r}")
    return _LINE_MARKERS[first]
