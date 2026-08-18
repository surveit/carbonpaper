from fastapi.testclient import TestClient

from app.main import app


def test_intro_links_to_a_served_favicon() -> None:
    with TestClient(app) as client:
        page = client.get("/intro")
        favicon = client.get("/static/favicon.svg")

    assert page.status_code == 200
    assert '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">' in page.text
    assert favicon.status_code == 200
    assert favicon.headers["content-type"] == "image/svg+xml"
