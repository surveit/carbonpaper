"""Which columns the scope map draws, and where every bar and ribbon lands."""

from __future__ import annotations

from app.models.branch_analysis import BranchId, BranchReason, RowOrdinal
from app.models.schema import StageId
from app.web.merge_alias import AliasedMerge
from app.web.scope_payload import DrawnStage, ScopeMap

COLUMN, BAR, HEAD, GAP, CUT_LINE = 300, 11, 52, 16, 11
# Clear of the head: flush against it, the ribbons read as hanging off the text.
BAND_GAP = 14
LABEL_PITCH = 30
SHORTEST_BAND, TALLEST_BAND, BAND_PER_NODE, SWEEPS = 260, 620, 72, 4
SEP = " "


# ─── the rows, grouped so nothing below walks 45,000 of them ─────────────────


class Group:
    """Rows that agree on both their branches and the frames they were rows of."""

    def __init__(self, path: int, came: int) -> None:
        self.path = path
        self.came = came
        self.ordinals: list[RowOrdinal] = []


class Facts:
    """The scope map, read the way the drawing asks about it."""

    def __init__(self, scope: ScopeMap) -> None:
        self.scope = scope
        self.total = len(scope.covers.ordinals) or 1
        self._stage_of = {b: o.stage_id for b, o in scope.branches.items()}
        self._code = {b for b, o in scope.branches.items()
                      if o.reason is BranchReason.code}
        self._came = [set(names) for names in scope.came_through]
        self._at: dict[tuple[int, StageId], tuple[BranchId, ...]] = {}
        self.groups = self._gather_groups()

    def _gather_groups(self) -> list[Group]:
        held: dict[tuple[int, int], Group] = {}
        for position, ordinal in enumerate(self.scope.covers.ordinals):
            key = (self.scope.branch_path_index[position],
                   self._came_index(position))
            held.setdefault(key, Group(*key)).ordinals.append(ordinal)
        return list(held.values())

    def _came_index(self, position: int) -> int:
        index = self.scope.came_through_index
        return index[position] if position < len(index) else -1

    def branches_at(self, group: Group, stage_id: StageId) -> tuple[BranchId, ...]:
        key = (group.path, stage_id)
        if key not in self._at:
            path = self.scope.branch_paths[group.path]
            self._at[key] = tuple(b for b in path
                                  if self._stage_of.get(b) == stage_id)
        return self._at[key]

    def came_through(self, group: Group, stage_id: StageId) -> bool:
        """A lookup table a row never matched held no row of it."""
        if not 0 <= group.came < len(self._came):
            return True
        return stage_id in self._came[group.came]

    def is_code(self, branch_id: BranchId) -> bool:
        return branch_id in self._code


# ─── which columns, and which bars in each ───────────────────────────────────


class Node:
    """A bar while it is being placed: the rows on it, then where it landed."""

    def __init__(self, key: str, branches: tuple[BranchId, ...]) -> None:
        self.key = key
        self.branches = branches
        self.rows = 0
        self.on: list[RowOrdinal] = []
        self.drawn_rows: int | None = None
        self.is_figure = False
        self.alias_of: AliasedMerge | None = None
        self.y = 0.0
        self.height = 0.0
        self.label_y = 0.0


class Column:
    def __init__(self, stage: DrawnStage, nodes: list[Node],
                 gone: list[tuple[BranchId, int]], alias: AliasedMerge | None,
                 expanded: bool) -> None:
        self.stage = stage
        self.nodes = nodes
        self.gone = gone
        self.alias = alias
        self.expanded = expanded
        self.x = 0.0
        self.bottom = 0.0


def choose_columns(facts: Facts, every_stage: bool) -> list[Column]:
    drawn = [_build_column(facts, stage) for stage in facts.scope.stages
             if every_stage or _is_worth_a_column(facts, stage)]
    kept = [column for column in drawn if column.nodes or column.gone]
    return kept if every_stage else _keep_informative(facts, kept)


# Dropping the cited cell's own stage leaves the last frame drawn looking like it.
def _is_worth_a_column(facts: Facts, stage: DrawnStage) -> bool:
    if stage.id == facts.scope.citation.stage_id and not facts.scope.is_a_cut:
        return True
    return bool(facts.scope.aliased_merges.get(stage.id)) or any(
        facts.branches_at(group, stage.id) for group in facts.groups)


