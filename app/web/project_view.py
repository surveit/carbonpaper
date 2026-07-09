"""View helpers for the project shell (app.web layer).

app.services.project.project_state gives the DOMAIN status snapshot (counts,
states, coverage) — no UI, no URLs. This module adds what the shell needs to
RENDER but the domain layer must not know: the "what to do next" call-to-action,
whose label text and section href are presentation/routing concerns. The section
routes render shell_state(pdir), not the bare domain snapshot.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.services import project


class NextAction(BaseModel):
    """The sidebar's "what to do next" call-to-action: a stable key, the button
    label, and the section href it links to (a path under /project/<name>)."""

    key: str
    label: str
    href: str


class ShellState(project.ProjectState):
    """A project's domain status snapshot (project.ProjectState) plus the sidebar's
    next_action CTA. The CTA is a web-layer concern (label text + routing), so it is
    added here rather than in the domain model. This is the object the shell and its
    section templates render."""

    next_action: NextAction


def shell_state(pdir: Path) -> ShellState:
    """The domain status snapshot (project.project_state) plus the sidebar's
    next_action CTA. This is the object the shell and its section templates render."""
    state = project.project_state(pdir)
    return ShellState(**state.model_dump(), next_action=_next_action(state))


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
      5. workflow approved, 0 runs → run it               (/workflow)
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
    # 5. Workflow fully approved but never run → run it (the run button is on /workflow).
    if runs.n == 0:
        return NextAction(
            key="run_workflow",
            label="Run the workflow",
            href=f"{base}/workflow",
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
