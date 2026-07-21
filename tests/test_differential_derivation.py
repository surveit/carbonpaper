"""N-version differential derivation (app/evals/differential.py).

No LLM: the derivation step is injected, returning canned candidate code, so the
whole loop — run the frozen suite against each candidate, probe the survivors,
diff their behavior — is exercised deterministically. Covers the two shapes the
issue names: independently-derived candidates that behaviorally AGREE report no
ambiguity, and candidates that DIVERGE on an input beyond the frozen tests
produce an AmbiguityFinding (never a majority-vote winner).
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.models import Stage
from app.evals.differential import (
    DerivedCandidate,
    derive_n_version_and_diff,
)

_IN_SCHEMA = {"columns": [{"name": "amount", "type": "float", "nullable": True}]}
_OUT_SCHEMA = {"columns": [
    {"name": "amount", "type": "float", "nullable": True},
    {"name": "band", "type": "str", "nullable": True},
]}

# The frozen suite only pins amounts strictly above and strictly below 100 — it
# never states what happens exactly AT the boundary, so a candidate is free to
# read `>= 100` or `> 100` and still pass.
_FROZEN_TESTS = [
    {"name": "low_is_small", "inputs": {"load": [{"amount": 10.0}]},
     "expected": [{"amount": 10.0, "band": "small"}]},
    {"name": "high_is_big", "inputs": {"load": [{"amount": 250.0}]},
     "expected": [{"amount": 250.0, "band": "big"}]},
]


def _stage() -> Stage:
    return Stage.model_validate({
        "id": "banding", "name": "Band amounts", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "output_schema": _OUT_SCHEMA,
        "function": {"kind": "inline", "code": _GE_100},
        "tests": _FROZEN_TESTS,
    })


# Two faithful readings that BOTH pass the frozen suite but split at amount == 100.
_GE_100 = (
    "def transform(row):\n"
    "    band = 'big' if row['amount'] >= 100 else 'small'\n"
    "    return {**row, 'band': band}\n"
)
_GT_100 = (
    "def transform(row):\n"
    "    band = 'big' if row['amount'] > 100 else 'small'\n"
    "    return {**row, 'band': band}\n"
)


def _fixed_deriver(codes: list[str]):
    async def derive(index: int) -> DerivedCandidate:
        return DerivedCandidate(index=index, code=codes[index])
    return derive


def _run(**kwargs):
    return asyncio.run(derive_n_version_and_diff(**kwargs))


def test_agreeing_candidates_report_no_ambiguity():
    # All three candidates read the boundary the same way — no probe separates them.
    report = _run(
        document="Amounts of 100 or more are big; less than 100 are small.",
        stage=_stage(),
        probe_inputs=[{"load": [{"amount": 100.0}]}],
        derive_candidate=_fixed_deriver([_GE_100, _GE_100, _GE_100]),
    )
    assert report.status == "no_ambiguity"
    assert report.finding is None
    assert report.passing_candidate_count == 3


def test_diverging_candidates_beyond_frozen_tests_produce_a_finding():
    # Two readings that both pass the suite but split exactly at the boundary the
    # frozen tests never pin. The probe (amount == 100) is outside the frozen set.
    report = _run(
        document="Amounts at or above the 100 cutoff are big.",
        stage=_stage(),
        probe_inputs=[{"load": [{"amount": 100.0}]}],
        derive_candidate=_fixed_deriver([_GE_100, _GT_100, _GE_100]),
    )
    assert report.status == "ambiguity_detected"
    finding = report.finding
    assert finding is not None
    assert finding.stage_id == "banding"
    assert finding.passing_candidate_count == 3
    # The finding carries the diverging probe and every survivor's output on it —
    # the adjudication evidence, not a chosen winner.
    assert finding.probe.inputs == {"load": [{"amount": 100.0}]}
    bands = {
        tuple(row["band"] for row in (output.rows or []))
        for output in finding.probe.outputs
    }
    assert bands == {("big",), ("small",)}


def test_probe_only_within_frozen_coverage_finds_nothing():
    # Divergent candidates, but the only probe is one the frozen tests already
    # pin — so both readings agree on it and no ambiguity is surfaced.
    report = _run(
        document="Amounts at or above the 100 cutoff are big.",
        stage=_stage(),
        probe_inputs=[{"load": [{"amount": 250.0}]}],
        derive_candidate=_fixed_deriver([_GE_100, _GT_100, _GE_100]),
    )
    assert report.status == "no_ambiguity"
    assert report.finding is None


def test_all_candidates_failing_the_suite_is_not_ambiguity():
    broken = "def transform(row):\n    return {**row, 'band': 'wrong'}\n"
    report = _run(
        document="Amounts of 100 or more are big.",
        stage=_stage(),
        probe_inputs=[{"load": [{"amount": 100.0}]}],
        derive_candidate=_fixed_deriver([broken, broken, broken]),
    )
    assert report.status == "no_candidates_passed"
    assert report.finding is None
    assert report.passing_candidate_count == 0
    assert report.failing_candidate_count == 3


def test_error_versus_rows_on_a_probe_is_divergence():
    # One survivor raises on a negative amount the frozen tests never cover while
    # the other returns a band: the tests do not pin how this input is handled.
    guarded = (
        "def transform(row):\n"
        "    if row['amount'] < 0:\n"
        "        raise ValueError('negative amount')\n"
        "    band = 'big' if row['amount'] >= 100 else 'small'\n"
        "    return {**row, 'band': band}\n"
    )
    report = _run(
        document="Amounts of 100 or more are big.",
        stage=_stage(),
        probe_inputs=[{"load": [{"amount": -5.0}]}],
        derive_candidate=_fixed_deriver([_GE_100, guarded, _GE_100]),
    )
    assert report.status == "ambiguity_detected"
    assert report.finding is not None
    errored = [o for o in report.finding.probe.outputs if o.error is not None]
    assert len(errored) == 1 and "negative amount" in (errored[0].error or "")


def test_single_passing_candidate_cannot_differentiate():
    broken = "def transform(row):\n    return {**row, 'band': 'wrong'}\n"
    report = _run(
        document="Amounts of 100 or more are big.",
        stage=_stage(),
        probe_inputs=[{"load": [{"amount": 100.0}]}],
        derive_candidate=_fixed_deriver([_GE_100, broken, broken]),
    )
    assert report.status == "no_ambiguity"
    assert report.passing_candidate_count == 1


def test_requires_frozen_tests():
    stage = _stage().model_copy(update={"tests": None})
    with pytest.raises(ValueError, match="no frozen tests"):
        _run(
            document="x", stage=stage, probe_inputs=[],
            derive_candidate=_fixed_deriver([_GE_100, _GE_100]),
        )


def test_requires_at_least_two_versions():
    with pytest.raises(ValueError, match="differentiate"):
        _run(
            document="x", stage=_stage(), probe_inputs=[], n=1,
            derive_candidate=_fixed_deriver([_GE_100]),
        )
