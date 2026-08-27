"""Architecture: app/static/base.css is the only place a typeface may be named."""
from __future__ import annotations

import re
from pathlib import Path

from arch.vendored import VENDORED_SRI

_APP = Path(__file__).resolve().parents[2] / "app"
_BASE = _APP / "static" / "base.css"
_VENDORED = {f"static/{name}" for name in VENDORED_SRI}

_FAMILY = re.compile(r"font-family\s*:\s*([^;{}\n]+)")
_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.S)
# `inherit` and friends name no face, so they carry nothing that can drift.
_NAMES_NO_FACE = re.compile(r"^\s*(?:var\(--[\w-]+\)|inherit|initial|unset|revert)\s*$")


def find_typeface_literals() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(_APP.rglob("*")):
        rel = path.relative_to(_APP).as_posix()
        if not path.is_file() or path == _BASE or rel in _VENDORED:
            continue
        spelled = [
            value.strip()
            for value in _FAMILY.findall(_scannable_css(path) or "")
            if not _NAMES_NO_FACE.match(value)
        ]
        if spelled:
            found[rel] = sorted(set(spelled))
    return found


def _scannable_css(path: Path) -> str | None:
    if path.suffix == ".css":
        return path.read_text(encoding="utf-8")
    if path.suffix == ".html":
        return "\n".join(_STYLE_BLOCK.findall(path.read_text(encoding="utf-8")))
    return None


def test_no_file_but_base_names_a_typeface() -> None:
    stray = find_typeface_literals()
    assert not stray, (
        f"typeface literals outside {_BASE.name}: {stray}. Every face the app spends is "
        "declared once in app/static/base.css as --ui, --prose or --mono, and referenced "
        "as var(--token). A stack at the use site is the drift those tokens exist to end; "
        "if the role you need has no token, add one to base.css with the job it does "
        "written down, rather than a stack here."
    )


def test_the_scan_reaches_both_places_a_face_can_be_written() -> None:
    sheets = sorted((_APP / "static").glob("*.css"))
    assert sheets, "no stylesheet under app/static is being read — the rule is vacuous"
    assert _scannable_css(_BASE), "base.css is not being read — the rule is vacuous"
    inline = [p for p in (_APP / "templates").glob("*.html") if _scannable_css(p)]
    assert inline, "no template <style> block is being read — half the rule is vacuous"
    assert _FAMILY.search("font-family: Georgia, serif;"), "the pattern matches no stack"
    assert _NAMES_NO_FACE.match(" var(--mono) "), "a token is being read as a literal"
    assert not _NAMES_NO_FACE.match(" ui-monospace, monospace "), "a stack reads as a token"
