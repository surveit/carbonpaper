from __future__ import annotations

from pathlib import Path

import pytest

from critic.cli import main
from critic.comparison import COMPARISON_HTML, COMPARISON_JSON, Comparison, ScoredCell, compare_runs
from critic.corpus import load_pr_listings
from critic.evaluation import EvalResult, assemble_result
from critic.matching import assemble_matching, match_exactly
from critic.planning import EvalPlan
from critic.rubric import Rubric
from critic.scoring import ALL_ROUTES, score_unit
from critic.tests.fixture_data import FIXTURES, echo_as_prediction, load_labels, read_fixture_text
from critic.unit_scoring import ScoredUnit

NOTES = ["The second rubric catches the question on #976."]


def _load_result() -> EvalResult:
    return EvalResult.model_validate_json(read_fixture_text("eval-result-973-976.json"))


def _rebuild(result: EvalResult, units: list[ScoredUnit], rubric: str) -> EvalResult:
    plan = EvalPlan(units=[], skipped_prs=result.skipped_prs, excluded_labels=result.excluded_labels)
    settings = result.settings.model_copy(update={"rubric_dir": f"critic/rubrics/{rubric}"})
    empty = Rubric(name=rubric, files=[])
    return assemble_result(settings, empty, plan, list(units), load_labels().themes, result.started_at)


def _echo_the_reviewer(unit: ScoredUnit) -> ScoredUnit:
    predictions = [echo_as_prediction(real) for real in unit.reals]
    exact = match_exactly(predictions, unit.reals, unit.commit_sha)
    matching = assemble_matching(exact, len(predictions), len(unit.reals))
    review = unit.review.model_copy(update={"predictions": predictions})
    score = score_unit(predictions, unit.reals, matching, ALL_ROUTES)
    return unit.model_copy(update={"review": review, "matching": matching, "score": score})


def _silence(unit: ScoredUnit) -> ScoredUnit:
    """The unit as if the reviewer had left nothing on it."""
    predictions = unit.review.predictions
    matching = assemble_matching([], len(predictions), 0)
    score = score_unit(predictions, [], matching, ALL_ROUTES)
    return unit.model_copy(update={"reals": [], "matching": matching, "score": score})


def _compare(tmp_path: Path, *results: EvalResult) -> Comparison:
    runs = {tmp_path / f"run-{index}" / "results.json": result for index, result in enumerate(results)}
    return compare_runs(runs, load_pr_listings(FIXTURES / "corpus"), NOTES, tmp_path)


def _two_runs() -> tuple[EvalResult, EvalResult]:
    result = _load_result()
    echoed = [_echo_the_reviewer(unit) if unit.pr == 976 else unit for unit in result.scored]
    return _rebuild(result, result.scored, "none"), _rebuild(result, echoed, "echo")


def test_each_pr_counts_its_matches_by_route_and_its_spend(tmp_path: Path) -> None:
    comparison = _compare(tmp_path, *_two_runs())
    row = next(row for row in comparison.prs if row.pr == 976)
    assert row.real == 1 and row.changed_lines == 71 and row.size_bucket == "50-199"
    echoed = row.cells[1]
    assert isinstance(echoed, ScoredCell)
    assert (echoed.predicted, echoed.matched_exact, echoed.matched_judge) == (1, 1, 0)
    assert echoed.cost_usd == pytest.approx(0.1520275)
    assert [run.overall.matched_reals for run in comparison.runs] == [0, 1]
    assert [run.results_path for run in comparison.runs] == ["run-0/results.json", "run-1/results.json"]


def test_a_theme_one_run_never_touched_scores_zero_in_it(tmp_path: Path) -> None:
    comparison = _compare(tmp_path, *_two_runs())
    tests_row = next(row for row in comparison.themes if row.theme == "tests")
    assert [score.predicted for score in tests_row.scores] == [1, 0]
    assert comparison.themes[0].theme == "needless_abstraction"


def test_runs_over_different_prs_do_not_compare(tmp_path: Path) -> None:
    result = _load_result()
    only_973 = _rebuild(result, [unit for unit in result.scored if unit.pr == 973], "narrow")
    with pytest.raises(ValueError, match="do not compare"):
        _compare(tmp_path, _rebuild(result, result.scored, "none"), only_973)


def test_predictions_on_a_silent_pr_are_counted_in_its_size_bucket(tmp_path: Path) -> None:
    result = _load_result()
    silenced = _rebuild(result, [_silence(unit) if unit.pr == 976 else unit for unit in result.scored], "none")
    comparison = _compare(tmp_path, silenced)
    assert [(bucket.size_bucket, bucket.silent_prs, bucket.predictions) for bucket in comparison.silent_buckets] == [
        ("50-199", 1, [3]),
    ]
    run = comparison.runs[0]
    assert (run.silent_predictions, run.on_commented_prs.predicted, run.overall.predicted) == (3, 3, 6)
    assert [row.pr for row in comparison.prs] == [973, 976]


def test_compare_writes_the_raw_json_beside_the_page(tmp_path: Path) -> None:
    paths = []
    for index, result in enumerate(_two_runs()):
        path = tmp_path / f"run-{index}.json"
        path.write_text(result.model_dump_json(), encoding="utf-8")
        paths.append(str(path))
    notes = tmp_path / "notes.txt"
    notes.write_text(NOTES[0].replace(" on ", "\non ") + "\n\nSecond paragraph.\n", encoding="utf-8")
    out = tmp_path / "out"
    assert main(["compare", *paths, "--notes", str(notes), "--corpus", str(FIXTURES / "corpus"), "--out", str(out)]) == 0
    comparison = Comparison.model_validate_json((out / COMPARISON_JSON).read_text(encoding="utf-8"))
    assert comparison.notes == [*NOTES, "Second paragraph."]
    html = (out / COMPARISON_HTML).read_text(encoding="utf-8")
    assert '<a href="../run-0.json">../run-0.json</a>' in html
    assert 'href="https://github.com/surveit/carbonpaper/pull/976"' in html
    assert f"<p>{NOTES[0]}</p>" in html
