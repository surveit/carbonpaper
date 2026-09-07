"""Architecture: words banned from source, tests, docs and templates alike.

A banned word is one that reads as precision but carries none — it names a
property without saying whose, so the reader has to guess. Say the specific
thing instead.
"""
from __future__ import annotations

from pathlib import Path

from arch import find_banned_words, scan_all_text

# "canonical to whom, by what rule?" — say what actually holds: a sorted-key JSON
# dump, the spec-dict form, the on-disk text, LOADER_BOOKKEEPING_KEYS.
#
# "deriv*" — we do not derive anything here. An LLM turn that writes a stage's
# example test cases GENERATES them (test generation). A value computed from
# other values is computed from / read off / built from / follows from the
# thing it comes from — name that thing.

# sorry/unfortunately: state the boundary, do not apologise. docs/visual-language.md

# human review queue: the type is `review_queue` — every queue here is worked by a person.
BANNED_WORDS = {
    "canonical", "deriv", "sorry", "unfortunately",
    "human review queue", "human_review_queue",
}

# 0021 renamed the type; these name the store as it was actually written.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WROTE_THE_OLD_NAME = (
    _REPO_ROOT / "alembic" / "versions",
    _REPO_ROOT / "scripts" / "stage_signatures.py",
    *(_REPO_ROOT / "tests" / f"test_migration_{rev}.py" for rev in ("0002", "0007", "0010", "0021")),
)

_SCANNED_SUFFIXES = (".py", ".md", ".html", ".js", ".css")


def test_no_banned_words() -> None:
    offenders = find_banned_words(
        scan_all_text(_SCANNED_SUFFIXES), BANNED_WORDS,
        exempt={Path(__file__), *_find_files_naming_the_stored_past()},
    )
    assert not offenders, (
        "banned word — name the specific property instead (see BANNED_WORDS "
        "in this file):\n  " + "\n  ".join(offenders)
    )


def _find_files_naming_the_stored_past() -> list[Path]:
    return [
        path for target in _WROTE_THE_OLD_NAME
        for path in ([target] if target.is_file() else sorted(target.rglob("*.py")))
    ]
