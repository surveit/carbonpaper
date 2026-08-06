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

## Two things a curator has to get right

**The brief must not leak the method.** It states the story and the output schema. Where the
golden's own column name would reveal the method, the case renames it: in
`wyoming_refugee_arrivals` the golden's key column is `state`, which comes from the population
table and so hints at which frame to join from, and the brief and contract call it
`jurisdiction` instead.

**Tolerance is capped by the golden's rendered precision.** A golden cell is the text pandas
printed, so a rate rendered to six decimal places cannot be compared more tightly than that.
Comparison coerces both sides before comparing, since the golden is text and a build's output
is typed.
