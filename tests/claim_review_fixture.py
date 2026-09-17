"""One finished scope-fixture run with one claim on its total, for the review tests."""
from __future__ import annotations

from app.models.citations import StageOutputCellCitation
from app.models.claims import ClaimImportance, ClaimShapeInput, DataUniverseRequirement
from app.models.records.claims import Claim
from app.models.records.workflow_output import WorkflowOutput
from app.services import claim_shapes, claims
from app.services import run as run_service
from app.services.project import save_working_copy_as_version
from scope_fixture import column, stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "scope_fixture"
TOTAL_TEXT = "Grants came to 2,200 in total."
TOTAL_SHAPE = ClaimShapeInput(
    label="What the grants came to, in whole units",
    universe=DataUniverseRequirement.closed, importance=ClaimImportance.primary)


SANDBOXED_CODE = 'def should_include(row):\n    return row["amount"] > 0\n'


def run_the_fixture(projects_root) -> str:
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, add_a_sandboxed_filter(stage_specs(data)))
    save_working_copy_as_version(PROJECT, message="fixture")
    return str(run_service.execute(PROJECT)["run_id"])


def claim_the_total(run_id: str, text: str = TOTAL_TEXT) -> Claim:
    [shape] = claim_shapes.write_claim_shapes(PROJECT, [TOTAL_SHAPE])
    WorkflowOutput(
        slug="grant-total", label="What the grants came to", primary=True, shape_id=shape.id,
        citation=StageOutputCellCitation(run_id=run_id, stage_id="grant_totals", row_ordinal=0,
            column="total_amount", value=2200),
    ).save()
    WorkflowOutput(
        slug="grant-count", label="How many grants", primary=False, shape_id=None,
        citation=StageOutputCellCitation(run_id=run_id, stage_id="grant_totals", row_ordinal=0, column="grants", value=5),
    ).save()
    return claims.submit_claim(PROJECT, run_id, "grant-total", {}, text)


NULLABLE_PROJECT = "nullable_fixture"
NULLABLE_TEXT = "Doubled, the largest amount came to 22,000."

# B's doubled amount is null, so the int column the run writes holds a null.
_DOUBLING_CODE = '''
def transform(row):
    if row["name"] == "B":
        return {"doubled": None}
    return {"doubled": row["amount"] * 2}
'''


def claim_a_nullable_figure(projects_root) -> Claim:
    data = projects_root / NULLABLE_PROJECT / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "amounts.csv").write_text("name,amount\nA,11000\nB,1\nC,1100\n", encoding="utf-8")
    set_stages(NULLABLE_PROJECT, _nullable_stage_specs(data))
    save_working_copy_as_version(NULLABLE_PROJECT, message="fixture")
    run_id = str(run_service.execute(NULLABLE_PROJECT)["run_id"])
    [shape] = claim_shapes.write_claim_shapes(NULLABLE_PROJECT, [TOTAL_SHAPE])
    for slug, row_ordinal, value, shape_id in [("doubled-large", 0, 22000, shape.id),
                                               ("doubled-small", 2, 2200, None)]:
        WorkflowOutput(
            slug=slug, label=f"Doubled amount, row {row_ordinal}", shape_id=shape_id,
            citation=StageOutputCellCitation(run_id=run_id,
                                             stage_id="doubled",
                                             row_ordinal=row_ordinal, column="doubled",
                                             value=value),
        ).save()
    return claims.submit_claim(NULLABLE_PROJECT, run_id, "doubled-large", {}, NULLABLE_TEXT)


def _nullable_stage_specs(data) -> list[dict]:
    amounts = [column("name", "str", False), column("amount", "int", False)]
    return [
        {"id": "load_amounts", "type": "input_data", "cache": True,
         "description": "Amounts by name.",
         "connector": {"kind": "file", "params": {
             "paths": [str(data / "amounts.csv")], "format": "csv"}},
         "signature": {"form": "replaces", "produces": amounts}},
        {"id": "doubled", "type": "starlark_row_function", "cache": True,
         "description": "Doubles each amount, except B's.",
         "inputs": [{"id": "load_amounts"}],
         "starlark": {"summary": "Doubles an amount; B's is null.",
                      "corner_cases": [{"case": "name is B", "expected": "doubled is null"}],
                      "code": _DOUBLING_CODE},
         "signature": {"form": "extends", "reads": [{"input": "load_amounts", "columns": amounts}],
                       "adds": [column("doubled", "int")], "rewrites": []}},
    ]


def add_a_sandboxed_filter(specs: list[dict]) -> list[dict]:
    """A starlark_filter_rows stage, whose code sits in a field named for its own type."""
    return [*specs, {
        "id": "sandboxed_positive", "type": "starlark_filter_rows", "cache": True,
        "description": "Keeps the grants recorded above zero, in the sandbox.",
        "inputs": [{"id": "grants_only"}],
        "starlark_filter": {
            "summary": "Keeps a grant only where the recorded amount is above zero.",
            "corner_cases": [{"case": "amount is 0", "expected": "the row is dropped"}],
            "code": SANDBOXED_CODE,
        },
        "signature": {"form": "extends", "reads": [
            {"input": "grants_only", "columns": [column("amount", "int", False)]}],
            "adds": [], "rewrites": []},
    }]
