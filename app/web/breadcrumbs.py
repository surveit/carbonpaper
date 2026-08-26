"""The trail in the global header: the rungs naming where you are, and which ones switch.

A rung switches only where it names a thing the reader might have meant a different one
of — the project, the version being read, the run being read. Section rungs never do.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.services.project_record import read_project_name


class Crumb(BaseModel):
    """`picker_current` is the id the popover marks as current, where that is not the label."""

    label: str
    href: str | None = None
    is_code: bool = False
    picker: str | None = None
    picker_current: str | None = None


class PickerRow(BaseModel):
    """`badge_kind` is a `.badge` modifier: ok/warn/err/awaiting/pending, not a new hue."""

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
    heading: str
    rows: list[PickerRow]
    all_href: str | None = None
    all_label: str | None = None


def build_section_crumbs(
    project_id: str, *, label: str, parent: tuple[str, str] | None = None
) -> list[Crumb]:
    above = [_link(*parent)] if parent else []
    return [*_project_trail(project_id), *above, _here(label)]


def build_version_crumbs(project_id: str, version_id: str) -> list[Crumb]:
    return [
        *_project_trail(project_id),
        _link("Workflow", _workflow_href(project_id)),
        _link("Versions", _versions_href(project_id)),
        _switcher(version_id, _versions_picker_href(project_id), is_code=True),
    ]


def build_run_crumbs(project_id: str, run_id: str) -> list[Crumb]:
    return [*_runs_trail(project_id), _switcher(run_id, _runs_picker_href(project_id), is_code=True)]


def build_runs_child_crumbs(project_id: str, *, label: str) -> list[Crumb]:
    return [*_runs_trail(project_id), _here(label)]


def build_run_child_crumbs(project_id: str, run_id: str, *, label: str) -> list[Crumb]:
    return [
        *build_run_crumbs(project_id, run_id)[:-1],
        _link(run_id, _run_href(project_id, run_id), is_code=True),
        _here(label),
    ]


def build_eval_crumbs(project_id: str, *, config_name: str) -> list[Crumb]:
    return [
        *_project_trail(project_id),
        _link("Workflow", _workflow_href(project_id)),
        _link("Evals", _evals_href(project_id)),
        _here(config_name),
    ]


def build_eval_run_crumbs(
    project_id: str, *, config_name: str, config_id: str, run_id: str
) -> list[Crumb]:
    return [
        *_project_trail(project_id),
        _link("Workflow", _workflow_href(project_id)),
        _link("Evals", _evals_href(project_id)),
        _link(config_name, f"{_evals_href(project_id)}/{config_id}"),
        _here(run_id, is_code=True),
    ]


def build_figure_crumbs(project_id: str, run_id: str, *, label: str) -> list[Crumb]:
    """No switcher: the figure card is read by someone with no other project in mind."""
    return [
        _home(),
        _link(read_project_name(project_id), _project_href(project_id)),
        _link(run_id, _run_href(project_id, run_id), is_code=True),
        _here(label),
    ]


def build_home_crumbs(here: str) -> list[Crumb]:
    return [_home(), _here(here)]


def build_chat_crumbs(title: str | None) -> list[Crumb]:
    return [_home(), _link(_CHATS_LABEL, _CHATS_HREF), _here(title or _UNTITLED_SESSION)]


# The trail's first rung renders the wordmark (_wordmark.html) rather than this text.
_HOME_LABEL = "Carbon Paper"
_CHATS_LABEL = "Chats"
_CHATS_HREF = "/chat"
# States the absence rather than naming the session something it is not called.
_UNTITLED_SESSION = "Untitled session"
# Their own prefix: a picker beside the thing it lists gets shadowed by that thing's
# own `{id}` route, which matches "picker" as an id.
_PICKERS = "/pickers"
_PROJECTS_PICKER = "/pickers/projects"


def _project_trail(project_id: str) -> list[Crumb]:
    """Every caller passes an ID; the crumb READS as the name, which may repeat."""
    return [
        _home(),
        _switcher(read_project_name(project_id), _PROJECTS_PICKER, picker_current=project_id),
    ]


def _runs_trail(project_id: str) -> list[Crumb]:
    return [
        *_project_trail(project_id),
        _link("Workflow", _workflow_href(project_id)),
        _link("Runs", _runs_href(project_id)),
    ]


def _home() -> Crumb:
    return Crumb(label=_HOME_LABEL, href="/")


def _link(label: str, href: str, *, is_code: bool = False) -> Crumb:
    return Crumb(label=label, href=href, is_code=is_code)


def _here(label: str, *, is_code: bool = False) -> Crumb:
    return Crumb(label=label, is_code=is_code)


def _switcher(label: str, picker: str, *, is_code: bool = False,
              picker_current: str | None = None) -> Crumb:
    return Crumb(label=label, picker=picker, is_code=is_code, picker_current=picker_current)


def _project_href(project_id: str) -> str:
    return f"/project/{project_id}"


def _workflow_href(project_id: str) -> str:
    return f"{_project_href(project_id)}/workflow"


def _versions_href(project_id: str) -> str:
    return f"{_workflow_href(project_id)}/versions"


def _versions_picker_href(project_id: str) -> str:
    return f"{_PICKERS}/project/{project_id}/versions"


def _runs_href(project_id: str) -> str:
    return f"{_project_href(project_id)}/runs"


def _run_href(project_id: str, run_id: str) -> str:
    return f"{_runs_href(project_id)}/{run_id}"


def _runs_picker_href(project_id: str) -> str:
    return f"{_PICKERS}/project/{project_id}/runs"


def _evals_href(project_id: str) -> str:
    return f"{_project_href(project_id)}/evals"
