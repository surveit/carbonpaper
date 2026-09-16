from __future__ import annotations

import importlib
import inspect
import pkgutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import JsonValue

from evals import harness
from evals.harness import cli, orchestration
from evals.harness.binding import ResolvedEval, bind_eval
from evals.harness.cli import main
from evals.harness.definition import CaseRefused
from evals.harness.report import AttemptOutcome, PassReport
from evals.harness.rulings import Disagreement
from fixed_output_eval import FIXED_OUTPUT_EVAL
from test_eval_passes import build_case, read_pass_record, write_dataset
from test_eval_report import MovableClock
from test_eval_rulings import build_ruling, write_rulings

_EVAL_NAME = FIXED_OUTPUT_EVAL.name
_PASS_ID = "20260915T120000"


def test_cli_run_resolves_the_eval_and_its_dataset_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eval_dir = tmp_path / _EVAL_NAME
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    resolver = RecordingResolver()

    exit_code = main(
        ["run", _EVAL_NAME, "--loads", "2"], evals_root=tmp_path, resolve_eval=resolver
    )

    printed = capsys.readouterr()
    pass_dir = Path(printed.out.strip())
    assert exit_code == 0
    assert resolver.names == [_EVAL_NAME]
    assert printed.out == f"{pass_dir}\n"
    assert pass_dir.parent == eval_dir / "passes"
    assert read_pass_record(pass_dir).case_ids == ["agrees"]


@pytest.mark.parametrize(
    ("repeats_argv", "loads", "attempts"),
    [([], "2", [1, 2]), (["--repeats", "1"], "1", [1])],
    ids=["the_evals_default_repeats", "one_repeat"],
)
def test_cli_run_loads_each_case_the_evals_default_repeats_unless_repeats_says_otherwise(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    repeats_argv: list[str],
    loads: str,
    attempts: list[int],
) -> None:
    eval_dir = tmp_path / _EVAL_NAME
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))

    exit_code = main(
        ["run", _EVAL_NAME, "--loads", loads, *repeats_argv],
        evals_root=tmp_path,
        resolve_eval=resolve_fixed_output,
    )

    pass_dir = Path(capsys.readouterr().out.strip())
    assert exit_code == 0
    assert FIXED_OUTPUT_EVAL.default_repeats == 2
    assert [load.attempt for load in read_pass_record(pass_dir).loads] == attempts


def test_cli_run_loads_only_the_cases_the_case_flags_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eval_dir = tmp_path / _EVAL_NAME
    write_dataset(
        eval_dir,
        build_case("first", "yes", answer="yes"),
        build_case("second", "no", answer="no"),
        build_case("third", "maybe", answer="maybe"),
    )

    exit_code = main(
        ["run", _EVAL_NAME, "--loads", "4", "--case", "third", "--case", "first"],
        evals_root=tmp_path,
        resolve_eval=resolve_fixed_output,
    )

    pass_dir = Path(capsys.readouterr().out.strip())
    assert exit_code == 0
    assert read_pass_record(pass_dir).case_ids == ["first", "third"]


def test_cli_judge_and_report_read_the_eval_from_the_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eval_dir = tmp_path / _EVAL_NAME
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    resolver = RecordingResolver()
    assert main(["run", _EVAL_NAME, "--loads", "2"], evals_root=tmp_path, resolve_eval=resolver) == 0
    pass_dir = Path(capsys.readouterr().out.strip())
    write_dataset(eval_dir, build_case("agrees", "yes", answer="no"))

    judged = main(["judge", str(pass_dir)], evals_root=tmp_path, resolve_eval=resolver)
    reported = main(["report", str(pass_dir)], evals_root=tmp_path, resolve_eval=resolver)

    assert (judged, reported) == (0, 0)
    assert resolver.names == [_EVAL_NAME, _EVAL_NAME, _EVAL_NAME]
    assert capsys.readouterr().out == ""
    assert read_report(pass_dir).cases[0].rows[0].outcome_by_attempt == [
        AttemptOutcome(attempt=1, outcome="differed"),
        AttemptOutcome(attempt=2, outcome="differed"),
    ]


