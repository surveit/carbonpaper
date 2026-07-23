"""Architecture: import-graph structure checks that declared contracts can't express.

`pyproject.toml`'s `[tool.importlinter.contracts]` declare layering — which named
layer may import which other named layer — and are enforced by `lint-imports`.
That catches a *forbidden edge between two named layers*, but says nothing
about two structural properties of the graph as a whole, checked here with
`grimp` (already a dependency, as import-linter's own engine):

1. **Cycles anywhere under `app/`** — not just between the layers a contract
   names. A cycle can form entirely within one layer (e.g. two sibling
   modules under `app.models`), which no layering contract sees.
2. **Fan-in / fan-out degree drift** — a module quietly becoming a god-hub
   (many other modules depend on it, or it depends on many others) is not an
   illegal edge; it is a *count* that only grows one import at a time, so no
   single PR's contract violation would ever catch it.

All three checks share one `grimp.build_graph("app")` — building the graph is
the expensive step, so it happens once per test module (a module-scoped
fixture) rather than once per test.

`app.models` and `app.core` are the repo's two acknowledged, fan-in-unbounded
import targets — the placement rule for genuinely foundational code (see
`docs/architecture.md`) — so they are exempt from the fan-in ceiling; nothing
is exempt from fan-out (a module importing everything is doing too many jobs
regardless of where it lives) or from the no-cycles rule.

The shared graph is built with `exclude_type_checking_imports=True`: a
reference only used inside an `if TYPE_CHECKING:` guard (a standard forward-
reference pattern for typing a parameter/return without a load-time import)
never executes at runtime, so counting it as a real edge would flag
`app.models.stage` <-> `app.models.stages` as a cycle when it is not one —
`app.models.stages/__init__.py` only reaches back to `app.models.stage` for a
type annotation, guarded by `TYPE_CHECKING`. Confirmed at HEAD by rebuilding
the graph both ways and diffing the cycle set.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import grimp
import pytest

_APP_PACKAGE = "app"

# Modules under these prefixes are the intended, fan-in-unbounded landing zone
# for shared contracts/infrastructure (the repo's placement rule for
# foundational code) — exempt from the fan-in ceiling below.
_CORE_PACKAGE_PREFIXES: tuple[str, ...] = ("app.models", "app.core")

# Measured non-core fan-in maximum at HEAD (post row_model/schema fold) was 11
# (app.services.versioning, app.services.loader, tied). Ceiling set with ~45%
# headroom: a module quietly climbing from 11 toward 16 direct dependents is
# becoming load-bearing infrastructure worth a deliberate look — if it is
# genuinely foundational, the remedy is moving it under app.models/app.core
# (both exempt), not raising this ceiling.
_FAN_IN_CEILING = 16

# Measured fan-out maximum at HEAD (post row_model/schema fold) was 15
# (app.web.routers.runs). Ceiling set with ~40% headroom: a module climbing
# from 15 toward 21 direct imports is doing too many jobs — the remedy is
# splitting it, not raising this ceiling.
_FAN_OUT_CEILING = 21


@dataclass(frozen=True)
class ImportEdge:
    """One direct, app-internal import: `importer` imports `imported`."""

    importer: str
    imported: str


@dataclass(frozen=True)
class ModuleDegree:
    """One module's measured degree (fan-in or fan-out): how many edges touch
    it, and which modules are on the other end of each — so a violation
    message can show the actual coupling driving the count, not just a
    number."""

    module: str
    degree: int
    neighbors: tuple[str, ...]


# --- shared graph fixture ---------------------------------------------------


@pytest.fixture(scope="module")
def app_internal_edges() -> list[ImportEdge]:
    graph = grimp.build_graph(_APP_PACKAGE, exclude_type_checking_imports=True)
    return find_app_internal_edges(graph, _APP_PACKAGE)


def find_app_internal_edges(graph: "grimp.ImportGraph", package: str) -> list[ImportEdge]:
    """Every direct import edge with both ends inside `package` (e.g. "app"),
    as a plain dataclass decoupled from grimp's own dict-shaped `Import` type."""
    matches = graph.find_matching_direct_imports(f"{package}.** -> {package}.**")
    return [ImportEdge(importer=match["importer"], imported=match["imported"]) for match in matches]


# --- live checks over the real graph at HEAD --------------------------------


def test_app_has_no_import_cycles(app_internal_edges: list[ImportEdge]) -> None:
    cycles = find_import_cycles(app_internal_edges)
    assert not cycles, _describe_cycles_failure(cycles)


def test_non_core_modules_stay_under_the_fan_in_ceiling(app_internal_edges: list[ImportEdge]) -> None:
    fan_in = compute_fan_in(app_internal_edges)
    violations = find_fan_in_violations(fan_in, _FAN_IN_CEILING, is_core_module)
    assert not violations, _describe_ceiling_failure("fan-in", violations)


