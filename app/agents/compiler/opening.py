"""The written first turn, chosen by the shape of the page the chat was opened on."""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel

from app.core.agent.registry import OpeningTurn


class PageOpening(BaseModel):
    # Verbatim from the app's routes; an arch test fails a template naming none.
    route: str
    says: str
    offers: list[str]


def choose_opening_turn(path: str | None, project_name: str | None) -> OpeningTurn:
    """`path` is where the chat was opened, `project_name` what to call the project there."""
    page = find_page_opening(path) if project_name else None
    if page is None:
        return _fallback_turn(project_name)
    return OpeningTurn(text=page.says.format(name=project_name), offers=page.offers)


def find_page_opening(path: str | None) -> PageOpening | None:
    if not path:
        return None
    # ?column= on a lineage page, ?chat= once the rail is open: the route is the path.
    segments = urlparse(path).path.strip("/").split("/")
    for page in PAGE_OPENINGS:
        if _matches_route(segments, page.route):
            return page
    return None


def _matches_route(segments: list[str], route: str) -> bool:
    expected = route.strip("/").split("/")
    if len(segments) != len(expected):
        return False
    return all(
        want.startswith("{") or want == got
        for want, got in zip(expected, segments, strict=True)
    )


def _fallback_turn(project_name: str | None) -> OpeningTurn:
    if project_name is None:
        return OpeningTurn(text=_NO_PROJECT_SAYS, offers=_NO_PROJECT_OFFERS)
    return OpeningTurn(
        text=_PROJECT_SAYS.format(name=project_name), offers=_PROJECT_OFFERS)


_NO_PROJECT_SAYS = (
    "How can I help? I build the workflows here — the stages that turn your data into a "
    "result someone else can check."
)

_NO_PROJECT_OFFERS = [
    "Start from data I have",
    "Start from a methodology document",
    "Change a project that exists",
]

_PROJECT_SAYS = "How can I help with {name}?"

_PROJECT_OFFERS = [
    "Explain what this project does",
    "Where does this project stand?",
    "Edit the workflow",
]


# Longest route first: /project/{id} sits under every route below it.
PAGE_OPENINGS = [
    PageOpening(
        route="/project/{project_id}/runs/{run_id}/stage/{stage_id}/row/{row}/trace/view",
        says="How can I help with this value?",
        offers=[
            "Explain how this value was built",
            "Check this value for mistakes",
            "Show me the rows behind it",
        ],
    ),
    PageOpening(
        route="/project/{project_name}/workflow/version/{version_id}",
        says="How can I help with this saved version of {name}?",
        offers=["Explain what changed in this version", "Run this version as a test"],
    ),
    PageOpening(
        route="/project/{project_name}/workflow/versions",
        says="How can I help with {name}'s saved versions?",
        offers=["Compare the saved versions", "Which version did the last run use?"],
    ),
    PageOpening(
        route="/project/{project_id}/runs/{run_id}",
        says="How can I help with this {name} run?",
        offers=[
            "Explain what happened",
            "Review the run for possible mistakes",
            "Rerun with different inputs",
        ],
    ),
    PageOpening(
        route="/project/{project_id}/runs",
        says="How can I help with {name}'s runs?",
        offers=["Compare the recent runs", "Start a new run"],
    ),
    PageOpening(
        route="/project/{project_name}/workflow",
        says="How can I help with the {name} workflow?",
        offers=[
            "Explain what this workflow does",
            "Edit the workflow",
            "Run this workflow",
        ],
    ),
    PageOpening(
        # Nothing here reads the document, so neither offer claims to have read it.
        route="/project/{project_name}/methodology",
        says="How can I help with the {name} methodology?",
        offers=["Explain what this workflow does", "Edit the workflow"],
    ),
    PageOpening(
        route="/project/{project_name}/glossary",
        says="How can I help with {name}'s agreed words?",
        offers=["What do these terms control?", "Add a term"],
    ),
    PageOpening(
        route="/project/{project_id}/files",
        says="How can I help with {name}'s files?",
        offers=["What's in this data?", "Use one of these as an input"],
    ),
    PageOpening(
        route="/project/{project_name}",
        says=_PROJECT_SAYS,
        offers=_PROJECT_OFFERS,
    ),
]
