"""View helpers for the project shell (app.web layer).

app.services.project.project_state gives the DOMAIN status snapshot (counts,
states, coverage) — no UI, no URLs. This module adds what the shell needs to
RENDER but the domain layer must not know: the left-nav tree (labels, hrefs, and
a per-item status token) and the "what to do next" call-to-action. The section
routes render shell_state(pdir), not the bare domain snapshot.

The nav carries a semantic `status` token per item (ok / warn / bad / todo / none
/ review / present / home / evals); the TEMPLATE maps that token to a glyph + colour
(project_shell.html), so the visual vocabulary lives next to the markup while the
structure + classification stay here and stay unit-testable.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from app.core.run_status import RunStatus
from app.services import project


class NavItem(BaseModel):
    """One sidebar entry: a stable `key` (matched against the active `section` to
    highlight it), the visible label, the section href, a semantic `status` token
    (the template maps it to a glyph + colour), and any child entries rendered
    indented beneath it. Only the Workflow group carries children."""

    key: str
    label: str
    href: str
    status: str
    children: list["NavItem"] = Field(default_factory=list)


NavItem.model_rebuild()


class NextAction(BaseModel):
    """The sidebar's "what to do next" call-to-action: a stable key, the button
    label, and the section href it links to (a path under /project/<name>)."""

    key: str
    label: str
    href: str


class ShellState(project.ProjectState):
    """A project's domain status snapshot (project.ProjectState) plus the two
    web-layer additions the shell renders: the left-nav tree and the next_action
    CTA (label text + routing — presentation concerns the domain model must not
    carry). This is the object the shell and its section templates render."""

    nav: list[NavItem]
    next_action: NextAction


def shell_state(pdir: Path) -> ShellState:
    """The domain status snapshot (project.project_state) plus the sidebar's nav
    tree and next_action CTA. This is the object the shell and its section templates
    render."""
    state = project.project_state(pdir)
    return ShellState(
        **state.model_dump(),
        nav=build_nav(state),
        next_action=_next_action(state),
    )


def build_nav(state: project.ProjectState) -> list[NavItem]:
    """The shell's left-nav tree: Overview / Document / Data model / Workflow, with
    Versions, Runs, and Evals nested under Workflow — the three things a workflow
    has: its versioned snapshots, its executions, and the evals that score them.

    Each item's `status` is derived from `state` (the truthful on-disk status); the
    template turns it into a glyph. An absent thing is "none" (renders ○), never a
    fabricated done-marker."""
    base = f"/project/{state.name}"
    return [
        _nav_leaf("overview", "Overview", base, "home"),
        _nav_leaf("document", "Document", f"{base}/document",
                  _present_status(state.has_document)),
        _nav_leaf("data_model", "Data model", f"{base}/data_model",
                  _data_model_status(state.data_model)),
        _nav_leaf("workflow", "Workflow", f"{base}/workflow",
                  _workflow_status(state.workflow),
                  children=[
                      _nav_leaf("versions", "Versions", f"{base}/workflow/versions",
                                _present_status(state.versions > 0)),
                      _nav_leaf("runs", "Runs", f"{base}/runs",
                                _runs_status(state.runs)),
                      _nav_leaf("evals", "Evals", f"{base}/evals", "evals"),
                  ]),
    ]


def _next_action(state: project.ProjectState) -> NextAction:
    """The 'what to do next' rung for a project — first match wins. Returns a
    NextAction {key, label, href}; href is a section path under /project/<name>. Reads
    only the domain snapshot's data_model / workflow / runs; the decision is UI-facing
    (it picks the button a reviewer sees next), so it lives in the web layer.

    Ladder (top-down):
      1. no data model             → author it            (/data_model)
      2. data model not approved   → approve it           (/data_model)
      3. no workflow               → build the workflow   (/workflow)
      4. workflow approved<total   → review the workflow  (/workflow)
      5. workflow approved, 0 runs → run it               (/workflow/versions)
      6. runs awaiting_review>0    → review the run       (/runs)
      7. otherwise                 → view runs            (/runs)
    """
    name = state.name
    data_model = state.data_model
    workflow = state.workflow
    runs = state.runs
    base = f"/project/{name}"

    # 1. No data model → author it.
    if not data_model.present:
        return NextAction(
            key="author_data_model",
            label="Author the data model",
            href=f"{base}/data_model",
        )
    # 2. Data model present but not approved → approve it.
    if data_model.state != "approved":
        return NextAction(
            key="approve_data_model",
            label="Approve the data model",
            href=f"{base}/data_model",
        )
    # 3. Data model approved, no workflow → build the workflow.
    if not workflow.present:
        return NextAction(
            key="build_workflow",
            label="Build the workflow",
            href=f"{base}/workflow",
        )
    # 4. Workflow present but not fully approved → review the workflow.
    cov = workflow.coverage
    if cov is not None and cov.approved < cov.total:
        return NextAction(
            key="review_workflow",
            label="Review the workflow",
            href=f"{base}/workflow",
        )
    # 5. Workflow fully approved but never run → run it (picked from the version
    #    list, since a run pins to a published version).
    if runs.n == 0:
        return NextAction(
            key="run_workflow",
            label="Run the workflow",
            href=f"{base}/workflow/versions",
        )
    # 6. A run is halted awaiting review → review the run.
    if runs.awaiting_review > 0:
        return NextAction(
            key="review_run",
            label="Review the run",
            href=f"{base}/runs",
        )
    # 7. Nothing outstanding → view runs.
    return NextAction(
        key="view_runs",
        label="View runs",
        href=f"{base}/runs",
    )


# ─── Nav structure + status tokens ────────────────────────────────────────────
# A nav item's status is a semantic token (ok / warn / bad / todo / none / review /
# present / home / evals); project_shell.html maps it to a glyph + colour. Deriving
# it here (not in Jinja) keeps the classification testable and the template dumb.


def _nav_leaf(key: str, label: str, href: str, status: str,
              children: list[NavItem] | None = None) -> NavItem:
    """Build a NavItem with a status token and optional children."""
    return NavItem(key=key, label=label, href=href, status=status,
                   children=children or [])


def _present_status(present: bool) -> str:
    """Present/absent items (Document, Versions): "present" when the thing exists,
    "none" when it does not."""
    return "present" if present else "none"


def _data_model_status(data_model: project.DataModelStatus) -> str:
    """The data model's status token by approval state."""
    by_state = {
        "approved": "ok",
        "edited_stale": "warn",
        "rejected": "bad",
        "unreviewed": "todo",
        "none": "none",
    }
    return by_state.get(data_model.state, "none")


def _workflow_status(workflow: project.WorkflowStatus) -> str:
    """The workflow's status token by coverage: "none" when there is no workflow,
    "ok" when every stage is approved, "warn" otherwise. (The data model is optional
    input to the workflow, so there is no locked state.)"""
    coverage = workflow.coverage
    if not workflow.present:
        return "none"
    if coverage is not None and coverage.total > 0 and coverage.approved == coverage.total:
        return "ok"
    return "warn"


def _runs_status(runs: project.RunsSummary) -> str:
    """The runs' status token: "review" when a run awaits review (that wins), "none"
    when there are no runs, then by the latest run's status (ok / bad / todo)."""
    if runs.awaiting_review > 0:
        return "review"
    if runs.n == 0:
        return "none"
    if runs.latest_status == RunStatus.OK:
        return "ok"
    # "error" (singular) is not a RunStatus member the runner ever writes at the
    # run level (only RunStatus.ERRORS, plural) — matched here defensively
    # alongside it in case an older/foreign manifest used the singular form.
    if runs.latest_status in (RunStatus.ERRORS, "error"):
        return "bad"
    return "todo"
