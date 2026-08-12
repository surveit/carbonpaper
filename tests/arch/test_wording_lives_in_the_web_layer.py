"""Architecture: `describe` is the web layer's verb, because it is the only layer with a reader."""
from __future__ import annotations

from pathlib import Path

# `describe` means one thing: take a value and reform the language around it. That is
# presentation, so it belongs where a reader is. Sizes are the case that forced the
# rule — app/services/uploads.py raises FileOverCeiling and StoreOverQuota carrying byte
# counts, and app/web/file_sizes.py turns those into "over the 512MB limit", so one
# refusal reads differently in a JSON body, a chat bubble and a page without the service
# knowing any of them exist.
#
# Nothing is grandfathered, and that is the point: the two `describe_` names that used to
# sit outside app/web were not describing. read_workflow_summary returns a WorkflowSummary
# and read_project_name reads a name — both were transforming or reading, and naming them
# so is what let this rule hold with an empty allowlist instead of a list of excuses.
#
# The rule is on the EXPORTED name. A module-private `_describe_…` wording its own
# ValueError or its own issue strings is still describing, but it is describing to no one
# else — a join\'s cardinality message belongs beside the join, not in a web module that
# would then have to know what a join is. What may not cross a layer is a describe_ that
# something else calls: that is presentation with an audience, and its audience is a
# surface.

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP = _REPO_ROOT / "app"
_WEB = _APP / "web"
# The identifier, not the English word: prompts and comments say "describe" about
# what a model should do, and that is prose, not a function naming itself.
_VERB = "describe_"

# The web layer holds them, so a rename that empties app/web is caught rather than passing.
_PRESENTATION_NAMES = ("describe_bytes", "describe_attachment", "describe_refusal")


def find_exported_describe_outside_web() -> list[str]:
    offenders = []
    for path in sorted(_APP.rglob("*.py")):
        if _WEB in path.parents or "_vendor" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _VERB in line and f"_{_VERB}" not in line:
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}  {line.strip()}")
    return offenders


def test_no_module_outside_web_exports_a_describe() -> None:
    offenders = find_exported_describe_outside_web()
    assert not offenders, (
        "`describe` reforms the language around a value, which only a layer with a reader "
        "has any business doing — see app/web/file_sizes.py. A service states the facts "
        "and each surface writes its own sentence from them. If the function is not "
        "wording something, name what it does: it is reading, transforming, filtering or "
        "summarising:\n  " + "\n  ".join(offenders)
    )


def test_the_web_layer_actually_holds_the_wording() -> None:
    """Else the rule above passes because the functions were deleted, not moved."""
    text = (_WEB / "file_sizes.py").read_text(encoding="utf-8")
    missing = [name for name in _PRESENTATION_NAMES if f"def {name}" not in text]
    assert not missing, f"app/web/file_sizes.py no longer defines: {missing}"
