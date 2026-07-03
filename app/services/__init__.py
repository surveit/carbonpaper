"""Services shared by the web routers and the runtime: per-node belief review
(`node_review`) and immutable DAG version snapshots (`versioning`). These are
model-agnostic — they operate on raw stage dicts, not `app.models` types."""
