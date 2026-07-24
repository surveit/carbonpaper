"""Architecture: the stage-result cache sits BELOW the domain-model layer.

`app/services/stage_cache.py` is a content-addressed store of generic
(output row | tombstone) payloads keyed by fingerprints. It must not depend on
`app.models` — the domain-model layer (verdicts, stages, schemas) sits above it,
so any verdict or column vocabulary a payload carries is assigned above this
seam, never known here.

The rule is executable and structural: `stage_cache.py` imports no module named
`app.models` or under it. This is a module-level dependency fact — a `from
app.models import ...` in that file makes it red — not a symbol denylist that a
rename or a sibling symbol could dodge.
"""
from __future__ import annotations

from pathlib import Path

from arch._helpers import find_imported_modules, parse_module

_STAGE_CACHE = Path(__file__).resolve().parents[1] / "stage_cache.py"
_DOMAIN_MODELS = "app.models"


def find_domain_model_imports(path: Path) -> list[str]:
    """The modules `path` imports that are `app.models` or a submodule of it —
    the domain-model dependencies a below-the-model store must not have."""
    imported = find_imported_modules(parse_module(path))
    return sorted(
        name for name in imported
        if name == _DOMAIN_MODELS or name.startswith(f"{_DOMAIN_MODELS}.")
    )


def test_stage_cache_does_not_import_domain_models() -> None:
    offenders = find_domain_model_imports(_STAGE_CACHE)
    assert not offenders, (
        "the content-addressed stage-result cache sits below the domain-model "
        "layer and must not import app.models — verdict and column vocabulary "
        "belong above this seam (app/services/review.py), not in the store:\n  "
        + "\n  ".join(offenders)
    )


# --- unit tests for find_domain_model_imports, on inline snippets ---


def test_flags_a_domain_model_import(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("from app.models import RowReviewDecision\n")
    assert find_domain_model_imports(target) == ["app.models"]


def test_flags_a_domain_model_submodule_import(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("import app.models.stage\n")
    assert find_domain_model_imports(target) == ["app.models.stage"]


def test_ignores_a_file_importing_no_domain_models(tmp_path: Path) -> None:
    target = tmp_path / "m.py"
    target.write_text("from app.core.persistence import JsonDict\n")
    assert find_domain_model_imports(target) == []
