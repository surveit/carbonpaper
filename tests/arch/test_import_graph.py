"""Architecture: import cycles anywhere under `app/` — between modules, and between sibling
packages at every depth — plus a fan-out ceiling every module is held to: whole-graph properties
`pyproject.toml`'s layering contracts can't express. `if TYPE_CHECKING:` imports are excluded —
they never execute at runtime and would otherwise read as a cycle: `app.models.stage`/`.stages`.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import grimp
import pytest

_APP_PACKAGE = "app"

# Modules under these prefixes are the intended landing zone for shared
# contracts/infrastructure (the repo's placement rule for foundational code).
# The import-graph report excludes them when naming the highest-fan-in module,
# since a foundational module's popularity says nothing about the rest.
_CORE_PACKAGE_PREFIXES: tuple[str, ...] = ("app.models", "app.core")

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


@dataclass(frozen=True)
class PackageCycle:
    """One cyclic package group at one rollup depth, plus a cycle path through it."""

    depth: int
    members: tuple[str, ...]
    path: tuple[str, ...]


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


def test_sibling_packages_are_acyclic_at_every_depth(app_internal_edges: list[ImportEdge]) -> None:
    cycles = find_package_cycles_at_every_depth(app_internal_edges)
    assert not cycles, _describe_package_cycles_failure(cycles)


def test_modules_stay_under_the_fan_out_ceiling(app_internal_edges: list[ImportEdge]) -> None:
    fan_out = compute_fan_out(app_internal_edges)
    violations = find_fan_out_violations(fan_out, _FAN_OUT_CEILING)
    assert not violations, _describe_ceiling_failure("fan-out", violations)


# --- checking logic (pure — unit-tested with synthetic data below) ---------


def is_core_module(module: str) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.") for prefix in _CORE_PACKAGE_PREFIXES
    )


def describe_core_package_prefixes() -> tuple[str, ...]:
    """The prefixes `is_core_module` matches, so the CI report can name them without re-typing."""
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
    return [
        _trace_cycle_within(component, adjacency) for component in find_cyclic_components(edges)
    ]


def find_cyclic_components(edges: list[ImportEdge]) -> list[frozenset[str]]:
    """Full membership of every cyclic strongly connected component (size > 1, or a self-loop)."""
    adjacency = _build_adjacency(edges)
    return [
        component
        for component in _find_strongly_connected_components(adjacency)
        if len(component) > 1 or _has_self_loop(component, adjacency)
    ]


def find_package_cycles_at_every_depth(edges: list[ImportEdge]) -> list[PackageCycle]:
    return [
        cycle
        for depth in range(1, find_deepest_package_depth(edges) + 1)
        for cycle in find_package_cycles_at_depth(edges, depth)
    ]


def find_package_cycles_at_depth(edges: list[ImportEdge], depth: int) -> list[PackageCycle]:
    rolled = roll_up_edges_to_depth(edges, depth)
    paths = find_import_cycles(rolled)
    return [
        PackageCycle(depth, tuple(sorted(component)), _find_cycle_path_within(component, paths))
        for component in find_cyclic_components(rolled)
    ]


def roll_up_edges_to_depth(edges: list[ImportEdge], depth: int) -> list[ImportEdge]:
    """Edges between packages at `depth`, deduplicated; an edge inside one package collapses away."""
    rolled: set[ImportEdge] = set()
    for edge in edges:
        importer = roll_up_module_to_depth(edge.importer, depth)
        imported = roll_up_module_to_depth(edge.imported, depth)
        if importer != imported:
            rolled.add(ImportEdge(importer=importer, imported=imported))
    return sorted(rolled, key=lambda edge: (edge.importer, edge.imported))


def roll_up_module_to_depth(module: str, depth: int) -> str:
    """`app.a.b.c` at depth 2 is `app.a.b`; a module shallower than `depth` is unchanged."""
    return ".".join(module.split(".")[: depth + 1])


def find_deepest_package_depth(edges: list[ImportEdge]) -> int:
    """Depth of the deepest module, counting `app` as 0 and `app.services.loader` as 2."""
    modules = {edge.importer for edge in edges} | {edge.imported for edge in edges}
    return max((module.count(".") for module in modules), default=0)


def _find_cycle_path_within(component: frozenset[str], paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    for path in paths:
        if set(path) <= component:
            return path
    raise ValueError(f"no traced cycle path lies within the component {sorted(component)}")


# --- degree violation messages ----------------------------------------------


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


def _describe_package_cycles_failure(cycles: list[PackageCycle]) -> str:
    groups = "\n  ".join(_describe_package_cycle(cycle) for cycle in cycles)
    return (
        "package-level import cycle(s) found under app/ — at every level of the tree, sibling "
        "packages must form a DAG, so that each package can be read, tested and moved without "
        f"dragging its siblings along:\n  {groups}"
    )


def _describe_package_cycle(cycle: PackageCycle) -> str:
    # The path closes one loop, so it understates a tangle of three or more.
    return (
        f"depth {cycle.depth}: {len(cycle.members)} packages tangled "
        f"{{{', '.join(cycle.members)}}} — e.g. {' -> '.join(cycle.path)}"
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


def test_find_cyclic_components_names_every_member_not_just_a_path_through_them() -> None:
    edges = [
        ImportEdge("app.a", "app.b"),
        ImportEdge("app.b", "app.c"),
        ImportEdge("app.c", "app.a"),
    ]
    assert find_cyclic_components(edges) == [frozenset({"app.a", "app.b", "app.c"})]


def test_roll_up_module_to_depth_truncates_deeper_modules_and_leaves_shallower_ones() -> None:
    assert roll_up_module_to_depth("app.a.b.c", 2) == "app.a.b"
    assert roll_up_module_to_depth("app.a", 2) == "app.a"


def test_roll_up_edges_to_depth_drops_within_package_edges_and_deduplicates() -> None:
    edges = [
        ImportEdge("app.x.one", "app.x.two"),  # both ends collapse to app.x
        ImportEdge("app.x.one", "app.y.one"),
        ImportEdge("app.x.two", "app.y.two"),  # rolls up to the same edge as the line above
    ]
    assert roll_up_edges_to_depth(edges, 1) == [ImportEdge("app.x", "app.y")]


def test_find_deepest_package_depth_counts_the_longest_dotted_module() -> None:
    assert find_deepest_package_depth([ImportEdge("app.a", "app.b.c.d")]) == 3


def test_find_package_cycles_flags_a_graph_that_is_module_acyclic_but_package_cyclic() -> None:
    edges = [ImportEdge("app.x.one", "app.y.one"), ImportEdge("app.y.two", "app.x.two")]
    assert find_import_cycles(edges) == []
    assert find_package_cycles_at_every_depth(edges) == [
        PackageCycle(depth=1, members=("app.x", "app.y"), path=("app.x", "app.y", "app.x"))
    ]


def test_find_package_cycles_passes_a_graph_acyclic_at_every_depth() -> None:
    edges = [ImportEdge("app.x.one", "app.y.one"), ImportEdge("app.x.two", "app.y.two")]
    assert find_package_cycles_at_every_depth(edges) == []


def test_find_package_cycles_flags_a_tangle_that_only_appears_below_the_first_level() -> None:
    # At depth 1 both ends roll up to app.m, so the edges vanish as self-edges.
    edges = [ImportEdge("app.m.a.one", "app.m.b.one"), ImportEdge("app.m.b.two", "app.m.a.two")]
    assert find_package_cycles_at_depth(edges, 1) == []
    assert find_package_cycles_at_every_depth(edges) == [
        PackageCycle(depth=2, members=("app.m.a", "app.m.b"), path=("app.m.a", "app.m.b", "app.m.a"))
    ]


def test_find_package_cycles_reports_all_three_members_of_a_three_package_tangle() -> None:
    edges = [
        ImportEdge("app.x.one", "app.y.one"),
        ImportEdge("app.y.one", "app.z.one"),
        ImportEdge("app.z.one", "app.x.two"),
    ]
    cycles = find_package_cycles_at_every_depth(edges)
    assert [cycle.members for cycle in cycles] == [("app.x", "app.y", "app.z")]
    message = _describe_package_cycles_failure(cycles)
    assert "3 packages tangled" in message
    assert all(package in message for package in ("app.x", "app.y", "app.z"))


def test_compute_fan_in_counts_distinct_importers_and_names_them() -> None:
    edges = [ImportEdge(f"app.importer{i}", "app.services.hub") for i in range(3)]
    assert compute_fan_in(edges)["app.services.hub"] == ModuleDegree(
        "app.services.hub", 3, ("app.importer0", "app.importer1", "app.importer2")
    )


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
