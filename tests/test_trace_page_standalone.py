from app.runtime.trace_page import render_standalone_trace_page, render_trace_body
from test_trace_page_render import _view

VIEW = _view()


def test_standalone_is_a_complete_document():
    html = render_standalone_trace_page(VIEW, asset_prefix="../_assets/")
    assert html.lstrip().lower().startswith("<!doctype html")
    assert "</html>" in html.lower()


def test_standalone_references_assets_relatively():
    html = render_standalone_trace_page(VIEW, asset_prefix="../_assets/")
    assert "../_assets/style.css" in html
    assert "../_assets/trace.css" in html


def test_standalone_makes_no_absolute_requests():
    html = render_standalone_trace_page(VIEW, asset_prefix="../_assets/")
    assert "http://" not in html
    assert "https://" not in html


def test_standalone_contains_the_same_body_as_the_live_page():
    assert render_trace_body(VIEW) in render_standalone_trace_page(VIEW, "../_assets/")