def test_modules_stay_under_the_fan_out_ceiling(app_internal_edges: list[ImportEdge]) -> None:
    fan_out = compute_fan_out(app_internal_edges)
    violations = find_fan_out_violations(fan_out, _FAN_OUT_CEILING)
    assert not violations, _describe_ceiling_failure("fan-out", violations)


# --- checking logic (pure — unit-tested with synthetic data below) ---------


def is_core_module(module: str) -> bool:
    """Whether `module` is `app.models`/`app.core` or something below either —
    the two acknowledged fan-in-unbounded import targets, exempt from the
    fan-in ceiling."""
    return any(
        module == prefix or module.startswith(f"{prefix}.") for prefix in _CORE_PACKAGE_PREFIXES
    )


def describe_core_package_prefixes() -> tuple[str, ...]:
    """The core-package prefixes `is_core_module` exempts from the fan-in
    ceiling, exposed as a public accessor so other consumers (e.g. the
    import-graph CI report) can name them without re-typing the list — the
    module-level constant itself stays private since nothing but
    `is_core_module` needs to iterate it directly."""
    return _CORE_PACKAGE_PREFIXES


def compute_fan_in(edges: list[ImportEdge]) -> dict[str, ModuleDegree]:
    """Fan-in per module: how many distinct app modules import it directly,
    and which ones."""
    return _compute_degree(edges, node_of=lambda edge: edge.imported, neighbor_of=lambda edge: edge.importer)


def compute_fan_out(edges: list[ImportEdge]) -> dict[str, ModuleDegree]:
    """Fan-out per module: how many distinct app modules it imports directly,
    and which ones."""
    return _compute_degree(edges, node_of=lambda edge: edge.importer, neighbor_of=lambda edge: edge.imported)


def _compute_degree(
    edges: list[ImportEdge],
    node_of: Callable[[ImportEdge], str],
    neighbor_of: Callable[[ImportEdge], str],
) -> dict[str, ModuleDegree]:
    neighbors_by_module: dict[str, set[str]] = {}
    for edge in edges:
        neighbors_by_module.setdefault(node_of(edge), set()).add(neighbor_of(edge))
    return {
        module: ModuleDegree(module, len(neighbors), tuple(sorted(neighbors)))
        for module, neighbors in neighbors_by_module.items()
    }


def find_fan_in_violations(
    fan_in: dict[str, ModuleDegree], ceiling: int, is_exempt: Callable[[str], bool]
) -> list[str]:
    """Non-exempt modules whose fan-in exceeds `ceiling`, as offender lines
    naming the module, its measured/ceiling degree, and its importers."""
    return [
        _describe_fan_in_violation(degree, ceiling)
        for degree in fan_in.values()
        if degree.degree > ceiling and not is_exempt(degree.module)
    ]


def find_fan_out_violations(fan_out: dict[str, ModuleDegree], ceiling: int) -> list[str]:
    """Modules whose fan-out exceeds `ceiling`, as offender lines naming the
    module, its measured/ceiling degree, and what it imports."""
    return [
        _describe_fan_out_violation(degree, ceiling)
        for degree in fan_out.values()
        if degree.degree > ceiling
    ]


def find_import_cycles(edges: list[ImportEdge]) -> list[tuple[str, ...]]:
    """One concrete cycle path per strongly connected component of size > 1
    among `edges` (plus any direct self-loop) — every import cycle anywhere
    under `app/`, not just between the layers a contract names. Each returned
    tuple is module names in import order, with the starting module repeated
    at the end to show the loop closes."""
    adjacency = _build_adjacency(edges)
    components = _find_strongly_connected_components(adjacency)
    return [
        _trace_cycle_within(component, adjacency)
        for component in components
        if len(component) > 1 or _has_self_loop(component, adjacency)
    ]


# --- degree violation messages ----------------------------------------------


def _describe_fan_in_violation(degree: ModuleDegree, ceiling: int) -> str:
    return (
        f"{degree.module}: fan-in {degree.degree} exceeds the ceiling of {ceiling} "
        f"(imported by {', '.join(degree.neighbors)}) — this module is becoming load-bearing "
        "infrastructure; if it's genuinely foundational, move it under app.models or app.core "
        "(both exempt), otherwise reduce how many modules depend on it directly"
    )


def _describe_fan_out_violation(degree: ModuleDegree, ceiling: int) -> str:
    return (
        f"{degree.module}: fan-out {degree.degree} exceeds the ceiling of {ceiling} "
        f"(imports {', '.join(degree.neighbors)}) — this module is doing too many jobs; "
        "split it so each piece imports only what it needs"
    )


def _describe_ceiling_failure(kind: str, violations: list[str]) -> str:
    return f"{kind} ceiling exceeded:\n  " + "\n  ".join(violations)


