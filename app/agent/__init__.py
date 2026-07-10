"""Reusable, embeddable AI chat subsystem.

A generic chat engine (Claude Agent SDK) with per-host pluggable tools,
file-based session persistence, and a streaming transport that surfaces
thinking/tool events to the browser and lets a turn be re-attached after
navigation.

Separate from the `llm_transform` batch path in app.runtime — this is the
interactive, multi-turn surface.
"""
