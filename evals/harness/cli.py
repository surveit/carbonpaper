"""`python -m evals.harness.cli` — extract a golden from a notebook, or compare a build."""
from __future__ import annotations

import argparse
from pathlib import Path

from evals.harness.case import load_case
from evals.harness.compare import Comparison, compare_case_csv
from evals.harness.golden import extract_golden_table


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "extract-golden":
        golden = extract_golden_table(args.notebook, args.cell_index, args.key_column)
        print(golden.model_dump_json(indent=2))
        return 0
    comparison = compare_case_csv(load_case(args.case), args.actual)
    print(comparison.model_dump_json(indent=2) if args.full else _summarize(comparison))
    return 0 if comparison.agrees() else 1


def _summarize(comparison: Comparison) -> str:
    lines = [
        f"case            {comparison.case_id}",
        f"golden keys     {comparison.golden_keys}",
        f"output keys     {comparison.output_keys}",
        f"shared keys     {comparison.shared_keys}",
        f"missing         {', '.join(comparison.missing_from_output) or '(none)'}",
        f"extra           {', '.join(comparison.extra_in_output) or '(none)'}",
        f"figures differ  {len(comparison.figure_disagreements)}",
    ]
    for disagreement in comparison.figure_disagreements[:10]:
        lines.append(
            f"  {disagreement.key} / {disagreement.column}: "
            f"golden {disagreement.golden!r} vs {disagreement.actual!r}"
        )
    if len(comparison.figure_disagreements) > 10:
        lines.append(f"  ... {len(comparison.figure_disagreements) - 10} more (--full)")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evals.harness.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract-golden", help="read a golden table from a notebook")
    extract.add_argument("notebook", type=Path)
    extract.add_argument("cell_index", type=int)
    extract.add_argument("key_column")

    compare = sub.add_parser("compare", help="compare a build's output CSV against a case")
    compare.add_argument("case", type=Path)
    compare.add_argument("actual", type=Path)
    compare.add_argument("--full", action="store_true", help="print the whole comparison as JSON")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
