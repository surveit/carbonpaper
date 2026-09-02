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


def choose_opening_turn(path: str | None, in_project: bool) -> OpeningTurn:
    """Every page below sits under /project/<id>, so one outside a project has no words here."""
    page = find_page_opening(path) if in_project else None
    if page is None:
        return _fallback_turn(in_project)
    return OpeningTurn(text=page.says, offers=page.offers)


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


def _fallback_turn(in_project: bool) -> OpeningTurn:
    if not in_project:
        return OpeningTurn(text=_NO_PROJECT_SAYS, offers=_NO_PROJECT_OFFERS)
    return OpeningTurn(text=_PROJECT_SAYS, offers=_PROJECT_OFFERS)


_NO_PROJECT_SAYS = "Hello. What would you like to do today?"

_NO_PROJECT_OFFERS = [
    "Start from data I have",
    "Start from a methodology document",
    "Change a project that exists",
]

_PROJECT_SAYS = "Hello. What do you want to do with this project?"

_PROJECT_OFFERS = [
    "Explain what this project does",
    "Where does this project stand?",
    "Edit the workflow",
]


# Longest route first: /project/{id} sits under every route below it.
PAGE_OPENINGS = [
    PageOpening(
        route="/project/{project_id}/runs/{run_id}/stage/{stage_id}/row/{row}/trace/view",
        says="Hello. What do you want to do with this row lineage?",
        offers=["Explain how to use this page", "Explain how this value was built"],
    ),
    PageOpening(
        route="/project/{project_name}/workflow/version/{version_id}",
        says="Hello. What do you want to do with this saved version?",
        offers=["Explain what changed in this version", "Run this version as a test"],
    ),
    PageOpening(
        route="/project/{project_name}/workflow/versions",
        says="Hello. What do you want to do with these saved versions?",
        offers=["Compare the saved versions", "Which version did the last run use?"],
    ),
    PageOpening(
        route="/project/{project_id}/runs/{run_id}",
        says="Hello. What do you want to do with this run?",
        offers=[
            "Explain what happened",
            "Review the run for potential errors",
            "Rerun with different inputs",
        ],
    ),
    PageOpening(
        route="/project/{project_id}/runs",
        says="Hello. What do you want to do with these runs?",
        offers=["Compare the recent runs", "Start a new run"],
    ),
    PageOpening(
        route="/project/{project_name}/workflow",
        says="Hello. What do you want to do with this workflow?",
        offers=[
            "Explain what this workflow does",
            "Edit the workflow",
            "Run this workflow",
        ],
    ),
    PageOpening(
        route="/project/{project_name}/methodology",
        says="Hello. What do you want to do with this project's documentation?",
        offers=["What do these terms control?", "Add a term"],
    ),
    PageOpening(
        route="/project/{project_id}/files",
        says="Hello. What do you want to do with these files?",
        offers=["What's in this data?", "Use one of these as an input"],
    ),
]
