"""What a stage's authored-code block promises on screen, keyed by block class."""

from __future__ import annotations

from typing import Optional

from app.models.schema import _Base
from app.models.stages.code import AuthoredCode, PythonFunction
from app.models.stages.filter_rows import FilterConfig
from app.models.stages.starlark import StarlarkFunction
from app.models.stages.starlark_filter import StarlarkFilter
from app.models.stages.stage_base import AbstractStage


class CodeBlockCopy(_Base):
    # What the block's shape guarantees, `backticks` around code. None promises nothing.
    note: Optional[str] = None
    caption: str


# One entry per AuthoredCode block; the arch test fails on a missing one.
BLOCK_COPY: dict[type[AuthoredCode], CodeBlockCopy] = {
    PythonFunction: CodeBlockCopy(
        caption="the implementation of the summary above",
    ),
    StarlarkFunction: CodeBlockCopy(
        caption="the implementation of the summary above",
    ),
    FilterConfig: CodeBlockCopy(
        note="Keeps the rows this predicate returns `True` for; every kept row passes "
             "through unchanged, in order.",
        caption="the predicate behind the summary above",
    ),
    StarlarkFilter: CodeBlockCopy(
        note="Keeps the rows this predicate returns `True` for; every kept row passes "
             "through unchanged, in order.",
        caption="the predicate behind the summary above",
    ),
}


def find_authored_code(stage: object) -> Optional[AuthoredCode]:
    if not isinstance(stage, AbstractStage):
        return None
    return stage.find_authored_code_block()


def describe_code_block(block: Optional[AuthoredCode]) -> Optional[CodeBlockCopy]:
    return BLOCK_COPY.get(type(block)) if block is not None else None
