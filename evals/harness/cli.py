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
        golden = extract_golden_table(args.notebook, args.cell_index, args.index_column)
        print(golden.model_dump_json(indent=2))
        return 0
    comparison = compare_case_csv(load_case(args.case), args.actual)
    print(comparison.model_dump_json(indent=2) if args.full else _summarize(comparison))
    return 0 if comparison.agrees() else 1


def _summarize(comparison: Comparison) -> str:
    lines = [
        f"case             {comparison.case_id}",
        f"golden rows      {comparison.golden_rows}",
        f"output rows      {comparison.output_rows}",
        f"aligned in order {comparison.aligned_rows}",
        f"row differences  {len(comparison.row_differences)}",
        f"cell differences {len(comparison.cell_differences)}",
    ]
    for difference in comparison.row_differences[:12]:
        shown = " · ".join(str(v) for v in list(difference.row.values())[:4])
        lines.append(f"  {difference.kind:<7} at {difference.position:>4}  {shown[:88]}")
    if len(comparison.row_differences) > 12:
        lines.append(f"  ... {len(comparison.row_differences) - 12} more (--full)")
    for cell in comparison.cell_differences[:8]:
        lines.append(
            f"  cell    at {cell.position:>4}  {cell.column}: "
            f"golden {cell.golden!r} vs {cell.actual!r}"
        )
    if len(comparison.cell_differences) > 8:
        lines.append(f"  ... {len(comparison.cell_differences) - 8} more (--full)")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evals.harness.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract-golden", help="read a golden table from a notebook")
    extract.add_argument("notebook", type=Path)
    extract.add_argument("cell_index", type=int)
    extract.add_argument(
        "--index-column",
        default=None,
        help="name for the rendered pandas index; omit to DROP it (a positional int index)",
    )

    compare = sub.add_parser("compare", help="compare a build's output CSV against a case")
    compare.add_argument("case", type=Path)
    compare.add_argument("actual", type=Path)
    compare.add_argument("--full", action="store_true", help="print the whole comparison as JSON")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
