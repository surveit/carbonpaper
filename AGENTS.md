# Carbon Paper — reviewable AI workflows

Docs are in `docs/`. Index:

- [getting-started.md](docs/getting-started.md) — `./start`, signing in so the LLM stages run, where state lives.
- [overview.md](docs/overview.md) — the mission, the locked vocabulary, and the three features.
- [architecture.md](docs/architecture.md) — the code map: the entrypoints and what each package owns.
- [models-and-storage.md](docs/models-and-storage.md) — the Pydantic contract and the compiled-stage JSON only the loader reads.
- [named-schemas.md](docs/named-schemas.md) — the named-schema and eval models, validated but not yet consumed by the runtime.
- [llm-transform-output-spec.md](docs/llm-transform-output-spec.md) — what an `llm_transform` reply must carry: 1:1, append-only, 1:N as one array column.
- [run-and-review-ui.md](docs/run-and-review-ui.md) — the operator screens and the routers, templates and CSS behind them.
- [visual-language.md](docs/visual-language.md) — where colour comes from, error vs warning, the agent mark, and the arch tests holding each.
- [figure-card.md](docs/figure-card.md) — the permalink for one published figure: what counts as one, what the receipt reads off, and why there is no `og:image`.
- [run-manifest.md](docs/run-manifest.md) — a run's own record: areas, why `exclude_unset` is load-bearing, and the queue halt's sidecar.
- [branch-analysis.md](docs/branch-analysis.md) — what a branch is, which are recorded and which are worked out, and what you can ask once every row carries them.
- [self-hosting.md](docs/self-hosting.md) — serving it to other people: the file-upload endpoint and its quotas, and the Fly.io deploy.

Also `app/AGENTS.md` (web layer), `app/runtime/AGENTS.md` (the Runner), `README.md` (quickstart).

## Conventions (load-bearing)
- **Never fabricate.** An unsourceable value is `null`/`unknown`; the pipeline fails loudly
  rather than inventing a number, URL, or citation. A requested LLM backend that isn't
  available raises rather than silently substituting another.
- Layering is enforced by import-linter, which keeps abstractions clean.
- **Never `except Exception` or bare `except`.** Catch specific types — swallowing errors breaks
  fail-loudly. Enforced by Ruff `BLE001`.
- **A system prompt states the model's ROLE in the wider system, and what becomes of its
  output.** Not just the task: who reads the result, what it is shown beside, what the reader
  is deciding, and what the model will *not* be told (e.g. the stage-test generator never sees the
  code or the pass/fail). A model given only a task optimises the artifact; one given its place
  optimises the reader's decision, and the two differ — ordering worked examples so they
  *explain* rather than merely cover does not follow from the task alone. Include at least one
  worked example of the output.
- **No `dict[str, Any]` as a stand-in for a structured value.** A dict with a known, fixed set of
  keys is a missing model — define a Pydantic model (`PersistedModel` for a stored object) or
  reference an existing one, and pass *that*. A function returning `dict[str, Any]`, or a field
  typed `dict[str, Any]`, for something with named fields is a review-blocking smell — model it.
  `dict[str, Any]` is allowed only at a genuine dynamic-JSON boundary where the shape is
  caller-defined and not yet known: a raw stage-spec dict that may be invalid mid-edit (matching
  `stage_to_spec_dict` / `validate_workflow_draft`), or foreign JSON being parsed — and even
  there, parse into a model at the first point the shape is known.

  As a follow-up here, try to reuse existing types instead of defining new ones.

  Where a dynamic bundle is genuinely unavoidable, ALIAS it rather than spelling
  `dict[str, Any]` inline. The alias name says what the bundle is, who supplies it, and that it
  is unchecked — `TypeUnsafeUserStageConfigOverride` — so a reader meets those three facts at
  every use site instead of inferring them.
- **A function takes the type it needs. Validate where the failure happens.** A parameter typed
  `X | None` with a companion argument explaining the None (`workflow: Workflow | None,
  workflow_issues: Sequence[str]`) is a failure the caller declined to handle, pushed downstream
  and re-described in prose. It also states an invariant the type system cannot check — that the
  two agree — so nothing catches them disagreeing. Resolve it at the boundary that can act: load
  the workflow there, fail there, hand the reason to whoever asked. By the time the value reaches
  the function it is a `Workflow`, and the function has one path. Optional and fallback shapes are
  for TRUE data unknowns — a column a source may not carry — never for deferring a decision to a
  layer with less context.
- **Banned words keep vocabulary limited** `tests/arch/test_no_banned_words.py` fails on any
  word in its `BANNED_WORDS` set across `.py`/`.md`/`.html`/`.js`/`.css` — read that set for the
  current list and the replacement each word owes you. This exists to reduce the number of nouns and verbs, which confuses both developers and users. Add inaccurate synonyms you find yourself using to the BANNED_WORDS list.
