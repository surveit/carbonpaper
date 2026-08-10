"""Architecture: app/static/*.css and _stylesheets.html name exactly the same sheets.

An unlinked sheet is dead CSS that looks live; a linked one that is missing is a 404
whose rules silently vanish. The packet reads its order from the same list.
"""
from __future__ import annotations

from pathlib import Path

from app.web.review_packet.pages import read_app_cascade_order

_STATIC = Path(__file__).resolve().parents[2] / "app" / "static"


def test_every_stylesheet_on_disk_is_linked() -> None:
    on_disk = {path.name for path in _STATIC.glob("*.css")}
    assert on_disk, f"no .css under {_STATIC} — this rule is vacuous"
    unlinked = sorted(on_disk - set(read_app_cascade_order()))
    assert not unlinked, (
        f"{unlinked} sit in app/static but app/templates/_stylesheets.html links none of "
        "them, so no page serves them and no review packet vendors them. Add each to that "
        "list at the position its cascade needs."
    )


def test_every_linked_stylesheet_exists() -> None:
    missing = sorted(name for name in read_app_cascade_order() if not (_STATIC / name).is_file())
    assert not missing, (
        f"app/templates/_stylesheets.html links {missing}, which are not in app/static. "
        "Each is a 404 on every page, and the rules it was meant to carry are simply gone."
    )
