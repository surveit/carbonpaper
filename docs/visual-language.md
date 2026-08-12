# Visual language

What the app's surfaces already agree on. Every rule here is one the code follows
today; most are held by an arch test, named inline.

## Colour comes from one file

`app/static/palette.css` is the only place a colour may be written — no hex, `rgb()`,
or bare keyword (`white`, `red`) may appear in any other `.css`, `.html`, `.js` or
Python string. Held by `tests/arch/test_palette_owns_colour.py`.

The one exception: a mermaid `style` line takes a literal hex and cannot read a custom
property, so `app/web/diagrams.py` repeats the palette in Python. Every literal there
is compared back to the property it copies by
`tests/arch/test_status_colour_contract.py`, which also checks each `--*-ink` stays
readable on the tint it prints on. That test is the price of the exemption; no other
file may join it.

A page must load `palette.css` before any sheet spending its tokens. An undefined
`var()` drops its whole declaration, so `1px solid var(--border)` renders as *no*
border rather than a broken one — which looks designed. One list owns the order
(`app/templates/_stylesheets.html`), held by
`tests/arch/test_palette_loads_before_style.py` and
`tests/arch/test_every_stylesheet_is_linked.py`.

## Three axes, one set of hues

Green means good on all three, but they are separate token groups because they answer
different questions:

| axis | question | tokens |
|---|---|---|
| **run state** | what did the runner do? | `--state-done/warn/failed/review/idle` × `-bg` `-ink` |
| **verdict** | did a check pass? | `--verdict-pass/warn/fail/neutral` × `-bg` `-bd` |
| **severity** | how loud is this issue? | reuses `--state-failed-*` and `--state-warn-*` |

Seven stage statuses map onto five run states — running, cancelled and pending all
take `idle`, and the node glyph (`⟳` / `✖` / `…`) separates them. Running holds no hue
on purpose: it is the one status that is not a verdict, and the strip says it with
motion instead.

## Error vs warning

Two severities (`app/models/severity.py`). `error` means an edit is owed before anyone
signs the workflow off. `warning` means a human should look — not thereby unimportant,
and several warnings are deliberate authoring choices that are wrong to refuse.

| | the shape | the word | the row | the panel title |
|---|---|---|---|---|
| **error** | filled disc, failed ink | `--state-failed-ink` | tinted `--state-failed-bg` | `--state-failed-ink` |
| **warning** | outlined triangle, `--muted` | `--muted` | untinted | `--muted` (the default) |

**The shape says which; the colour says how loud.** Carbon's accessibility rule is that
two states need at least three of {symbol, shape, colour, type} between them. Severity
had a word and a hue, so the hue was doing the separating — and the only way to make
the difference visible was to make the warning's amber loud, on rows where nothing is
owed. Giving each severity its own shape (`app/templates/_severity_icon.html`) is what
freed the warning to go quiet: the triangle tells the two apart more sharply than the
amber did, so the amber is not needed and no longer spent here.

An error row stays findable while scrolling. A warning row is legible without taking
the page. Both are drawn by `app/templates/_issue_table.html`, the one macro pair the
run page and the Workflow page share, so neither can word its counts differently.

`tests/arch/test_severity_is_not_colour_alone.py` renders the macro for every
`UserFacingErrorSeverity` member and asserts two things: each returns an `<svg`, and no
two return the same markup. Both failures are silent otherwise — the macro emits `""`
for a severity it has no branch for. That a given surface *calls* the macro is left to
review: a hand-kept list of surfaces is a second registry to maintain, and it catches
nothing a reader of the diff would miss.

Amber has not left the app — `--state-warn-*` still carries the run-state badge, the
`validation_warnings` status pill and the unresolved-value marker. It is no longer the
severity signal in an issue list.

## Mark by exception

A mark that appears on everything means nothing. Two rules follow:

- **A passing check carries no mark.** An example that matches the description shows
  its name alone; only the ones needing a read carry the warning triangle — the same
  shape the issue table gives that severity. Thirteen agreeing cases each wearing a
  green pill is thirteen invitations to read a list where nothing is owed.
- **A count of zero is not written.** "0 errors" reads as a result; the result is the
  other number. The issue panel is titled by its counts and nothing else.

## The agent mark

`app/templates/_sparkle.html` draws the app's one mark for work a model did or is
about to do: the certification badge, each example's verdict, the LLM-call block, and
every control that spends an agent turn.

It is inline SVG filled `var(--accent)`, sized in `em` so it tracks whatever it sits
in — not the `✨` character, which a font paints in its own colours and which ignores
`fill`. It is `aria-hidden`: the sentence beside it always names the agent in words, so
the mark never carries the meaning alone. The mermaid node keeps the character, since
a graph label holds no markup.

`tests/arch/test_agent_work_carries_the_sparkle.py` holds one thing: no template may
spell the ✨ character, because typing it instead of calling the macro fails silently —
the font paints it in its own colours and nothing reports a wrong-coloured glyph.

**Which surfaces deserve the mark is a review call, not a test.** No static rule can
tell that a control spends an agent turn: the URLs are built in JS at runtime, and a
button reading "Draft the guide" would be as much agent work as one reading "Generate".
A keyword scan would pass while missing exactly the case it was written for, which is
worse than no rule at all. When you add a surface where a model did the work, or is
about to, call the macro.

One more thing the mark cannot survive: `textContent`. A control that rebuilds its own
label after a generation finishes must save and restore `innerHTML`.

## Controls

`.btn` is the base; `.primary` is the accent fill, `.secondary` the quieter outline,
`.danger` the bad tint. **A control carrying a hue keeps that hue on hover** — a green
button turning beige under the cursor reads as the colour draining out rather than as
the button responding, so a tinted variant owes its own `:hover`. Held by
`tests/arch/test_hover_stays_in_its_hue.py`.

A stage-type badge class is emitted by `TYPE_CLASS`, never hand-written: borrowing one
for something that is not a stage type means the chip silently changes when types do.
Held by `tests/arch/test_type_badges_are_not_borrowed.py`.

Zero-state layout is a page convention rather than a colour one — see `app/CLAUDE.md`.
