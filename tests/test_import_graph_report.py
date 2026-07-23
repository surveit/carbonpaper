"""Unit tests for tools/import_graph_report.py: propagation-cost math on
synthetic graphs with a known-by-hand answer, fan-extreme selection, markdown
rendering, and the JSON round-trip that carries metrics between the two
checkouts the CI job compares.

Propagation cost is cross-checked two independent ways before it ever
reaches these tests (see `compute_propagation_cost_percent`'s own
forward/backward grimp traversal check) — on top of that, this file
additionally cross-checks grimp's `find_upstream_modules` against a manual
BFS reimplementation, so the metric never rests on trusting grimp alone
either.

Tests that point `compute_import_graph_metrics` at a *different* `app`
package than the real repo's run the script as a subprocess
(`_run_script`), not as an in-process call. That's not a style choice: once
`conftest.py`'s fixtures import the real `app` package elsewhere in the same
pytest session, `sys.modules["app"]` is cached, and Python's import system
resolves `find_spec("app")` from that cached entry regardless of later
`sys.path` changes — so `_make_root_importable` cannot redirect an
already-imported `app` within one process. This is also exactly how the CI
job uses `--root`: two separate `python tools/import_graph_report.py`
process invocations (one per checkout), never two calls in one process — so
the subprocess tests exercise the script the way it is actually invoked,
not a workaround for the caching gap.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import grimp
import pytest

from tools.import_graph_report import (
    FanExtremeReport,
    ImportGraphMetricError,
    ImportGraphMetricsReport,
    compute_import_graph_metrics,
    compute_propagation_cost_percent,
    find_fan_extreme,
    render_comment_body,
)

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "tools" / "import_graph_report.py"


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the report script as a fresh subprocess — the same way the CI
    job runs it (see the module docstring for why an in-process call can't
    substitute for this when the target root differs from the real repo)."""
    return subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
    )


# --- test helpers ------------------------------------------------------------


def _build_synthetic_graph(modules: list[str], edges: list[tuple[str, str]]) -> "grimp.ImportGraph":
    """A grimp `ImportGraph` built entirely in memory (`add_module`/
    `add_import`, no files on disk) — verified empirically against grimp
    3.15 to support exactly this construction."""
    graph = grimp.ImportGraph()
    for module in modules:
        graph.add_module(module)
    for importer, imported in edges:
        graph.add_import(importer=importer, imported=imported)
    return graph


def _reachable_via_manual_bfs(adjacency: dict[str, set[str]], start: str) -> set[str]:
    """Independent, from-scratch reachable-set computation (breadth-first
    over a plain adjacency dict) — deliberately not sharing any code with
    grimp or with `compute_propagation_cost_percent`, so it can catch either
    one being wrong."""
    visited: set[str] = set()
    frontier = list(adjacency.get(start, set()))
    while frontier:
        node = frontier.pop()
        if node in visited:
            continue
        visited.add(node)
        frontier.extend(adjacency.get(node, set()))
    return visited


class _InconsistentReachabilityGraph:
    """A minimal stand-in exposing only the two traversal methods
    `compute_propagation_cost_percent` calls, deliberately returning
    forward/backward reachable sets that disagree — used to verify the
    cross-check inside the function actually fires rather than silently
    reporting whichever total it computed first."""

    def find_upstream_modules(self, module: str) -> set[str]:
        return {"nonempty"}

    def find_downstream_modules(self, module: str) -> set[str]:
        return set()


# --- grimp API verification: cross-check against a manual BFS ---------------


def test_grimp_find_upstream_modules_matches_a_manual_bfs_on_a_diamond() -> None:
    # a -> b, a -> c, b -> d, c -> d: a reaches everything, b and c each
    # reach only d, d reaches nothing.
    adjacency = {
        "pkg.a": {"pkg.b", "pkg.c"},
        "pkg.b": {"pkg.d"},
        "pkg.c": {"pkg.d"},
        "pkg.d": set(),
    }
    edges = [(importer, imported) for importer, targets in adjacency.items() for imported in targets]
    graph = _build_synthetic_graph(modules=list(adjacency), edges=edges)

    for module in adjacency:
        assert graph.find_upstream_modules(module) == _reachable_via_manual_bfs(adjacency, module)


# --- propagation cost: known-by-hand answers ---------------------------------


def test_compute_propagation_cost_percent_on_a_three_chain() -> None:
    # a -> b -> c: reachable ordered pairs are (a,b), (a,c), (b,c) = 3 of
    # the 6 possible ordered pairs among 3 modules = 50%.
    graph = _build_synthetic_graph(
        modules=["pkg.a", "pkg.b", "pkg.c"],
        edges=[("pkg.a", "pkg.b"), ("pkg.b", "pkg.c")],
    )
    cost = compute_propagation_cost_percent(graph, frozenset({"pkg.a", "pkg.b", "pkg.c"}))
    assert cost == pytest.approx(50.0)


