# Eval Authoring Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rework eval authoring per `docs/superpowers/specs/2026-07-04-eval-authoring-redesign.md`: pick override/target on the workflow graph, reject unreachable pathways, decouple the cases file from the config (`table` optional), remove `EvalRun.passed`, add a "no cases yet" status, and let cases be attached later.

**Architecture:** Small model change (`EvalConfig.table` optional, drop `EvalRun.passed`) at the base; two new checks in the pure `eval_compat` service (reachability, tolerate no file); one new status value threaded through `eval_store.eval_status` and the read pages; the authoring page's stage dropdowns replaced by a graph selector with a server-computed descendants map for reachability dimming; an attach-cases-later endpoint. All on branch `eval-ui` (PR #59), pre-merge.

**Tech Stack:** FastAPI + Jinja2 + vanilla JS + mermaid + pydantic + pandas; pytest + TestClient. All already in place.

## Global Constraints

- **Banned words in ALL new copy, comments, docs:** "DAG", "socket", "frontier", "passing" (as an eval status). The model field `EvalRunSettings.frontier` keeps its code name; do not adopt the word in new prose. User-visible copy calls the stages graph the **workflow** and the executing stages "executing".
- Status vocabulary, exact strings: `broken`, `no cases yet`, `never run`, `stale`, `run errored`, `run succeeded`.
- **Never fabricate:** no default/fallback standing in for data; missing file/schema/stage → loud, specific problem string. No silent `except`. A derived column type is never guessed — it comes from a stage schema or it's a reported problem.
- mypy strict (`files = ["app"]`), no `Any` shortcuts, no `# type: ignore`. ruff clean. Tests offline, never touch real `examples/` (use the `tmp_examples` fixture in `tests/test_evals_web.py`, which monkeypatches `EXAMPLES_DIR` on the `loading`, `evals`, and `methodology` routers plus `REPO_ROOT` on `evals`).
- New identifiers follow existing code; do not pre-apply the naming refactor.
- Storage unchanged: configs `examples/{m}/eval_config/{id}.yaml`; dataset uploads `examples/{m}/eval_data/{filename}` (never overwrite); runs read-only `examples/{m}/eval_run/{run_id}.json`.
- Commit per task, message `feat(evals): …` / `test(evals): …` / `refactor(evals): …`; push to `origin/eval-ui` after each task (always push).
- Full suite baseline at plan start: 261 passed. Each task keeps `python -m pytest -q`, `python -m ruff check app tests`, `python -m mypy` green.

---

### Task 1: Model — `table` optional, drop `EvalRun.passed`

**Files:**
- Modify: `app/models/eval.py` (EvalConfig.table ~line 100; EvalRun.passed line 205 + docstring lines 16-17)
- Modify: `app/templates/eval_run.html` (lines 17-18, the "scorer judgment" block)
- Modify: `tests/test_eval.py` (EvalRun fixture ~lines 155-157)
- Modify: `tests/test_evals_web.py` (EvalRun fixture ~line 170)
- Test: `tests/test_eval.py` (add cases)

**Interfaces:**
- Produces: `EvalConfig.table: Optional[TableRef]` (default `None`); `EvalRun` no longer has a `passed` attribute. Later tasks rely on both.

- [ ] **Step 1: Write failing tests** in `tests/test_eval.py`. Reuse the module's existing `EvalConfig`/`EvalRun` construction style (dicts via `model_validate`, or kwargs — match what's already there).

