from __future__ import annotations

import json
from pathlib import Path

from app.seeds.backfill import Verdict, backfill_project_records, render_report
from app.services.project import Project


def _stage(projects_root: Path, name: str, project_json: object | None) -> Path:
    pdir = projects_root / name
    pdir.mkdir(parents=True)
    if project_json is not None:
        text = project_json if isinstance(project_json, str) else json.dumps(project_json)
        (pdir / "project.json").write_text(text, encoding="utf-8")
    return pdir


def _verdict_for(report, name: str):
    return next(item for item in report.verdicts if item.name == name)


def test_backfill_creates_a_record_carrying_every_project_json_field(projects_root):
    _stage(projects_root, "alpha", {
        "name": "alpha", "title": "Alpha", "model": "sonnet",
        "source": "paste", "created_at": "2026-01-02T03:04:05",
    })

    report = backfill_project_records(apply=True)

    assert _verdict_for(report, "alpha").verdict is Verdict.CREATED
    record = Project.load_or_none("alpha")
    assert record is not None
    assert (record.title, record.model, record.source) == ("Alpha", "sonnet", "paste")
    assert record.authored_at == "2026-01-02T03:04:05"


def test_backfill_leaves_authored_at_none_when_project_json_has_no_date(projects_root):
    _stage(projects_root, "undated", {"name": "undated", "model": "sonnet", "source": "paste"})

    backfill_project_records(apply=True)

    record = Project.load_or_none("undated")
    assert record is not None
    assert record.authored_at is None, "an absent date must stay absent, never inferred"
    assert record.title is None


def test_backfill_never_touches_an_existing_record(projects_root):
    Project(
        id="existing", title="Kept", model="opus", source="import", authored_at="2025-12-25",
    ).save()
    _stage(projects_root, "existing", {
        "name": "existing", "title": "Overwritten", "model": "haiku",
        "source": "paste", "created_at": "2026-06-06",
    })

    report = backfill_project_records(apply=True)

    assert _verdict_for(report, "existing").verdict is Verdict.ALREADY_RECORDED
    record = Project.load_or_none("existing")
    assert record is not None
    assert (record.title, record.model, record.source, record.authored_at) == (
        "Kept", "opus", "import", "2025-12-25",
    )


def test_backfill_refuses_a_directory_with_no_project_json_and_names_it(projects_root):
    husk = _stage(projects_root, "congresswatch", None)
    (husk / "stages").mkdir()

    report = backfill_project_records(apply=True)

    item = _verdict_for(report, "congresswatch")
    assert item.verdict is Verdict.NOT_A_PROJECT
    assert item.detail is not None and "project.json" in item.detail
    assert Project.load_or_none("congresswatch") is None
    assert any("congresswatch" in line for line in render_report(report))


def test_backfill_reports_malformed_project_json_as_an_error_and_writes_nothing(projects_root):
    _stage(projects_root, "broken", "{not json,")

    report = backfill_project_records(apply=True)

    assert _verdict_for(report, "broken").verdict is Verdict.MALFORMED
    assert Project.load_or_none("broken") is None


def test_dry_run_writes_nothing(projects_root):
    _stage(projects_root, "alpha", {"name": "alpha", "created_at": "2026-01-02"})

    report = backfill_project_records(apply=False)

    assert _verdict_for(report, "alpha").verdict is Verdict.CREATED
    assert Project.list() == []
    assert any("would create" in line for line in render_report(report))
    assert any("DRY RUN" in line for line in render_report(report))


def test_backfill_is_idempotent(projects_root):
    _stage(projects_root, "alpha", {"name": "alpha", "created_at": "2026-01-02"})
    backfill_project_records(apply=True)

    second = backfill_project_records(apply=True)

    assert _verdict_for(second, "alpha").verdict is Verdict.ALREADY_RECORDED
    assert second.count(Verdict.CREATED) == 0
    assert [record.id for record in Project.list()] == ["alpha"]


def test_backfill_reports_every_directory_and_counts_them(projects_root):
    _stage(projects_root, "alpha", {"name": "alpha"})
    _stage(projects_root, "husk", None)
    _stage(projects_root, "broken", "{")

    report = backfill_project_records(apply=False)

    assert {item.name for item in report.verdicts} == {"alpha", "husk", "broken"}
    assert report.count(Verdict.CREATED) == 1
    assert report.count(Verdict.NOT_A_PROJECT) == 1
    assert report.count(Verdict.MALFORMED) == 1


def test_backfill_on_a_missing_projects_root_reports_nothing(tmp_path):
    from app.services.workspace import set_projects_dir
    set_projects_dir(tmp_path / "absent")

    report = backfill_project_records(apply=True)

    assert report.verdicts == []
