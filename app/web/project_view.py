"""View helpers for the project shell: the left-nav tree and the call-to-action.

The nav is navigation only — labels and hrefs, no marks. What a section's state is,
and what is waiting in it, its own page states in words.
"""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.services import project
from app.web.breadcrumbs import Crumb, build_section_crumbs


class NavItem(BaseModel):
    key: str
    label: str
    href: str
    children: list["NavItem"] = Field(default_factory=list)


class NavGroup(BaseModel):
    """A heading over its items, naming no page of its own."""

    label: str
    children: list[NavItem]


NavItem.model_rebuild()

NavBlock = NavItem | NavGroup


class NextAction(BaseModel):
    key: str
    label: str
    href: str


class ShellState(project.ProjectState):
    nav: list[NavBlock]
    crumbs: list[Crumb]
    next_action: NextAction


# The web layer's one project guard: every route that names a project runs it, so a
# missing (or escaping) id is a 404 here rather than a confusing empty page below.
def validate_project_or_404(project_id: str) -> str:
    if not project.project_exists(project_id):
        raise HTTPException(status_code=404, detail=f"No project '{project_id}'")
    return project_id


def shell_state(project_id: str, section: str) -> ShellState:
    state = project.project_state(validate_project_or_404(project_id))
    nav = build_nav(state.id)
    return ShellState(
        **state.model_dump(),
        nav=nav,
        crumbs=build_shell_crumbs(nav, section, state.id),
        next_action=_next_action(state),
    )


def build_shell_crumbs(nav: list[NavBlock], section: str, project_id: str) -> list[Crumb]:
    for block in nav:
        crumbs = _find_section_crumbs(block, section, project_id)
        if crumbs is not None:
            return crumbs
    raise ValueError(f"no nav item for section '{section}' — the trail would be unlabelled")


def build_nav(project_id: str) -> list[NavBlock]:
    base = f"/project/{project_id}"
    return [
        _nav_leaf("overview", "Overview", base),
        _nav_leaf("workflow", "Workflow", f"{base}/workflow",
                  children=[
                      _nav_leaf("files", "Files", f"{base}/files"),
                      _nav_leaf("versions", "Versions", f"{base}/workflow/versions"),
                      _nav_leaf("runs", "Runs", f"{base}/runs"),
                      _nav_leaf("evals", "Evals", f"{base}/evals"),
                  ]),
        NavGroup(label="Documentation", children=[
            _nav_leaf("methodology", "Methodology", f"{base}/methodology"),
            _nav_leaf("terms", "Terms", f"{base}/terms"),
        ]),
    ]


def _next_action(state: project.ProjectState) -> NextAction:
    project_id = state.id
    data_model = state.data_model
    workflow = state.workflow
    runs = state.runs
    base = f"/project/{project_id}"

    # 1. No nouns → the words have not been agreed.
    if not data_model.present:
        return NextAction(
            key="agree_terms",
            label="Agree the project's terms",
            href=f"{base}/terms",
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


def _find_section_crumbs(block: NavBlock, section: str, project_id: str) -> list[Crumb] | None:
    # A group opens no page, so its children hang straight off the project rung.
    parent: tuple[str, str] | None = None
    if isinstance(block, NavItem):
        if block.key == section:
            return build_section_crumbs(project_id, label=block.label)
        parent = (block.label, block.href)
    for child in block.children:
        if child.key == section:
            return build_section_crumbs(project_id, label=child.label, parent=parent)
    return None
