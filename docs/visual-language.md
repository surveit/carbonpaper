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

## Typography comes from one file too

`app/static/base.css` is the only place a typeface may be NAMED. Three roles are
declared there and referenced as `var(--ui)` / `var(--prose)` / `var(--mono)`; a stack
written anywhere else fails `tests/arch/test_base_owns_typography.py`.

The rule exists because the drift already happened twice. `--mono` was created when four
fixed-width stacks had come apart across the sheets, one of them leading with
`ui-monospace` — which Chromium does not implement, so there it falls through to whatever
the reader has set as their browser's fixed-width font, while the sheet beside it named
Consolas. Same page, two typefaces, no rule saying which. It then came back in template
`<style>` blocks, which no scan was reading: six more `ui-monospace` spellings across
three templates, plus one sheet re-spelling the whole `--mono` stack by hand and another
hiding a second stack in a `var(--mono, ui-monospace, …)` fallback.

So the scan reads BOTH places a face can be written — `app/static/*.css` and every
`<style>` block under `app/templates/` — and a `var()` fallback carrying a stack counts
as a literal, because that is a second answer to the same question.

`font: inherit` and `font-family: inherit` name no face and are untouched.

## A table class must match a rule

Every class on a `<table>` matches a rule in some stylesheet, held by
`tests/arch/test_every_table_class_is_styled.py`. The failure it catches is silent: a
class matching nothing renders as a bare browser table beside styled ones, which reads
as a choice rather than an omission. `class="table"` sat in two eval pages that way
until PR #579, and nothing reported it.

Reach for the shape whose page role matches — `.stages` for a list of things on a
section page, `.schema` for config inside a pane, `.data-preview` inside `.table-scroll`
for rows. Four more exist, each written for one surface: `.issue-table`, `.kv`,
`.join-keys`/`.aggs`, `.files-table`. There is no generic `.table`.

The scan reads template `<style>` blocks as well as `app/static`, so a table styled on
its own page counts as styled.

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

Two severities (`app/models/severity.py`). **`error` is the runtime's word**: a stage
that stopped — a schema violation, an authored refusal, a raise — which ended the run
and blocked what was downstream. `warning` means a human should look, and is not
thereby unimportant.

Every COMPILER note is a warning. Nothing about a workflow as written refuses an
action: a version snapshots whatever the author has, and an undescribed stage, one no
example checks, or a model call that re-rolls every run are all things an author may
knowingly leave standing. A compiler note borrowing `error` claimed a severity it
could not act on.

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
