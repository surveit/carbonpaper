"""Added lines under app/ that the suite never executes."""
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import coverage
from pydantic import BaseModel

from scripts.check_added_comment_length import find_added_lines

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOVERNED_PREFIX = "app/"
_MARKER = "<!-- added-coverage-report -->"

# Routers, views and template glue: reached through a request rather than called, so an
# uncovered line here costs a reader less than one in the runtime. Counted, never listed.
_EXEMPT_PREFIXES = ("app/web/",)


class UnrunSpan(BaseModel):
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class AddedCoverage:
    added: int
    unrun: list[UnrunSpan]
    exempt_unrun: int = 0

    @property
    def unrun_lines(self) -> int:
        return sum(span.end_line - span.start_line + 1 for span in self.unrun)


def find_unrun_added_lines(
    added_by_file: dict[str, set[int]], missing_for: "MissingLookup"
) -> AddedCoverage:
    added = 0
    exempt_unrun = 0
    unrun: list[UnrunSpan] = []
    for path in sorted(added_by_file):
        if not path.startswith(_GOVERNED_PREFIX) or not path.endswith(".py"):
            continue
        executable, missing = missing_for(path)
        if executable is None:
            continue
        touched = added_by_file[path] & executable
        if path.startswith(_EXEMPT_PREFIXES):
            exempt_unrun += len(touched & missing)
            continue
        added += len(touched)
        unrun.extend(_group_runs(path, sorted(touched & missing)))
    return AddedCoverage(added=added, unrun=unrun, exempt_unrun=exempt_unrun)


def _group_runs(path: str, lines: list[int]) -> list[UnrunSpan]:
    spans: list[UnrunSpan] = []
    for line in lines:
        if spans and line == spans[-1].end_line + 1:
            spans[-1] = spans[-1].model_copy(update={"end_line": line})
        else:
            spans.append(UnrunSpan(path=path, start_line=line, end_line=line))
    return spans


class MissingLookup:
    """Executable and never-executed line sets for one file, from a coverage data file."""

    def __init__(self, data_file: Path) -> None:
        self._coverage = coverage.Coverage(data_file=str(data_file))
        self._coverage.load()

    def __call__(self, path: str) -> tuple[set[int] | None, set[int]]:
        try:
            _, statements, _, missing, _ = self._coverage.analysis2(str(_REPO_ROOT / path))
        except coverage.exceptions.CoverageException:
            return None, set()
        return set(statements), set(missing)


def _describe_exempt(result: AddedCoverage) -> str:
    if not result.exempt_unrun:
        return ""
    return f" {result.exempt_unrun} more under `app/web/` are out of scope."


def render_markdown(result: AddedCoverage, repo: str, sha: str) -> str:
    if not result.unrun:
        return (
            f"{_MARKER}\n### 🟢 added-line coverage — every added line runs\n\n"
            f"`{result.added}` added executable lines under `app/`, all reached by the suite."
            f"{_describe_exempt(result)}\n"
        )
    lines = [
        f"{_MARKER}",
        f"### 🟡 added-line coverage — {result.unrun_lines} of {result.added} added lines never run",
        "",
        "Each row is executable code this pull request adds that no test in the suite reaches.",
        "",
        "| lines | file |",
        "|---|---|",
    ]
    for span in result.unrun:
        where = f"L{span.start_line}" + (f"-L{span.end_line}" if span.end_line > span.start_line else "")
        link = f"https://github.com/{repo}/blob/{sha}/{span.path}#{where}"
        lines.append(f"| [{where.replace('L', '')}]({link}) | `{span.path}` |")
    lines.append("")
    lines.append(
        "<sub>Report-only. A line executed only at import counts as run."
        f"{_describe_exempt(result)}</sub>"
    )
    return "\n".join(lines) + "\n"


def _run_git_diff(base: str, head: str, repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "--no-color", "-U0", f"{base}...{head}", "--", "app"],
        cwd=repo_root, capture_output=True, text=True, check=True,
    )
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/master")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--coverage-data", default=str(_REPO_ROOT / ".coverage"))
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--repo", default="")
    parser.add_argument("--sha", default="")
    args = parser.parse_args(argv)

    if args.markdown and not (args.repo and args.sha):
        parser.error("--markdown needs --repo and --sha to build source links")

    diff_text = _run_git_diff(args.base, args.head, _REPO_ROOT)
    result = find_unrun_added_lines(
        find_added_lines(diff_text), MissingLookup(Path(args.coverage_data))
    )
    if args.markdown:
        sys.stdout.write(render_markdown(result, args.repo, args.sha))
        return 0
    for span in result.unrun:
        print(f"{span.path}:{span.start_line}-{span.end_line} added but never executed")
    print(f"{result.unrun_lines} of {result.added} added executable lines under app/ never run")
    if result.exempt_unrun:
        print(f"{result.exempt_unrun} more under app/web/ are out of scope")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
