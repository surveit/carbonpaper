"""Every canned opening names a route the app serves; a moved page would match none."""
from __future__ import annotations

import pytest

from app.agents.compiler.opening import PAGE_OPENINGS, choose_opening_turn, find_page_opening


def read_served_routes() -> set[str]:
    """The app's own routes, so this compares against what is served, not a second list."""
    import app.main

    return {path for path, verbs in app.main.app.openapi()["paths"].items() if "get" in verbs}


@pytest.mark.parametrize("page", PAGE_OPENINGS, ids=lambda p: p.route)
def test_the_route_an_opening_answers_is_one_the_app_serves(page) -> None:
    served = read_served_routes()

    assert page.route in served, (
        f"no GET route is {page.route!r}, so this opening can never be chosen. If the page "
        "moved, move the route string with it; if it is gone, delete the opening."
    )


def test_no_two_openings_answer_the_same_route() -> None:
    routes = [page.route for page in PAGE_OPENINGS]

    assert len(routes) == len(set(routes))


def test_a_longer_route_is_matched_before_the_route_it_sits_under() -> None:
    # Order is the whole of the matching rule: /project/{id} sits under everything.
    lineage = find_page_opening(
        "/project/p1/runs/r1/stage/parse/row/0/trace/view")
    run = find_page_opening("/project/p1/runs/r1")

    assert lineage is not None and "row lineage" in lineage.says
    assert run is not None and "this run" in run.says
    # A project page has no entry of its own: the fallback already says these words.
    assert find_page_opening("/project/p1") is None


def test_a_page_no_opening_answers_falls_back_rather_than_guessing() -> None:
    assert find_page_opening("/admin") is None
    assert find_page_opening("/project/p1/runs/r1/scope") is None

    turn = choose_opening_turn("/admin", in_project=True)

    assert turn.text == "Hello. What do you want to do with this project?"


def test_an_opening_outside_any_project_offers_the_ways_in() -> None:
    turn = choose_opening_turn("/files", in_project=False)

    assert turn.text == "Hello. What would you like to do today?"
