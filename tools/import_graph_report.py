"""Per-PR import-graph coupling report.

Computes coupling metrics for the `app` package (propagation cost, module/
edge counts, edge density, fan-in/fan-out extremes, import-cycle count) and
renders the head-vs-base comment body the `import-graph-report` CI job posts
on every pull request. Purely informational — the job that runs this is
never merge-blocking — so a metric this script cannot compute must fail loudly
(an exception) rather than surface as a wrong or estimated number in
someone's PR comment.

Usage:
    python tools/import_graph_report.py [--root PATH] > metrics.json
    python tools/import_graph_report.py --markdown HEAD_JSON BASE_JSON

`--root` points at the repository root whose `app` package should be
scanned (default: this script's own repo root). The CI job runs this same
script twice — once for the PR head checkout, once for a second checkout of
the base ref — passing `--root` at the base checkout's path, so the base
ref never needs its own copy of this script.

Reuses `tests/arch/test_import_graph.py`'s edge-finding, fan-in/fan-out
degree, cycle-detection, and core-module logic (imported from *this*
checkout's tests/ regardless of which root's `app` package is being scanned
— that logic is pure Python over an edge list, not tied to a particular
checkout's tests/ directory being present). Where the gate enforces a metric
(cycles, fan-out) it runs this same code, so the report cannot drift from
it. Propagation cost is new logic the gate doesn't need, and lives here
instead.
"""
from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from arch.test_import_graph import ModuleDegree

_APP_PACKAGE = "app"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_MARKER = "<!-- import-graph-report -->"


class ReachabilityGraph(Protocol):
    """The two grimp `ImportGraph` traversal methods propagation-cost
    computation needs, narrowed to a Protocol so `grimp.ImportGraph` (whose
    real methods) satisfies it structurally and tests can cross-check
    against a duck-typed stub without importing grimp's concrete type."""

    def find_upstream_modules(self, module: str) -> set[str]: ...
    def find_downstream_modules(self, module: str) -> set[str]: ...


class ImportGraphMetricError(Exception):
    """A metric could not be computed from grimp's graph — the caller must see
    this and fail the step; there is no fallback value to report instead."""


# --- report schema (also the on-disk JSON shape) ----------------------------


class FanExtremeReport(BaseModel):
    """The module(s) tied for the maximum degree (fan-in or fan-out) among
    the modules considered, and that degree. `modules` holds every module
    tied for the maximum — never an arbitrary single pick when several
    modules share the top degree."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    modules: tuple[str, ...]
    degree: int


class ImportGraphMetricsReport(BaseModel):
    """One computed snapshot of the app-internal import graph's coupling
    metrics. This is both the return type of `compute_import_graph_metrics`
    and the on-disk JSON schema `--markdown` mode reads back in (via
    `model_validate_json`) — the two checkouts' snapshots it renders into a
    comment body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    modules: int
    internal_edges: int
    edge_density: float
    propagation_cost_percent: float
    cycles: int
    max_fan_in: FanExtremeReport | None
    max_fan_out: FanExtremeReport | None


# --- metrics computation -----------------------------------------------------


def compute_import_graph_metrics(app_root: Path) -> ImportGraphMetricsReport:
    """Every report metric for the `app` package rooted at `app_root`."""
    _make_tests_dir_importable()
    _make_root_importable(app_root)

    import grimp
    from arch.test_import_graph import (
        compute_fan_in,
        compute_fan_out,
        find_app_internal_edges,
        find_import_cycles,
        is_core_module,
    )

    try:
        graph = grimp.build_graph(_APP_PACKAGE, exclude_type_checking_imports=True)
    except ValueError as error:
        # Empirically, grimp 3.15 raises ValueError("Could not find package
        # 'app' in your Python path.") rather than returning an empty graph
        # when `app` isn't importable under `app_root` — confirmed by
        # pointing --root at a directory with no `app` package at all.
        raise ImportGraphMetricError(
            f"grimp could not build the import graph for `app` under {app_root}: {error}"
        ) from error
    edges = find_app_internal_edges(graph, _APP_PACKAGE)
    modules = frozenset(graph.modules)

    return ImportGraphMetricsReport(
        modules=len(modules),
        internal_edges=len(edges),
        edge_density=len(edges) / len(modules),
        propagation_cost_percent=compute_propagation_cost_percent(graph, modules),
        cycles=len(find_import_cycles(edges)),
        max_fan_in=find_fan_extreme(compute_fan_in(edges), exclude=is_core_module),
        max_fan_out=find_fan_extreme(compute_fan_out(edges), exclude=lambda _module: False),
    )


