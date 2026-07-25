"""
app.compiler — the GENERATION BRIDGES onto the app.core.agent spine.

Public surface: none. Each module here builds one headless `Agent[T]` and runs it
as a chat turn on the shared spine, handing the submitted, schema-validated object
back through a callback:
  - `data_model`  — prose → the project's named schemas (`SchemaLibrary`).
  - `stage_tests` — methodology + one stage → that stage's derived test suite.

app.compiler is the only allowed importer of the agent spine below app.services, so
`app.services.generation` delegates here rather than reaching into the spine itself;
persisting whatever an agent submits is the caller's job, never this package's.

There is deliberately NO prose → whole-workflow compiler. A workflow's stages are
authored one at a time through `app.services.stage_edit`, each write re-validating
the entire resulting workflow, so no code path can overwrite or reset a draft.
"""

from __future__ import annotations
