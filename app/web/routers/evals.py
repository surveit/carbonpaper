"""Eval pages: the evals home for a methodology, a config's detail page
(pathway, compatibility problems, cases table, scoring rules, run history), a
single run's detail page, and the authoring form (create + edit).
`build_eval_overlay` assembles the per-eval status/pathway summary shared by
the evals home page and the methodology page's workflow-graph overlay."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from app.models import Column, EvalConfig, EvalRun, FileFormat, Stage, TableRef, TableSchema
from app.models.schema import format_errors
from app.services.eval_compat import check_eval_compatibility
from app.services.eval_store import (
    eval_status,
    latest_version_id,
    list_eval_configs,
    list_eval_runs,
    load_eval_config,
    load_eval_run,
    save_dataset_upload,
    save_eval_config,
)
from app.services.table_check import read_table, table_columns, validate_table_file
from app.web.config import EXAMPLES_DIR, REPO_ROOT, templates
from app.web.diagrams import build_mermaid_graph
from app.web.loading import load_stages

router = APIRouter()

CASES_PREVIEW_ROWS = 50

# Extension -> FileFormat, for inferring format from an uploaded or
# path-referenced filename. Eval datasets support the same tabular formats
# table_check.read_table does; geojson is deliberately excluded there too.
_FORMAT_BY_EXTENSION = {
    ".csv": FileFormat.csv, ".parquet": FileFormat.parquet, ".json": FileFormat.json,
}


class StageSchemaColumn(TypedDict):
    name: str
    type: str


class StageSchemaResponse(TypedDict):
    stage_id: str
    columns: list[StageSchemaColumn]


class InspectTableResponse(TypedDict):
    columns: list[str]
    path: str


class EvalOverlayEntry(TypedDict):
    """One eval's pathway summary, used both by the evals home table and the
    methodology page's workflow-graph overlay. `overridden` and `executing`
    are stage-id lists; `target` is a single stage id, or "" for a config that
    failed to parse (it has no resolvable pathway). `executing` is empty
    whenever compatibility couldn't derive run settings (unresolved stages, or
    the config itself is unreadable)."""
    id: str
    name: str
    status: str
    overridden: list[str]
    executing: list[str]
    target: str
    url: str


def _list_eval_runs_safe(methodology_dir: Path, config_id: str) -> tuple[list[EvalRun], str | None]:
    """`list_eval_runs` raises loudly on a malformed `eval_run/*.json` file (by
    design — see eval_store.list_eval_runs). A page should still render: return
    the error text instead so the template can show it in a `.load-issues`
    block rather than a 500."""
    try:
        return list_eval_runs(methodology_dir, config_id), None
    except (OSError, ValueError) as exc:
        return [], str(exc)


def _eval_overlay_with_issues(
    methodology: str, methodology_dir: Path, stages: list[Stage]
) -> list[tuple[EvalOverlayEntry, list[str]]]:
    """One (overlay entry, issues) pair per `eval_config/*.yaml` file. Issues
    are parse problems (unreadable YAML/schema) for a config that failed to
    load, or the run-listing error for one that loaded fine but whose
    `eval_run/` has a corrupt file — kept separate from `EvalOverlayEntry`
    because that shape is shared with the workflow-graph overlay, which has
    no use for free-text issue strings."""
    entries = list_eval_configs(methodology_dir)
    latest_version = latest_version_id(methodology_dir)
    out: list[tuple[EvalOverlayEntry, list[str]]] = []
    for entry in entries:
        eval_id = entry.path.stem
        url = f"/methodology/{methodology}/evals/{eval_id}"
        if entry.config is None:
            out.append((EvalOverlayEntry(
                id=eval_id, name=eval_id, status="broken",
                overridden=[], executing=[], target="", url=url,
            ), entry.issues))
            continue
        config = entry.config
        report = check_eval_compatibility(config, stages)
        runs, runs_error = _list_eval_runs_safe(methodology_dir, config.id)
        status = ("broken" if runs_error else
                  eval_status(report, runs, latest_version,
                              has_cases=config.table is not None))
        executing = report.settings.frontier if report.settings is not None else []
        overridden = [config.override_stage,
                      *(ov.stage_id for ov in config.reference_overrides)]
        out.append((EvalOverlayEntry(
            id=config.id, name=config.name, status=status,
            overridden=overridden, executing=executing,
            target=config.target_stage, url=url,
        ), [runs_error] if runs_error else []))
    return out


def build_eval_overlay(
    methodology: str, methodology_dir: Path, stages: list[Stage]
) -> list[EvalOverlayEntry]:
    """One entry per `eval_config/*.yaml` file, in `list_eval_configs` order. A
    config that fails to parse gets `overridden=[]`, `executing=[]`,
    `target=""` — it still shows up (as `broken`) but contributes nothing to a
    workflow pathway since it has none."""
    return [entry for entry, _issues in
            _eval_overlay_with_issues(methodology, methodology_dir, stages)]


def uncovered_stages(stages: list[Stage], overlay: list[EvalOverlayEntry]) -> list[str]:
    """Stage ids on no eval's pathway (overridden, executing, or target on ANY
    eval). Empty when there are zero evals — that's its own empty state, not
    every stage being a warning."""
    if not overlay:
        return []
    covered: set[str] = set()
    for e in overlay:
        covered.update(e["overridden"])
        covered.update(e["executing"])
        if e["target"]:
            covered.add(e["target"])
    return [s.id for s in stages if s.id not in covered]


@router.get("/methodology/{methodology}/evals", response_class=HTMLResponse)
async def evals_index(request: Request, methodology: str):
    listing = load_stages(methodology)
    methodology_dir = EXAMPLES_DIR / methodology
    rows = [
        {**entry, "issues": issues}
        for entry, issues in
        _eval_overlay_with_issues(methodology, methodology_dir, listing.stages)
    ]

    return templates.TemplateResponse(
        request,
        "evals_index.html",
        {
            "methodology": methodology,
            "evals": rows,
            "load_issues": listing.issues,
        },
    )


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


def _stage_options(stages: list[Stage]) -> list[dict[str, Any]]:
    """Stage picker options for the form: id, name, and whether the stage has
    no output schema (that stage can't be an override or target -- selecting
    one would leave the eval unable to derive required columns)."""
    return [
        {"id": s.id, "name": s.name, "schemaless": s.output_schema is None}
        for s in stages
    ]


def _stage_columns(stage: Stage) -> list[StageSchemaColumn]:
    if stage.output_schema is None:
        return []
    return [StageSchemaColumn(name=c.name, type=c.type) for c in stage.output_schema.columns]


class EvalFormValues(TypedDict):
    """Everything the form template needs to redisplay a submission (valid or
    not) plus prefill an edit. `expected_rows` is the parallel-array data
    zipped into per-row dicts for easy template iteration."""
    id: str
    name: str
    description: str
    override_stage: str
    target_stage: str
    table_path: str
    table_format: str
    expected_rows: list[dict[str, str]]


def _empty_expected_row() -> dict[str, str]:
    return {"actual": "", "dataset": "", "metric": "exact", "tolerance": ""}


def _values_from_config(config: EvalConfig) -> EvalFormValues:
    return EvalFormValues(
        id=config.id,
        name=config.name,
        description=config.description or "",
        override_stage=config.override_stage,
        target_stage=config.target_stage,
        table_path=config.table.path if config.table is not None else "",
        table_format=config.table.format if config.table is not None else "csv",
        expected_rows=[
            {
                "actual": exp.actual,
                "dataset": exp.expected,
                "metric": exp.metric,
                "tolerance": "" if exp.tolerance is None else str(exp.tolerance),
            }
            for exp in config.expected
        ] or [_empty_expected_row()],
    )


def _blank_values() -> EvalFormValues:
    return EvalFormValues(
        id="", name="", description="", override_stage="", target_stage="",
        table_path="", table_format="csv",
        expected_rows=[_empty_expected_row()],
    )


@router.get("/methodology/{methodology}/evals/new", response_class=HTMLResponse)
async def eval_new_form(request: Request, methodology: str):
    listing = load_stages(methodology)
    return templates.TemplateResponse(
        request,
        "eval_form.html",
        {
            "methodology": methodology,
            "mode": "create",
            "eval_id": None,
            "stages": _stage_options(listing.stages),
            "mermaid": build_mermaid_graph(listing.stages, methodology),
            "descendants_map": _descendants_map(listing.stages),
            "values": _blank_values(),
            "errors": [],
        },
    )


@router.get("/methodology/{methodology}/evals/{eval_id}/edit", response_class=HTMLResponse)
async def eval_edit_form(request: Request, methodology: str, eval_id: str):
    methodology_dir = EXAMPLES_DIR / methodology
    try:
        config = load_eval_config(methodology_dir, eval_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    listing = load_stages(methodology)
    return templates.TemplateResponse(
        request,
        "eval_form.html",
        {
            "methodology": methodology,
            "mode": "edit",
            "eval_id": eval_id,
            "stages": _stage_options(listing.stages),
            "mermaid": build_mermaid_graph(listing.stages, methodology),
            "descendants_map": _descendants_map(listing.stages),
            "values": _values_from_config(config),
            "errors": [],
        },
    )


def _derive_table_schema(
    by_id: dict[str, Stage],
    override_stage: str,
    target_stage: str,
    expected_rows: list[dict[str, str]],
    errors: list[str],
) -> TableSchema:
    """The user never authors the cases table's column types -- they're
    sourced from the stages the eval binds to. The injected columns are
    `override_stage`'s entire output schema (an eval replaces that stage's
    whole output, so there's no meaningful subset); each expected row's
    dataset column is typed by its `actual` column on `target_stage`'s output
    schema. A column that can't be resolved to a type (unknown stage, no
    output_schema, or the column isn't declared there) is skipped in the
    derived schema -- that gap is exactly what check_eval_compatibility
    reports -- but is also recorded here as a form-level error so the user
    sees why."""
    override = by_id.get(override_stage)
    target = by_id.get(target_stage)

    columns: dict[str, Column] = {}
    if override is None:
        errors.append(f"override stage `{override_stage}` does not exist in the methodology")
    elif override.output_schema is None:
        errors.append(f"override stage `{override_stage}` declares no output schema")
    else:
        for col in override.output_schema.columns:
            columns[col.name] = Column(name=col.name, type=col.type)

    target_types: dict[str, str] = {}
    if target is None:
        errors.append(f"target stage `{target_stage}` does not exist in the methodology")
    elif target.output_schema is None:
        errors.append(f"target stage `{target_stage}` declares no output schema")
    else:
        target_types = {c.name: c.type for c in target.output_schema.columns}

    for row in expected_rows:
        actual = row["actual"]
        dataset_name = row["dataset"]
        if not actual or not dataset_name:
            continue
        col_type = target_types.get(actual)
        if col_type is None:
            errors.append(
                f"expected column asserts on `{actual}`, which target `{target_stage}` does not emit"
            )
            continue
        columns[dataset_name] = Column(name=dataset_name, type=col_type)

    return TableSchema(columns=list(columns.values()))


@router.get(
    "/methodology/{methodology}/evals/stage-schema/{stage_id}.json",
)
async def stage_schema_json(methodology: str, stage_id: str) -> JSONResponse:
    listing = load_stages(methodology)
    stage = next((s for s in listing.stages if s.id == stage_id), None)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"no stage `{stage_id}` in {methodology}")
    if stage.output_schema is None:
        return JSONResponse(
            status_code=422,
            content={"error": f"stage `{stage_id}` declares no output schema"},
        )
    body: StageSchemaResponse = StageSchemaResponse(
        stage_id=stage_id, columns=_stage_columns(stage)
    )
    return JSONResponse(content=dict(body))


def _resolve_table_format(filename: str) -> FileFormat:
    ext = Path(filename).suffix.lower()
    fmt = _FORMAT_BY_EXTENSION.get(ext)
    if fmt is None:
        raise ValueError(f"unrecognized table file extension `{ext}` in `{filename}`")
    return fmt


@router.post("/methodology/{methodology}/evals/inspect-table")
async def inspect_table(request: Request, methodology: str) -> JSONResponse:
    form = await request.form()
    methodology_dir = EXAMPLES_DIR / methodology

    upload = form.get("file")
    if isinstance(upload, UploadFile):
        content = await upload.read()
        filename = upload.filename or ""
        try:
            fmt = _resolve_table_format(filename)
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"error": str(exc)})
        try:
            saved_path = save_dataset_upload(methodology_dir, filename, content)
        except FileExistsError as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"error": str(exc)})
        columns = table_columns(saved_path, fmt)
        body: InspectTableResponse = InspectTableResponse(
            columns=columns, path=saved_path.relative_to(REPO_ROOT).as_posix()
        )
        return JSONResponse(content=dict(body))

    raw_path = form.get("path")
    if isinstance(raw_path, str) and raw_path:
        candidate = (EXAMPLES_DIR / methodology / raw_path).resolve()
        if not candidate.is_file():
            candidate = (REPO_ROOT / raw_path).resolve()
        if not candidate.is_relative_to(REPO_ROOT.resolve()):
            raise HTTPException(status_code=404, detail=f"path escapes the repo: {raw_path}")
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail=f"no table file at {raw_path}")
        try:
            fmt = _resolve_table_format(candidate.name)
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"error": str(exc)})
        columns = table_columns(candidate, fmt)
        body = InspectTableResponse(
            columns=columns, path=candidate.relative_to(REPO_ROOT.resolve()).as_posix()
        )
        return JSONResponse(content=dict(body))

    raise HTTPException(status_code=422, detail="inspect-table needs a `file` upload or a `path` field")


async def _read_eval_form(request: Request) -> dict[str, Any]:
    """Pull the eval-authoring fields out of a submitted form, as plain python
    values (parallel arrays zipped into row dicts). No validation here -- the
    handler validates via EvalConfig / table_check / eval_compat."""
    form = await request.form()

    def _str(key: str) -> str:
        v = form.get(key, "")
        return v if isinstance(v, str) else ""

    actual = [v for v in form.getlist("expected_actual") if isinstance(v, str)]
    dataset = [v for v in form.getlist("expected_dataset") if isinstance(v, str)]
    metric = [v for v in form.getlist("expected_metric") if isinstance(v, str)]
    tolerance = [v for v in form.getlist("expected_tolerance") if isinstance(v, str)]

    n = max(len(actual), len(dataset), len(metric), len(tolerance))
    expected_rows = []
    for i in range(n):
        row_actual = actual[i] if i < len(actual) else ""
        row_dataset = dataset[i] if i < len(dataset) else ""
        row_metric = metric[i] if i < len(metric) else "exact"
        row_tolerance = tolerance[i] if i < len(tolerance) else ""
        if not row_actual and not row_dataset:
            continue  # drop fully-empty trailing rows
        expected_rows.append({
            "actual": row_actual,
            "dataset": row_dataset,
            "metric": row_metric or "exact",
            "tolerance": row_tolerance,
        })

    return {
        "id": _str("id"),
        "name": _str("name"),
        "description": _str("description"),
        "override_stage": _str("override_stage"),
        "target_stage": _str("target_stage"),
        "table_path": _str("table_path") or _str("path"),
        "table_format": _str("table_format") or "csv",
        "expected_rows": expected_rows,
    }


async def _handle_eval_form_post(
    request: Request, methodology: str, eval_id: str | None
) -> HTMLResponse | RedirectResponse:
    """Shared create/edit POST handler. `eval_id` is the path id for edit (the
    posted id is ignored for edit -- it always saves under the same id);
    `None` for create, where the posted id is used."""
    fields = await _read_eval_form(request)
    resolved_id = eval_id if eval_id is not None else fields["id"]

    listing = load_stages(methodology)
    by_id = {s.id: s for s in listing.stages}

    errors: list[str] = []

    table_schema = _derive_table_schema(
        by_id,
        fields["override_stage"],
        fields["target_stage"],
        fields["expected_rows"],
        errors,
    )

    expected_dicts = []
    for row in fields["expected_rows"]:
        tolerance: float | None = None
        if row["tolerance"]:
            try:
                tolerance = float(row["tolerance"])
            except ValueError:
                errors.append(f"tolerance '{row['tolerance']}' is not a number")
        expected_dicts.append({
            "actual": row["actual"],
            "expected": row["dataset"],
            "metric": row["metric"],
            "tolerance": tolerance,
        })

    has_file = bool(fields["table_path"])
    config_dict: dict[str, Any] = {
        "id": resolved_id,
        "methodology": methodology,
        "name": fields["name"],
        "description": fields["description"] or None,
        "override_stage": fields["override_stage"],
        "target_stage": fields["target_stage"],
        "expected": expected_dicts,
    }
    if has_file:
        config_dict["table"] = {
            "path": fields["table_path"],
            "format": fields["table_format"],
            "table_schema": table_schema.model_dump(mode="json"),
        }

    config: EvalConfig | None = None
    try:
        config = EvalConfig.model_validate(config_dict)
    except ValidationError as exc:
        errors.extend(format_errors(exc))

    if config is not None:
        if config.table is not None:
            table_path = REPO_ROOT / config.table.path
            if not table_path.is_file():
                errors.append(f"cases table not found: {config.table.path}")
            else:
                try:
                    validation_report = validate_table_file(
                        table_path, config.table.format, config.table.table_schema
                    )
                except (FileNotFoundError, ValueError) as exc:
                    errors.append(str(exc))
                else:
                    errors.extend(
                        issue.message for issue in validation_report.issues
                        if issue.severity == "error"
                    )

        compat = check_eval_compatibility(config, listing.stages)
        errors.extend(compat.problems)

        if eval_id is None:
            existing_path = EXAMPLES_DIR / methodology / "eval_config" / f"{config.id}.yaml"
            if existing_path.is_file():
                errors.append(f"an eval with id '{config.id}' already exists")

    values = EvalFormValues(
        id=resolved_id,
        name=fields["name"],
        description=fields["description"],
        override_stage=fields["override_stage"],
        target_stage=fields["target_stage"],
        table_path=fields["table_path"],
        table_format=fields["table_format"],
        expected_rows=fields["expected_rows"] or [_empty_expected_row()],
    )

    if errors or config is None:
        return templates.TemplateResponse(
            request,
            "eval_form.html",
            {
                "methodology": methodology,
                "mode": "edit" if eval_id is not None else "create",
                "eval_id": eval_id,
                "stages": _stage_options(listing.stages),
                "mermaid": build_mermaid_graph(listing.stages, methodology),
                "descendants_map": _descendants_map(listing.stages),
                "values": values,
                "errors": errors,
            },
            status_code=200,
        )

    methodology_dir = EXAMPLES_DIR / methodology
    save_eval_config(methodology_dir, config)
    return RedirectResponse(
        url=f"/methodology/{methodology}/evals/{config.id}", status_code=303
    )


@router.post("/methodology/{methodology}/evals/new", response_model=None)
async def eval_create(request: Request, methodology: str) -> HTMLResponse | RedirectResponse:
    return await _handle_eval_form_post(request, methodology, eval_id=None)


@router.post("/methodology/{methodology}/evals/{eval_id}/edit", response_model=None)
async def eval_edit_submit(
    request: Request, methodology: str, eval_id: str
) -> HTMLResponse | RedirectResponse:
    return await _handle_eval_form_post(request, methodology, eval_id=eval_id)


def _render_detail(
    request: Request,
    methodology: str,
    methodology_dir: Path,
    config: EvalConfig,
    *,
    attach_errors: list[str] | None = None,
) -> HTMLResponse:
    """Build the eval detail page for a loaded config. `attach_errors` carries
    problems from a just-submitted attach-cases upload (empty list when there
    are none, e.g. a normal GET of the page) so the template can show them
    alongside the attach-cases form without disturbing any other section."""
    listing = load_stages(methodology)
    report = check_eval_compatibility(config, listing.stages)
    runs, runs_error = _list_eval_runs_safe(methodology_dir, config.id)
    latest_version = latest_version_id(methodology_dir)
    status = ("broken" if runs_error else
              eval_status(report, runs, latest_version,
                          has_cases=config.table is not None))

    executing = report.settings.frontier if report.settings is not None else []

    cases_columns: list[str] = []
    cases_rows: list[dict[str, Any]] = []
    cases_error: str | None = None
    cases_capped = False
    if config.table is not None:
        cases_columns = [c.name for c in config.table.table_schema.columns]
        table_path = (REPO_ROOT / config.table.path)
        try:
            df = read_table(table_path, config.table.format)
            cases_capped = len(df) > CASES_PREVIEW_ROWS
            preview = df.head(CASES_PREVIEW_ROWS).fillna("").astype(str).to_dict(orient="records")
            cases_rows = [{str(k): v for k, v in row.items()} for row in preview]
        except (FileNotFoundError, ValueError) as exc:
            cases_error = str(exc)

    return templates.TemplateResponse(
        request,
        "eval_detail.html",
        {
            "methodology": methodology,
            "config": config,
            "report": report,
            "status": status,
            "executing": executing,
            "runs": runs,
            "runs_error": runs_error,
            "cases_columns": cases_columns,
            "cases_rows": cases_rows,
            "cases_error": cases_error,
            "cases_capped": cases_capped,
            "cases_cap": CASES_PREVIEW_ROWS,
            "has_cases": config.table is not None,
            "attach_errors": attach_errors or [],
        },
    )


@router.post("/methodology/{methodology}/evals/{eval_id}/attach-cases", response_model=None)
async def eval_attach_cases(
    request: Request, methodology: str, eval_id: str
) -> HTMLResponse | RedirectResponse:
    """Attach a cases file to a config that was saved with `table=None` (Task
    3 lets an eval be authored without one). Reuses the same
    upload/derive/validate sequence `_handle_eval_form_post` runs for the
    authoring form's table field."""
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
        [{"actual": e.actual, "dataset": e.expected} for e in config.expected],
        errors,
    )
    if not errors:
        report = validate_table_file(saved, fmt, schema)
        errors.extend(i.message for i in report.issues if i.severity == "error")

    if errors:
        return _render_detail(request, methodology, methodology_dir, config, attach_errors=errors)

    config = config.model_copy(update={"table": TableRef(
        path=saved.relative_to(REPO_ROOT).as_posix(), format=fmt,
        table_schema=schema)})
    save_eval_config(methodology_dir, config)
    return RedirectResponse(
        url=f"/methodology/{methodology}/evals/{config.id}", status_code=303)


@router.get("/methodology/{methodology}/evals/{eval_id}", response_class=HTMLResponse)
async def eval_detail(request: Request, methodology: str, eval_id: str):
    methodology_dir = EXAMPLES_DIR / methodology
    try:
        config = load_eval_config(methodology_dir, eval_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _render_detail(request, methodology, methodology_dir, config)


@router.get(
    "/methodology/{methodology}/evals/{eval_id}/runs/{run_id}",
    response_class=HTMLResponse,
)
async def eval_run_detail(request: Request, methodology: str, eval_id: str, run_id: str):
    methodology_dir = EXAMPLES_DIR / methodology
    try:
        config = load_eval_config(methodology_dir, eval_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        run = load_eval_run(methodology_dir, run_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"no run {run_id!r} for this eval"
        ) from exc
    except ValueError as exc:
        # The requested run file itself exists but can't be read -- distinct
        # from "not found": say so explicitly rather than folding it into a
        # 404, and don't let it be confused with some other run being broken.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return templates.TemplateResponse(
        request,
        "eval_run.html",
        {
            "methodology": methodology,
            "config": config,
            "run": run,
        },
    )
