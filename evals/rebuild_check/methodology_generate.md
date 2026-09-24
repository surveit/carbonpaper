# Generating eval items from published artifacts

One row per repository link. What comes out is an eval item: the input files, the shape an
answer should take, and what the artifact itself expects.

`read_artifact` renders the page — its figures are computed in the browser and absent from the
committed file — and records each quantity it states, typed as the page states it, and where it
said it. It returns direct URLs to tabular data files only: a repository page read as a source
turns prose into numbers nobody computed.

`answer_shape` turns those quantities into a JSON Schema carrying no value, keeping the field
names exactly. The names are the key the answer is later compared on, so renaming one makes a
right figure uncomparable. `shape_carries_no_figure` stops the run if a value leaked in.

`fetch_sources` pulls the cited files onto disk and hashes them, so an item names bytes rather
than a URL that may move under it.

Nothing here builds or checks anything. Generating items and running evals are separate runs so
that an item is a fixed thing an eval can be re-run against.
