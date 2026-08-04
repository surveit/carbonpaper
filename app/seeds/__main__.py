"""python -m app.seeds — thin CLI over app.seeds.seed.

Bootstraps the document store itself: this standalone process has no app.main
lifespan to do it."""
from __future__ import annotations

import argparse

from app.seeds.bootstrap import configure_projects_dir_from_env, ensure_store_configured
from app.seeds.seed import discover_workflow_files, seed_all


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.seeds",
        description="Import the committed example bundles under app/seeds/data/ into the workspace.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    _parse_args(argv)
    configure_projects_dir_from_env()
    ensure_store_configured()
    imported = set(seed_all())
    # Each fixture file is named after the project it imports as (see
    # app/seeds/__init__.py) — its stem doubles as this status line's label.
    for wf_path in discover_workflow_files():
        name = wf_path.stem
        print(f"imported: {name}" if name in imported else f"skipped (already exists): {name}")


if __name__ == "__main__":
    main()