# --- cycle detection (Tarjan SCC + a concrete path within each component) --


def _build_adjacency(edges: list[ImportEdge]) -> dict[str, frozenset[str]]:
    """Directed adjacency (importer -> the modules it imports), deduplicated —
    grimp reports one edge per import statement, but two statements between
    the same pair of modules are one graph edge for cycle purposes."""
    neighbors_by_module: dict[str, set[str]] = {}
    for edge in edges:
        neighbors_by_module.setdefault(edge.importer, set()).add(edge.imported)
    return {module: frozenset(neighbors) for module, neighbors in neighbors_by_module.items()}


def _has_self_loop(component: frozenset[str], adjacency: dict[str, frozenset[str]]) -> bool:
    node = next(iter(component))
    return len(component) == 1 and node in adjacency.get(node, frozenset())


def _find_strongly_connected_components(adjacency: dict[str, frozenset[str]]) -> list[frozenset[str]]:
    """Every strongly connected component of `adjacency` (Tarjan's algorithm),
    including singletons — the caller filters for size > 1 or a self-loop to
    find genuine cycles."""
    nodes = set(adjacency) | {neighbor for neighbors in adjacency.values() for neighbor in neighbors}
    finder = _TarjanState(adjacency)
    for node in sorted(nodes):
        if node not in finder.index:
            finder.connect(node)
    return finder.components


@dataclass
class _TarjanState:
    """Mutable working state for one run of Tarjan's SCC algorithm — shared
    across the recursive `connect` calls rather than threaded through as
    explicit parameters, since every field mutates on nearly every call."""

    adjacency: dict[str, frozenset[str]]
    index: dict[str, int] = field(default_factory=dict)
    lowlink: dict[str, int] = field(default_factory=dict)
    on_stack: dict[str, bool] = field(default_factory=dict)
    stack: list[str] = field(default_factory=list)
    components: list[frozenset[str]] = field(default_factory=list)
    _next_index: int = 0

    def connect(self, node: str) -> None:
        # Recurses one frame per edge on the current DFS path, so depth is
        # bounded by the longest simple import chain in app/ — nowhere near
        # Python's default recursion limit for a 124-module graph.
        self.index[node] = self._next_index
        self.lowlink[node] = self._next_index
        self._next_index += 1
        self.stack.append(node)
        self.on_stack[node] = True
        for neighbor in self.adjacency.get(node, frozenset()):
            if neighbor not in self.index:
                self.connect(neighbor)
                self.lowlink[node] = min(self.lowlink[node], self.lowlink[neighbor])
            elif self.on_stack.get(neighbor):
                self.lowlink[node] = min(self.lowlink[node], self.index[neighbor])
        if self.lowlink[node] == self.index[node]:
            self._pop_component(node)

    def _pop_component(self, root: str) -> None:
        component: list[str] = []
        while True:
            member = self.stack.pop()
            self.on_stack[member] = False
            component.append(member)
            if member == root:
                break
        self.components.append(frozenset(component))


def _trace_cycle_within(component: frozenset[str], adjacency: dict[str, frozenset[str]]) -> tuple[str, ...]:
    """A concrete cycle reachable inside `component` — already known to be a
    single strongly connected component (or a self-loop), so a depth-first
    walk that stops at the first repeated node always finds one."""
    start = min(component)
    if start in adjacency.get(start, frozenset()):
        return (start, start)
    cycle = _walk_for_cycle(start, component, adjacency, path=[], on_path=set(), visited=set())
    assert cycle is not None  # a non-trivial SCC always contains a cycle reachable from any of its nodes
    return tuple(cycle)


def _walk_for_cycle(
    node: str,
    component: frozenset[str],
    adjacency: dict[str, frozenset[str]],
    path: list[str],
    on_path: set[str],
    visited: set[str],
) -> list[str] | None:
    """Depth-first search for a cycle reachable from `node`, restricted to
    `component`. Returns the closed cycle path (the first repeated module
    last) the moment a back-edge to an ancestor still on the current path is
    found, or None if this subtree holds none."""
    path.append(node)
    on_path.add(node)
    visited.add(node)
    for neighbor in sorted(adjacency.get(node, frozenset())):
        if neighbor not in component:
            continue
        if neighbor in on_path:
            return path[path.index(neighbor) :] + [neighbor]
        if neighbor not in visited:
            found = _walk_for_cycle(neighbor, component, adjacency, path, on_path, visited)
            if found is not None:
                return found
    path.pop()
    on_path.discard(node)
    return None


def _describe_cycles_failure(cycles: list[tuple[str, ...]]) -> str:
    paths = "\n  ".join(" -> ".join(cycle) for cycle in cycles)
    return (
        "import cycle(s) found under app/ — every import chain here must be acyclic; "
        f"restructure so the dependency only points one way:\n  {paths}"
    )


