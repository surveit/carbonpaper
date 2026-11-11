"""A directory standing in for CourtListener: files it fetched, each with a fetch record."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from app.core.errors import MirrorDisagrees, SourceUnavailable
from app.core.files import compute_sha256
from app.core.paths import repo_root

MIRROR_MANIFEST = "manifest.jsonl"


def resolve_mirror_dir(cache_dir: str) -> Path:
    path = Path(cache_dir)
    return path if path.is_absolute() else repo_root() / path


class MirrorRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    origin_url: str | None = Field(validation_alias=AliasChoices("source_url", "origin_url"))
    fetched_at: str
    sha256: str


@dataclass(frozen=True)
class Mirror:
    root: Path
    records_by_url: dict[str, list[MirrorRecord]]

    @classmethod
    def load(cls, root: Path) -> Mirror:
        manifest = root / MIRROR_MANIFEST
        if not manifest.is_file():
            raise SourceUnavailable(
                f"no {MIRROR_MANIFEST} in {root}: a mirror must record where and when it "
                "fetched each file it holds")
        records_by_url: dict[str, list[MirrorRecord]] = defaultdict(list)
        lines = manifest.read_text(encoding="utf-8").splitlines()
        for record in _parse_mirror_records(manifest, lines):
            if record.origin_url is not None:
                records_by_url[record.origin_url].append(record)
        return cls(root, dict(records_by_url))

    def find(self, url: str) -> tuple[Path, MirrorRecord]:
        records = self.records_by_url.get(url, [])
        if len(records) != 1:
            raise SourceUnavailable(
                f"the mirror at {self.root} records {url} {len(records)} times, not once")
        path = Path(records[0].path)
        stored = path if path.is_absolute() else self.root / path
        if not stored.is_file():
            raise SourceUnavailable(f"the mirror records {url} at {stored}, which is not a file")
        return stored, records[0]

    def read(self, url: str) -> bytes:
        path, record = self.find(url)
        digest = compute_sha256(path)
        if digest != record.sha256:
            raise MirrorDisagrees(
                name=str(path), digest=digest, recorded=record.sha256, origin_url=url)
        return path.read_bytes()


def _parse_mirror_records(manifest: Path, lines: list[str]) -> list[MirrorRecord]:
    try:
        return [MirrorRecord.model_validate_json(line) for line in lines if line.strip()]
    except ValidationError as invalid:
        raise SourceUnavailable(
            f"{manifest} holds a line that is not a fetch record: {invalid}") from invalid
