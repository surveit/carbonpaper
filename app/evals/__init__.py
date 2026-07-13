"""Eval subsystem: run an eval against a workflow version and score it.

Depends on app.runtime (to execute a stage subset), app.services (the eval
substrate: compatibility, store, dataset-column derivation — see issue #103 to
fold those in here too), and app.models. Nothing here is imported by those layers.
"""
