from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from evals.harness.binding import EvalNotFound, ResolvedEval, resolve_eval_by_name
from evals.harness.dataset import DatasetInvalid
from evals.harness.definition import EvalDefinitionInvalid
from evals.harness.orchestration import (
    CaseSelectionInvalid,
    CodeCommitUnreadable,
    JudgementInvalid,
    LoadCountNotConfirmed,
    RepeatsInvalid,
)
from evals.harness.passes import PassFolder, PassFolderExists, PassIncomplete
from evals.harness.report import (
    CaseNotInDataset,
    EarlierPassInvalid,
    JudgementMissing,
    JudgementOutcomeUnknown,
)
from evals.harness.rulings import RULINGS_FILE, Disagreement, RulingNotJudged, RulingsInvalid

ResolveEval = Callable[[str], ResolvedEval]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EVALS_ROOT = _REPO_ROOT / "evals"


def main(
    argv: list[str] | None = None,
    *,
    evals_root: Path = _EVALS_ROOT,
    resolve_eval: ResolveEval = resolve_eval_by_name,
) -> int:
    arguments = _parse_arguments(argv)
    try:
        return _COMMANDS[arguments.command](arguments, evals_root, resolve_eval)
    except REFUSALS as refusal:
        print(refusal, file=sys.stderr)
        return 2


def _run_a_pass(arguments: argparse.Namespace, evals_root: Path, resolve_eval: ResolveEval) -> int:
    resolved = resolve_eval(arguments.eval)
    pass_dir = resolved.run_pass(
        eval_dir=evals_root / resolved.name,
        repeats=_find_repeats(arguments.repeats, resolved.default_repeats),
        confirmed_loads=arguments.loads,
        case_ids=[] if arguments.case is None else arguments.case,
        repo_root=_REPO_ROOT,
    )
    print(pass_dir)
    return 0


def _judge_a_pass(
    arguments: argparse.Namespace, evals_root: Path, resolve_eval: ResolveEval
) -> int:
    resolved = resolve_eval(_read_pass_eval_name(arguments.pass_dir))
    resolved.judge_pass(arguments.pass_dir, eval_dir=evals_root / resolved.name)
    return 0


def _write_a_report(
    arguments: argparse.Namespace, evals_root: Path, resolve_eval: ResolveEval
) -> int:
    resolved = resolve_eval(_read_pass_eval_name(arguments.pass_dir))
    resolved.write_report(
        arguments.pass_dir, eval_dir=evals_root / resolved.name, against=arguments.against
    )
    return 0


def _list_disagreements(
    arguments: argparse.Namespace, evals_root: Path, resolve_eval: ResolveEval
) -> int:
    resolved = resolve_eval(arguments.eval)
    disagreements = resolved.find_ruling_disagreements(evals_root / resolved.name / RULINGS_FILE)
    for disagreement in disagreements:
        print(_describe_disagreement(disagreement))
    return 1 if disagreements else 0


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="load an eval's cases into a new pass")
    run.add_argument("eval")
    run.add_argument("--loads", type=int, required=True, help="the load count to confirm")
    run.add_argument("--repeats", type=int, help="loads per case; the eval's default when absent")
    run.add_argument("--case", action="append", help="load only this case_id; repeatable")
    judge = commands.add_parser("judge", help="judge a pass's stored outputs again")
    judge.add_argument("pass_dir", type=Path)
    report = commands.add_parser("report", help="write a pass's report")
    report.add_argument("pass_dir", type=Path)
    report.add_argument("--against", type=Path, help="an earlier pass to show beside this one")
    rulings = commands.add_parser("rulings", help="compare a person's rulings with the judge")
    rulings.add_argument("eval")
    return parser.parse_args(argv)


def _find_repeats(repeats: int | None, default_repeats: int) -> int:
    return default_repeats if repeats is None else repeats


def _read_pass_eval_name(pass_dir: Path) -> str:
    return PassFolder(pass_dir).read_record().eval


def _describe_disagreement(disagreement: Disagreement) -> str:
    return (
        f"{disagreement.ruling_id} {disagreement.key}: person {disagreement.person}, "
        f"judge {disagreement.judge}: {disagreement.note}"
    )


REFUSALS: tuple[type[Exception], ...] = (
    CaseNotInDataset,
    CaseSelectionInvalid,
    CodeCommitUnreadable,
    DatasetInvalid,
    EarlierPassInvalid,
    EvalDefinitionInvalid,
    EvalNotFound,
    JudgementInvalid,
    JudgementMissing,
    JudgementOutcomeUnknown,
    LoadCountNotConfirmed,
    PassFolderExists,
    PassIncomplete,
    RepeatsInvalid,
    RulingNotJudged,
    RulingsInvalid,
)

_COMMANDS: dict[str, Callable[[argparse.Namespace, Path, ResolveEval], int]] = {
    "run": _run_a_pass,
    "judge": _judge_a_pass,
    "report": _write_a_report,
    "rulings": _list_disagreements,
}
