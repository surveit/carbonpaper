from pathlib import Path

from app.runtime.trace_assets import copy_trace_assets


def test_copies_both_assets(tmp_path: Path):
    copy_trace_assets(tmp_path / "_assets")
    assert (tmp_path / "_assets/style.css").is_file()
    assert (tmp_path / "_assets/mermaid.min.js").is_file()


def test_is_idempotent(tmp_path: Path):
    dest = tmp_path / "_assets"
    copy_trace_assets(dest)
    copy_trace_assets(dest)
    assert (dest / "mermaid.min.js").stat().st_size > 0


def test_vendored_mermaid_is_real(tmp_path: Path):
    copy_trace_assets(tmp_path / "_assets")
    assert (tmp_path / "_assets/mermaid.min.js").stat().st_size > 100_000
