"""python -m app.seeds — thin CLI over app.seeds.seed.

Bootstraps the document store itself: this standalone process has no app.main
lifespan to do it."""
from __future__ import annotations

import argparse

from app.core.store_config import refuse_renamed_env_vars
from app.seeds.bootstrap import configure_default_document_store, configure_projects_dir_from_env
from app.seeds.seed import discover_workflow_files, seed_all
from app.services.project import read_project_name


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.seeds",
        description="Import the committed example bundles under app/seeds/data/ into the workspace.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    _parse_args(argv)
    refuse_renamed_env_vars()
    configure_projects_dir_from_env()
    configure_default_document_store()
    # seed_all returns project IDs; the status line speaks in the labels the fixture
    # files are named after (see app/seeds/__init__.py), so map each id back.
    imported = {read_project_name(project_id) for project_id in seed_all()}
    for wf_path in discover_workflow_files():
        label = wf_path.stem
        print(f"imported: {label}" if label in imported
              else f"skipped (already seeded): {label}")


if __name__ == "__main__":
    main()