def _build_column(facts: Facts, stage: DrawnStage) -> Column:
    nodes = _tally(facts, stage.id)
    alias = facts.scope.aliased_merges.get(stage.id)
    for node in nodes:
        node.alias_of = alias
        if stage.id == facts.scope.citation.stage_id and not facts.scope.is_a_cut:
            node.is_figure, node.drawn_rows = True, 1
    held = {b for node in nodes for b in node.branches}
    gone = sorted(((r.branch, r.taken) for r in facts.scope.reach
                   if facts.scope.branches[r.branch].stage_id == stage.id
                   and r.branch not in held
                   and facts.scope.branches[r.branch].role.value == "removes"),
                  key=lambda pair: -pair[1])
    expanded = (alias is None and stage.id != facts.scope.nearest_merge
                and stage.id in facts.scope.resolved_merges)
    return Column(stage, nodes, gone, alias, expanded)


def _tally(facts: Facts, stage_id: StageId) -> list[Node]:
    seen: dict[str, Node] = {}
    for group in facts.groups:
        if not facts.came_through(group, stage_id):
            continue
        branches = facts.branches_at(group, stage_id)
        key = node_key(stage_id, branches)
        node = seen.setdefault(key, Node(key, branches))
        node.rows += len(group.ordinals)
        node.on.extend(group.ordinals)
    return sorted(seen.values(), key=lambda node: -node.rows)


def node_key(stage_id: StageId, branches: tuple[BranchId, ...]) -> str:
    return stage_id + SEP + ",".join(branches)


# A column adds nothing when every row it separates was already separated.
def _keep_informative(facts: Facts, all_columns: list[Column]) -> list[Column]:
    running = [""] * len(facts.groups)
    arrived = [False] * len(facts.groups)
    kept: list[Column] = []
    for column in all_columns:
        stage_id = column.stage.id
        nxt = [was + "|" + ",".join(facts.branches_at(group, stage_id))
               for was, group in zip(running, facts.groups)]
        brings = any(not seen and facts.came_through(group, stage_id)
                     for seen, group in zip(arrived, facts.groups))
        if not (brings or stage_id == facts.scope.citation.stage_id or column.gone
                or column.alias or column.expanded
                or _is_a_finer_reading(nxt, running, facts)):
            continue
        kept.append(column)
        running = nxt
        arrived = [seen or facts.came_through(group, stage_id)
                   for seen, group in zip(arrived, facts.groups)]
    return kept


# Groups, not rows, so a set of them counts what a set of rows would have.
def _is_a_finer_reading(nxt: list[str], running: list[str], facts: Facts) -> bool:
    return len(set(nxt)) > len(set(running))


# ─── the ribbons, and keeping them apart ─────────────────────────────────────


class Ribbon:
    def __init__(self, ci: int, cj: int, a: Node, b: Node) -> None:
        self.ci, self.cj, self.a, self.b = ci, cj, a, b
        self.rows = 0
        self.y0 = self.y1 = self.h0 = self.h1 = 0.0


# Each row runs to the next column it is AT, which is not always the one beside it.
def gather_ribbons(facts: Facts, columns: list[Column]) -> list[Ribbon]:
    seen: dict[str, Ribbon] = {}
    for group in facts.groups:
        at = _on_the_flow(facts, columns, group)
        for step in range(len(at) - 1):
            (ci, a), (cj, b) = at[step], at[step + 1]
            key = f"{ci}{a.key}>{cj}{b.key}"
            ribbon = seen.setdefault(key, Ribbon(ci, cj, a, b))
            ribbon.rows += len(group.ordinals)
    return list(seen.values())


def _on_the_flow(facts: Facts, columns: list[Column],
                 group: Group) -> list[tuple[int, Node]]:
    at = []
    for ci, column in enumerate(columns):
        node = _node_at(facts, column, group)
        if node is not None:
            at.append((ci, node))
    return at


# The "—" key is the same whether a row passed through branchless or was never here.
def _node_at(facts: Facts, column: Column, group: Group) -> Node | None:
    if not facts.came_through(group, column.stage.id):
        return None
    want = node_key(column.stage.id, facts.branches_at(group, column.stage.id))
    return next((node for node in column.nodes if node.key == want), None)


# The barycentre heuristic: a column takes its neighbours' mean, weighted by rows.
def order_nodes(columns: list[Column], ribbons: list[Ribbon]) -> None:
    for _ in range(SWEEPS):
        for down in range(1, len(columns)):
            _sort_column(columns, down, down - 1, ribbons)
        for up in range(len(columns) - 2, -1, -1):
            _sort_column(columns, up, up + 1, ribbons)


