from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path

from critic.records import CriticRecord

AUTHORED_SUFFIX = ".md"
# A NAME.from-repo file holds a path under the repo root; its text is read live, never copied.
FROM_REPO_SUFFIX = ".from-repo"
# A NAME.from-env file holds the name of an environment variable; that variable holds the path.
FROM_ENV_SUFFIX = ".from-env"
REPO_ROOT = Path(__file__).resolve().parents[1]
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


class RubricStamp(CriticRecord):
    name: str
    origin: str
    sha256: str


def load_rubric(directory: Path, environ: Mapping[str, str] = os.environ) -> Rubric:
    if not directory.is_dir():
        raise FileNotFoundError(f"rubric directory not found: {directory}")
    visible = sorted(path for path in directory.iterdir() if not path.name.startswith("."))
    unreadable = [path.name for path in visible if not _is_rubric_file(path)]
    if unreadable:
        raise ValueError(f"rubric {directory} holds files it does not read: {unreadable}")
    return Rubric(name=directory.name, files=[read_rubric_file(path, environ) for path in visible])


def read_rubric_file(path: Path, environ: Mapping[str, str]) -> RubricFile:
    if path.suffix == AUTHORED_SUFFIX:
        return _read_source(path.name, path, _describe_origin(path))
    pointer = path.read_text(encoding="utf-8").strip()
    if path.suffix == FROM_REPO_SUFFIX:
        source = _resolve_repo_path(path, pointer)
        return _read_source(path.stem, source, _describe_origin(source))
    # The variable's name, not its value: the value is a path on one machine.
    return _read_source(path.stem, _resolve_env_path(path, pointer, environ), f"${pointer}")


def stamp_rubric(rubric: Rubric) -> list[RubricStamp]:
    """Which text each file held: every source is read live, so it can change between runs."""
    return [_stamp_file(file) for file in rubric.files]


def render_rubric(rubric: Rubric) -> str:
    if not rubric.files:
        return NO_RUBRIC_TEXT
    return "\n\n".join(f"--- {file.name} ---\n{file.text.strip()}" for file in rubric.files)


def _resolve_repo_path(path: Path, pointer: str) -> Path:
    source = (REPO_ROOT / pointer).resolve()
    if REPO_ROOT not in source.parents:
        raise ValueError(f"rubric file {path.name} points outside the repo: {pointer}")
    return source


def _resolve_env_path(path: Path, variable: str, environ: Mapping[str, str]) -> Path:
    if not environ.get(variable):
        message = f"rubric file {path.name} reads its path from ${variable}, which is unset"
        raise UnsetRubricSourceError(message)
    return Path(environ[variable])


def _read_source(name: str, source: Path, origin: str) -> RubricFile:
    if not source.is_file():
        raise FileNotFoundError(f"rubric file {name} names {source}, which is not a file")
    return RubricFile(name=name, origin=origin, text=source.read_text(encoding="utf-8"))


def _describe_origin(source: Path) -> str:
    """A path under the repo is given relative to it, so `git show COMMIT:PATH` names the same file."""
    resolved = source.resolve()
    return str(resolved.relative_to(REPO_ROOT)) if REPO_ROOT in resolved.parents else str(source)


def _stamp_file(file: RubricFile) -> RubricStamp:
    digest = hashlib.sha256(file.text.encode("utf-8")).hexdigest()
    return RubricStamp(name=file.name, origin=file.origin, sha256=digest)


def _is_rubric_file(path: Path) -> bool:
    return path.is_file() and path.suffix in (AUTHORED_SUFFIX, FROM_REPO_SUFFIX, FROM_ENV_SUFFIX)
