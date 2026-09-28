"""Fail a PR whose title opens with a removal verb when its diff adds more lines than it deletes."""
from __future__ import annotations

import argparse
import re
import sys

LIFTING_LABEL = "grows-on-purpose"
REMOVAL_VERBS = frozenset({
    "delete", "remove", "drop", "retire", "cut", "consolidate", "simplify", "collapse", "prune",
    "trim", "shrink", "halve", "dedupe", "fold", "unify", "merge", "inline",
})

_TYPE_SCOPE_PREFIX = re.compile(r"^\s*[a-z]+(?:\([^)]*\))?!?:\s*", re.IGNORECASE)
_FIRST_WORD = re.compile(r"[A-Za-z]+")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--additions", type=int, required=True)
    parser.add_argument("--deletions", type=int, required=True)
    args = parser.parse_args(argv)
    failure = check_removal_pr_shrinks(args.title, args.additions, args.deletions)
    if failure is None:
        print(f"shrink-intent: passes (+{args.additions} -{args.deletions})")
        return 0
    print(failure, file=sys.stderr)
    return 1


def check_removal_pr_shrinks(title: str, additions: int, deletions: int) -> str | None:
    verb = find_removal_verb(title)
    if verb is None or additions <= deletions:
        return None
    return (
        f"The title opens with the removal verb '{verb}', but the diff adds {additions} lines "
        f"and deletes {deletions}. Shrink the diff or retitle the PR. If it grows on purpose, "
        f"a human sets the label '{LIFTING_LABEL}', which skips this check."
    )


def find_removal_verb(title: str) -> str | None:
    first_word = _FIRST_WORD.match(_TYPE_SCOPE_PREFIX.sub("", title, count=1))
    if first_word is None:
        return None
    word = first_word.group(0).lower()
    return word if word in REMOVAL_VERBS else None


if __name__ == "__main__":
    sys.exit(main())
