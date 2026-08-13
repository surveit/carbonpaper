"""View helpers for the project shell: the left-nav tree and the call-to-action.

The nav is navigation only — labels and hrefs, no marks. What a section's state is,
and what is waiting in it, its own page states in words.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from app.services import project
from app.web.breadcrumbs import Crumb, build_section_crumbs


class NavItem(BaseModel):
    key: str
    label: str
    href: str
    children: list["NavItem"] = Field(default_factory=list)


NavItem.model_rebuild()


class NextAction(BaseModel):
    key: str
    label: str
    href: str


class ShellState(project.ProjectState):
    nav: list[NavItem]
    crumbs: list[Crumb]
    next_action: NextAction


def shell_state(pdir: Path, section: str) -> ShellState:
    state = project.project_state(pdir)
    nav = build_nav(state.id)
    return ShellState(
        **state.model_dump(),
        nav=nav,
        crumbs=build_shell_crumbs(nav, section, state.id),
        next_action=_next_action(state),
    )


def build_shell_crumbs(nav: list[NavItem], section: str, project_id: str) -> list[Crumb]:
    for item in nav:
        if item.key == section:
            return build_section_crumbs(project_id, label=item.label)
        for child in item.children:
            if child.key == section:
                return build_section_crumbs(
                    project_id, label=child.label, parent=(item.label, item.href)
                )
    raise ValueError(f"no nav item for section '{section}' — the trail would be unlabelled")


def build_nav(project_id: str) -> list[NavItem]:
    base = f"/project/{project_id}"
    return [
        _nav_leaf("overview", "Overview", base),
        _nav_leaf("document", "Document", f"{base}/document"),
        _nav_leaf("terms", "Terms", f"{base}/terms"),
        _nav_leaf("files", "Files", f"{base}/files"),
        _nav_leaf("workflow", "Workflow", f"{base}/workflow",
                  children=[
                      _nav_leaf("versions", "Versions", f"{base}/workflow/versions"),
                      _nav_leaf("runs", "Runs", f"{base}/runs"),
                      _nav_leaf("evals", "Evals", f"{base}/evals"),
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
