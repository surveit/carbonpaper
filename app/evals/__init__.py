"""Eval subsystem: everything that defines, checks, stores, runs, and scores an eval.

  - run_settings.py     — can this eval run be scored declaratively (resolve_eval_run_settings)
  - compatibility.py    — does an EvalConfig still fit the workflow (validate_eval_compatibility)
  - dataset_columns.py  — derive the eval-dataset columns from override/target/checks
  - store.py            — read/write eval configs and runs in the document store
  - runner.py           — run an eval against a workflow version (run_eval)
  - scoring.py          — compare a target's output to the dataset's expected columns
  - differential.py     — derive a stage's transform N times independently and surface
                          where survivors that all pass the frozen tests still diverge,
                          i.e. where the tests underdetermine the spec (derive_n_version_and_diff)

Depends on app.runtime (to execute a stage subset), app.services (versioning),
and app.core.models. Nothing in those layers imports app.evals. The eval MODELS live in
app.core.models.eval; the eval WEB routes in app.web.routers.evals.
"""
