"""A stage is described at TWO levels and only the outer one is the reviewer's: the
STAGE level (`inputs[].schema`, `resolve_output_schema()`) is what the run page shows,
while the TRANSFORM level (a signature's reads and writes) is the author's contract and
is deliberately narrower. `stage_tests` is the one sanctioned crossing.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES = _ROOT / "app" / "templates"
_STATIC = _ROOT / "app" / "static"

# Every name here is also an ordinary English word in at least one stylesheet comment
# ("reads as a heading"), which is why the scans below look only at Jinja expressions
# and at comment-stripped property access — never at raw file text.
_TRANSFORM_LEVEL_NAMES = ("signature", "reads", "rewrites", "adds", "produces")

_JINJA_EXPRESSION = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
_JS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)

_WHY = (
    "the TRANSFORM level (a signature's reads/adds/rewrites/produces) reached a "
    "reviewer-facing surface. A reviewer judges a result — what went into the stage and "
    "what came out — so these pages show the STAGE level: `inputs[].schema` and "
    "`resolve_output_schema()`. Do NOT widen the name list to make this pass. A hit means "
    "either the page is about to teach a reviewer a calling convention, or the thing being "
    "rendered is stage-level and is misnamed. Both are decisions for a human to review."
)


def find_jinja_references() -> list[str]:
    return [
        f"{path.relative_to(_ROOT)}: `{name}` in {expression.strip()[:80]}"
        for path in sorted(_TEMPLATES.rglob("*.html"))
        for expression in _JINJA_EXPRESSION.findall(path.read_text(encoding="utf-8"))
        for name in _TRANSFORM_LEVEL_NAMES
        if re.search(rf"\b{name}\b", expression)
    ]


def find_script_references() -> list[str]:
    offenders = []
    for path in sorted(_STATIC.rglob("*.js")):
        source = _JS_COMMENT.sub("", path.read_text(encoding="utf-8"))
        for name in _TRANSFORM_LEVEL_NAMES:
            # Property access or a JSON key — `stage.reads`, `stage["reads"]`. A bare
            # identifier would catch a local variable that means something else.
            for match in re.finditer(rf"""[.\[\"']{name}\b""", source):
                excerpt = source[max(0, match.start() - 40):match.end()]
                offenders.append(f"{path.relative_to(_ROOT)}: {excerpt!r}")
    return offenders


def test_no_reviewer_facing_template_names_the_transform_level() -> None:
    offenders = find_jinja_references()
    assert not offenders, f"{_WHY}\n  " + "\n  ".join(offenders)


def test_no_reviewer_facing_script_names_the_transform_level() -> None:
    offenders = find_script_references()
    assert not offenders, f"{_WHY}\n  " + "\n  ".join(offenders)


def test_the_scan_still_reaches_the_templates_and_scripts_it_is_about() -> None:
    # A moved directory makes both rules above pass by scanning nothing.
    templates = list(_TEMPLATES.rglob("*.html"))
    scripts = list(_STATIC.rglob("*.js"))
    assert templates and scripts, (
        f"found {len(templates)} template(s) under {_TEMPLATES} and {len(scripts)} "
        f"script(s) under {_STATIC} — point this test at wherever the reviewer-facing "
        "UI moved to, or the two-level boundary goes unchecked."
    )
    expressions = sum(
        len(_JINJA_EXPRESSION.findall(path.read_text(encoding="utf-8")))
        for path in templates
    )
    assert expressions > 100, (
        f"only {expressions} Jinja expression(s) matched across {len(templates)} "
        "templates — the expression pattern has stopped matching the template syntax, "
        "so the rule above is scanning almost nothing."
    )