def compute_propagation_cost_percent(graph: ReachabilityGraph, modules: frozenset[str]) -> float:
    """Density of the transitive closure of the app-internal import graph, as
    a percentage: among all ordered pairs (a, b) with a != b, the fraction
    where b is reachable from a by following import edges (a imports b,
    directly or transitively) — the standard "propagation cost" coupling
    metric.

    Computed via grimp's own traversal two ways — forward
    (`find_upstream_modules(a)`, the modules `a` reaches) summed over every
    `a`, and backward (`find_downstream_modules(b)`, the modules that reach
    `b`) summed over every `b` — and cross-checked against each other before
    either total is trusted. Both sums count the same set of reachable
    ordered pairs, so they must agree; a mismatch fails loudly instead of
    reporting a number that can't be trusted. This is a same-library
    consistency check (both directions are grimp's own graph traversal, so a
    bug shared by both would pass it); the genuinely independent
    verification — grimp's reachable-set traversal cross-checked against a
    from-scratch manual BFS reimplementation on a synthetic graph — lives in
    `tests/test_import_graph_report.py`
    (`test_grimp_find_upstream_modules_matches_a_manual_bfs_on_a_diamond`),
    not here.
    """
    forward_total = sum(len(graph.find_upstream_modules(module)) for module in modules)
    backward_total = sum(len(graph.find_downstream_modules(module)) for module in modules)
    if forward_total != backward_total:
        raise ImportGraphMetricError(
            "propagation cost cross-check failed: forward traversal "
            f"(find_upstream_modules) counted {forward_total} reachable ordered pairs "
            f"but backward traversal (find_downstream_modules) counted {backward_total} "
            "— the two directions disagree, so the metric cannot be trusted"
        )
    module_count = len(modules)
    if module_count < 2:
        raise ImportGraphMetricError(
            f"propagation cost is undefined for {module_count} module(s) — "
            "need at least 2 modules to form an ordered pair"
        )
    total_ordered_pairs = module_count * (module_count - 1)
    return 100 * forward_total / total_ordered_pairs


def find_fan_extreme(
    degrees: dict[str, "ModuleDegree"], exclude: Callable[[str], bool]
) -> FanExtremeReport | None:
    """The maximum-degree module(s) among `degrees`, skipping any module
    `exclude` rejects (max fan-in skips the core packages, whose popularity
    is their job; fan-out skips nothing, so its caller passes a predicate
    that rejects nothing). Returns every module tied for the maximum, or None
    if nothing qualifies (e.g. an empty graph)."""
    candidates = [degree for degree in degrees.values() if not exclude(degree.module)]
    if not candidates:
        return None
    max_degree = max(candidate.degree for candidate in candidates)
    winners = tuple(sorted(candidate.module for candidate in candidates if candidate.degree == max_degree))
    return FanExtremeReport(modules=winners, degree=max_degree)


# --- sys.path plumbing --------------------------------------------------------


