"""Per-stage-type models: each module holds one family of stage types' config
block, its `StageBase` subclass, and the column checks that subclass runs.
`app.models.stage` unions those subclasses into `Stage`."""
