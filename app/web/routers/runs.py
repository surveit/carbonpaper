"""Run lifecycle: trigger a run, list runs, poll live status, render a run's
detail + per-stage panel, the scratch in-memory re-run, artifact serving, and
resume."""

from __future__ import annotations

import html
import json
import threading
import traceback
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.errors import NoVersionToRunError
from app.services.loader import WorkflowLoadError, load_workflow
from app.runtime.preview import PREVIEWABLE_TYPES, PreviewError, run_stage_preview
from app.runtime.runner import prepare_run, resume_run, run_prepared
from app.runtime.trace import trace_row, trace_to_dict
from app.web.trace_view import build_trace_view
from app.web.config import EXAMPLES_DIR, REPO_ROOT, templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_mermaid_graph
from app.web.loading import (
    build_llm_example,
    find_stage,
    list_runs,
    load_manifest,
    load_output_preview,
    load_output_row,
    load_output_table,
    load_stages,
    manifest_stage,
    read_output_df,
    resolve_function_code,
    runs_dir,
)

router = APIRouter()


def run_in_background(target, *args) -> None:
    """Run a (possibly slow, LLM-driven) execution off the event loop so the
    run page stays responsive and can poll live progress. Errors are recorded
    on the manifest by the runner itself; this just keeps the thread from dying
    silently."""
    def _wrapped():
        try:
            target(*args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    threading.Thread(target=_wrapped, daemon=True).start()


@router.post("/project/{project}/run")
async def trigger_run(project: str):
    project_dir = EXAMPLES_DIR / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    # Set up the run (writes an initial `running` manifest), kick off execution
    # in a background thread, and redirect immediately. The run page polls.
    try:
        prep = prepare_run(project_dir, REPO_ROOT)
    except NoVersionToRunError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except WorkflowLoadError as exc:
        return JSONResponse({"detail": "compiled workflow failed validation",
                             "issues": exc.issues}, status_code=400)
    run_in_background(run_prepared, prep)
    return RedirectResponse(
        url=f"/project/{project}/runs/{prep['run_id']}",
        status_code=303,
    )


@router.get("/project/{project}/runs", response_class=HTMLResponse)
async def runs_index(request: Request, project: str):
    return templates.TemplateResponse(
        request,
        "runs_index.html",
        {"project": project, "runs": list_runs(project)},
    )


@router.get("/project/{project}/runs/{run_id}/status")
async def run_status(project: str, run_id: str):
    """Lightweight JSON for the live poller: current status, per-stage statuses,
    counts, and a freshly-built mermaid graph. Lets the run page update progress
    in place (no full-page reload) so it stays clickable while running."""
    manifest = load_manifest(runs_dir(project) / run_id)
    mstages = manifest.get("stages", [])
    status_by_id = {s["stage_id"]: s.get("status", "") for s in mstages}
    mermaid = build_mermaid_graph(load_stages(project).stages, project, status_by_id=status_by_id)

    def _count(st: str) -> int:
        return sum(1 for s in mstages if s.get("status") == st)

    return JSONResponse({
        "status": manifest.get("status"),
        "terminal": manifest.get("status") != "running",
        "halted_at": manifest.get("halted_at"),
        "finished_at": manifest.get("finished_at"),
        "counts": {"ok": _count("ok"), "warn": _count("validation_warnings"),
                   "err": _count("error"), "total": len(mstages),
                   "done": _count("ok") + _count("validation_warnings"),
                   "running": _count("running"), "pending": _count("pending"),
                   "awaiting": _count("awaiting_review")},
        "stages": [{"stage_id": s["stage_id"], "status": s.get("status")} for s in mstages],
        "mermaid": mermaid,
    })


@router.get("/project/{project}/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, project: str, run_id: str):
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)
    stages = load_stages(project).stages
    status_by_id = {s["stage_id"]: s.get("status", "") for s in manifest.get("stages", [])}
    mermaid = build_mermaid_graph(stages, project, status_by_id=status_by_id)

    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "project": project,
            "run_id": run_id,
            "manifest": manifest,
            "mermaid": mermaid,
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/partial",
    response_class=HTMLResponse,
)
async def run_stage_partial(
    request: Request, project: str, run_id: str, stage_id: str
):
    """Per-run stage detail panel — status, validation, preview, error trace."""
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)
    stage_record = next(
        (s for s in manifest.get("stages", []) if s.get("stage_id") == stage_id),
        None,
    )
    if stage_record is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in run")

    output_preview = load_output_preview(run_dir, stage_record.get("output_path"))

    # Build input previews from upstream stages' outputs in this run.
    stages_static = load_stages(project).stages
    stage_def = find_stage(stages_static, stage_id)
    output_by_id = {
        s.get("stage_id"): s.get("output_path") for s in manifest.get("stages", [])
    }
    input_previews: list[dict[str, Any]] = []
    if stage_def is not None:
        for input_id in stage_def.input_ids:
            input_previews.append(
                {
                    "id": input_id,
                    "preview": load_output_preview(run_dir, output_by_id.get(input_id)),
                }
            )

    function_code = resolve_function_code(stage_def)
    llm_example = build_llm_example(stage_def, input_previews) if stage_def else None

    return templates.TemplateResponse(
        request,
        "_run_stage_panel.html",
        {
            "project": project,
            "run_id": run_id,
            "stage": stage_record,
            "stage_def": stage_def,
            "preview": output_preview,
            "input_previews": input_previews,
            "function_code": function_code,
            "llm_example": llm_example,
            "previewable": stage_def is not None and stage_def.type in PREVIEWABLE_TYPES,
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/rows",
    response_class=HTMLResponse,
)
async def run_stage_rows(
    request: Request, project: str, run_id: str, stage_id: str
):
    """Full table of one stage's output, capped at MAX_TABLE_ROWS rendered rows.
    The page links to the uncapped CSV download."""
    run_dir = runs_dir(project) / run_id
    stage_record = manifest_stage(run_dir, stage_id)
    table = load_output_table(run_dir, stage_record.get("output_path"))
    return templates.TemplateResponse(
        request,
        "run_stage_rows.html",
        {
            "project": project,
            "run_id": run_id,
            "stage_id": stage_id,
            "stage": stage_record,
            "output_path": stage_record.get("output_path"),
            **table,
        },
    )


@router.get("/project/{project}/runs/{run_id}/stage/{stage_id}/rows.csv")
async def run_stage_rows_csv(project: str, run_id: str, stage_id: str):
    """One stage's complete output as a CSV download (no row cap)."""
    run_dir = runs_dir(project) / run_id
    stage_record = manifest_stage(run_dir, stage_id)
    df = read_output_df(run_dir, stage_record.get("output_path"))
    filename = f"{project}__{run_id}__{stage_id}.csv"
    return Response(
        content=df.to_csv(index=False),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/lineage_panel",
    response_class=HTMLResponse,
)
async def run_stage_lineage_panel(
    request: Request, project: str, run_id: str, stage_id: str, row: int
):
    """Minimal stage view for the lineage page: the transform, the output
    schema, and the output trimmed to `row`. Reuses `_stage_executable.html`
    and `schema_table` — not the whole run-detail panel."""
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)
    stage_record = next(
        (s for s in manifest.get("stages", []) if s.get("stage_id") == stage_id),
        None,
    )
    if stage_record is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in run")
    # Transform detail needs the compiled stage; if it's unavailable the output
    # table still renders (stage_def None → the template says so).
    try:
        stages = load_stages(project).stages
    except HTTPException:
        stages = []
    stage_def = find_stage(stages, stage_id)
    return templates.TemplateResponse(
        request,
        "_lineage_stage.html",
        {
            "project": project,
            "run_id": run_id,
            "stage": stage_record,
            "stage_def": stage_def,
            "function_code": resolve_function_code(stage_def),
            "preview": load_output_row(run_dir, stage_record.get("output_path"), row),
            "scoped_row": row,
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@router.get("/project/{project}/runs/{run_id}/stage/{stage_id}/row/{row}/trace")
async def run_stage_row_trace(project: str, run_id: str, stage_id: str, row: int):
    """Show-your-work for one output row: its ancestry through row-preserving
    stages, as JSON. 404 if the run/stage is absent, 400 if the row ordinal is
    out of range."""
    run_dir = runs_dir(project) / run_id
    load_manifest(run_dir)  # 404s if the run doesn't exist
    try:
        trace = trace_row(run_dir, stage_id, row)
    except ValueError as exc:
        detail = str(exc)
        if "not in run" in detail:
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc
    return JSONResponse(trace_to_dict(trace))


_TRACE_VIEW_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<title>lineage · __TITLE__</title>
<link rel="stylesheet" href="/static/style.css">
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>
.lin-wrap{max-width:1100px;margin:18px auto;padding:0 20px}
.lin-sub{color:#888;font-size:13px;margin-bottom:8px}
.toggle{display:inline-flex;border:1px solid #ddd;border-radius:8px;overflow:hidden;margin:4px 0 14px}
.toggle button{border:0;background:#fff;padding:5px 14px;font-size:13px;cursor:pointer;color:#555}
.toggle button.on{background:#1a3a72;color:#fff}
.story{font-size:15px;line-height:1.95}
.step{margin:2px 0}.stepno{display:inline-block;min-width:56px;color:#888;font-size:12px;font-weight:500}
.stage-link{font-weight:600;cursor:pointer;color:#1a3a72;text-decoration:underline;text-underline-offset:2px}
.row-chip{cursor:pointer;background:#eef4ff;color:#1a3a72;border:1px solid #cfe0ff;border-radius:6px;padding:0 7px;font-size:12.5px;white-space:nowrap}
.row-chip::before{content:"[";color:#8aa}.row-chip::after{content:"]";color:#8aa}
.claim-tag{font-size:10.5px;border-radius:5px;padding:0 5px;margin-left:3px;background:#e8f8e8;color:#1f5a1f}
.trunc{background:#fff4e6;color:#7a4a00;border-radius:8px;padding:8px 12px;font-size:13px;margin-bottom:12px}
.mermaid{background:#fff}.nograph{color:#888;font-size:13px}
.lin-note{color:#888;font-size:12.5px;margin-bottom:14px}
.lin-panel .lin-disc{margin:10px 0}
.lin-panel summary.disclosure{cursor:pointer;font-weight:500;padding:4px 0}
.lin-panel{margin-top:18px;border-top:1px solid #eee;padding-top:14px}
.lin-empty{color:#888;font-size:14px;padding:8px 0}
.hidden{display:none}
</style></head><body>
<div class="lin-wrap">
<h1>Lineage</h1><div class="lin-sub" id="sub"></div>
<div class="lin-note">Tracing the pipeline filtered to this single row — every step below is the one row on this row's path, so each carries [1 row].</div>
<div class="toggle"><button id="b-story" class="on">Story</button><button id="b-graph">Graph</button></div>
<div id="story" class="story"></div>
<div id="graph" class="hidden"><pre class="mermaid" id="mmd">__MERMAID__</pre><div id="nograph" class="nograph hidden">Graph needs the compiled workflow, which isn't available for this run.</div></div>
<div class="lin-panel" id="stage-panel"><div class="lin-empty">Click a step or a graph node to inspect that stage — its inputs, transform and output, trimmed to this row.</div></div>
</div>
<script>
const V = __PAYLOAD__, PROJECT = __PROJECT__;
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
document.getElementById('sub').textContent = `run ${V.run_id} · ${V.start_stage} row ${V.start_row} · reads top to bottom, step 1 first`;
const byStage = Object.fromEntries(V.nodes.map(n => [n.stage_id, n]));
// A transform-link opens the panel's Transform disclosure; a row-chip opens Output.
function tlink(sid, label){ return `<span class="stage-link" data-stage="${esc(sid)}" data-disc="transform">${esc(label)}</span>`; }
function ochip(n){ return `<span class="row-chip" data-stage="${esc(n.stage_id)}" data-disc="output">1 row</span>`; }
function verb(n){
  const k = n.transform.kind, sid = n.stage_id;
  if(k==='python') return `run python ${tlink(sid, sid + '()')}`;
  if(k==='llm') return `ask the LLM in ${tlink(sid, sid)}`;
  if(k==='join') return tlink(sid, 'join');
  return tlink(sid, sid);
}
function predsOf(step){
  const from = V.edges.filter(e => e.to_step === step).map(e => e.from_step);
  if(!from.length) return '';
  if(from.length === 1) return ` using step ${from[0]}'s output`;
  return ` joining steps ${from.join(' and ')}`;
}
// ---- Story: <transform-link> to get [1 row]; both open a panel disclosure ----
let html = '';
if(V.upstream.truncated){ html += `<div class="trunc">⋯ upstream not traced — ${esc(V.upstream.message)}</div>`; }
V.nodes.forEach((n, i) => {
  let sentence;
  if(i===0 && !V.upstream.truncated){
    sentence = `${tlink(n.stage_id, n.stage_id)} to get ${ochip(n)}`;
  } else {
    const claimTag = n.role==='claim' ? ' <span class="claim-tag">published claim</span>' : '';
    sentence = `${verb(n)}${predsOf(n.step)} to get ${ochip(n)}${claimTag}`;
  }
  html += `<div class="step"><span class="stepno">step ${n.step}</span>${sentence}</div>`;
});
document.getElementById('story').innerHTML = html;
// ---- The row-trimmed stage panel (the SAME _run_stage_panel.html as run-detail) ----
async function openStage(stageId, target){
  const n = byStage[stageId];
  if(!n) return;
  const panel = document.getElementById('stage-panel');
  panel.innerHTML = '<div class="lin-empty">loading…</div>';
  try {
    const r = await fetch(`/project/${encodeURIComponent(PROJECT)}/runs/${encodeURIComponent(V.run_id)}/stage/${encodeURIComponent(stageId)}/lineage_panel?row=${n.row_ordinal}`);
    if(!r.ok){ panel.innerHTML = `<div class="lin-empty">could not load ${esc(stageId)} (${r.status})</div>`; return; }
    panel.innerHTML = await r.text();
    const disc = panel.querySelector(`details[data-disc="${target || 'output'}"]`);
    if(disc) disc.open = true;
    panel.scrollIntoView({ behavior:'smooth', block:'start' });
  } catch(e){ panel.innerHTML = `<div class="lin-empty">error: ${esc(e)}</div>`; }
}
// mermaid's node click calls window.loadStage(sid); keep it distinct from
// openStage so this global can't shadow-recurse into itself.
window.loadStage = sid => openStage(sid, 'output');
document.getElementById('story').addEventListener('click', e => { const el = e.target.closest('[data-stage]'); if(el) openStage(el.dataset.stage, el.dataset.disc); });
// ---- Graph (reuses the central mermaid workflow component) ----
// mermaid loads from a CDN; if that's blocked, degrade gracefully — the story
// and panel must keep working, so guard every mermaid reference.
const mermaidOK = (typeof mermaid !== 'undefined');
const mmd = document.getElementById('mmd');
const hasGraph = mermaidOK && mmd.textContent.trim().length > 0;
if(!hasGraph){
  mmd.classList.add('hidden');
  const ng = document.getElementById('nograph');
  if(!mermaidOK) ng.textContent = "The graph library couldn't load (CDN blocked). The story and panel still work.";
  ng.classList.remove('hidden');
}
if(mermaidOK){ mermaid.initialize({ startOnLoad:false, theme:"default", flowchart:{curve:"basis", padding:20, useMaxWidth:true}, securityLevel:"loose" }); }
let graphRendered = false;
async function renderGraph(){ if(graphRendered || !hasGraph) return; graphRendered = true; try { await mermaid.run({ nodes:[mmd] }); } catch(e){ graphRendered = false; } }
// ---- Toggle ----
const story = document.getElementById('story'), graph = document.getElementById('graph');
const bStory = document.getElementById('b-story'), bGraph = document.getElementById('b-graph');
bStory.onclick = () => { story.classList.remove('hidden'); graph.classList.add('hidden'); bStory.classList.add('on'); bGraph.classList.remove('on'); };
bGraph.onclick = () => { graph.classList.remove('hidden'); story.classList.add('hidden'); bGraph.classList.add('on'); bStory.classList.remove('on'); renderGraph(); };
</script></body></html>"""


def _stages_by_id_safe(project: str) -> dict[str, Any]:
    """Compiled stages keyed by id for node-detail, or {} if they can't be
    loaded — the trace still renders (transform detail shows as unknown), since
    the tracer itself needs only the run directory."""
    try:
        listing = load_stages(project)
    except HTTPException:
        return {}
    return {s.id: s for s in listing.stages}


def _render_trace_html(view: dict[str, Any], mermaid: str, project: str) -> str:
    """The lineage page: a numbered story and a graph toggle on top; clicking a
    stage loads the shared row-trimmed stage panel below. Read-only."""
    # Embed the payload; neutralize any "</script" so it can't close the tag.
    payload = json.dumps(view).replace("</", "<\\/")
    title = html.escape(f"{view['start_stage']} · row {view['start_row']}")
    return (
        _TRACE_VIEW_HTML
        .replace("__TITLE__", title)
        .replace("__MERMAID__", html.escape(mermaid))
        .replace("__PROJECT__", json.dumps(project))
        .replace("__PAYLOAD__", payload)
    )


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/row/{row}/trace/view",
    response_class=HTMLResponse,
)
async def run_stage_row_trace_view(project: str, run_id: str, stage_id: str, row: int):
    """The row's show-your-work as a read-only HTML page: story view (default)
    with a graph toggle, same trace as the JSON endpoint."""
    run_dir = runs_dir(project) / run_id
    load_manifest(run_dir)
    try:
        trace = trace_row(run_dir, stage_id, row)
    except ValueError as exc:
        detail = str(exc)
        if "not in run" in detail:
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=400, detail=detail) from exc
    stages_by_id = _stages_by_id_safe(project)
    view = build_trace_view(trace_to_dict(trace), stages_by_id)
    ordered = [stages_by_id[n["stage_id"]] for n in view["nodes"]
               if n["stage_id"] in stages_by_id]
    mermaid = build_mermaid_graph(ordered, project) if len(ordered) == len(view["nodes"]) else ""
    return HTMLResponse(_render_trace_html(view, mermaid, project))


@router.post("/project/{project}/runs/{run_id}/stage/{stage_id}/preview")
async def run_stage_scratch_preview(
    request: Request, project: str, run_id: str, stage_id: str
):
    """SCRATCH in-memory re-run of one stage on a few selected input rows.

    Reads the chosen rows from this run's upstream outputs, runs the stage's
    handler in memory, and returns the output rows as JSON. Nothing is
    persisted: no manifest change, no output file, no artifact. Used by the
    node-detail panel's "Run transform on selected" button.

    Body (JSON): {"indices": [int, ...]}  — positional row indices into the
    stage's first upstream input.
    """
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    indices_raw = (body or {}).get("indices", [])
    indices: list[int] = []
    for i in indices_raw:
        try:
            indices.append(int(i))
        except (TypeError, ValueError):
            continue

    stages_static = load_stages(project).stages
    stage_def = find_stage(stages_static, stage_id)
    if stage_def is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}'")

    output_by_id = {
        s.get("stage_id"): s.get("output_path") for s in manifest.get("stages", [])
    }

    try:
        result = run_stage_preview(
            stage_def=stage_def,
            run_dir=run_dir,
            repo_root=REPO_ROOT,
            project_dir=EXAMPLES_DIR / project,
            output_by_id=output_by_id,
            selected_indices=indices,
        )
    except PreviewError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001 — surface the real failure
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}",
             "traceback": traceback.format_exc(limit=8)},
            status_code=500,
        )

    return JSONResponse({"ok": True, **result})


@router.get("/project/{project}/runs/{run_id}/artifact/{filename:path}", response_class=HTMLResponse)
async def run_artifact(project: str, run_id: str, filename: str):
    """Serve generated HTML artifacts (per-org profiles etc.) inline."""
    run_dir = runs_dir(project) / run_id
    candidate = (run_dir / "artifacts" / filename).resolve()
    if not candidate.exists() or not str(candidate).startswith(str(run_dir.resolve())):
        raise HTTPException(status_code=404, detail="Artifact not found")
    return HTMLResponse(content=candidate.read_text(encoding="utf-8"))


@router.post("/project/{project}/runs/{run_id}/resume")
async def resume_run_route(project: str, run_id: str):
    """Resume/continue a run from where it stopped, re-running any stage that is
    NOT already complete (so this serves BOTH: a halted run after its review
    decisions, AND an ERRORED run after the bug is fixed — it re-runs the failed
    stage + downstream and reuses completed upstream outputs)."""
    project_dir = EXAMPLES_DIR / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    run_dir = runs_dir(project) / run_id
    if not (run_dir / "manifest.json").exists():
        raise HTTPException(status_code=404, detail="Run not found")
    # Validate the compiled workflow synchronously so load errors surface as a 400
    # here rather than being swallowed on the background thread below.
    try:
        load_workflow(project_dir)
    except WorkflowLoadError as exc:
        return JSONResponse({"detail": "compiled workflow failed validation",
                             "issues": exc.issues}, status_code=400)
    # Resume re-runs the queue stage + downstream (LLM-heavy) — do it in the
    # background and redirect immediately so the page can poll progress.
    run_in_background(resume_run, project_dir, run_id, REPO_ROOT)
    return RedirectResponse(
        url=f"/project/{project}/runs/{run_id}",
        status_code=303,
    )
