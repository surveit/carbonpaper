"""The chat subsystem's web-facing surface: FastAPI routes (`router.py`) and the
Jinja2 templates they render.

The generic chat engine this surface drives (session store, turn manager, the
SDK-driving engine, and the agent registry) lives in `app.core.agent`.
"""
