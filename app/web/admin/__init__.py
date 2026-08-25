"""The workspace admin surface: seed bundles and project export/import
(`workspace_router`), model spend (`spend_router` over `spend.py`), how far people got
(`activity_router` over `activity.py`), and moving a project's stage cache between
workspaces (`cache_router`). Four routers because each reaches the platform through its
own named seams — `_arch_tests/`.
"""
