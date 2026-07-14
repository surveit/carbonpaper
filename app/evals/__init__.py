"""Eval subsystem: everything that defines, checks, stores, runs, and scores an eval.

  - run_settings.py     — can this eval run be scored declaratively (resolve_eval_run_settings)
  - compatibility.py    — does an EvalConfig still fit the workflow (check_eval_compatibility)
  - dataset_columns.py  — derive the eval-dataset columns from override/target/checks
  - store.py            — read/write eval configs and runs on disk
  - runner.py           — run an eval against a workflow version (run_eval)
  - scoring.py          — compare a target's output to the dataset's expected columns

Depends on app.runtime (to execute a stage subset), app.services (versioning),
and app.models. Nothing in those layers imports app.evals. The eval MODELS live in
app.models.eval; the eval WEB routes in app.web.routers.evals.
"""