def test_compute_propagation_cost_percent_on_fully_disconnected_modules() -> None:
    graph = _build_synthetic_graph(modules=["pkg.a", "pkg.b", "pkg.c"], edges=[])
    cost = compute_propagation_cost_percent(graph, frozenset({"pkg.a", "pkg.b", "pkg.c"}))
    assert cost == pytest.approx(0.0)


def test_compute_propagation_cost_percent_on_a_complete_graph() -> None:
    # Every module imports every other directly: all 6 ordered pairs among
    # 3 modules are reachable = 100%.
    modules = ["pkg.a", "pkg.b", "pkg.c"]
    edges = [(a, b) for a in modules for b in modules if a != b]
    graph = _build_synthetic_graph(modules=modules, edges=edges)
    cost = compute_propagation_cost_percent(graph, frozenset(modules))
    assert cost == pytest.approx(100.0)


def test_compute_propagation_cost_percent_fails_loudly_below_two_modules() -> None:
    graph = _build_synthetic_graph(modules=["pkg.a"], edges=[])
    with pytest.raises(ImportGraphMetricError, match="undefined"):
        compute_propagation_cost_percent(graph, frozenset({"pkg.a"}))


def test_compute_propagation_cost_percent_fails_loudly_on_a_cross_check_mismatch() -> None:
    with pytest.raises(ImportGraphMetricError, match="cross-check failed"):
        compute_propagation_cost_percent(_InconsistentReachabilityGraph(), frozenset({"a", "b"}))


# --- fan extreme selection ----------------------------------------------------


def test_find_fan_extreme_picks_the_single_maximum() -> None:
    from arch.test_import_graph import ModuleDegree

    degrees = {
        "app.a": ModuleDegree("app.a", degree=2, neighbors=("x", "y")),
        "app.b": ModuleDegree("app.b", degree=5, neighbors=("x", "y", "z", "w", "v")),
    }
    extreme = find_fan_extreme(degrees, exclude=lambda _module: False)
    assert extreme == FanExtremeReport(modules=("app.b",), degree=5)


def test_find_fan_extreme_returns_every_module_tied_for_the_maximum() -> None:
    from arch.test_import_graph import ModuleDegree

    degrees = {
        "app.a": ModuleDegree("app.a", degree=3, neighbors=("x", "y", "z")),
        "app.b": ModuleDegree("app.b", degree=3, neighbors=("x", "y", "z")),
        "app.c": ModuleDegree("app.c", degree=1, neighbors=("x",)),
    }
    extreme = find_fan_extreme(degrees, exclude=lambda _module: False)
    assert extreme == FanExtremeReport(modules=("app.a", "app.b"), degree=3)


def test_find_fan_extreme_respects_the_exclude_predicate() -> None:
    from arch.test_import_graph import ModuleDegree

    degrees = {
        "app.models.schema": ModuleDegree("app.models.schema", degree=50, neighbors=()),
        "app.services.hub": ModuleDegree("app.services.hub", degree=5, neighbors=()),
    }
    extreme = find_fan_extreme(degrees, exclude=lambda module: module.startswith("app.models"))
    assert extreme == FanExtremeReport(modules=("app.services.hub",), degree=5)


def test_find_fan_extreme_returns_none_when_everything_is_excluded() -> None:
    from arch.test_import_graph import ModuleDegree

    degrees = {"app.models.schema": ModuleDegree("app.models.schema", degree=50, neighbors=())}
    assert find_fan_extreme(degrees, exclude=lambda _module: True) is None


def test_find_fan_extreme_returns_none_for_an_empty_graph() -> None:
    assert find_fan_extreme({}, exclude=lambda _module: False) is None


# --- compute_import_graph_metrics: an on-disk synthetic package -------------


def _write_synthetic_three_chain_package(root: Path) -> None:
    """A tiny real `app` package (a -> b -> c) written to `root`, so
    `compute_import_graph_metrics` can be exercised end to end through
    grimp's real filesystem scanning, not just in-memory graphs."""
    app_dir = root / "app"
    app_dir.mkdir()
    (app_dir / "__init__.py").write_text("", encoding="utf-8")
    (app_dir / "a.py").write_text("from app import b\n", encoding="utf-8")
    (app_dir / "b.py").write_text("from app import c\n", encoding="utf-8")
    (app_dir / "c.py").write_text("", encoding="utf-8")


def test_compute_import_graph_metrics_on_a_synthetic_three_chain_package(tmp_path: Path) -> None:
    _write_synthetic_three_chain_package(tmp_path)

    result = _run_script("--root", str(tmp_path))

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    # Modules: app, app.a, app.b, app.c. Edges: a->b, b->c.
    assert payload["modules"] == 4
    assert payload["internal_edges"] == 2
    assert payload["edge_density"] == pytest.approx(0.5)
    assert payload["cycles"] == 0
    # Reachable pairs: a->{b,c} (2), b->{c} (1), c->{} (0), app->{} (0) = 3
    # of 4*3 = 12 possible ordered pairs.
    assert payload["propagation_cost_percent"] == pytest.approx(25.0)
    assert payload["max_fan_in"] == {"modules": ["app.b", "app.c"], "degree": 1}
    assert payload["max_fan_out"] == {"modules": ["app.a", "app.b"], "degree": 1}


