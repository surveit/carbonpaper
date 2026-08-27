"""Reading order for a packet's steps: one branch at a time, joins last."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.services.review_packet.branch_order import sort_stages_by_branch


@dataclass
class _Node:
    id: str
    input_ids: list[str] = field(default_factory=list)


def _order(edges: dict[str, list[str]]) -> list[str]:
    return [node.id for node in sort_stages_by_branch(_nodes(edges))]


def _nodes(edges: dict[str, list[str]]) -> list[_Node]:
    return [_Node(stage_id, parents) for stage_id, parents in edges.items()]


def test_a_single_chain_stays_in_its_own_order():
    assert _order({"a": [], "b": ["a"], "c": ["b"]}) == ["a", "b", "c"]


def test_a_diamond_takes_one_side_then_the_other_then_the_join():
    edges = {"top": [], "left": ["top"], "right": ["top"], "join": ["left", "right"]}
    assert _order(edges) == ["top", "left", "right", "join"]


def test_two_chains_meeting_late_are_not_interleaved():
    edges = {
        "a1": [], "a2": ["a1"], "a3": ["a2"],
        "b1": [], "b2": ["b1"], "b3": ["b2"],
        "join": ["a3", "b3"],
    }
    # A topological sort emits a1, b1, a2, b2, ... — one branch at a time is the point.
    assert _order(edges) == ["a1", "a2", "a3", "b1", "b2", "b3", "join"]


def test_a_step_with_three_parents_waits_for_all_three():
    edges = {"p1": [], "p2": [], "p3": [], "join": ["p1", "p2", "p3"]}
    assert _order(edges) == ["p1", "p2", "p3", "join"]


def test_the_branch_taken_next_is_the_one_the_stall_waits_on():
    # Declaration order would take `unrelated` next and strand `join`.
    edges = {
        "x": [], "unrelated": [], "z": [],
        "a": ["x"], "b": ["unrelated"],
        "join": ["a", "z"],
    }
    assert _order(edges) == ["x", "a", "z", "join", "unrelated", "b"]


def test_ties_break_on_the_order_the_stages_were_given():
    edges = {"top": [], "right": ["top"], "left": ["top"], "join": ["left", "right"]}
    assert _order(edges) == ["top", "right", "left", "join"]


def test_every_stage_is_placed_exactly_once():
    edges = {
        "a1": [], "a2": ["a1"], "b1": [], "b2": ["b1"],
        "join": ["a2", "b2"], "after": ["join"],
    }
    assert sorted(_order(edges)) == sorted(edges)


def test_an_input_naming_no_stage_here_is_not_waited_for():
    assert _order({"a": ["gone"], "b": ["a"]}) == ["a", "b"]


def test_a_cycle_is_refused_rather_than_half_ordered():
    with pytest.raises(ValueError, match="cyclic stages"):
        _order({"a": ["b"], "b": ["a"], "c": []})


def test_the_stages_themselves_come_back_not_their_ids():
    nodes = _nodes({"a": [], "b": ["a"]})
    assert sort_stages_by_branch(nodes) == [nodes[0], nodes[1]]
