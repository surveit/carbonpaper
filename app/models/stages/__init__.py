"""`stage_base.py` holds the type vocabulary and `AbstractStage`; each per-type module
holds one family of stage types' config block, its `AbstractStage` subclass, and the
column checks that subclass runs. `app.models.stage` unions those into `Stage`."""
