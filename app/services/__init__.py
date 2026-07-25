"""Services shared by the web routers and the runtime: per-node belief review
(`node_review`), immutable workflow version snapshots (`versioning`), stage-by-stage
workflow authoring (`stage_edit`), and LLM generation of a project's data model and
stage tests (`generation`). `node_review` and `versioning` are model-agnostic — they
operate on raw stage dicts, not `app.models` types; `generation` drives `app.compiler`
and persists what it submits."""