def _make_tests_dir_importable() -> None:
    """Add tests/ to sys.path the same way pytest.ini's `pythonpath = . tests`
    does for pytest, so this script can import the `arch` toolkit
    (tests/arch/) exactly like the test suite does — the same edge/degree/
    cycle logic the arch gate itself runs, never a re-typed copy."""
    tests_dir = str(_REPO_ROOT / "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)


def _make_root_importable(root: Path) -> None:
    """Put `root` first on sys.path so `grimp.build_graph("app")` scans the
    `app` package under `root` — how the CI job points this one script at
    both the PR head checkout and the base-ref checkout without the base ref
    needing its own copy of it."""
    root_str = str(root.resolve())
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


# --- markdown rendering -------------------------------------------------------


def render_comment_body(head: ImportGraphMetricsReport, base: ImportGraphMetricsReport) -> str:
    """The full PR-comment markdown: HTML marker (so the CI job can find and
    edit this same comment on later pushes), the headline propagation-cost
    score with its definition, the metrics table (base vs head vs delta),
    and a one-line legend."""
    return "\n".join(
        [
            _MARKER,
            "### Import-graph coupling report",
            "",
            _render_headline(head),
            "",
            *_render_table(head, base),
            "",
            _render_legend(),
        ]
    )


def _render_legend() -> str:
    """The table's one-line legend, naming the same core-package prefixes
    `is_core_module` matches — imported from `tests/arch/test_import_graph.py`
    rather than re-typed, so this text can never drift from what the max
    fan-in row actually skips."""
    _make_tests_dir_importable()
    from arch.test_import_graph import describe_core_package_prefixes

    core_prefixes = ", ".join(f"`{prefix}`" for prefix in describe_core_package_prefixes())
    return (
        "_Δ = head vs base. ▲/▼/= = increased/decreased/unchanged. "
        f"pp = percentage points. Core (skipped by max fan-in) = {core_prefixes}._"
    )


def _render_headline(head: ImportGraphMetricsReport) -> str:
    return (
        f"**Propagation cost: {head.propagation_cost_percent:.2f}%** — the fraction of "
        "ordered module pairs (a, b), a ≠ b, where b is transitively reachable from a "
        "along app-internal import edges (density of the transitive closure)."
    )


def _render_table(head: ImportGraphMetricsReport, base: ImportGraphMetricsReport) -> list[str]:
    rows = [
        _metric_row(
            "Propagation cost",
            base.propagation_cost_percent,
            head.propagation_cost_percent,
            decimals=2,
            unit="%",
            delta_unit="pp",
        ),
        _metric_row("Modules", base.modules, head.modules, decimals=0, unit="", delta_unit=""),
        _metric_row(
            "Internal edges", base.internal_edges, head.internal_edges, decimals=0, unit="", delta_unit=""
        ),
        _metric_row(
            "Edge density (edges/module)",
            base.edge_density,
            head.edge_density,
            decimals=2,
            unit="",
            delta_unit="",
        ),
        _fan_extreme_row("Max fan-in (non-core)", base.max_fan_in, head.max_fan_in),
        _fan_extreme_row("Max fan-out", base.max_fan_out, head.max_fan_out),
        _metric_row("Import cycles", base.cycles, head.cycles, decimals=0, unit="", delta_unit=""),
    ]
    return ["| Metric | Base | Head | Δ |", "|---|---|---|---|", *rows]


def _metric_row(
    name: str, base_value: float, head_value: float, *, decimals: int, unit: str, delta_unit: str
) -> str:
    base_cell = f"{base_value:.{decimals}f}{unit}"
    head_cell = f"{head_value:.{decimals}f}{unit}"
    delta_cell = _format_delta(base_value, head_value, decimals=decimals, unit=delta_unit)
    return f"| {name} | {base_cell} | {head_cell} | {delta_cell} |"


def _fan_extreme_row(name: str, base: FanExtremeReport | None, head: FanExtremeReport | None) -> str:
    base_cell = _format_fan_extreme(base)
    head_cell = _format_fan_extreme(head)
    base_degree = base.degree if base is not None else 0
    head_degree = head.degree if head is not None else 0
    delta_cell = _format_delta(base_degree, head_degree, decimals=0, unit="")
    return f"| {name} | {base_cell} | {head_cell} | {delta_cell} |"


def _format_fan_extreme(extreme: FanExtremeReport | None) -> str:
    if extreme is None:
        return "—"
    return f"{', '.join(extreme.modules)} ({extreme.degree})"


def _format_delta(base_value: float, head_value: float, *, decimals: int, unit: str) -> str:
    diff = head_value - base_value
    if round(diff, decimals) == 0:
        return "="
    arrow = "▲" if diff > 0 else "▼"
    sign = "+" if diff > 0 else "-"
    return f"{arrow} {sign}{abs(diff):.{decimals}f}{unit}"


# --- CLI ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    # The rendered comment body uses non-ASCII characters (≠, ▲, ▼, —); stdout's
    # default encoding is locale-dependent (e.g. cp1252 on a default Windows
    # console) and can't represent them, so force UTF-8 regardless of platform
    # rather than letting the failure mode depend on where this happens to run.
    # (stdout is a plain TextIOWrapper except when something has replaced it,
    # e.g. under a test runner's capture — leave those alone.)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parse_args(argv)
    if args.markdown is not None:
        head_path, base_path = args.markdown
        head = ImportGraphMetricsReport.model_validate_json(Path(head_path).read_text(encoding="utf-8"))
        base = ImportGraphMetricsReport.model_validate_json(Path(base_path).read_text(encoding="utf-8"))
        print(render_comment_body(head, base))
        return 0
    metrics = compute_import_graph_metrics(args.root)
    print(metrics.model_dump_json(indent=2))
    return 0


@dataclass(frozen=True)
class _Args:
    root: Path
    markdown: tuple[str, str] | None


def _parse_args(argv: list[str] | None) -> _Args:
    parser = argparse.ArgumentParser(description=__doc__)
    # A mutually exclusive group: --root selects which checkout to compute
    # metrics for, --markdown instead renders from two already-computed JSON
    # snapshots and never touches a checkout, so combining them is a
    # contradictory invocation, not a silently-ignored one — argparse itself
    # rejects it (exit 2) rather than this script picking one and dropping
    # the other.
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root containing the `app` package to analyze (default: this script's own repo root).",
    )
    mode.add_argument(
        "--markdown",
        nargs=2,
        metavar=("HEAD_JSON", "BASE_JSON"),
        default=None,
        help="Render the PR-comment markdown body from two saved metrics JSON files instead of computing metrics.",
    )
    namespace = parser.parse_args(argv)
    raw_markdown: list[str] | None = namespace.markdown
    markdown: tuple[str, str] | None = None
    if raw_markdown is not None:
        head_json, base_json = raw_markdown
        markdown = (head_json, base_json)
    root: Path = namespace.root if namespace.root is not None else _REPO_ROOT
    return _Args(root=root, markdown=markdown)


if __name__ == "__main__":
    raise SystemExit(main())
