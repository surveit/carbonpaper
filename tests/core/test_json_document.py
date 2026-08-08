from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.errors import InvalidJsonDocument
from app.core.json_document import read_json_document


def test_a_file_in_a_legacy_codepage_is_refused_as_an_invalid_document(tmp_path: Path) -> None:
    path = tmp_path / "entity.json"
    # ensure_ascii=False is load-bearing: the default escapes the accent to \uXXXX, so
    # the cp1252 encode would emit pure ASCII, decode cleanly as UTF-8, and this test
    # would pass without ever reaching the branch it exists to pin.
    path.write_bytes(json.dumps({"title": "Sociétés"}, ensure_ascii=False).encode("cp1252"))

    with pytest.raises(InvalidJsonDocument) as raised:
        read_json_document(path)

    assert "entity.json" in str(raised.value)
    assert "not UTF-8 text" in str(raised.value)
