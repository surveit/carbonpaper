"""Architecture: app/web names a project by its ID, never by its DIRECTORY.

Resolving an id to a `Path` is a service's job (`workspace.resolve_project_dir`), which
also refuses an id escaping the workspace — `projects_dir() / project` does not, and
answered 200 for `..`. `_NOT_YET_MIGRATED` is a ratchet: entries may only be removed.
"""
from __future__ import annotations

from pathlib import Path

from arch import find_governed_files, find_project_directory_names

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Every app/web module that still names a project directory, with the count of places
# at the time the rule was written. A NEW offender must be migrated, not listed here;
# an entry may only be deleted. Migrating one means giving the service it calls an
# id-taking entry point (see services.versioning.list_project_versions) and calling that.
# The burn-down, with the per-module breakdown and the order: issue #505.
_NOT_YET_MIGRATED: dict[str, int] = {
    "app/web/routers/runs.py": 7,
    "app/web/routers/project.py": 12,
    "app/web/routers/evals.py": 11,
    "app/web/routers/node.py": 8,
    "app/web/loading.py": 3,
    "app/web/routers/guide.py": 2,
    "app/web/project_view.py": 1,
    "app/web/review_packet/packet.py": 1,
}


def test_no_new_module_names_a_project_directory() -> None:
    offenders = _find_offenders()
    unlisted = sorted(f for f in offenders if _module_of(f) not in _NOT_YET_MIGRATED)
    assert not unlisted, (
        "app/web must name a project by its id (str), not its directory. Call a service "
        "that takes the id and resolves the path itself — that is also what refuses an id "
        "escaping the workspace. Do NOT add the module to _NOT_YET_MIGRATED; that dict is "
        "a ratchet and only shrinks:\n  " + "\n  ".join(unlisted)
    )


def test_a_listed_module_never_grows_more_project_directory_names() -> None:
    offenders = _find_offenders()
    grown = [
        f"{module}: {_count_for(offenders, module)} now, {budget} when the rule was written"
        for module, budget in _NOT_YET_MIGRATED.items()
        if _count_for(offenders, module) > budget
    ]
    assert not grown, (
        "these app/web modules name more project directories than when the rule was written; "
        "migrate the new call to an id-taking service instead:\n  " + "\n  ".join(grown)
    )


def test_the_ratchet_carries_no_stale_entry() -> None:
    offenders = _find_offenders()
    stale = [
        f"{module} (listed at {budget}, now {_count_for(offenders, module)})"
        for module, budget in _NOT_YET_MIGRATED.items()
        if _count_for(offenders, module) < budget
    ]
    assert not stale, (
        "_NOT_YET_MIGRATED is out of date — lower or delete these entries so the ratchet "
        "cannot be undone:\n  " + "\n  ".join(stale)
    )


def _find_offenders() -> list[str]:
    return find_project_directory_names(find_governed_files(__file__), root=_REPO_ROOT)


def _module_of(offender: str) -> str:
    return offender.split(":", 1)[0]


def _count_for(offenders: list[str], module: str) -> int:
    return sum(1 for offender in offenders if _module_of(offender) == module)
