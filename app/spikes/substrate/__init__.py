"""Spike for issue #194 — move the data substrate off numpy-backed pandas.

The question the issue poses is not "DuckDB xor pandas" but *what type system
the data carries between stages*, split over two layers:

| layer                      | today                        | prototyped here          |
|----------------------------|------------------------------|--------------------------|
| relational stages          | pandas + `parse_predicate`   | `duckdb_aggregate.py`    |
| python-transform boundary  | numpy-backed pandas          | `arrow_rows.py`          |
| glue                       | —                            | Arrow (`arrow_types.py`) |

One relational stage (`aggregate`) and one Python-transform stage
(`python_row_function`) are prototyped, per the issue's "prototype one of each
and measure before committing". Neither is wired into `app.runtime.stages` —
the production handlers are untouched.

The bug class being measured, all three from the issue:

1. a nullable `str` column hands a Python transform `float('nan')`, not `None`;
2. a `list[str]` column hands it a `numpy.ndarray`, not a `list`;
3. an empty result frame has *no columns*, so every declared output column
   reports as missing.

`tests/spikes/test_null_semantics.py` runs one authored transform function
across all three substrates and records which of the three each one exhibits.

The invariant the relational half must satisfy (from the PR #182 discussion,
restated in the issue): **the parse used to validate a filter's columns must be
the same parse that executes it.** `sql_predicate.py` is the demonstration —
one DuckDB parse, read for its column references and rendered back to the SQL
that executes.
"""
