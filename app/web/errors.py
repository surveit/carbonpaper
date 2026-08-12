"""One HTTPException, two renderings: a page for a person, the JSON body for everything else.

The discriminator is an explicit `text/html` in Accept, which only a browser
navigation sends — the app's own fetch() calls send `*/*` and read `detail`.
"""
from __future__ import annotations

from http import HTTPStatus

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.web.breadcrumbs import build_home_crumbs
from app.web.config import templates

# Asked for by a browser navigating, and by nothing else here: the app's own fetch()
# calls send */* and read `detail`.
_PAGE_MEDIA_TYPE = "text/html"


class NoSuchProject(HTTPException):
    """Its own type so the page can say the project is gone rather than the address wrong."""

    def __init__(self, project: str) -> None:
        super().__init__(status_code=404, detail=f"No project '{project}'")


def install_error_pages(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, render_error)


async def render_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    if not _asks_for_a_page(request):
        return await http_exception_handler(request, exc)
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "crumbs": build_home_crumbs(_headline(exc.status_code)),
            "status": exc.status_code,
            "headline": _headline(exc.status_code),
            "detail": _detail(exc),
            "project_may_be_deleted": isinstance(exc, NoSuchProject),
        },
        status_code=exc.status_code,
    )


def _asks_for_a_page(request: Request) -> bool:
    return _PAGE_MEDIA_TYPE in request.headers.get("accept", "")


def _headline(status_code: int) -> str:
    """The status's own phrase, so an unmapped code still gets a true heading."""
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def _detail(exc: StarletteHTTPException) -> str:
    # An unmatched path 404s with the phrase the heading already says.
    if exc.detail == HTTPStatus.NOT_FOUND.phrase:
        return "There is no page at this address."
    return str(exc.detail)
