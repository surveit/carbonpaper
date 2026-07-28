from __future__ import annotations

import shutil
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "static"
_ASSETS = {"style.css": _STATIC / "style.css",
           "mermaid.min.js": _STATIC / "vendor/mermaid.min.js"}


def copy_trace_assets(dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name, source in _ASSETS.items():
        if not source.is_file():
            raise FileNotFoundError(f"vendored trace asset missing: {source}")
        shutil.copyfile(source, dest_dir / name)
