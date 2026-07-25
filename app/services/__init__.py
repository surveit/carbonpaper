"""Services shared by the web routers and the runtime: per-node belief review
(`node_review`), immutable workflow version snapshots (`versioning`), and the
single validated stage writer (`stage_edit`). `node_review` and `versioning` are
model-agnostic — they operate on raw stage dicts, not `app.models` types;
`stage_edit` is the ONLY writer into a project's `compiled/` workflow, and it
changes exactly ONE stage per call, re-validating the whole resulting workflow
before it writes."""