def _sort_column(columns: list[Column], ci: int, neighbour: int,
                 ribbons: list[Ribbon]) -> None:
    place = {node.key: at for at, node in enumerate(columns[neighbour].nodes)}
    held = {node.key: at for at, node in enumerate(columns[ci].nodes)}
    pull: dict[str, list[float]] = {}
    for ribbon in ribbons:
        if (ribbon.ci, ribbon.cj) != (min(ci, neighbour), max(ci, neighbour)):
            continue
        here, there = ((ribbon.a, ribbon.b) if ci < neighbour
                       else (ribbon.b, ribbon.a))
        if there.key not in place:
            continue
        got = pull.setdefault(here.key, [0.0, 0.0])
        got[0] += place[there.key] * ribbon.rows
        got[1] += ribbon.rows
    columns[ci].nodes.sort(key=lambda node: _measure_mean_place(pull, held, node))


# A node with nothing running to that neighbour has no opinion, so it stays put.
def _measure_mean_place(pull: dict[str, list[float]], held: dict[str, int],
                node: Node) -> float:
    got = pull.get(node.key)
    return got[0] / got[1] if got and got[1] else float(held[node.key])


# Each end is stacked in the order of the node at the OTHER end, so ribbons fan out.
def stack_ribbons(ribbons: list[Ribbon]) -> None:
    for ribbon in ribbons:
        ribbon.h0 = ribbon.a.height * (ribbon.rows / max(1, ribbon.a.rows))
        ribbon.h1 = ribbon.b.height * (ribbon.rows / max(1, ribbon.b.rows))
    _stack_end(ribbons, leaving=True)
    _stack_end(ribbons, leaving=False)


def _stack_end(ribbons: list[Ribbon], leaving: bool) -> None:
    groups: dict[str, list[Ribbon]] = {}
    for ribbon in ribbons:
        end = ribbon.a if leaving else ribbon.b
        groups.setdefault(end.key, []).append(ribbon)
    for group in groups.values():
        group.sort(key=lambda r: (r.b if leaving else r.a).y)
        y = (group[0].a if leaving else group[0].b).y
        for ribbon in group:
            if leaving:
                ribbon.y0, y = y, y + ribbon.h0
            else:
                ribbon.y1, y = y, y + ribbon.h1


# ─── where the bars land ─────────────────────────────────────────────────────


def place_bars(facts: Facts, columns: list[Column], top: float,
               has_ribbons: bool) -> None:
    scale = _measure_scale(facts, columns, has_ribbons)
    entered = _where_each_group_enters(facts, columns)
    for ci, column in enumerate(columns):
        # The rows running past hold the top of the band, so a source stacks below.
        y = top + _count_running_past(facts, columns, ci, entered) * scale
        for node in column.nodes:
            node.height = max(2.0, (node.drawn_rows or node.rows) * scale)
            node.y = y
            y += node.height + GAP
        column.x = ci * COLUMN
        column.bottom = max(y, _place_labels(column.nodes, top))


# A ribbon travelling further than it runs across bulges, so the busiest sets this.
def _measure_scale(facts: Facts, columns: list[Column],
                   has_ribbons: bool) -> float:
    most = max([len(column.nodes) for column in columns] + [1])
    # Nothing runs between one column's bars, so the labels are all the room needed.
    floor = SHORTEST_BAND if has_ribbons else most * LABEL_PITCH
    band = min(TALLEST_BAND, max(floor, most * BAND_PER_NODE if has_ribbons else 0))
    fits = [(band - (len(c.nodes) - 1) * GAP) / facts.total for c in columns]
    return min(fits + [band / facts.total])


def _where_each_group_enters(facts: Facts, columns: list[Column]) -> list[int]:
    entered = []
    for group in facts.groups:
        at = _on_the_flow(facts, columns, group)
        entered.append(at[0][0] if at else len(columns))
    return entered


def _count_running_past(facts: Facts, columns: list[Column], ci: int,
                        entered: list[int]) -> int:
    return sum(len(group.ordinals)
               for group, first in zip(facts.groups, entered)
               if first < ci and _node_at(facts, columns[ci], group) is None)


# Returns where the label stack ends, which outruns the bars once they are thin.
def _place_labels(nodes: list[Node], top: float) -> float:
    y = top
    for node in nodes:
        node.label_y = max(y, node.y + node.height / 2)
        y = node.label_y + LABEL_PITCH
    return y


def count_removal_lines(columns: list[Column]) -> int:
    return max([len(column.gone) for column in columns] + [0])


def measure_height(columns: list[Column]) -> float:
    foot = 17 if any(c.alias or c.expanded for c in columns) else 0
    return max(column.bottom for column in columns) + 14 + foot
