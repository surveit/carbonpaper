from __future__ import annotations

from pathlib import Path

from critic.evaluation import EvalResult
from critic.matching import assemble_matching, match_exactly
from critic.report import REPORT_FILE, RESULTS_FILE, render_report, write_eval_outputs
from critic.scoring import ALL_ROUTES, score_unit
from critic.tests.fixture_data import echo_as_prediction, read_fixture_text

RESULT_FILE = "eval-result-973-976.json"


def _load_result() -> EvalResult:
    return EvalResult.model_validate_json(read_fixture_text(RESULT_FILE))


def _echo_the_reviewer_on_pr_976(result: EvalResult) -> EvalResult:
    """PR 976's review swapped for the reviewer's own comment, so the report has a match to draw."""
    unit = next(unit for unit in result.scored if unit.pr == 976)
    predictions = [echo_as_prediction(real) for real in unit.reals]
    matching = assemble_matching(
        match_exactly(predictions, unit.reals, unit.commit_sha), len(predictions), len(unit.reals)
    )
    echoed = unit.model_copy(update={
        "review": unit.review.model_copy(update={"predictions": predictions}),
        "matching": matching,
        "score": score_unit(predictions, unit.reals, matching, ALL_ROUTES),
    })
    return result.model_copy(update={"scored": [echoed]})


def test_every_real_comment_and_prediction_is_linked() -> None:
    result = _load_result()
    html = render_report(result)
    for unit in result.scored:
        assert f'href="https://github.com/surveit/carbonpaper/pull/{unit.pr}"' in html
        for real in unit.reals:
            assert f'href="{real.html_url}"' in html
        for prediction in unit.review.predictions:
            blob = f"/blob/{unit.commit_sha}/{prediction.path}#L{prediction.line}"
            assert blob in html


def test_an_ask_left_on_a_later_commit_is_marked() -> None:
    result = _load_result()
    html = render_report(result)
    unit = next(unit for unit in result.scored if unit.pr == 973)
    later = next(real for real in unit.reals if real.commit_sha != unit.commit_sha)
    assert f"(left on <code>{later.commit_sha[:10]}</code>)" in html


def test_an_exact_match_shows_both_sides_and_its_route() -> None:
    html = render_report(_echo_the_reviewer_on_pr_976(_load_result()))
    assert "<th>Real comment</th><th>Predicted comment</th><th>Route</th>" in html
    assert "exact, 0 lines apart" in html


def test_scores_print_as_percentages_and_an_empty_ratio_as_n_a() -> None:
    result = _load_result()
    html = render_report(result)
    assert (result.overall.matched_reals, result.overall.real) == (0, 22)
    assert "0.0%" in html
    assert "n/a" in html


def test_comment_text_is_escaped_not_rendered() -> None:
    html = render_report(_load_result())
    assert "if it&#39;s hoisted why is it on starlark.py?" in html


def test_the_raw_result_is_written_beside_the_report(tmp_path: Path) -> None:
    result = _load_result()
    written = write_eval_outputs(result, tmp_path)
    assert [path.name for path in written] == [RESULTS_FILE, REPORT_FILE]
    assert EvalResult.model_validate_json((tmp_path / RESULTS_FILE).read_text(encoding="utf-8")) == result
