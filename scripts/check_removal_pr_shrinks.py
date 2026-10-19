"""Fail a PR whose title opens with a removal verb when its non-test lines grow."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import PurePosixPath
from typing import Any, NamedTuple

# What `gh api --paginate --slurp .../pulls/{n}/files` prints; nothing checks its shape.
UncheckedGitHubPrFilePages = list[list[dict[str, Any]]]

GROWS_ON_PURPOSE_LABEL = "grows-on-purpose"
REMOVAL_VERBS = frozenset({
    "delete", "remove", "drop", "retire", "cut", "consolidate", "simplify", "collapse", "prune",
    "trim", "shrink", "halve", "dedupe", "fold", "unify", "merge", "inline",
})

_TITLE_PREFIX = re.compile(r"^(?:\[[^\]]*\]\s*)*(?:[\w.-]+(?:\([^)]*\))?!?:\s*)?")
_FIRST_WORD = re.compile(r"[A-Za-z]+(?![\w-])")


class FileChange(NamedTuple):
    path: str
    additions: int
    deletions: int


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--pr-files", required=True)
    args = parser.parse_args(argv)
    with open(args.pr_files, encoding="utf-8") as pages:
        files = read_pr_files(json.load(pages))
    additions, deletions = count_non_test_lines(files)
    failure = check_removal_pr_shrinks(args.title, additions, deletions)
    if failure is None:
        print(f"shrink-intent: passes (+{additions} -{deletions} outside test files)")
        return 0
    print(failure, file=sys.stderr)
    return 1


def check_removal_pr_shrinks(title: str, additions: int, deletions: int) -> str | None:
    verb = find_removal_verb(title)
    if verb is None or additions <= deletions:
        return None
    return (
        f"The title opens with the removal verb '{verb}', but outside test files the diff adds "
        f"{additions} lines and deletes {deletions}. Shrink the diff or retitle the PR. If it grows "
        f"on purpose, a human sets the label '{GROWS_ON_PURPOSE_LABEL}', which skips this check."
    )


def find_removal_verb(title: str) -> str | None:
    first_word = _FIRST_WORD.match(_TITLE_PREFIX.sub("", title.strip(), count=1))
    if first_word is None:
        return None
    word = first_word.group(0).lower()
    return word if word in REMOVAL_VERBS else None


def count_non_test_lines(files: list[FileChange]) -> tuple[int, int]:
    counted = [file for file in files if not is_test_path(file.path)]
    return sum(file.additions for file in counted), sum(file.deletions for file in counted)


def is_test_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1]
    in_test_dir = "tests" in parts[:-1] or "_arch_tests" in parts[:-1]
    return in_test_dir or name == "conftest.py" or (name.startswith("test_") and name.endswith(".py"))


def read_pr_files(pages: UncheckedGitHubPrFilePages) -> list[FileChange]:
    return [
        FileChange(file["filename"], int(file["additions"]), int(file["deletions"]))
        for page in pages
        for file in page
    ]


if __name__ == "__main__":
    sys.exit(main())
