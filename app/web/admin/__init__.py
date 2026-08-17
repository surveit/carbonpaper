"""The workspace admin surface: seed bundles and project export/import
(`workspace_router`), and what the workspace has spent on models
(`spend_router` over `spend.py`). Two routers rather than one because
`workspace_router` may reach the platform only through four named seams, and
reading a run manifest is not one of them — `_arch_tests/`.
"""
