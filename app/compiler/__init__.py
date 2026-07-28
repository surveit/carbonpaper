"""The prose → LLM generation engines that seed a project's authoring artifacts.

Both `data_model` and `stage_tests` run an Agent targeting a model schema, so a
schema-invalid reply is corrected inside the agent's own loop. Persisting what comes
back belongs to `app.services.generation`.
"""

from __future__ import annotations
