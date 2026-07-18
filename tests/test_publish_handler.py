"""handle_publish: the terminal, whole-frame publish operation (issue #125).

Publish is NOT a row map — it is handed the full frame(s) plus an `output_dir`,
writes artifacts, and returns a manifest frame whose declared `path_column`
carries each artifact's path. These tests pin that contract: output_dir is
passed through, and the declared path column is required."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.core.models import Stage
from app.runtime.stages.publish import handle_publish


def _publish_stage(code: str, *, path_column: str | None = None) -> Stage:
    publish: dict = {"format": "json"}
    if path_column is not None:
        publish["path_column"] = path_column
    return Stage.model_validate({
        "id": "pub", "name": "pub", "type": "publish",
        "inputs": [{"id": "src"}],
        "publish": publish,
        "function": {"kind": "inline", "code": code},
    })


def test_publish_passes_output_dir_and_returns_manifest(tmp_path: Path):
    # The function receives the frame positionally + an output_dir kwarg, writes
    # there, and returns one row per artifact with a `path` column.
    code = (
        "def transform(df, output_dir):\n"
        "    import os, pandas as pd\n"
        "    os.makedirs(output_dir, exist_ok=True)\n"
        "    paths = []\n"
        "    for i, _ in df.iterrows():\n"
        "        p = os.path.join(output_dir, f'{i}.html')\n"
        "        open(p, 'w').write('<html></html>')\n"
        "        paths.append(p)\n"
        "    return pd.DataFrame({'path': paths})\n"
    )
    ctx = {"run_dir": tmp_path}
    out = handle_publish(_publish_stage(code), {"src": pd.DataFrame({"x": [1, 2]})}, ctx)
    assert list(out.columns) == ["path"]
    assert len(out) == 2
    assert all(Path(p).exists() for p in out["path"])


def test_publish_requires_declared_path_column(tmp_path: Path):
    code = (
        "def transform(df, output_dir):\n"
        "    import pandas as pd\n"
        "    return pd.DataFrame({'not_path': ['a.html']})\n"
    )
    ctx = {"run_dir": tmp_path}
    with pytest.raises(ValueError, match="declared path column 'path'"):
        handle_publish(_publish_stage(code), {"src": pd.DataFrame({"x": [1]})}, ctx)


def test_publish_honors_custom_path_column(tmp_path: Path):
    code = (
        "def transform(df, output_dir):\n"
        "    import pandas as pd\n"
        "    return pd.DataFrame({'artifact_path': ['a.html']})\n"
    )
    ctx = {"run_dir": tmp_path}
    out = handle_publish(
        _publish_stage(code, path_column="artifact_path"),
        {"src": pd.DataFrame({"x": [1]})}, ctx,
    )
    assert list(out["artifact_path"]) == ["a.html"]


def test_publish_empty_frame_is_allowed(tmp_path: Path):
    # An empty manifest (nothing written) should not trip the column check.
    code = (
        "def transform(df, output_dir):\n"
        "    import pandas as pd\n"
        "    return pd.DataFrame()\n"
    )
    ctx = {"run_dir": tmp_path}
    out = handle_publish(_publish_stage(code), {"src": pd.DataFrame({"x": [1]})}, ctx)
    assert out.empty
