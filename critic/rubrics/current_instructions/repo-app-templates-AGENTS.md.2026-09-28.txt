# app/templates — read the visual style guide before writing markup

`docs/visual-language.md` is the source of record for how this app looks: where colour
comes from, error vs warning, the agent mark, and the arch test holding each rule. Read
it, not this file — what follows is only the three rules that bite while writing a
template, each naming the test that fails when you break it.

- **The agent mark is `sparkle()`, never the character.** Work a model did or is about
  to do carries the inline SVG from `_sparkle.html`
  (`{% from "_sparkle.html" import sparkle %}`, then `{{ sparkle() }}`). A pasted `✨`
  is painted by the font in its own colours and ignores `fill`, so it fails silently.
  Held by `tests/arch/test_agent_work_carries_the_sparkle.py`.
- **Colour comes from one file.** No hex, `rgb()` or bare colour keyword in a template
  — spend a `var(--…)` token from `app/static/palette.css`. Held by
  `tests/arch/test_palette_owns_colour.py`, and a page must load `palette.css` first
  (`tests/arch/test_palette_loads_before_style.py`).
- **A stage-type badge class comes from `TYPE_CLASS`, never hand-written.** Borrowing
  one for something that is not a stage type means the chip changes when the types do.
  Held by `tests/arch/test_type_badges_are_not_borrowed.py`.

Page-level conventions — zero states, the run page's columns, the stage panel's tabs —
are in `app/AGENTS.md`.
