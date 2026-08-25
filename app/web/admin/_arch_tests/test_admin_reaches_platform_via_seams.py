"""Architecture: the workspace-admin routers reach the platform only through their seams.

Default-deny over first-party imports: each router below may name only the modules
listed against it, so sqlite3, app.core.persistence and app.core.frames are denied
without being listed — and so is any future backend module.
"""
from __future__ import annotations

from arch import find_governed_files
from arch.import_allowlist import find_disallowed_imports

_SEAMS_BY_ROUTER = {
    # ONE file, as before the surface became a package: spend.py and the router over it
    # read a run manifest, which is the whole reason they are not in this rule.
    "workspace_router.py": {
        "app.seeds",
        "app.services.project",
        "app.core.errors",
        "app.web.config",
    },
    # The page over activity.py, which reads a run manifest — the router itself only
    # renders what that module counted.
    "activity_router.py": {
        "app.web.admin.activity",
        "app.web.breadcrumbs",
        "app.web.config",
    },
    # Moving cache entries between workspaces is its own seam, and the reason this
    # router is not a route on workspace_router.
    "cache_router.py": {
        "app.services.project",
        "app.services.stage_cache_transfer",
        "app.web.breadcrumbs",
        "app.web.config",
    },
}


def test_admin_routers_import_only_their_seams() -> None:
    governed = {path.name: path for path in find_governed_files(__file__)}
    for router, seams in _SEAMS_BY_ROUTER.items():
        assert router in governed, f"expected app/web/admin/{router} in this arch test\'s scope"
        offenders = find_disallowed_imports(
            [governed[router]], roots={"app", "sqlite3"}, allow=seams
        )
        assert not offenders, (
            f"app/web/admin/{router} must reach the platform through its seams — "
            f"{', '.join(sorted(seams))} — never the storage backend directly "
            "(sqlite3, app.core.persistence, app.core.frames). Route the new call "
            "through a service module; widen this test only if the admin page "
            "genuinely gains a new seam. Offending imports:\n  "
            + "\n  ".join(offenders)
        )
