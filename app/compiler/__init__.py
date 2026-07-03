"""
app.compiler — the prose → LLM → DAG authoring engine.

Public surface (the compile MECHANISM):
    read_input, compile_methodology, validate, call_llm  — from .compiler
    SYSTEM_PROMPT, build_compile_prompt                  — from .prompt

Persisting a compile as a first-class object (manifest / what-happened / DAG on
disk, plus the index/detail loaders) is a separate concern owned by
`app.services.compilation`. The command-line entry point is `app.compiler.__main__`
(`python -m app.compiler`).
"""

from __future__ import annotations

from app.compiler.compiler import (
    call_llm,
    compile_methodology,
    read_input,
    validate,
)
from app.compiler.prompt import SYSTEM_PROMPT, build_compile_prompt

__all__ = [
    "read_input",
    "compile_methodology",
    "validate",
    "call_llm",
    "SYSTEM_PROMPT",
    "build_compile_prompt",
]
