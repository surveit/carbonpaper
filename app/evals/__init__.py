"""Eval subsystem: defines, checks, stores, runs, and scores an eval.

Depends on app.runtime, app.services, and app.models; nothing in those layers
imports app.evals. The eval MODELS live in app.models.eval, the WEB routes in
app.web.routers.evals.
"""
