"""Architecture: a stored record is declared in app/models/records/, never in a service."""
from __future__ import annotations

from pathlib import Path

from arch._helpers import find_subclasses_of, parse_module
from arch.scope import find_source_files_under

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVICES_DIR = _REPO_ROOT / "app" / "services"
_PERSISTED_MODEL = "PersistedModel"


def find_record_declaration_offenders(paths: list[Path]) -> list[str]:
    return [
        f"{path.name}:{node.lineno}  class {node.name}"
        for path in paths
        for node in find_subclasses_of(parse_module(path), _PERSISTED_MODEL)
    ]


def test_no_service_declares_a_record() -> None:
    offenders = find_record_declaration_offenders(find_source_files_under(_SERVICES_DIR))
    assert not offenders, (
        "a PersistedModel subclass belongs in app/models/records/, one module per "
        "record — the service keeps the functions that load, mutate and save it and "
        "imports the class. See docs/models-and-storage.md:\n  " + "\n  ".join(offenders)
    )


# --- unit tests for the finder, on inline snippets (red + green) ---------


def test_the_finder_flags_a_record_declared_in_a_service(tmp_path: Path) -> None:
    target = tmp_path / "project.py"
    target.write_text("class Project(PersistedModel):\n    name: str\n", encoding="utf-8")
    assert find_record_declaration_offenders([target]) == ["project.py:1  class Project"]


def test_the_finder_ignores_a_plain_model(tmp_path: Path) -> None:
    target = tmp_path / "project.py"
    target.write_text("class Summary(BaseModel):\n    name: str\n", encoding="utf-8")
    assert find_record_declaration_offenders([target]) == []
