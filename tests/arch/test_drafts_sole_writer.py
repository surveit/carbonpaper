"""Architecture: app.services.drafts is the sole owner of the drafts/ directory.

A draft is disposable scratch space — persistence is a robustness substrate, not a
lifecycle promise, so nothing outside the drafts service may construct a path into
<project>/drafts/. Concretely: the directory-name literal "drafts" may appear in
code only in app/services/drafts.py. Whole-repo scope; import-graph boundaries live
in pyproject [tool.importlinter]."""
from __future__ import annotations

from arch import check_literal_confined, scan_all_source


def test_drafts_dir_literal_confined_to_drafts_service() -> None:
    offenders = check_literal_confined(
        scan_all_source(), literal="drafts", owner_suffix="app/services/drafts.py"
    )
    assert not offenders, (
        "the drafts/ directory is owned by app.services.drafts — route access "
        "through it:\n  " + "\n  ".join(offenders)
    )
