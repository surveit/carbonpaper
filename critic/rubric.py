from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from critic.records import CriticRecord

# .md holds authored rules; .txt holds a verbatim snapshot, which no word rule may rewrite.
RUBRIC_SUFFIXES = (".md", ".txt")
# A NAME.from-env file holds the name of an environment variable; that variable holds the path.
FROM_ENV_SUFFIX = ".from-env"
NO_RUBRIC_TEXT = "None. Review as a strict reviewer who holds no house rules."


class UnsetRubricSourceError(RuntimeError):
    pass


class RubricFile(CriticRecord):
    name: str
    origin: str
    text: str


class Rubric(CriticRecord):
    name: str
    files: list[RubricFile]


def load_rubric(directory: Path, environ: Mapping[str, str] = os.environ) -> Rubric:
    if not directory.is_dir():
        raise FileNotFoundError(f"rubric directory not found: {directory}")
    visible = sorted(path for path in directory.iterdir() if not path.name.startswith("."))
    unreadable = [path.name for path in visible if not _is_rubric_file(path)]
    if unreadable:
        raise ValueError(f"rubric {directory} holds files it does not read: {unreadable}")
    return Rubric(name=directory.name, files=[read_rubric_file(path, environ) for path in visible])


def read_rubric_file(path: Path, environ: Mapping[str, str]) -> RubricFile:
    """A file kept outside the repo, such as a person's own instructions, arrives through FROM_ENV_SUFFIX."""
    if path.suffix != FROM_ENV_SUFFIX:
        return RubricFile(name=path.name, origin=str(path), text=path.read_text(encoding="utf-8"))
    variable = path.read_text(encoding="utf-8").strip()
    if not environ.get(variable):
        raise UnsetRubricSourceError(f"rubric file {path.name} reads its path from ${variable}, which is unset")
    source = Path(environ[variable])
    if not source.is_file():
        raise FileNotFoundError(f"${variable} names {source}, which is not a file")
    return RubricFile(name=path.stem, origin=str(source), text=source.read_text(encoding="utf-8"))


def render_rubric(rubric: Rubric) -> str:
    if not rubric.files:
        return NO_RUBRIC_TEXT
    return "\n\n".join(f"--- {file.name} ---\n{file.text.strip()}" for file in rubric.files)


def _is_rubric_file(path: Path) -> bool:
    return path.is_file() and path.suffix in (*RUBRIC_SUFFIXES, FROM_ENV_SUFFIX)
