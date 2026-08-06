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

**The brief must not leak the method.** It states the story and the output schema, in the
words a journalist would use. `ComparisonContract.output_key_column` exists so a case *can*
rename the key, but `wyoming_refugee_arrivals` does not use it: declaring a per-state output
row already tells an author most of what a renamed key would have hidden, so the rename buys
nothing and a vaguer word changes which rows an author thinks belong in the table.

**Tolerance is capped by the golden's rendered precision.** A golden cell is the text pandas
printed, so a rate rendered to six decimal places cannot be compared more tightly than that.
Comparison coerces both sides before comparing, since the golden is text and a build's output
is typed.
