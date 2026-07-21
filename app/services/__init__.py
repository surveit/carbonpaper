"""Services shared by the web routers and the runtime: per-node belief review
(`node_review`), immutable workflow version snapshots (`versioning`), and the
compilation-object lifecycle (`compilation`). `node_review` and `versioning` are
model-agnostic — they operate on raw stage dicts, not `app.models` types;
`compilation` drives `app.compiler` and persists its output."""
