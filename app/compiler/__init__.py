"""
app.compiler — the prose → LLM → DAG authoring engine.

Public surface: `read_input` and `compile_methodology`. The LLM call, the
validation pass, and the prompt builders are internal to the package — callers
drive a compile through `compile_methodology`.

Persisting a compile as a first-class object (manifest / what-happened / DAG on
disk, plus the index/detail loaders) is a separate concern owned by
`app.services.compilation`. The command-line entry point is `app.compiler.__main__`
(`python -m app.compiler`).
"""

from __future__ import annotations

from app.compiler.compiler import compile_methodology, read_input

__all__ = ["read_input", "compile_methodology"]
