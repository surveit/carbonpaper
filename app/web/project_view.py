"""View helpers for the project shell: the left-nav tree and the call-to-action.

The nav is navigation only — labels and hrefs, no marks. What a section's state is,
and what is waiting in it, its own page states in words.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from app.services import project


class NavItem(BaseModel):
    """`key` matches the active section, to highlight it."""

    key: str
    label: str
    href: str
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

    The tree is navigation, not a status report: it carries no mark at all. Every
    section's own page states its status, and its queue, in words."""
    base = f"/project/{state.name}"
    return [
        _nav_leaf("overview", "Overview", base),
        _nav_leaf("document", "Document", f"{base}/document"),
        _nav_leaf("data_model", "Data model", f"{base}/data_model"),
        _nav_leaf("workflow", "Workflow", f"{base}/workflow",
                  children=[
                      _nav_leaf("versions", "Versions", f"{base}/workflow/versions"),
                      _nav_leaf("runs", "Runs", f"{base}/runs"),
                      _nav_leaf("evals", "Evals", f"{base}/evals"),
                  ]),
    ]


def _next_action(state: project.ProjectState) -> NextAction:
    """The 'what to do next' rung for a project — first match wins. Returns a
    NextAction {key, label, href}; href is a section path under /project/<name>. Reads
    only the domain snapshot's data_model / workflow / runs; the decision is UI-facing
    (it picks the button a reviewer sees next), so it lives in the web layer.

    Ladder (top-down):
      1. no data model             → author it            (/data_model)
      2. no workflow               → build the workflow   (/workflow)
      3. workflow, 0 runs          → run it               (/runs/new)
      4. runs awaiting_review>0    → review the run       (/runs)
      5. otherwise                 → view runs            (/runs)
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
    # 2. No workflow → build it.
    if not workflow.present:
        return NextAction(
            key="build_workflow",
            label="Build the workflow",
            href=f"{base}/workflow",
        )
    # 3. Workflow present but never run → run it (on the run-launch page, which is
    #    where the version is picked and the inputs bound).
    if runs.n == 0:
        return NextAction(
            key="run_workflow",
            label="Run the workflow",
            href=f"{base}/runs/new",
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


# ─── Nav structure ────────────────────────────────────────────────────────────


def _nav_leaf(key: str, label: str, href: str,
              children: list[NavItem] | None = None) -> NavItem:
    return NavItem(key=key, label=label, href=href, children=children or [])
