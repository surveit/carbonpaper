# evals — carbonpaper against published data-journalism analyses

Data journalists publish their analysis code alongside their articles, and they commit the
notebook **with its rendered outputs**, because the rendered table is what readers see. That
table went through a newsroom's fact-checking and years of public exposure. So it is a golden
answer available without executing anything: no dependency rot, no human labelling.

A case hands a blind agent the same input files plus a brief — the story to tell and the
output schema, never the method — and diffs the carbonpaper build's output against that
golden.

A disagreement is not a failing grade. It resolves three ways: a carbonpaper defect, an
undiscovered defect in the published analysis, or undecidable from the data alone. All three
are findings, tracked in issue #469.

**No accuracy percentage is computed anywhere.** The cases are hand-picked, not sampled, so a
rate over them would not measure anything.

## Layout

```
evals/harness/case.py     the Case manifest model, and load/write
evals/harness/golden.py   read a golden table out of a notebook's committed HTML output
evals/harness/compare.py  line a build's output up against the golden; classify disagreements
evals/harness/cli.py      python -m evals.harness.cli
evals/cases/<id>/case.json  one case, golden inline
```

The golden is stored **inline in the manifest** — a few KB, so comparison needs no network and
a golden change shows up as a reviewable diff. Input files are stored **by reference**
(repo + commit + path + sha256), which keeps multi-megabyte source data out of git and avoids
redistributing other outlets' data.

## Running

Read a golden out of a notebook:

```
python -m evals.harness.cli extract-golden <notebook.ipynb> <code_cell_index> <key_column>
```

It refuses a cell that stored no HTML table, stored more than one, or rendered an **elided**
table — pandas cuts long frames with `...`, and such an output is a prefix of the answer, not
the answer.

Compare a build's output CSV against a case:

```
python -m evals.harness.cli compare evals/cases/<id>/case.json <build_output.csv>
```

Exit status is 0 when everything agrees, 1 otherwise. `--full` prints the whole comparison as
JSON.

## Comparison is positional

There is no key column. The two tables are lined up as **sequences**, because the row order is
part of the answer — every golden in this corpus is a ranked or sorted table, and a build that
produces the right rows in the wrong order has not reproduced it.

Alignment is a sequence diff over rows, so one missing row reads as one missing row instead of
shifting every row after it into a disagreement. Its representation rounds numbers to four
significant figures, deliberately coarser than any tolerance: pairing only has to be good
enough, and **every paired row is then checked at the case's real tolerance**, so coarseness
can mis-pair a row but cannot pass a difference.

Three kinds of difference come out:

| | |
|---|---|
| `missing` row | in the golden, no counterpart in the build |
| `extra` row | in the build, no counterpart in the golden |
| cell difference | the two tables share a position, and a column disagrees there |

## Two things a curator has to get right

**The brief must state the sort, including tie-breaks.** Comparison is positional, so an
under-specified sort makes a build disagree for no reason — and if the sort really is
under-specified, the source's own output was not determined either.

**The brief must not leak the method.** It states the story, the output schema, and the sort,
in the words a journalist would use. Renaming a column to hide where it came from does not
work: declaring a per-state output row already tells an author most of what a renamed column
would have hidden.

**Tolerance is capped by the golden's rendered precision.** A golden cell is the text pandas
printed, so a rate rendered to six decimal places cannot be compared more tightly than that.
Comparison coerces both sides before comparing, since the golden is text and a build's output
is typed.
