"""A stage is described at TWO levels and only the outer one is the reviewer's: the
STAGE level (`inputs[].schema`, `resolve_output_schema()`) is what the run page shows,
while the TRANSFORM level (a signature's reads and writes) is the author's contract and
is deliberately narrower. `stage_tests` is the one sanctioned crossing.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES = _ROOT / "app" / "templates"
_STATIC = _ROOT / "app" / "static"

# Every name here is also an ordinary English word in at least one stylesheet comment
# ("reads as a heading"), which is why the scans below look only at Jinja expressions
# and at comment-stripped property access — never at raw file text.
_TRANSFORM_LEVEL_NAMES = ("signature", "reads", "rewrites", "adds", "produces")

_JINJA_EXPRESSION = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
_JS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


class Reference(NamedTuple):
    path: str
    name: str
    excerpt: str

    def key(self) -> tuple[str, str]:
        return (self.path, self.name)


# Where the reviewer's page reads the transform level anyway, and why it is allowed to.
#
# An `llm_transform`'s REPLY SHAPE — the columns the model is asked to answer with — is
# not stored on its `llm` config block, and never has been: it used to be computed as
# output_schema minus input, and is now read off `signature.adds`. The model forbids the
# two from differing (find_llm_signature_issues refuses `rewrites` and requires at least
# one `add`), so the signature is the ONLY statement of the reply shape and the panel
# reads the contract because there is nothing else to read.
#
# Every other type's panel renders the artifact out of its config block — a code stage
# shows `function.code`, a join shows `join.keys`, an aggregate its formulas — because
# the thing the reviewer judges IS the config. The reply shape belongs there too; it is
# simply not stored there yet. When it moves, these entries fail as stale and come out.
#
# Entries may only be REMOVED. A new one is a design decision for a human, not a way to
# make this file pass.
_BYPASSES: dict[tuple[str, str], str] = {
    ("app/templates/_stage_executable.html", "signature"): (
        "the LLM reply shape has no config field yet"
    ),
    ("app/templates/_stage_executable.html", "adds"): (
        "the LLM reply shape has no config field yet"
    ),
}

_WHY = (
    "the TRANSFORM level (a signature's reads/adds/rewrites/produces) reached a "
    "reviewer-facing surface. A reviewer judges a result — what went into the stage and "
    "what came out — so these pages show the STAGE level: `inputs[].schema` and "
    "`resolve_output_schema()`. Do NOT widen the name list to make this pass, and do not "
    "add a _BYPASSES entry to silence it. A hit means either the page is about to teach a "
    "reviewer a calling convention, or the thing rendered belongs in the stage's config "
    "block and is being read off the contract instead. Both are for a human to review."
)


def find_jinja_references() -> list[Reference]:
    return [
        Reference(str(path.relative_to(_ROOT)), name, expression.strip()[:80])
        for path in sorted(_TEMPLATES.rglob("*.html"))
        for expression in _JINJA_EXPRESSION.findall(path.read_text(encoding="utf-8"))
        for name in _TRANSFORM_LEVEL_NAMES
        if re.search(rf"\b{name}\b", expression)
    ]


def find_script_references() -> list[Reference]:
    references = []
    for path in sorted(_STATIC.rglob("*.js")):
        source = _JS_COMMENT.sub("", path.read_text(encoding="utf-8"))
        for name in _TRANSFORM_LEVEL_NAMES:
            # Property access or a JSON key — `stage.reads`, `stage["reads"]`. A bare
            # identifier would catch a local variable that means something else.
            for match in re.finditer(rf"""[.\[\"']{name}\b""", source):
                excerpt = source[max(0, match.start() - 40):match.end()]
                references.append(
                    Reference(str(path.relative_to(_ROOT)), name, excerpt)
                )
    return references


def _unbypassed(references: list[Reference]) -> list[str]:
    return [
        f"{reference.path}: `{reference.name}` in {reference.excerpt}"
        for reference in references
        if reference.key() not in _BYPASSES
    ]


def test_no_reviewer_facing_template_names_the_transform_level() -> None:
    offenders = _unbypassed(find_jinja_references())
    assert not offenders, f"{_WHY}\n  " + "\n  ".join(offenders)


def test_no_reviewer_facing_script_names_the_transform_level() -> None:
    offenders = _unbypassed(find_script_references())
    assert not offenders, f"{_WHY}\n  " + "\n  ".join(offenders)


def test_every_bypass_still_names_a_real_reference() -> None:
    # Burn-down, not a parking lot: a bypass whose reference is gone must be deleted.
    live = {reference.key() for reference in find_jinja_references()}
    live |= {reference.key() for reference in find_script_references()}
    stale = sorted(f"{path}: `{name}` — {why}" for (path, name), why in _BYPASSES.items()
                   if (path, name) not in live)
    assert not stale, (
        "a _BYPASSES entry no longer matches anything the UI renders — the crossing it "
        "excused is gone, so delete the entry:\n  " + "\n  ".join(stale)
    )


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
