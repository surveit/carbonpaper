"""`function.kind=module` names a path, not code: these pin that the module's
CONTENTS reach the fingerprint, that an unreadable module fails loudly, and that
kind=inline fingerprints are byte-identical to before the digest existed."""
from __future__ import annotations

import importlib
import sys

import pytest
from pydantic import ValidationError

from app.models import Stage
from app.models.stages.module_source import (
    compute_module_source_digest,
    verify_pinned_module_digest,
)

_SCHEMA = {"columns": [{"name": "a", "type": "str", "nullable": False}]}
_PASSTHROUGH = "def transform(row):\n    return row\n"
_CHANGED = "def transform(row):\n    return {**row, 'a': 'x'}\n"

# The kind=inline fingerprint as it stood before `module_digest` was added to
# PythonFunction.FINGERPRINT_FIELDS. Every stage in the repo is inline, and each
# carries cached run results keyed on this value: it must not move.
_INLINE_FINGERPRINT_BEFORE_MODULE_DIGEST = "a022af129feebb01"


def _stage(function: dict) -> Stage:
    return Stage.model_validate({
        "id": "step",
        "type": "python_row_function",
        "name": "Step",
        "inputs": [{"id": "src", "schema": _SCHEMA}],
        "output_schema": _SCHEMA,
        "function": function,
    })


@pytest.fixture
def module_on_path(tmp_path, monkeypatch):
    """A writable importable module: returns (module_name, write(source))."""
    monkeypatch.syspath_prepend(str(tmp_path))
    name = "carbonpaper_digest_fixture"
    path = tmp_path / f"{name}.py"

    def write(source: str) -> None:
        path.write_text(source, encoding="utf-8")
        importlib.invalidate_caches()
        sys.modules.pop(name, None)

    write(_PASSTHROUGH)
    yield name, write
    sys.modules.pop(name, None)


def test_inline_fingerprint_is_unchanged_by_the_module_digest_field():
    assert _stage({"kind": "inline", "code": _PASSTHROUGH}).compute_definition_fingerprint() == (
        _INLINE_FINGERPRINT_BEFORE_MODULE_DIGEST
    )


def test_inline_handle_carries_no_module_digest():
    stage = _stage({"kind": "inline", "code": _PASSTHROUGH})
    assert stage.function is not None
    assert stage.function.module_digest is None


def test_module_stage_fingerprint_is_stable_while_the_source_is(module_on_path):
    name, _ = module_on_path
    first = _stage({"kind": "module", "module": name}).compute_definition_fingerprint()
    second = _stage({"kind": "module", "module": name}).compute_definition_fingerprint()
    assert first == second


def test_module_stage_fingerprint_changes_when_the_module_source_changes(module_on_path):
    name, write = module_on_path
    before = _stage({"kind": "module", "module": name}).compute_definition_fingerprint()
    write(_CHANGED)
    after = _stage({"kind": "module", "module": name}).compute_definition_fingerprint()
    assert before != after


def test_module_stage_pins_the_digest_it_was_validated_against(module_on_path):
    name, write = module_on_path
    pinned = _stage({"kind": "module", "module": name}).function.module_digest
    assert pinned == compute_module_source_digest(name)
    # A persisted digest is kept verbatim — reloading a stored spec must not
    # silently re-derive it from whatever the module says today.
    write(_CHANGED)
    reloaded = _stage({"kind": "module", "module": name, "module_digest": pinned})
    assert reloaded.function.module_digest == pinned


def test_unresolvable_module_is_refused_rather_than_hashing_the_path():
    with pytest.raises(ValidationError, match="no_such_carbonpaper_module"):
        _stage({"kind": "module", "module": "no_such_carbonpaper_module"})


def test_compute_module_source_digest_raises_for_an_unresolvable_module():
    with pytest.raises(ValueError, match="cannot be resolved"):
        compute_module_source_digest("no_such_carbonpaper_module")


def test_verify_pinned_module_digest_accepts_the_matching_source(module_on_path):
    name, _ = module_on_path
    verify_pinned_module_digest(name, compute_module_source_digest(name))


def test_verify_pinned_module_digest_rejects_changed_source(module_on_path):
    name, write = module_on_path
    pinned = compute_module_source_digest(name)
    write(_CHANGED)
    with pytest.raises(ValueError, match="has changed"):
        verify_pinned_module_digest(name, pinned)
