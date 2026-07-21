"""Committed example projects, portable via app.services.project.

data/<name>.json holds one project as a WorkflowFile document (methodology +
data model + workflow stages) that ships with the repo, so a fresh clone has
a real, importable example with no generation and no LLM call. A sibling
data/<name>.csv, when present, is sample input data for the imported
project's input_data stage to be bound to at run time — WorkflowFile itself
carries no input data (see app.services.project). Each fixture is
produced by a small capture script (e.g. capture_lobbying.py) that reads a
source project through export_project — rerun the script to refresh it;
never hand-edit the files under data/.

This package goes through app.services only — never sqlite3, app.core.persistence,
or app.core.frames directly."""
