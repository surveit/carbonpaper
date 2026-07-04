"""
app.compiler — the prose → LLM → workflow authoring engine.

Public surface:
  - `read_input` / `compile_methodology`  — the one-shot BATCH compile (from
    .compiler): prose → LLM → a workflow dict.
  - `stream_compile_chat`                 — the INTERACTIVE, human-gated compile
    chat (from .chat): an async event stream co-authoring named schemas + workflow
    stages one fenced block at a time.

The LLM call, the validation pass, and the prompt builders are internal to the
package. Persisting a batch compile as a first-class object (manifest /
what-happened / workflow on disk, plus the index/detail loaders) is a separate
concern owned by `app.services.compilation`. The command-line entry point is
`app.compiler.__main__` (`python -m app.compiler`).
"""

from __future__ import annotations

from app.compiler.chat import stream_compile_chat
from app.compiler.compiler import compile_methodology, read_input

__all__ = ["read_input", "compile_methodology", "stream_compile_chat"]