def test_cli_report_shows_the_pass_that_against_names_beside_this_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], clock: MovableClock
) -> None:
    eval_dir = tmp_path / _EVAL_NAME
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    earlier = run_a_pass(tmp_path, capsys)
    clock.move_on(60)
    later = run_a_pass(tmp_path, capsys)

    exit_code = main(
        ["report", str(later), "--against", str(earlier)],
        evals_root=tmp_path,
        resolve_eval=resolve_fixed_output,
    )

    assert exit_code == 0
    assert read_report(later).against_pass_id == earlier.name


def test_cli_resolves_an_eval_by_importing_its_module_and_reading_its_eval_attribute(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "EVAL", FIXED_OUTPUT_EVAL, raising=False)
    write_rulings_for(tmp_path, build_ruling("ruled", "no", answer=("yes", "matched")))

    exit_code = main(["rulings", "harness"], evals_root=tmp_path)

    assert exit_code == 1
    assert capsys.readouterr().out == (
        "ruled answer: person matched, judge differed: answer 'no' is not 'yes'\n"
    )


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("no_such_eval", "no eval named 'no_such_eval': no module evals.no_such_eval"),
        ("harness", "no eval named 'harness': evals.harness defines no EVAL"),
    ],
    ids=["no_module", "no_eval_attribute"],
)
def test_cli_refuses_a_name_no_eval_module_or_eval_attribute_answers_to(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, message: str
) -> None:
    exit_code = main(["rulings", name], evals_root=tmp_path)

    assert exit_code == 2
    assert capsys.readouterr().err == f"{message}\n"


def test_cli_refuses_a_module_whose_eval_attribute_is_not_an_eval_definition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "EVAL", _EVAL_NAME, raising=False)

    exit_code = main(["rulings", "harness"], evals_root=tmp_path)

    assert exit_code == 2
    assert capsys.readouterr().err == (
        "no eval named 'harness': evals.harness.EVAL is a str, not an EvalDefinition\n"
    )


def test_cli_run_refuses_a_dataset_the_evals_models_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset_path = write_dataset(tmp_path / _EVAL_NAME, build_case("expects_nothing", "yes"))

    exit_code = main(
        ["run", _EVAL_NAME, "--loads", "2"], evals_root=tmp_path, resolve_eval=resolve_fixed_output
    )

    assert exit_code == 2
    assert capsys.readouterr().err == (
        f"{dataset_path}: case 'expects_nothing' has no expected outputs\n"
    )


def test_cli_run_refuses_a_load_count_that_differs_from_what_the_pass_plans(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_dataset(tmp_path / _EVAL_NAME, build_case("agrees", "yes", answer="yes"))

    exit_code = main(
        ["run", _EVAL_NAME, "--loads", "3"], evals_root=tmp_path, resolve_eval=resolve_fixed_output
    )

    assert exit_code == 2
    assert capsys.readouterr().err == "this pass plans 2 loads; confirm with --loads 2\n"


def test_cli_run_refuses_fewer_than_one_repeat(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_dataset(tmp_path / _EVAL_NAME, build_case("agrees", "yes", answer="yes"))

    exit_code = main(
        ["run", _EVAL_NAME, "--loads", "1", "--repeats", "0"],
        evals_root=tmp_path,
        resolve_eval=resolve_fixed_output,
    )

    assert exit_code == 2
    assert capsys.readouterr().err == "repeats must be at least 1, not 0\n"


def test_cli_run_refuses_a_case_id_the_dataset_does_not_hold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset_path = write_dataset(tmp_path / _EVAL_NAME, build_case("agrees", "yes", answer="yes"))

    exit_code = main(
        ["run", _EVAL_NAME, "--loads", "2", "--case", "missing"],
        evals_root=tmp_path,
        resolve_eval=resolve_fixed_output,
    )

    assert exit_code == 2
    assert capsys.readouterr().err == (
        f"{dataset_path}: case_ids not in the dataset: 'missing'\n"
    )


def test_cli_run_refuses_a_pass_folder_that_already_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], clock: MovableClock
) -> None:
    eval_dir = tmp_path / _EVAL_NAME
    write_dataset(eval_dir, build_case("agrees", "yes", answer="yes"))
    existing = eval_dir / "passes" / _PASS_ID
    existing.mkdir(parents=True)

    exit_code = main(
        ["run", _EVAL_NAME, "--loads", "2"], evals_root=tmp_path, resolve_eval=resolve_fixed_output
    )

    assert exit_code == 2
    assert capsys.readouterr().err == f"pass folder {existing} already exists\n"


