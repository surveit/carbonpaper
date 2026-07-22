"""Per-stage-type validation helpers that are too bulky to live inline on the
`Stage` model. Each module here holds the checks specific to one family of stage
types; `app.models.stage` imports them back into its model validators."""