- **No `__all__`.** Nothing here star-imports, so it is only a second registry of public names
  to keep in sync. A package hub re-exports with the redundant-alias form
  `from x import y as y`, which Ruff and mypy both read as explicit. Enforced by
  `tests/arch/test_no_dunder_all.py`, with an empty allowlist.
- **Never weaken an arch test without human approval.** The import-linter contracts
  (`pyproject.toml`, run as `lint-imports`) and the AST invariant tests (`_arch_tests/`,
  `tests/arch/`) exist to fail on work in progress — that failure is the signal, not an obstacle.
  Adding a contract or adding to a test is fine; relaxing, deleting, skipping, or adding an
  allowlist entry to an existing one is a human decision on the record. When an arch test blocks
  the change you were about to make, reroute the change and say in the PR which test caught you
  and what you did differently.
- **A module at the size ceiling gets split, not squeezed.** The file-size ratchet
  (`tests/arch/test_file_size_ratchet.py`) fails a file in `app/` over the LLOC ceiling, and the
  only sanctioned way back under is moving a cohesive group of code out to its own module. Never
  buy the statements back by writing denser code — fusing named steps into one long expression,
  dropping an intermediate variable that was carrying a name, inlining a small helper into its
  caller. That spends the readability the ceiling exists to protect and leaves the next change
  with even less room. If the split is bigger than the change you are on, say so in the PR and
  let a human decide whether to take it now.
- **A record is declared in `app/models/records/`, never in a service.** One module per
  `PersistedModel` subclass, holding the declaration and nothing else; the functions that
  load, mutate and save it stay in the service that owns its lifecycle and import the
  class. Held by the import-linter contract protecting `app.core.record`: declaring a
  record IS importing the base, so the whitelist of importers is the whitelist of places a
  row's shape may be written down. `app.runtime` is on it because
  `app/runtime/_arch_tests/test_stages_no_cross_run_disk.py` lets a runtime module call
  `.save()` only while it DECLARES a `PersistenceScope.RUN` record; `app.core` is on it for
  the three records `app/models` sits above (`ProjectFile`, `StageCacheEntry`,
  `AgentSession`).
- **Under `app/`, a record class is the only way to reach storage.** A second contract
  protects `app.core.persistence`, so only `app.core.record` (plus the store wiring) may
  hold the handle: no module calls `get_store()` to write a collection nothing models.
  A raw payload a reader must tolerate comes off the record too — `load_raw`,
  `load_raw_or_none`, `list_raw`. Tests are outside the contract and may still reach the
  handle to arrange a fixture. `JsonDict`/`JsonScalar` live in `app.core.json_types` and
  are open to all: naming a payload's shape is not reaching for storage.
- **A `PersistedModel`'s `id` is opaque and frozen. Never build one out of the record's own data.**
  A sha256, a filename, a name someone typed, a fingerprint — putting any of them in the id
  makes the id move when the value does. The record then has two identities that must agree,
  nothing checks that they do, and re-keying it means deleting and re-writing the row rather
  than editing a field. Leave `id` alone and let it default to `uuid4().hex`; the real key
  goes in FIELDS, which is what a lookup filters on — `find()` selects on stored fields, so
  a scope has no reason to be smuggled into the id. `StageCacheEntry` is the deliberate
  exception and the only one: a cache entry IS its content hash, so its id is built from the
  fingerprints it looks up by. `id` carries `frozen=True`, so reassigning it on a loaded
  record raises rather than silently re-keying the row — a record's identity is settled
  when it is constructed.
- **When master is red, do not fix it unless that fix is your whole task.** A trunk breakage
  is shared state: parallel sessions each patching it on their own branches fork the same fix
  N ways, and every branch conflicts when the first copy merges. If you hit a red master
  mid-task, keep working on your branch — do not fold a trunk fix into it. The fix belongs to
  one agent dedicated to it, as a single-commit PR off master, merged as soon as checks pass.
- **A newly added comment or docstring carries at most one short sentence.** CI diffs
  every push/PR against its base and fails on any *added* comment/docstring over 100
  characters that isn't a tool directive (`# noqa`, `# type: ignore`, ...) or a bare link
  to `docs/*.md` or a GitHub issue — see `docs/no-long-comments-policy.md` and
  `scripts/check_added_comment_length.py`. Diff-scoped on purpose: existing code is never
  swept, and there is no exception list to grow. The default is still no comment at all;
  name the thing instead of explaining it.
- **Planning docs stay out of the repo.** Design specs, implementation/execution plans,
  brainstorming or "rethink" notes, and refactor/migration roadmaps are ephemeral working
  artifacts — keep them in scratch or the PR description, never commit them. Committed docs
  describe what the code does *today* (reference docs like `docs/architecture.md`), not what we
  plan to do.
