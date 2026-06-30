"""
pages.py — the COMPILER feature's PAGE routes (HTML, read-only).

Split out of `app/main.py` so the compiler's UI isn't bolted onto the app shell.
The action/JSON side of the feature lives in `app/api/compile.py`; the rendering
helpers + singletons both share live in `app/web_context.py`.

    GET /compile                  — list of compilation objects
    GET /compile/new              — the "new compilation" form
    GET /compile/{compilation_id} — the compilation object view (input · what
                                    happened · DAG output)

Declared on an APIRouter that `main.py` mounts via app.include_router(). Route
order matters: `/compile/new` is declared before `/compile/{compilation_id}` so
"new" is not captured as an id.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import compiler
from app.web_context import (
    COMPILATIONS_DIR,
    TYPE_CLASS,
    TYPE_GLYPH,
    build_mermaid_graph,
    list_inputs,
    templates,
)

router = APIRouter()


@router.get("/compile", response_class=HTMLResponse)
async def compilations_index(request: Request):
    """LIST of compilation objects (parallels runs_index). Each row is a persisted
    compilation; "New compilation" opens the form."""
    return templates.TemplateResponse(
        request,
        "compilations_index.html",
        {"compilations": compiler.list_compilations(COMPILATIONS_DIR)},
    )


@router.get("/compile/new", response_class=HTMLResponse)
async def compile_new_form(request: Request):
    """The compile FORM — pick an input, an out-name, a model."""
    return templates.TemplateResponse(
        request,
        "compile_new.html",
        {"inputs": list_inputs()},
    )


@router.get("/compile/new-methodology")
async def compile_new_methodology_redirect():
    """Back-compat: the gated-compile form moved to the project create form
    (GET /methodology/new). Anything still linking the old URL lands on the new one.
    Declared HERE (before /compile/{compilation_id}) so 'new-methodology' is matched
    as a literal and not captured as a compilation id."""
    return RedirectResponse(url="/methodology/new", status_code=302)


@router.post("/compile/new-methodology")
async def compile_new_methodology_post_redirect():
    """Back-compat for any cached POST to the old gated-create endpoint — point at the
    new create route (303 so the browser re-issues it as the GET form, not a re-POST)."""
    return RedirectResponse(url="/methodology/new", status_code=303)


@router.get("/compile/{compilation_id}", response_class=HTMLResponse)
async def compilation_detail(request: Request, compilation_id: str):
    """The COMPILATION OBJECT view (parallels run_detail). Three sections:
    (a) INPUT — the source + a prose excerpt of what was fed in;
    (b) WHAT HAPPENED — the LLM prompt sent, the raw response, the validation result;
    (c) DAG OUTPUT — mermaid graph + stage table + methodology_raw.md."""
    try:
        comp = compiler.load_compilation(COMPILATIONS_DIR, compilation_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Compilation not found")

    stages = comp["stages"]
    mermaid = build_mermaid_graph(stages, comp["manifest"].get("name", compilation_id)) if stages else None

    return templates.TemplateResponse(
        request,
        "compile_detail.html",
        {
            "compilation_id": compilation_id,
            "manifest": comp["manifest"],
            "what_happened": comp["what_happened"],
            "stages": stages,
            "methodology_raw": comp["methodology_raw"],
            "error_text": comp["error_text"],
            "mermaid": mermaid,
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
        },
    )
