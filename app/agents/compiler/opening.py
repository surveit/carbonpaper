"""The written first turn, chosen by the shape of the page the chat was opened on."""

from __future__ import annotations

from pydantic import BaseModel

from app.core.agent.registry import OpeningTurn


class PageOpening(BaseModel):
    # Verbatim from the app's routes; an arch test fails a template naming none.
    route: str
    says: str
    offers: list[str]


def choose_opening_turn(path: str | None, project_name: str | None) -> OpeningTurn:
    """`path` is where the chat was opened, `project_name` what to call the project there."""
    page = find_page_opening(path)
    if page is None:
        return _fallback_turn(project_name)
    return OpeningTurn(text=_render_says(page.says, project_name), offers=page.offers)


def find_page_opening(path: str | None) -> PageOpening | None:
    if not path:
        return None
    segments = path.strip("/").split("/")
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


def _render_says(says: str, project_name: str | None) -> str:
    if project_name is None:
        return says.replace("You're in {name}, on ", "You're on ").replace(
            "You're in {name}", "You're here")
    return says.format(name=project_name)


def _fallback_turn(project_name: str | None) -> OpeningTurn:
    if project_name is None:
        return OpeningTurn(text=_NO_PROJECT_SAYS, offers=_NO_PROJECT_OFFERS)
    return OpeningTurn(
        text=f"You're in {project_name}, and I can see the page you're on.",
        offers=_IN_PROJECT_OFFERS)


_NO_PROJECT_SAYS = (
    "I build and edit the workflows here — the stages that turn your data into a result "
    "someone else can check."
)

_NO_PROJECT_OFFERS = [
    "Start from data I have",
    "Start from a methodology document",
    "Change a project that exists",
]

_IN_PROJECT_OFFERS = [
    "What is this page telling me?",
    "What would you change here?",
    "Run it and show me the rows",
]


# Longest route first: /project/{id} sits under every route below it.
PAGE_OPENINGS = [
    PageOpening(
        route="/project/{project_id}/runs/{run_id}/stage/{stage_id}/row/{row}/trace/view",
        says="You're in {name}, on the lineage for one row — every step that made it.",
        offers=[
            "Explain how this value was built",
            "Did anything upstream error?",
            "Show me the rows around it",
        ],
    ),
    PageOpening(
        route="/project/{project_name}/workflow/version/{version_id}",
        says="You're in {name}, on one saved version of the workflow.",
        offers=[
            "What changed in this version?",
            "What would this establish if I ran it?",
            "Run it as a test",
        ],
    ),
    PageOpening(
        route="/project/{project_name}/workflow/versions",
        says="You're in {name}, on its saved versions.",
        offers=["Which version did the last run use?", "What changed between them?"],
    ),
    PageOpening(
        route="/project/{project_id}/runs/{run_id}",
        says="You're in {name}, on one run.",
        offers=[
            "How did this run go?",
            "Show me what it published",
            "Run it again on more rows",
        ],
    ),
    PageOpening(
        route="/project/{project_id}/runs",
        says="You're in {name}, on its runs.",
        offers=["Which run should I trust?", "Start a new run"],
    ),
    PageOpening(
        route="/project/{project_name}/workflow",
        says="You're in {name}, on the workflow — the stages that make the result.",
        offers=[
            "What does this workflow establish?",
            "Add or change a stage",
            "Run it as a test",
        ],
    ),
    PageOpening(
        route="/project/{project_name}/methodology",
        says="You're in {name}, on the methodology — what this project says it does.",
        offers=["Does the workflow match this document?", "Change the document"],
    ),
    PageOpening(
        route="/project/{project_name}/glossary",
        says="You're in {name}, on the words it has agreed.",
        offers=["What do these terms control?", "Add a term"],
    ),
    PageOpening(
        route="/project/{project_id}/files",
        says="You're in {name}, on its files.",
        offers=["What's in this data?", "Use one of these as an input"],
    ),
    PageOpening(
        route="/project/{project_name}",
        says="You're in {name}.",
        offers=[
            "Where does this project stand?",
            "What should I do next?",
            "Change the workflow",
        ],
    ),
]
