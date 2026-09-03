"""SHA-256 of every file in a finished packet, in `shasum -a 256` format so a
reviewer verifies the folder with `shasum -c checksums.txt` and no other tool."""
from __future__ import annotations

from pathlib import Path

from app.core.files import compute_sha256 as compute_sha256

CHECKSUMS_FILE = "checksums.txt"


def write_checksums(root: Path) -> str:
    # Covers every file but itself: a manifest cannot carry its own hash.
    lines = [
        f"{compute_sha256(path)}  {path.relative_to(root).as_posix()}"
        for path in _list_packet_files(root)
    ]
    (root / CHECKSUMS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return CHECKSUMS_FILE


def _list_packet_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name != CHECKSUMS_FILE
    )
