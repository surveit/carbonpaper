from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

from critic.backend import ClaudeCliBackend
from critic.corpus import fetch_corpus
from critic.diff import fetch_pull_request_diff, load_or_fetch_diff, save_diff
from critic.evaluation import DEFAULT_PR_RANGE, EvalSettings, run_eval
from critic.labels import load_test_split
from critic.report import write_eval_outputs
from critic.review import run_review
from critic.rubric import load_rubric
from critic.themes import load_theme_vocabulary, select_flag_themes

DEFAULT_REPO = "surveit/carbonpaper"
LOCAL_ROOT = Path(".critic")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command: Callable[[argparse.Namespace], int] = args.command
    return command(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m critic", description="Predict and score review comments.")
    commands = parser.add_subparsers(required=True)
    _add_fetch(commands.add_parser("fetch", help="re-fetch the review corpus through gh"))
    _add_diff(commands.add_parser("diff", help="fetch and cache one PR's diff"))
    _add_review(commands.add_parser("review", help="predict the reviewer's comments on one PR"))
    _add_eval(commands.add_parser("eval", help="score predictions against labeled real comments"))
    return parser


def run_fetch(args: argparse.Namespace) -> int:
    counts = fetch_corpus(args.repo, args.out)
    print(counts.model_dump_json())
    return 0


def run_diff(args: argparse.Namespace) -> int:
    diff = fetch_pull_request_diff(args.repo, args.pr, args.commit)
    print(save_diff(diff, args.out))
    return 0


def run_review_command(args: argparse.Namespace) -> int:
    backend = ClaudeCliBackend(model=args.model)
    backend.check_available()
    diff = (
        fetch_pull_request_diff(args.repo, args.pr, None)
        if args.commit is None
        else load_or_fetch_diff(args.repo, args.pr, args.commit, args.diff_dir)
    )
    themes = None if args.themes is None else select_flag_themes(load_theme_vocabulary(args.themes))
    review = run_review(diff, load_rubric(args.rubric), themes, backend)
    text = review.model_dump_json(indent=2)
    if args.out is None:
        print(text)
    else:
        args.out.write_text(text, encoding="utf-8")
    return 0


def run_eval_command(args: argparse.Namespace) -> int:
    review_backend = ClaudeCliBackend(model=args.model)
    judge_backend = ClaudeCliBackend(model=args.judge_model)
    review_backend.check_available()
    result = run_eval(_read_eval_settings(args), review_backend, judge_backend)
    for path in write_eval_outputs(result, args.out):
        print(path)
    print(json.dumps({"overall": result.overall.model_dump(), "exact_only": result.exact_only.model_dump()}))
    return 0


def parse_pr_list(text: str) -> list[int]:
    return [int(part) for part in text.split(",") if part.strip()]


def parse_pr_range(text: str) -> list[int]:
    low, high = (int(part) for part in text.split("-", 1))
    if low > high:
        raise argparse.ArgumentTypeError(f"range {text} runs backwards")
    return list(range(low, high + 1))


def _read_eval_settings(args: argparse.Namespace) -> EvalSettings:
    pr_numbers = _read_pr_numbers(args)
    if not pr_numbers:
        raise SystemExit("eval needs at least one PR number")
    return EvalSettings(
        repo=args.repo,
        rubric_dir=str(args.rubric),
        pr_numbers=pr_numbers,
        limit=args.limit,
        corpus_dir=str(args.corpus),
        labels_path=str(args.labels),
        themes_path=str(args.themes),
        diff_dir=str(args.diff_dir),
        review_model=args.model,
        judge_model=args.judge_model,
        jobs=args.jobs,
    )


def _read_pr_numbers(args: argparse.Namespace) -> list[int]:
    if args.prs is not None:
        return list(args.prs)
    if args.range is not None:
        return list(args.range)
    if args.split is not None:
        return load_test_split(args.split)
    low, high = DEFAULT_PR_RANGE
    return list(range(low, high + 1))


def _add_fetch(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--out", type=Path, required=True)
    parser.set_defaults(command=run_fetch)


def _add_diff(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("pr", type=int)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--commit", help="a commit the PR held; default: its head")
    parser.add_argument("--out", type=Path, required=True)
    parser.set_defaults(command=run_diff)


def _add_review(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("pr", type=int)
    parser.add_argument("--rubric", type=Path, required=True)
    parser.add_argument("--model", help="claude model alias or id; default: the CLI's own")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--commit", help="review the diff at this commit; default: the PR's head")
    parser.add_argument("--themes", type=Path, help="a themes.json the predictions must draw from")
    parser.add_argument("--diff-dir", type=Path, default=LOCAL_ROOT / "diffs")
    parser.add_argument("--out", type=Path, help="write the review JSON here; default: stdout")
    parser.set_defaults(command=run_review_command)


def _add_eval(parser: argparse.ArgumentParser) -> None:
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--prs", type=parse_pr_list, help="comma-separated PR numbers")
    low, high = DEFAULT_PR_RANGE
    selection.add_argument("--range", type=parse_pr_range, help=f"LO-HI; default {low}-{high}")
    selection.add_argument("--split", type=Path, help="a split.json; its test PRs are evaluated")
    parser.add_argument("--rubric", type=Path, required=True)
    parser.add_argument("--limit", type=int, help="stop after this many PRs with a scorable unit")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", help="review model; default: the CLI's own")
    parser.add_argument("--judge-model", help="judge model; default: the CLI's own")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--corpus", type=Path, default=LOCAL_ROOT / "corpus")
    parser.add_argument("--labels", type=Path, default=LOCAL_ROOT / "labels" / "corpus.human.jsonl")
    parser.add_argument("--themes", type=Path, default=LOCAL_ROOT / "labels" / "taxonomy.json")
    parser.add_argument("--diff-dir", type=Path, default=LOCAL_ROOT / "diffs")
    parser.add_argument("--jobs", type=int, default=1, help="review units run at once")
    parser.set_defaults(command=run_eval_command)