```python
def test_eval_config_table_optional():
    # A config with no cases file is valid: key/input_columns/expected still required.
    cfg = EvalConfig.model_validate({
        "id": "e1", "methodology": "m", "name": "E1",
        "override_stage": "a", "target_stage": "b",
        "key": ["doc_id"], "input_columns": ["x"],
        "expected": [{"actual": "score", "expected": "gold", "metric": "exact"}],
    })
    assert cfg.table is None

def test_eval_config_still_requires_nonempty_key():
    with pytest.raises(ValidationError):
        EvalConfig.model_validate({
            "id": "e1", "methodology": "m", "name": "E1",
            "override_stage": "a", "target_stage": "b",
            "key": [], "input_columns": ["x"],
            "expected": [{"actual": "score", "expected": "gold", "metric": "exact"}],
        })

def test_eval_run_has_no_passed_field():
    run = EvalRun.model_validate({
        "id": "r1", "config": "e1", "methodology": "m",
        "methodology_version": "v1", "status": "scored",
        "settings": {"can_score_declaratively": True, "frontier": ["b"], "blocking_stages": []},
        "metrics": {"match_rate": 1.0},
    })
    assert not hasattr(run, "passed")
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_eval.py -q`. Expected: `test_eval_config_table_optional` fails (table currently required), `test_eval_run_has_no_passed_field` fails (attribute still present).

- [ ] **Step 3: Implement** in `app/models/eval.py`:
  - Change the field (currently `table: TableRef`) to:

```python
    table: Optional[TableRef] = None
```

  - Delete the `passed: Optional[bool] = None` line (205) and rewrite the neighbouring comment so it no longer mentions an overall outcome:

```python
    # Score outputs — the scorer writes rollup metrics and a per-row result
    # table at `result_ref`. There is no overall pass/fail: a case row passes
    # iff all its checks match, and whether the eval looks good is a human's
    # methodology-review judgment, not a stored bool.
    metrics: dict[str, Any] = Field(default_factory=dict)
```

  - Update the EvalRun class docstring (lines ~16-17 region) to drop the `passed` reference.

- [ ] **Step 4: Update the template** `app/templates/eval_run.html` — delete the two-line block:

```jinja
    {% if run.passed is not none %}
    <li><strong>scorer judgment:</strong> {{ "passed" if run.passed else "failed" }}</li>
    {% endif %}
```

- [ ] **Step 5: Fix the two EvalRun fixtures** — remove the `"passed": True,` line from the `EvalRun` dict in `tests/test_eval.py` (~155) and `tests/test_evals_web.py` (~170). In `tests/test_eval.py` remove the `assert r.passed is True` line.

- [ ] **Step 6: Run tests** — `python -m pytest tests/test_eval.py tests/test_evals_web.py -q`. Expected: PASS.

- [ ] **Step 7: Full checks + commit**

```bash
python -m pytest -q && python -m ruff check app tests && python -m mypy
git add app/models/eval.py app/templates/eval_run.html tests/test_eval.py tests/test_evals_web.py
git commit -m "feat(evals): cases file optional on EvalConfig; drop EvalRun.passed"
git push origin eval-ui
```

---

### Task 2: Compatibility — reachability + tolerate no file

**Files:**
- Modify: `app/services/eval_compat.py` (the whole `check_eval_compatibility` body)
- Test: `tests/test_eval_compat.py` (add cases)

**Interfaces:**
- Consumes: `EvalConfig.table` may now be `None` (Task 1).
- Produces: unchanged signature `check_eval_compatibility(config, stages) -> CompatibilityReport`. New guarantees: an unreachable override→target is `ok=False`; a `table is None` config never raises and is checked for everything except file-schema coverage.

