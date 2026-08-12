"""Architecture: a service states facts; a surface writes the sentence a person reads."""
from __future__ import annotations

from pathlib import Path

# Sizes are the case this holds. app/services/uploads.py raises FileOverCeiling and
# StoreOverQuota carrying byte counts, and app/web/file_sizes.py turns those into "over
# the 512MB limit" — so one refusal can read differently in a JSON body, a chat bubble
# and a page without the service knowing any of them exist.
#
# Named functions rather than the word "describe": describe_workflow returns a
# WorkflowSummary and describe_dropped_fields returns issue strings — neither is
# presentation, so banning the word would flag four honest names and teach the next
# reader that the rule is about spelling.

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP = _REPO_ROOT / "app"
_WEB = _APP / "web"

# Turning a number into words a person reads. Wherever these live, that is the layer
# deciding how a size is spelled — so they live in the layer that has a reader.
_PRESENTATION_NAMES = ("describe_bytes", "describe_attachment", "describe_refusal")


def find_presentation_outside_web() -> list[str]:
    offenders = []
    for path in sorted(_APP.rglob("*.py")):
        if _WEB in path.parents or "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for name in _PRESENTATION_NAMES:
                if name in line:
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}  {name}")
    return offenders


def test_only_the_web_layer_spells_a_size_for_a_person() -> None:
    offenders = find_presentation_outside_web()
    assert not offenders, (
        "a size spelled for a reader belongs in app/web (see app/web/file_sizes.py). A "
        "service raises with the numbers — FileOverCeiling carries `ceiling`, "
        "StoreOverQuota carries `used`/`quota`/`sent` — and each surface writes its own "
        "sentence from them:\n  " + "\n  ".join(offenders)
    )


def test_the_web_layer_actually_holds_them() -> None:
    """Else the rule above passes because the functions were deleted, not moved."""
    text = (_WEB / "file_sizes.py").read_text(encoding="utf-8")
    missing = [name for name in _PRESENTATION_NAMES if f"def {name}" not in text]
    assert not missing, f"app/web/file_sizes.py no longer defines: {missing}"