# --- unit tests for the checking logic, on synthetic data (red + green) ----


def test_find_import_cycles_flags_a_direct_two_module_cycle() -> None:
    edges = [ImportEdge("app.a", "app.b"), ImportEdge("app.b", "app.a")]
    cycles = find_import_cycles(edges)
    assert cycles == [("app.a", "app.b", "app.a")]


def test_find_import_cycles_flags_a_self_loop() -> None:
    edges = [ImportEdge("app.a", "app.a")]
    assert find_import_cycles(edges) == [("app.a", "app.a")]


def test_find_import_cycles_flags_a_longer_cycle() -> None:
    edges = [ImportEdge("app.a", "app.b"), ImportEdge("app.b", "app.c"), ImportEdge("app.c", "app.a")]
    cycles = find_import_cycles(edges)
    assert cycles == [("app.a", "app.b", "app.c", "app.a")]


def test_find_import_cycles_passes_a_dag() -> None:
    edges = [ImportEdge("app.a", "app.b"), ImportEdge("app.b", "app.c"), ImportEdge("app.a", "app.c")]
    assert find_import_cycles(edges) == []


def test_find_import_cycles_reports_one_path_per_disjoint_cycle() -> None:
    # Two unrelated cycles, each its own strongly connected component — both
    # must be reported, one path each, neither swallowing the other.
    edges = [
        ImportEdge("app.a", "app.b"),
        ImportEdge("app.b", "app.a"),
        ImportEdge("app.x", "app.y"),
        ImportEdge("app.y", "app.x"),
    ]
    cycles = find_import_cycles(edges)
    assert len(cycles) == 2
    assert ("app.a", "app.b", "app.a") in cycles
    assert ("app.x", "app.y", "app.x") in cycles


def test_find_import_cycles_reports_a_self_loop_within_a_larger_scc() -> None:
    # app.a self-imports AND forms a 2-cycle with app.b, so both belong to
    # one strongly connected component. _trace_cycle_within's self-loop check
    # on the component's starting node (app.a, alphabetically first) fires
    # before the DFS walk runs — this exercises that early-return branch
    # rather than the general walk exercised by the tests above.
    edges = [
        ImportEdge("app.a", "app.a"),
        ImportEdge("app.a", "app.b"),
        ImportEdge("app.b", "app.a"),
    ]
    assert find_import_cycles(edges) == [("app.a", "app.a")]


def test_find_fan_in_violations_flags_a_non_core_module_over_the_ceiling() -> None:
    edges = [ImportEdge(f"app.importer{i}", "app.services.hub") for i in range(5)]
    violations = find_fan_in_violations(compute_fan_in(edges), ceiling=4, is_exempt=is_core_module)
    assert len(violations) == 1
    assert "app.services.hub" in violations[0] and "fan-in 5" in violations[0]


def test_find_fan_in_violations_exempts_core_modules() -> None:
    edges = [ImportEdge(f"app.importer{i}", "app.models.schema") for i in range(50)]
    violations = find_fan_in_violations(compute_fan_in(edges), ceiling=4, is_exempt=is_core_module)
    assert violations == []


def test_find_fan_in_violations_passes_a_module_under_the_ceiling() -> None:
    edges = [ImportEdge(f"app.importer{i}", "app.services.hub") for i in range(3)]
    violations = find_fan_in_violations(compute_fan_in(edges), ceiling=4, is_exempt=is_core_module)
    assert violations == []


def test_find_fan_out_violations_flags_a_module_over_the_ceiling() -> None:
    edges = [ImportEdge("app.services.god", f"app.target{i}") for i in range(5)]
    violations = find_fan_out_violations(compute_fan_out(edges), ceiling=4)
    assert len(violations) == 1
    assert "app.services.god" in violations[0] and "fan-out 5" in violations[0]


def test_find_fan_out_violations_passes_a_module_under_the_ceiling() -> None:
    edges = [ImportEdge("app.services.modest", f"app.target{i}") for i in range(3)]
    assert find_fan_out_violations(compute_fan_out(edges), ceiling=4) == []


def test_is_core_module_matches_app_models_and_app_core_and_their_descendants() -> None:
    assert is_core_module("app.models")
    assert is_core_module("app.models.schema")
    assert is_core_module("app.core")
    assert is_core_module("app.core.errors")


def test_is_core_module_rejects_everything_else() -> None:
    assert not is_core_module("app.services.versioning")
    assert not is_core_module("app.web.routers.runs")
    # not a prefix match past the dot boundary
    assert not is_core_module("app.modelsomething")


def test_describe_core_package_prefixes_matches_what_is_core_module_checks() -> None:
    prefixes = describe_core_package_prefixes()
    assert prefixes == ("app.models", "app.core")
    assert all(is_core_module(prefix) for prefix in prefixes)
