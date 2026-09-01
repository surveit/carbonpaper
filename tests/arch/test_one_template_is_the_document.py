"""Architecture: one template writes the document; every other page extends it."""
from __future__ import annotations

from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates"
_THE_DOCUMENT = "standalone_base.html"


def find_templates_writing_their_own_document() -> list[str]:
    return sorted(
        template.name
        for template in _TEMPLATES.rglob("*.html")
        if template.name != _THE_DOCUMENT
        and "<!doctype" in template.read_text(encoding="utf-8").lower()
    )


def test_only_one_template_writes_the_document() -> None:
    offenders = find_templates_writing_their_own_document()
    assert not offenders, (
        f"{offenders} open their own <!doctype>, so each repeats {_THE_DOCUMENT}'s head by "
        "hand — the charset, the viewport, the favicon, the stylesheets and the scripts "
        "every page's other scripts read. The miss is silent: a script added to a shared "
        "file dies here on its first call and the surface comes up blank. Extend "
        f"{_THE_DOCUMENT} and fill its blocks — `body` for a page with no shell, "
        "`stylesheets` and `static_root` for one written into a review packet."
    )


def test_the_document_is_where_the_rule_says() -> None:
    document = _TEMPLATES / _THE_DOCUMENT
    assert document.is_file(), f"{document} is gone — this rule now guards nothing"
    assert "<!doctype" in document.read_text(encoding="utf-8").lower()
