from pathlib import Path

import pytest

from app.runtime import trace_assets
from app.runtime.trace_assets import copy_trace_assets


def test_copies_both_stylesheets(tmp_path: Path):
    copy_trace_assets(tmp_path / "_assets")
    assert (tmp_path / "_assets/style.css").is_file()
    assert (tmp_path / "_assets/trace.css").is_file()


def test_copies_nothing_else(tmp_path: Path):
    dest = tmp_path / "_assets"
    copy_trace_assets(dest)
    assert sorted(p.name for p in dest.iterdir()) == ["style.css", "trace.css"]


def test_is_idempotent(tmp_path: Path):
    dest = tmp_path / "_assets"
    copy_trace_assets(dest)
    copy_trace_assets(dest)
    assert (dest / "trace.css").stat().st_size > 0


def test_raises_rather_than_shipping_a_bundle_missing_a_stylesheet(tmp_path, monkeypatch):
    monkeypatch.setitem(trace_assets._ASSETS, "trace.css", tmp_path / "gone.css")
    with pytest.raises(FileNotFoundError):
        copy_trace_assets(tmp_path / "_assets")
