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
BANNED_WORDS = {"canonical", "deriv", "sorry", "unfortunately"}

_SCANNED_SUFFIXES = (".py", ".md", ".html", ".js", ".css")


def test_no_banned_words() -> None:
    offenders = find_banned_words(
        scan_all_text(_SCANNED_SUFFIXES), BANNED_WORDS, exempt={Path(__file__)}
    )
    assert not offenders, (
        "banned word — name the specific property instead (see BANNED_WORDS "
        "in this file):\n  " + "\n  ".join(offenders)
    )