- [ ] **Step 1: Write failing tests** in `tests/test_eval_compat.py` (reuse the file's existing stage/config builders):
  - `test_target_not_reachable_from_override_is_broken`: stages where `override` is NOT upstream of `target` (e.g. two independent branches) → `ok is False`, a problem contains `is not reachable from override`.
  - `test_reachable_pathway_ok`: override upstream of target, grain-preserving, with a valid cases table → `ok is True`.
  - `test_table_none_does_not_crash_and_skips_file_checks`: a compatible config with `table=None` → returns a report (no exception), `ok is True`, `settings` present; and the cases-table coverage / key-in-dataset problems are absent.
  - `test_table_none_still_catches_target_assertion_error`: `table=None` but an `expected.actual` column the target does not emit → `ok is False`, problem names the column (proves file-independent checks still run).

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_eval_compat.py -q`. Expected: the four new tests fail (reachability unimplemented; `table=None` raises `AttributeError` on `config.table.table_schema`).

- [ ] **Step 3: Implement** — edit `check_eval_compatibility` in `app/services/eval_compat.py`:

  a. After Condition 1 (referenced stages exist, which early-returns on missing), add the reachability condition. Insert a descendants walk and check:

```python
    # Condition 1b: the target must be reachable from the override, else the
    # override injects into a branch that never feeds the target and the eval
    # is inert. Reference overrides are exempt (they inject side data).
    descendants: set[str] = set()
    stack = [config.override_stage]
    while stack:
        node = stack.pop()
        for s in stages:
            if node in s.input_ids and s.id not in descendants:
                descendants.add(s.id)
                stack.append(s.id)
    if config.target_stage not in descendants:
        problems.append(
            f"target `{config.target_stage}` is not reachable from override "
            f"`{config.override_stage}`; the override would not affect it")
```

  b. Guard Condition 2's cases-table coverage on a present table:

```python
    # Condition 2: every injected table is a valid stand-in. The cases table is
    # only checkable once it exists; a dataless config skips this and is scored
    # for everything else (its file is validated when attached).
    if config.table is not None:
        problems += _coverage_problems(
            config.table.table_schema, by_id[config.override_stage], "cases table")
    for ov in config.reference_overrides:
        problems += _coverage_problems(
            ov.table.table_schema, by_id[ov.stage_id],
            f"reference override `{ov.stage_id}`")
```

  c. Guard the `dataset_cols` / key-in-dataset block on a present table (keep the key-in-target check, which needs no file):

```python
    dataset_cols = ({c.name for c in config.table.table_schema.columns}
                    if config.table is not None else None)
    for k in config.key:
        if dataset_cols is not None and k not in dataset_cols:
            problems.append(f"key column `{k}` is not in the cases table")
        if target_types and k not in target_types:
            problems.append(f"key column `{k}` is not emitted by target `{target.id}`")
```

  (Conditions 3/4 already read only `target.output_schema`; the structural check and the grain condition via `resolve_eval_run_settings` need no table — leave them.)

- [ ] **Step 4: Run tests** — `python -m pytest tests/test_eval_compat.py -q`. Expected: PASS (all, including the pre-existing cases).

- [ ] **Step 5: Full checks + commit**

```bash
python -m pytest -q && python -m ruff check app tests && python -m mypy
git add app/services/eval_compat.py tests/test_eval_compat.py
git commit -m "feat(evals): compatibility rejects unreachable pathway; tolerates no cases file"
git push origin eval-ui
```

---

### Task 3: Status "no cases yet" + read pages tolerate no cases

**Files:**
- Modify: `app/services/eval_store.py` (`eval_status`, ~line 182)
- Modify: `app/web/routers/evals.py` (`_eval_overlay_with_issues` ~line 112; `eval_detail` ~lines 605-639)
- Modify: `app/templates/eval_detail.html` (cases-table section; badge map ~line 8)
- Modify: `app/templates/evals_index.html`, `app/templates/_stage_content.html`, `app/templates/methodology.html` (each badge map — add the new status)
- Test: `tests/test_eval_store.py`, `tests/test_evals_web.py`

**Interfaces:**
- Consumes: `check_eval_compatibility` (Task 2), `EvalConfig.table` optional (Task 1).
- Produces: `eval_status(report, runs, latest_version, *, has_cases: bool) -> str`; the new keyword-only param is `True` when the config has a cases file. `eval_detail` renders with `config.table` possibly `None`.

- [ ] **Step 1: Write failing tests**:
  - In `tests/test_eval_store.py`: `test_eval_status_no_cases_yet` — a compatible `report` (`ok=True`), `runs=[]`, `latest_version=None`, `has_cases=False` → `"no cases yet"`. And `test_eval_status_broken_beats_no_cases` — `report.ok=False`, `has_cases=False` → `"broken"`. Update any existing `eval_status(...)` calls in this test file to pass `has_cases=True`.
  - In `tests/test_evals_web.py`: `test_eval_detail_dataless_shows_attach` — build a `table=None` config in the tmp methodology (via `save_eval_config`), GET the detail page → 200, body contains `attach cases` (the affordance) and does NOT contain a cases-table `<table>` header row; status badge text is `no cases yet`.

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_eval_store.py tests/test_evals_web.py -q`. Expected: new tests fail (extra kwarg unknown; detail page 500s on `config.table.table_schema`).

- [ ] **Step 3: Implement `eval_status`** in `app/services/eval_store.py` — add the keyword-only param and the branch, ranked directly below `broken`:

```python
def eval_status(report: CompatibilityReport, runs: list[EvalRun],
                latest_version: str | None, *, has_cases: bool) -> str:
    """One word for "what do we currently know about this eval". Ordered by
    alarm: incompatible beats everything; a config with no cases file can't run
    yet; a result only counts as current when its run pinned the version the
    methodology is at now. "run succeeded" means the run produced a result —
    the metrics say whether the result is good."""
    if not report.ok:
        return "broken"
    if not has_cases:
        return "no cases yet"
    if not runs:
        return "never run"
    latest = runs[0]
    if latest_version is None or latest.methodology_version != latest_version:
        return "stale"
    if latest.status in ("error", "vetoed"):
        return "run errored"
    return "run succeeded"
```

- [ ] **Step 4: Update `evals.py` call sites** — both compute `has_cases`:
  - In `_eval_overlay_with_issues` (~line 112):

```python
        status = ("broken" if runs_error
                  else eval_status(report, runs, latest_version,
                                   has_cases=config.table is not None))
```

  - In `eval_detail` (~line 605): same change to the `status = …` line (`config` is in scope).

- [ ] **Step 5: Guard `eval_detail`'s cases render** (`app/web/routers/evals.py` ~lines 609-621) — only read the file when there's a table:

```python
    if config.table is not None:
        cases_columns = [c.name for c in config.table.table_schema.columns]
        table_path = REPO_ROOT / config.table.path
        try:
            df = read_table(table_path, config.table.format)
            cases_capped = len(df) > CASES_PREVIEW_ROWS
            preview = df.head(CASES_PREVIEW_ROWS).fillna("").astype(str).to_dict(orient="records")
            cases_rows = [{str(k): v for k, v in row.items()} for row in preview]
        except (FileNotFoundError, ValueError) as exc:
            cases_error = str(exc)
```

  Initialise `cases_columns: list[str] = []` before the guard so the template always gets it. Add `"has_cases": config.table is not None` to the template context.

- [ ] **Step 6: Update `eval_detail.html`** — wrap the cases table in `{% if has_cases %}`; in the `{% else %}` branch render the attach affordance (a small form; the POST endpoint lands in Task 4):

```jinja
{% if has_cases %}
  {# existing cases-table markup (cases_error banner, table, cap note) #}
{% else %}
  <section class="attach-cases">
    <h2>Cases</h2>
    <p>No cases file is attached yet. This eval can't run until it has one.</p>
    <form method="post" action="/methodology/{{ methodology }}/evals/{{ config.id }}/attach-cases" enctype="multipart/form-data">
      <input type="file" name="file" accept=".csv,.parquet,.json" required>
      <button type="submit" class="btn primary">Attach cases</button>
    </form>
  </section>
{% endif %}
```

- [ ] **Step 7: Add the new status to all four badge maps** — in `eval_detail.html` (~line 8), `evals_index.html`, `_stage_content.html`, `methodology.html`, add `"no cases yet": "pending"` to the `{"run succeeded": "ok", ...}` dict. (These four copies are the known-duplicated map; the macro cleanup is a separate follow-up — just add the key to each here.)

- [ ] **Step 8: Run tests** — `python -m pytest tests/test_eval_store.py tests/test_evals_web.py -q`. Expected: PASS.

- [ ] **Step 9: Full checks + commit**

```bash
python -m pytest -q && python -m ruff check app tests && python -m mypy
git add app/services/eval_store.py app/web/routers/evals.py app/templates/eval_detail.html app/templates/evals_index.html app/templates/_stage_content.html app/templates/methodology.html tests/test_eval_store.py tests/test_evals_web.py
git commit -m "feat(evals): 'no cases yet' status; detail page tolerates a dataless eval"
git push origin eval-ui
```

---

### Task 4: Attach cases to a dataless eval

**Files:**
- Modify: `app/web/routers/evals.py` (new route + a shared helper extracted from the POST handler)
- Test: `tests/test_evals_web.py`

**Interfaces:**
- Consumes: `save_dataset_upload`, `validate_table_file`, `_derive_table_schema`, `check_eval_compatibility`, `load_eval_config`, `save_eval_config`.
- Produces: `POST /methodology/{m}/evals/{eval_id}/attach-cases` — multipart `file`; on success sets `config.table` and re-saves, 303 to the config page; on failure re-renders the detail page with an error. Route registered BEFORE the `/evals/{eval_id}` catch-all GET (it's a POST, so no collision, but keep it with the other eval-id routes).

- [ ] **Step 1: Write failing tests** in `tests/test_evals_web.py`:
  - `test_attach_cases_to_dataless_eval`: save a `table=None` config; POST a valid CSV (columns matching the derived schema) to `.../attach-cases` → 303 to the config page; reload the config → `config.table is not None` and its `path` points under `eval_data/`; the detail page now shows the cases table and status is no longer `no cases yet`.
  - `test_attach_cases_rejects_mismatched_file`: POST a CSV missing a required derived column → 200 (re-render), body contains the validation problem; the config still has `table is None`.

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_evals_web.py -q`. Expected: 404/405 (route absent).

- [ ] **Step 3: Implement**. Add the route to `app/web/routers/evals.py`. Reuse the upload+derive+validate logic already in `_handle_eval_form_post` (Steps around 473-539) by factoring the file-validation into a helper if it reduces duplication; otherwise inline the same sequence:

```python
@router.post("/methodology/{methodology}/evals/{eval_id}/attach-cases", response_model=None)
async def eval_attach_cases(
    request: Request, methodology: str, eval_id: str
) -> HTMLResponse | RedirectResponse:
    methodology_dir = EXAMPLES_DIR / methodology
    try:
        config = load_eval_config(methodology_dir, eval_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    listing = load_stages(methodology)
    by_id = {s.id: s for s in listing.stages}
    errors: list[str] = []

    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise HTTPException(status_code=422, detail="attach-cases needs a `file` upload")
    content = await upload.read()
    filename = upload.filename or ""
    try:
        fmt = _resolve_table_format(filename)
        saved = save_dataset_upload(methodology_dir, filename, content)
    except ValueError as exc:
        errors.append(str(exc))
    except FileExistsError as exc:
        errors.append(str(exc))

    schema = _derive_table_schema(
        by_id, config.override_stage, config.target_stage,
        list(config.key), list(config.input_columns),
        [{"actual": e.actual, "dataset": e.expected} for e in config.expected],
        errors,
    )
    if not errors:
        report = validate_table_file(saved, fmt, schema)
        errors.extend(i.message for i in report.issues if i.severity == "error")

    if errors:
        return _render_detail(request, methodology, methodology_dir, config, extra_errors=errors)

    config = config.model_copy(update={"table": TableRef(
        path=saved.relative_to(REPO_ROOT).as_posix(), format=fmt,
        table_schema=schema)})
    save_eval_config(methodology_dir, config)
    return RedirectResponse(
        url=f"/methodology/{methodology}/evals/{config.id}", status_code=303)
```

  Import `TableRef` from `app.models`. Extract the detail-page render body of `eval_detail` into a `_render_detail(request, methodology, methodology_dir, config, *, extra_errors: list[str] = []) -> HTMLResponse` helper and call it from both `eval_detail` and here, so the error re-render reuses the real page (add an `attach_errors` context key the `{% else %}` branch shows). Keep `eval_detail`'s behavior identical when `extra_errors` is empty.

- [ ] **Step 4: Run tests** — `python -m pytest tests/test_evals_web.py -q`. Expected: PASS.

- [ ] **Step 5: Full checks + commit**

```bash
python -m pytest -q && python -m ruff check app tests && python -m mypy
git add app/web/routers/evals.py app/templates/eval_detail.html tests/test_evals_web.py
git commit -m "feat(evals): attach a cases file to a dataless eval later"
git push origin eval-ui
```

---

### Task 5: Graph-select authoring + optional-file POST

**Files:**
- Modify: `app/web/routers/evals.py` (`eval_new_form`/`eval_edit_form` context; `_handle_eval_form_post` table-optional; a `descendants_map` helper)
- Modify: `app/templates/eval_form.html` (replace stage dropdowns with the graph selector + JS; make the file section optional)
- Modify: `app/web/diagrams.py` only if a reusable `build_mermaid_graph` call needs a caller — reuse as-is
- Test: `tests/test_evals_web.py`

**Interfaces:**
- Consumes: `build_mermaid_graph(stages, methodology)` from `app.web.diagrams`; `_derive_table_schema`, `check_eval_compatibility`.
- Produces: create/edit POST accepts an empty file → saves `table=None`; the new-eval page embeds a `descendants_map: dict[str, list[str]]` (stage id → downstream stage ids) as JSON.

- [ ] **Step 1: Write failing tests** in `tests/test_evals_web.py`:
  - `test_create_dataless_eval_via_form`: POST to `.../evals/new` with valid override/target/key/input/expected fields and NO file / empty `table_path` → 303 to the config page; the saved config has `table is None`; status shows `no cases yet`.
  - `test_new_form_embeds_descendants_map`: GET `.../evals/new` → 200, body contains `descendants_map` (the embedded JSON) and the workflow graph container (`class="dag"` or the mermaid block), and does NOT contain a `<select name="override_stage">` dropdown (dropdowns replaced by graph selection + hidden inputs).
  - `test_create_unreachable_pathway_rejected`: POST override/target that aren't on a shared path → 200 re-render, body contains `is not reachable from override` (server-side backstop; the JS prevents it in-browser but the server still enforces).

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_evals_web.py -q`. Expected: dataless create fails (table currently always built), descendants-map test fails (not embedded), dropdown-absent test fails.

- [ ] **Step 3: Implement the descendants helper** in `app/web/routers/evals.py`:

```python
def _descendants_map(stages: list[Stage]) -> dict[str, list[str]]:
    """stage id -> the stage ids reachable downstream of it, for the authoring
    graph's reachability dimming (a target must be reachable from the override)."""
    out: dict[str, list[str]] = {}
    for start in stages:
        seen: set[str] = set()
        stack = [start.id]
        while stack:
            node = stack.pop()
            for s in stages:
                if node in s.input_ids and s.id not in seen:
                    seen.add(s.id)
                    stack.append(s.id)
        out[start.id] = sorted(seen)
    return out
```

- [ ] **Step 4: Thread the graph into the form context** — in `eval_new_form` and `eval_edit_form`, add to the context: `"mermaid": build_mermaid_graph(listing.stages, methodology)` and `"descendants_map": _descendants_map(listing.stages)`. Keep `stages` (the template still needs id→name for labels and the checks pickers). Import `build_mermaid_graph`.

- [ ] **Step 5: Make the POST table-optional** in `_handle_eval_form_post`. When no file was uploaded and `table_path` is empty, build the config WITHOUT a table and skip the file-validation block:

```python
    has_file = bool(fields["table_path"])
    config_dict: dict[str, Any] = {
        "id": resolved_id,
        "methodology": methodology,
        "name": fields["name"],
        "description": fields["description"] or None,
        "override_stage": fields["override_stage"],
        "target_stage": fields["target_stage"],
        "key": fields["key"],
        "input_columns": fields["input_columns"],
        "expected": expected_dicts,
    }
    if has_file:
        config_dict["table"] = {
            "path": fields["table_path"],
            "format": fields["table_format"],
            "table_schema": table_schema.model_dump(mode="json"),
        }
```

  Then wrap the existing file-validation block (`table_path = REPO_ROOT / config.table.path` … `validate_table_file` …) in `if config.table is not None:`. The `check_eval_compatibility` call and the create-time id-collision check stay unconditional. (`_derive_table_schema` still runs — it also surfaces column-resolution errors, which are useful even with no file; that's fine.)

- [ ] **Step 6: Rework `eval_form.html`** — replace the two `<select>` stage pickers with:
  - the workflow graph: render `{{ mermaid | safe }}` inside a `<div class="dag">…</div>` (mirror how `methodology.html` renders it), plus two hidden inputs `<input type="hidden" name="override_stage" id="f-override">` / `name="target_stage" id="f-target"` prefilled from `values`.
  - the embedded map: `<script>const DESCENDANTS = {{ descendants_map | tojson }};</script>`.
  - a small script (mirror the pattern already in `methodology.html`'s overlay script — node ids are `flowchart-<stageId>-<n>`, so match `[id^="flowchart-<id>-"]`; on first node click set override + dim every node whose id is not in `DESCENDANTS[override]`; on second click (a non-dimmed node) set target + highlight; a "clear" button resets both hidden inputs and classes). Reuse the CSS classes `eval-dim` / `eval-overridden` / `eval-target` already in `style.css`. Leave a comment noting the observed mermaid node-id format.
  - Regions 2 (name/checks) and 3 (file) stay largely as-is, EXCEPT the file input is no longer required — add copy under it: "Optional — you can attach cases later." The checks builder and key/input pickers are unchanged (they already populate from `stage-schema` JSON).

- [ ] **Step 7: Manual DOM check** — from the worktree, `python -m uvicorn app.main:app --port 8791`, open `/methodology/lobbymap/evals/new`, confirm: clicking a node sets the override and dims unreachable nodes; a second click sets the target; clear resets; submitting with no file creates a `no cases yet` eval. Kill the server. If the mermaid node-id format differs from `flowchart-<id>-<n>`, adjust the selector and the comment. Do not leave scratch eval files under `examples/` (delete any; confirm `git status` clean).

- [ ] **Step 8: Run tests** — `python -m pytest tests/test_evals_web.py -q`. Expected: PASS.

- [ ] **Step 9: Full checks + commit**

```bash
python -m pytest -q && python -m ruff check app tests && python -m mypy
git add app/web/routers/evals.py app/templates/eval_form.html tests/test_evals_web.py
git commit -m "feat(evals): pick override/target on the workflow graph; file optional at create"
git push origin eval-ui
```

---

## Self-Review

**Spec coverage:**
- `EvalConfig.table` optional → Task 1. ✓
- Remove `EvalRun.passed`, per-row pass = all checks match (documented in model comment) → Task 1. ✓
- Reachability condition → Task 2. ✓
- `check_eval_compatibility` tolerates `table=None` → Task 2. ✓
- "no cases yet" status + `has_cases` param + badge maps → Task 3. ✓
- Read pages tolerate a dataless eval (detail attach affordance) → Task 3. ✓
- Attach cases later → Task 4. ✓
- Graph-select authoring + descendants/reachability dimming → Task 5. ✓
- File optional at create → Task 5. ✓
- Out of scope (hand-entry, code-scorer authoring, file replace, runner, badge-map macro) → not planned. ✓

**Type consistency:** `eval_status(report, runs, latest_version, *, has_cases: bool)` used identically in Task 3 Steps 3-4. `_descendants_map(stages) -> dict[str, list[str]]` defined and consumed in Task 5. `_derive_table_schema` called in Task 4 with the `[{"actual":…, "dataset":…}]` row shape its signature expects (matches its `expected_rows` param usage). `TableRef` fields `path`/`format`/`table_schema` match the model used in the current router.

**Placeholder scan:** no TBD/TODO; every code step shows the code or the exact edit; test steps give assertions.
