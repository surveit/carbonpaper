"""Interactive chat surface: FastAPI routes (`router`) and the Jinja2 templates that
render the chat UI.

The reusable engine underneath — registry, turns, session store, the SDK engine that
drives claude_agent_sdk.query() — lives in `app.core.agent`.
"""
