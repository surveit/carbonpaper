# Version lifecycle: runs must be read-only

**Status:** DISCUSSION — decision needed before implementing. Captured 2026-07-04.
**Origin:** PR #30 review, runner.py `_resolve_version_id` (the "runner.py:153" thread).

---

## The problem

A run currently *creates* a version as a side effect. `prepare_run` calls
`_resolve_version_id`, which — when no version exists yet — calls
`create_version` to snapshot the working `compiled/` into `versions/<id>/`.
That snapshot happens **before** the DAG is validated, so:

1. An invalid workflow gets immortalised as a version, then the run fails.
2. Worse: that poisoned version becomes "the latest," and the default run path
   (`version_id=None`) pins to the latest *existing* version rather than
   re-snapshotting. So after you fix the workflow, the next default run **still
   loads the old broken snapshot** and fails with the stale error.

### Reproduction (confirmed)

```
[Run 1] invalid working copy
  threw MethodologyLoadError (run refused)          <- good: no run
  versions on disk after failed run: ['20260704T110857']  <- but a version WAS written
  runs dir exists: False

[Run 2] working copy now VALID (path fixed), default run
  STILL threw -- pinned to the poisoned version:
    ['01_load.yaml: connector ... requires params.path']  <- the OLD error, already fixed
```

Real-world evidence: during PR #30 verification, e2e runs of the congresswatch
example left an untracked `examples/congresswatch/versions/<ts>/` in the tree —
a run littering a version into the repo. That is precisely the side effect this
change forbids.

## Root cause

Version *creation* is coupled into the *run* path, and `create_version` does not
validate what it snapshots.

## Proposed principle

**Runs are read-only with respect to versions. A run MUST target an existing
version; it never creates one.** Version creation is owned by:

- **compile** — when compilation of a workflow completes, it emits the first
  version (v1). *(Compiler is not on master; see constraint below.)*
- **explicit action** — the "Create version" endpoint after edits
  (`create_version_route`).

This dissolves the bug at the root: if a run can't create a version, an invalid
workflow can never be immortalised by one.

## Concrete changes

1. **Remove the auto-create branch from `_resolve_version_id`.** New behaviour:
   - explicit `version_id` given → validate it exists, return it (unchanged);
   - `version_id=None` → return the latest existing version;
   - **no versions exist → raise loudly** ("no version to run — compile or create
     one first; runs don't create versions").
2. **`create_version` validates before it snapshots.** Strict-load the working
   copy; on `MethodologyLoadError`, propagate and write nothing. This makes
   "every version is a valid workflow" an invariant, from any creation seam
   (the endpoint today, compile later). The run-path strict-load then becomes
   belt-and-suspenders (only corruption on disk can trip it).

## Constraint / open decisions

- **Compiler is not on master** (it lives on `section-3-compiler`). So the
  "compile emits v1" half cannot land with this change. The examples ship
  pre-compiled with `compiled/` committed and **no committed version**, so the
  moment runs stop auto-creating, `lobbymap`/`drift` cannot run until something
  makes their v1.

  **Options for seeding example v1s (pick one):**
  - **(A) Commit a v1 for each example.** `versions/` is designed to be
    committable (durable reviewable artifact), so this is consistent with the
    model, not a hack — it's what a real compile would have produced. Cost:
    duplicates each example's `compiled/` bytes into `versions/<id>/compiled/`.
  - **(B) One-time seed step.** A small CLI (`python -m app.services.versioning
    <methodology> --message seed`) or `make seed`, documented as a prerequisite
    to running an example. Keeps the repo lighter; adds a manual step.

  Recommendation: **(A)** — it matches the "versions are the committed record"
  design and keeps `run` a pure read.

- **Land in PR #30 or a stacked follow-up?** This is the only item from the #30
  review with real surface area: it touches the run path, the example fixtures,
  and the run tests (which today call `execute_run` on version-less temp
  methodologies and would need to create a version first). If #30 should stay
  tight, pull this into its own PR stacked on #30.

## Tests to add

- The reproduction above, as a regression test: invalid workflow → run raises
  and **no version is written**; fix workflow → run still needs an explicit
  version-creation step, then succeeds (never silently pinned to a stale one).
- `create_version` on an invalid working copy raises and writes nothing.
- Existing run tests updated to create a version before running.

## Terminology note

Written before the naming refactor (see `docs/naming-refactor.md`). Under the
agreed vocabulary, the stage-list this validates is a **workflow**; "methodology"
here means the container directory. `MethodologyLoadError` and
`load_methodology_stages` are named per today's code and will be renamed by that
refactor.
