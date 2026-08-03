"""Static assets must be revalidated, not served blind from a browser's cache: a
page built from this commit's templates plus a stylesheet from an older one reads
as a missing CSS rule, and survives an ordinary reload."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_static_assets_are_revalidated_before_reuse():
    with TestClient(app) as client:
        response = client.get("/static/style.css")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    # The ETag is what makes revalidation cheap — no-cache without one would
    # re-send the whole asset on every request.
    assert response.headers.get("etag")


def test_revalidation_answers_a_matching_etag_without_a_body():
    with TestClient(app) as client:
        etag = client.get("/static/style.css").headers["etag"]
        response = client.get("/static/style.css", headers={"If-None-Match": etag})

    assert response.status_code == 304
    assert not response.content
