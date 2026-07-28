"""Architecture: the admin router reaches the platform only through its seams.

Default-deny over first-party imports: admin.py may name only the four modules
below, so sqlite3, app.core.persistence and app.core.frames are denied without
being listed — and so is any future backend module.
"""
from __future__ import annotations

from arch import find_governed_files
from arch.import_allowlist import find_disallowed_imports

_ADMIN_SEAMS = {
    "app.seeds",
    "app.services.project",
    "app.core.errors",
    "app.web.config",
}


def test_admin_router_imports_only_its_seams() -> None:
    admin = [p for p in find_governed_files(__file__) if p.name == "admin.py"]
    assert admin, "expected app/web/routers/admin.py in this arch test's scope"
    offenders = find_disallowed_imports(
        admin, roots={"app", "sqlite3"}, allow=_ADMIN_SEAMS
    )
    assert not offenders, (
        "app/web/routers/admin.py must reach the platform through its seams — "
        f"{', '.join(sorted(_ADMIN_SEAMS))} — never the storage backend directly "
        "(sqlite3, app.core.persistence, app.core.frames). Route the new call "
        "through app.services.project or app.seeds; widen the allowlist in this "
        "test only if the admin page genuinely gains a new seam. Offending "
        "imports:\n  " + "\n  ".join(offenders)
    )