def test_compute_import_graph_metrics_fails_loudly_when_app_is_not_importable(tmp_path: Path) -> None:
    empty_root = tmp_path / "nothing_here"
    empty_root.mkdir()

    result = _run_script("--root", str(empty_root))

    assert result.returncode != 0
    assert "grimp could not build the import graph" in result.stderr
    assert "Could not find package 'app'" in result.stderr


def test_compute_import_graph_metrics_on_the_real_repo_matches_the_arch_gate() -> None:
    # The arch gate (tests/arch/test_import_graph.py) pins the real repo's
    # import graph at zero cycles; this integration test checks the report
    # script computes the same thing over the same real `app` package,
    # plus sanity bounds on the new propagation-cost metric.
    repo_root = Path(__file__).resolve().parent.parent
    metrics = compute_import_graph_metrics(repo_root)
    assert metrics.cycles == 0
    assert metrics.modules > 0
    assert metrics.internal_edges > 0
    assert 0.0 <= metrics.propagation_cost_percent <= 100.0
    assert metrics.max_fan_in is not None
    assert metrics.max_fan_out is not None


# --- markdown rendering -------------------------------------------------------


def _sample_metrics(
    *,
    propagation_cost_percent: float = 14.08,
    modules: int = 124,
    internal_edges: int = 230,
    cycles: int = 0,
    max_fan_in_degree: int = 11,
    max_fan_out_degree: int = 15,
) -> ImportGraphMetricsReport:
    return ImportGraphMetricsReport(
        modules=modules,
        internal_edges=internal_edges,
        edge_density=internal_edges / modules,
        propagation_cost_percent=propagation_cost_percent,
        cycles=cycles,
        max_fan_in=FanExtremeReport(modules=("app.services.hub",), degree=max_fan_in_degree),
        max_fan_out=FanExtremeReport(modules=("app.web.routers.runs",), degree=max_fan_out_degree),
    )


def test_render_comment_body_starts_with_the_marker() -> None:
    body = render_comment_body(head=_sample_metrics(), base=_sample_metrics())
    assert body.startswith("<!-- import-graph-report -->")


def test_render_comment_body_shows_the_headline_score_and_definition() -> None:
    body = render_comment_body(head=_sample_metrics(propagation_cost_percent=14.08), base=_sample_metrics())
    assert "Propagation cost: 14.08%" in body
    assert "transitively reachable" in body


def test_render_comment_body_shows_computed_deltas_not_bare_arrows() -> None:
    head = _sample_metrics(propagation_cost_percent=14.08, modules=124, cycles=0)
    base = _sample_metrics(propagation_cost_percent=16.24, modules=126, cycles=1)
    body = render_comment_body(head=head, base=base)

    assert "▼ -2.16pp" in body  # propagation cost decreased
    assert "▼ -2" in body  # module count decreased by 2
    assert "▼ -1" in body  # cycle count decreased by 1


def test_render_comment_body_marks_an_unchanged_metric_with_equals() -> None:
    metrics = _sample_metrics()
    body = render_comment_body(head=metrics, base=metrics)
    # Every data row's delta cell should be exactly "=" when head and base
    # match. The header row is excluded via its literal "Δ" column title.
    data_rows = [line for line in body.splitlines() if line.startswith("| ") and "Δ" not in line]
    delta_cells = [line.split("|")[-2].strip() for line in data_rows]
    assert delta_cells and all(cell == "=" for cell in delta_cells)


def test_render_comment_body_shows_an_increase_with_an_up_arrow() -> None:
    head = _sample_metrics(propagation_cost_percent=20.0)
    base = _sample_metrics(propagation_cost_percent=14.08)
    body = render_comment_body(head=head, base=base)
    assert "▲ +5.92pp" in body


def test_render_comment_body_ends_with_the_legend() -> None:
    body = render_comment_body(head=_sample_metrics(), base=_sample_metrics())
    assert body.rstrip().splitlines()[-1].startswith("_Δ = head vs base")


def test_render_comment_body_shows_fan_extreme_holders() -> None:
    body = render_comment_body(head=_sample_metrics(), base=_sample_metrics())
    assert "app.services.hub (11)" in body
    assert "app.web.routers.runs (15)" in body


# --- JSON round-trip -----------------------------------------------------------


def test_metrics_json_round_trips_through_model_dump_and_validate() -> None:
    original = _sample_metrics()
    restored = ImportGraphMetricsReport.model_validate_json(original.model_dump_json())
    assert restored == original


def test_metrics_json_round_trips_with_no_fan_extreme() -> None:
    original = ImportGraphMetricsReport(
        modules=1,
        internal_edges=0,
        edge_density=0.0,
        propagation_cost_percent=0.0,
        cycles=0,
        max_fan_in=None,
        max_fan_out=None,
    )
    restored = ImportGraphMetricsReport.model_validate_json(original.model_dump_json())
    assert restored == original