def test_cli_rulings_refuses_a_rulings_file_the_eval_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rulings_path = write_rulings_for(tmp_path)

    exit_code = main(
        ["rulings", _EVAL_NAME], evals_root=tmp_path, resolve_eval=resolve_fixed_output
    )

    assert exit_code == 2
    assert capsys.readouterr().err == f"{rulings_path}: no rulings to compare\n"


@pytest.mark.parametrize(
    "refusal",
    [refusal_type("the harness refused, in one line") for refusal_type in cli.REFUSALS],
    ids=lambda refusal: type(refusal).__name__,
)
def test_cli_prints_one_line_and_exits_two_for_a_harness_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], refusal: Exception
) -> None:
    resolved: ResolvedEval = RefusingEval(refusal)

    exit_code = main(
        ["run", _EVAL_NAME, "--loads", "1"],
        evals_root=tmp_path,
        resolve_eval=lambda name: resolved,
    )

    printed = capsys.readouterr()
    assert exit_code == 2
    assert printed.err == f"{refusal}\n"
    assert printed.out == ""


def test_every_exception_the_harness_defines_is_a_cli_refusal_or_is_converted_first() -> None:
    # CaseRefused is converted where it is caught: into a load record, a judgement file or a refusal.
    assert find_harness_exceptions() == set(cli.REFUSALS) | {CaseRefused}


def test_cli_lets_an_error_no_refusal_names_propagate_with_its_traceback(tmp_path: Path) -> None:
    resolved: ResolvedEval = RefusingEval(KeyError("boom"))

    with pytest.raises(KeyError, match="boom"):
        main(
            ["run", _EVAL_NAME, "--loads", "1"],
            evals_root=tmp_path,
            resolve_eval=lambda name: resolved,
        )


def find_harness_exceptions() -> set[type[Exception]]:
    modules = [
        importlib.import_module(f"{harness.__name__}.{found.name}")
        for found in pkgutil.iter_modules(harness.__path__)
    ]
    return {
        member
        for module in modules
        for _, member in inspect.getmembers(module, inspect.isclass)
        if issubclass(member, Exception) and member.__module__ == module.__name__
    }


class RecordingResolver:
    def __init__(self) -> None:
        self.names: list[str] = []

    def __call__(self, name: str) -> ResolvedEval:
        self.names.append(name)
        return bind_eval(FIXED_OUTPUT_EVAL)


@dataclass(frozen=True)
class RefusingEval:
    refusal: Exception

    @property
    def name(self) -> str:
        return _EVAL_NAME

    @property
    def default_repeats(self) -> int:
        return FIXED_OUTPUT_EVAL.default_repeats

    def run_pass(
        self,
        *,
        eval_dir: Path,
        repeats: int,
        confirmed_loads: int,
        case_ids: list[str],
        repo_root: Path,
    ) -> Path:
        raise self.refusal

    def judge_pass(self, pass_dir: Path, *, eval_dir: Path) -> None:
        raise self.refusal

    def write_report(self, pass_dir: Path, *, eval_dir: Path, against: Path | None) -> None:
        raise self.refusal

    def find_ruling_disagreements(self, rulings_path: Path) -> list[Disagreement]:
        raise self.refusal


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> MovableClock:
    fake = MovableClock()
    monkeypatch.setattr(orchestration, "datetime", fake)
    monkeypatch.setattr(orchestration, "time", fake)
    return fake


def resolve_fixed_output(name: str) -> ResolvedEval:
    return bind_eval(FIXED_OUTPUT_EVAL)


def run_a_pass(evals_root: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    exit_code = main(
        ["run", _EVAL_NAME, "--loads", "2"],
        evals_root=evals_root,
        resolve_eval=resolve_fixed_output,
    )
    assert exit_code == 0
    return Path(capsys.readouterr().out.strip())


def write_rulings_for(evals_root: Path, *rulings: JsonValue) -> Path:
    eval_dir = evals_root / _EVAL_NAME
    eval_dir.mkdir(parents=True, exist_ok=True)
    return write_rulings(eval_dir, *rulings)


def read_report(pass_dir: Path) -> PassReport:
    return PassReport.model_validate_json((pass_dir / "report.json").read_text(encoding="utf-8"))
