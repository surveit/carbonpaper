# Packs

A **pack** is a subpackage `app/packs/<pack_id>/` that registers connector kinds. Every other
package under `app/` is the kernel.

## What a pack declares

Its `__init__.py` calls `register_pack(PackSpec(...))` (`app/models/packs.py`). A `PackSpec`
holds a `pack_id` and its `connectors`, each a `ConnectorSpec` (`app/models/connectors.py`):

- `kind`: what an `input_data` stage's `connector.kind` names.
- `params_model`: a `ConnectorParams` subclass, so every kind binds `paths` the same way.
- `metadata_columns`: the source table's columns after `SOURCE_COLUMNS` (`source_id`,
  `source_sha256`, `filename`, `origin_url`, `fetched_at`).
- `acquire(params)`: yields an `AcquiredBytes` per file: filename, origin URL, a way to open
  the bytes, metadata. A mirror's copy is a `MirroredBytes`, adding the time and sha256 the
  mirror recorded.

At import, `register_pack` refuses a pack id or kind already held, `file` included, and a
metadata column that is not nullable, starts with `_`, repeats a source column or is
declared twice.

A pack imports only `app.models`, `app.core` and itself (`app/packs/_arch_tests/`), and
writes no file or record: the kernel stores the bytes it hands over.

## How a connector is bound at run time

A run's bindings merge into `connector.params` key by key (`Workflow.apply_run_bindings`),
and the kind's `params_model` validates the result. Once every other check has passed,
`prepare_run` calls `acquire_input_data` for each stage of a pack's kind:

1. **Bound `paths` win.** Each must be a stored file of the project. Its metadata is null,
   which is why every metadata column must be nullable.
2. **Otherwise `acquire(params)` runs.** `receive_source` stores each file and stamps when
   its read began; `receive_mirrored_source` keeps the mirror's time, refusing bytes that
   hash to anything else.
3. Each file becomes a `ReadFile` in the manifest's `input_bindings`.
4. Only once every stage has acquired does each write its source table, one row per file,
   to `runs/<id>/sources/<stage_id>.parquet`, so a refused run leaves no run directory. The
   handler reads that table.

A file the connector cannot reach raises `SourceUnavailable`, refusing the run with the
stage and file named. A connector may instead yield `UnreachedSource(requested_url, metadata)`:
its row has null `source_id`, `source_sha256`, `filename` and `fetched_at`, `origin_url` is the
requested URL, its lineage has no parent file, and it is not a `ReadFile`. A stage that yields
only unreached sources is refused, and `read_pages` refuses an unreached row, so filter first. A resume reads the table its prepare wrote. A workflow test or an eval
prepares no run, so a stage of a pack's kind fails there.

## The docket pack

`recap_docket` reads a CourtListener docket's filings, one row per PDF; `entries` names each
by ECF number (`"58"`, `"221-1"`). It reads the docket's `type=rd` search pages and each PDF on
storage.courtlistener.com; a `cache_dir` whose `manifest.jsonl` records each fetch replaces
both, and a relative `cache_dir` names a directory of the checkout, such as the committed
`app/seeds/data/boeing`. A filing the archive or mirror lacks refuses the run. `ATTRIBUTION.md` holds the terms
line.

## The opinions pack

`courtlistener_opinions` reads an anonymous CourtListener opinion search (`type=o`: `query`,
`courts`, `filed_after`, `filed_before`, `statuses`) and one stored PDF per decision, one row
each. The search is restricted to the requested clusters: `query` is sent as
`(query) AND cluster_id:(...)` over `cluster_ids`. Each id must be a cluster the search returns
with exactly one stored PDF; any that is not (it fails the query, courts, dates or statuses, or
has no or several stored PDFs) refuses the run, naming it, before a PDF is fetched. The search
is read to its last page, so a repeat the cursor serves is seen, and a repeat naming other PDFs
than its first copy refuses the run. A 429 is waited out for the `Retry-After` it names, up to
`MAX_RATE_LIMIT_WAITS` (5) waits of at most `MAX_RETRY_AFTER_S` (60) seconds each
(`app/core/url_fetch.py`); a `Retry-After` over that, missing or not whole seconds, or a 429
after the 5th wait, refuses the run. There is no mirror: every run fetches. `ATTRIBUTION.md`
holds the terms line.

## The web pack

`web_pages` fetches each URL in `urls` (each with an optional `key`), or in the `url_column` of a
`url_table` file (with an optional `key_column`), and gives one row per URL in the order listed.
Give exactly one of `urls` and `url_table`; a URL listed twice refuses the run, naming it. A row
whose page was read carries the stored file (named `<key>.<suffix>`, or the URL's position in the
list when it has no key); a row whose page was not has no file and `fetch_status` says why. The
metadata columns are `url_key`, `requested_url`, `final_url` (after redirects), `content_type`,
`http_status` and `fetch_status`. Each request waits up to 30 seconds and sends the user agent in
`app/packs/web/connector.py`. A 429 is waited out as in the opinions pack.

| `fetch_status` | Meaning | File stored |
| --- | --- | --- |
| `ok` | Text length is within `min_text_chars` (200) and `max_text_chars` (2,000,000) | yes |
| `unreadable` | Fewer than `min_text_chars` characters of text read | yes |
| `too_long` | More than `max_text_chars` characters of text read | yes |
| `too_long` | The body is over the store's file ceiling (`CARBON_PAPER_MAX_UPLOAD_BYTES`) | no |
| `unsupported_type` | Not a PDF (body starts `%PDF-`), `text/html` or `text/plain` | no |
| `http_<code>` | The server answered with that status | no |
| `timeout` | No answer within the request timeout | no |
| `unreachable` | Any other connection failure | no |
| `invalid_url` | Not an http or https URL; no request is made | no |

Text length is what `read_pages` will read from the stored file: a PDF through pypdf, a page
as the article text trafilatura extracts, a text file as UTF-8. A run where no URL was read
refuses.
