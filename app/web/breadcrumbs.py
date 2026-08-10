"""The trail in the global header: the rungs naming where you are, and which ones switch.

A rung switches only where it names a thing the reader might have meant a different one
of — the project, the version being read, the run being read. Section rungs never do.
"""

from __future__ import annotations

from pydantic import BaseModel


class Crumb(BaseModel):
    """`href` None is the page you are on. `picker` is the partial a switcher rung loads."""

    label: str
    href: str | None = None
    is_code: bool = False
    picker: str | None = None


class PickerRow(BaseModel):
    """`badge_kind` is a `.badge` modifier (ok/warn/err/awaiting/pending), not a new hue."""

    label: str
    href: str
    is_code: bool = False
    badge: str | None = None
    badge_kind: str = "pending"
    description: str | None = None
    # An ISO timestamp the browser localises, and plain words beside it. Kept apart
    # because the `<time>` element the first one becomes would be a lie around the second.
    timestamp: str | None = None
    meta: str | None = None
    is_current: bool = False


class Picker(BaseModel):
    """`all_href` is set only where `rows` is the newest few of a longer list."""

    heading: str
    rows: list[PickerRow]
    all_href: str | None = None
    all_label: str | None = None


def build_section_crumbs(
    project: str, *, label: str, parent: tuple[str, str] | None = None
) -> list[Crumb]:
    """A project section that is itself the page; `parent` is the (label, href) above it."""
    above = [_link(*parent)] if parent else []
    return [*_project_trail(project), *above, _here(label)]


def build_version_crumbs(project: str, version_id: str) -> list[Crumb]:
    return [
        *_project_trail(project),
        _link("Workflow", _workflow_href(project)),
        _link("Versions", _versions_href(project)),
        _switcher(version_id, _versions_picker_href(project), is_code=True),
    ]


def build_run_crumbs(project: str, run_id: str) -> list[Crumb]:
    return [*_runs_trail(project), _switcher(run_id, _runs_picker_href(project), is_code=True)]


def build_runs_child_crumbs(project: str, *, label: str) -> list[Crumb]:
    """A page under Runs that is not itself a run — the launch form."""
    return [*_runs_trail(project), _here(label)]


def build_run_child_crumbs(project: str, run_id: str, *, label: str) -> list[Crumb]:
    """A page hanging off one run — its review queue, its stage rows, its lineage."""
    return [
        *build_run_crumbs(project, run_id)[:-1],
        _link(run_id, _run_href(project, run_id), is_code=True),
        _here(label),
    ]


def build_eval_crumbs(project: str, *, config_name: str) -> list[Crumb]:
    return [
        *_project_trail(project),
        _link("Workflow", _workflow_href(project)),
        _link("Evals", _evals_href(project)),
        _here(config_name),
    ]


def build_eval_run_crumbs(
    project: str, *, config_name: str, config_id: str, run_id: str
) -> list[Crumb]:
    return [
        *_project_trail(project),
        _link("Workflow", _workflow_href(project)),
        _link("Evals", _evals_href(project)),
        _link(config_name, f"{_evals_href(project)}/{config_id}"),
        _here(run_id, is_code=True),
    ]


def build_home_crumbs(here: str) -> list[Crumb]:
    """A page above any project — the project list itself, Admin."""
    return [_home(), _here(here)]


_HOME_LABEL = "workflow"
# Their own prefix: a picker beside the thing it lists gets shadowed by that thing's
# own `{id}` route, which matches "picker" as an id.
_PICKERS = "/pickers"
_PROJECTS_PICKER = "/pickers/projects"


def _project_trail(project: str) -> list[Crumb]:
    return [_home(), _switcher(project, _PROJECTS_PICKER)]


def _runs_trail(project: str) -> list[Crumb]:
    return [
        *_project_trail(project),
        _link("Workflow", _workflow_href(project)),
        _link("Runs", _runs_href(project)),
    ]


def _home() -> Crumb:
    return Crumb(label=_HOME_LABEL, href="/")


def _link(label: str, href: str, *, is_code: bool = False) -> Crumb:
    return Crumb(label=label, href=href, is_code=is_code)


def _here(label: str, *, is_code: bool = False) -> Crumb:
    return Crumb(label=label, is_code=is_code)


def _switcher(label: str, picker: str, *, is_code: bool = False) -> Crumb:
    return Crumb(label=label, picker=picker, is_code=is_code)


def _project_href(project: str) -> str:
    return f"/project/{project}"


def _workflow_href(project: str) -> str:
    return f"{_project_href(project)}/workflow"


def _versions_href(project: str) -> str:
    return f"{_workflow_href(project)}/versions"


def _versions_picker_href(project: str) -> str:
    return f"{_PICKERS}/project/{project}/versions"


def _runs_href(project: str) -> str:
    return f"{_project_href(project)}/runs"


def _run_href(project: str, run_id: str) -> str:
    return f"{_runs_href(project)}/{run_id}"


def _runs_picker_href(project: str) -> str:
    return f"{_PICKERS}/project/{project}/runs"


def _evals_href(project: str) -> str:
    return f"{_project_href(project)}/evals"
