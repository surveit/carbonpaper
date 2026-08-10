"""0010 strips the on-disk reader's bookkeeping keys off a stored version's
frozen schemas, which NamedSchema now forbids as extras."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.named_schemas import NamedSchema

_REVISION = (Path(__file__).resolve().parents[1]
             / "alembic/versions/0010_schemas_become_a_document.py")


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0010", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema_with_bookkeeping() -> dict[str, Any]:
    return {"name": "claim", "kind": "reference", "title": "Claim", "columns": [],
            "_filename": "01_claim.json", "_error": False}


def test_a_version_carrying_bookkeeping_keys_does_not_load_before_the_migration():
    with pytest.raises(ValidationError):
        NamedSchema.model_validate(_schema_with_bookkeeping())


def test_the_migration_strips_them_and_the_schema_loads():
    document = {"schemas": [_schema_with_bookkeeping()]}
    assert _load_revision()._strip_bookkeeping(document) is True
    assert NamedSchema.model_validate(document["schemas"][0]).name == "claim"


def test_a_clean_version_is_left_untouched():
    document = {"schemas": [{"name": "claim", "kind": "reference", "title": "Claim",
                             "columns": []}]}
    assert _load_revision()._strip_bookkeeping(document) is False


def test_a_version_with_no_schemas_key_is_left_untouched():
    assert _load_revision()._strip_bookkeeping({"stages": []}) is False
