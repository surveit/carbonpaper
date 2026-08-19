"""The workspace admin surface: seed bundles and project export/import
(`workspace_router`), model spend (`spend_router` over `spend.py`), and moving a
project's stage cache between workspaces (`cache_router`). Three routers because
each reaches the platform through its own named seams — `_arch_tests/`.
"""
