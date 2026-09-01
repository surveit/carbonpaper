"""Architecture: a page loads the script defining every window global its scripts read."""
from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, meta

_APP = Path(__file__).resolve().parents[2] / "app"
_TEMPLATES = _APP / "templates"
_STATIC = _APP / "static"

_DEFINES = re.compile(r"window\.([A-Za-z_$][\w$]*)\s*=")
_READS = re.compile(r"window\.([A-Za-z_$][\w$]*)")
# The src may be a Jinja expression, so match the filename rather than the path.
_LOADS = re.compile(r'<script[^>]*\ssrc="[^"]*?([\w.-]+\.js)"')


def find_globals_no_script_defines() -> list[str]:
    definers = read_who_defines_each_global()
    readers = read_what_each_script_needs(definers)
    offenders = []
    for page, loaded in find_scripts_each_page_loads().items():
        for script in sorted(loaded):
            for name in sorted(readers.get(script, ())):
                if not definers[name] & loaded:
                    offenders.append(
                        f"{page}: {script} reads window.{name}, which only "
                        f"{sorted(definers[name])} defines"
                    )
    return offenders


def read_who_defines_each_global() -> dict[str, set[str]]:
    definers: dict[str, set[str]] = {}
    for script in sorted(_STATIC.glob("*.js")):
        for name in _DEFINES.findall(script.read_text(encoding="utf-8")):
            definers.setdefault(name, set()).add(script.name)
    return definers


def read_what_each_script_needs(definers: dict[str, set[str]]) -> dict[str, set[str]]:
    """A global a script sets for itself is not a global it needs someone else to load."""
    needs = {}
    for script in sorted(_STATIC.glob("*.js")):
        text = script.read_text(encoding="utf-8")
        own = set(_DEFINES.findall(text))
        needs[script.name] = {n for n in _READS.findall(text) if n in definers} - own
    return needs


def find_scripts_each_page_loads() -> dict[str, set[str]]:
    """Page -> every script it loads, across the templates it extends and includes."""
    pages = {}
    for template in sorted(_TEMPLATES.rglob("*.html")):
        reached = _walk_referenced_templates(template.relative_to(_TEMPLATES).as_posix())
        texts = [(_TEMPLATES / name).read_text(encoding="utf-8") for name in reached]
        if any("<!doctype" in text.lower() for text in texts):
            pages[template.name] = {s for text in texts for s in _LOADS.findall(text)}
    return pages


def _walk_referenced_templates(name: str, reached: set[str] | None = None) -> set[str]:
    reached = set() if reached is None else reached
    if name in reached or not (_TEMPLATES / name).is_file():
        return reached
    reached.add(name)
    source = (_TEMPLATES / name).read_text(encoding="utf-8")
    parsed = Environment(loader=FileSystemLoader(str(_TEMPLATES))).parse(source)
    for referenced in meta.find_referenced_templates(parsed):
        if referenced:
            _walk_referenced_templates(referenced, reached)
    return reached


def test_every_page_loads_the_scripts_defining_what_it_reads() -> None:
    offenders = find_globals_no_script_defines()
    assert not offenders, (
        "a page loads a script that reads a window global no script on that page "
        "defines, so the script dies on its first call and the surface it draws comes "
        "up blank. Load the definer in the page's own <head>:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_reaches_the_pages_and_the_globals() -> None:
    pages = find_scripts_each_page_loads()
    assert "base.html" in pages and "_scope_panel.html" in pages, sorted(pages)
    assert "scope_map.js" in pages["_scope_panel.html"]
    definers = read_who_defines_each_global()
    assert definers.get("Figures") == {"figure_text.js"}
    assert "Figures" in read_what_each_script_needs(definers)["scope_map.js"]
