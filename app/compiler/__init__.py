"""
app.compiler — the prose → LLM generation engines that seed a project's authoring
artifacts.

  - `data_model` — a methodology document → the named schemas (the nouns) a human
    then reviews and approves.
  - `stage_tests` — one python-transform stage + the methodology → the example-based
    tests that stage must satisfy.

Each pairs with its own prompt module (`data_model_prompt`, `stage_tests_prompt`).
Both run an `app.core.agent.agent.Agent` targeting a model schema, so the model
submits through `submit_answer` and a schema-invalid reply is corrected inside the
agent's own loop. Persisting what comes back belongs to `app.services.generation`.

Stages themselves are authored one at a time through `app.services.stage_edit`, not
generated here.
"""

from __future__ import annotations
