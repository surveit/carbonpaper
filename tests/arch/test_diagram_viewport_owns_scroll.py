"""Nothing inside .diagram-viewport may declare its own overflow.

A descendant that scrolls absorbs the svg's overflow, so the viewport has no range
left and silently discards every scrollLeft write. That is how `pre { overflow-x:
auto }` broke zoom-to-node: the mermaid block is a `pre`.
"""

from __future__ import annotations

import re

from arch._helpers import read_stylesheets

# A declaration is fine only if it turns scrolling OFF.
_NON_SCROLLING = {"visible", "clip"}


def _find_rules_under_the_viewport(css: str) -> list[tuple[str, str]]:
    found = []
    # A comment sits between the previous `}` and this rule's `{`, so it lands inside the
    # selector capture unless it is removed first.
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selector, body = match.group(1).strip(), match.group(2)
        for one in (s.strip() for s in selector.split(",")):
            if not one.startswith(".diagram-viewport"):
                continue
            # ".diagram-viewport" / ".diagram-viewport.grabbing" / ":fullscreen" are the
            # container itself — it is SUPPOSED to scroll. Anything past a descendant
            # combinator is inside it.
            if (
                re.fullmatch(r"\.diagram-viewport[.:][\w-]+(\([^)]*\))?", one)
                or one == ".diagram-viewport"
            ):
                continue
            found.append((one, body))
    return found


def _find_scrolling_declarations(body: str) -> list[str]:
    return [
        f"{prop.strip()}: {value.strip()}"
        for prop, value in re.findall(r"\b(overflow(?:-x|-y)?)\s*:\s*([^;]+)", body)
        if value.strip().split()[0] not in _NON_SCROLLING
    ]


def test_no_descendant_of_the_diagram_viewport_scrolls() -> None:
    css = read_stylesheets()
    offenders = [
        f"{selector} {{ {'; '.join(scrolling)} }}"
        for selector, body in _find_rules_under_the_viewport(css)
        for scrolling in [_find_scrolling_declarations(body)]
        if scrolling
    ]
    assert not offenders, (
        "These rules make a descendant of .diagram-viewport its own scroll container, "
        "which steals the overflow the viewport needs in order to pan and centre:\n  "
        + "\n  ".join(offenders)
    )


def test_the_mermaid_block_cancels_the_base_pre_overflow() -> None:
    css = read_stylesheets()
    rules = dict(_find_rules_under_the_viewport(css))
    body = rules.get(".diagram-viewport .mermaid")
    assert body is not None, "expected a .diagram-viewport .mermaid rule to exist"
    declared = re.findall(r"\boverflow\s*:\s*([^;]+)", body)
    assert declared and declared[0].strip() in _NON_SCROLLING, (
        ".diagram-viewport .mermaid must set overflow to visible/clip so it does not "
        f"inherit the base `pre` rule's overflow-x: auto (found: {declared or 'nothing'})"
    )
