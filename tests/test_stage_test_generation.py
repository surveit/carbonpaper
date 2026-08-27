"""The generator bridge: task assembly is code-blind and document-blind, and every input
row a case feeds in is read off a real run rather than taken from what the agent sent."""
import pandas as pd
import pytest
from pydantic import ValidationError

from app.compiler.stage_tests import build_stage_test_generator, render_generation_task
from app.compiler.stage_tests_search import FIND_ROWS_TOOL, build_find_rows_tool
from app.compiler.stage_tests_submission import read_selected_rows
from app.models import NamedSchema, SchemaLibrary, Terms, Verb, parse_stage, Stage
from app.models.stages.stage_base import find_stage_test_class
from app.core.frames import frame_to_table
from app.services.frame_profile import profile_table
from app.core.row_search import InputRows

_CODE = "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"
_SUMMARY = "Doubles the reported `amount` into `doubled`."
_NO_TERMS = Terms(nouns=SchemaLibrary(schemas=[]), verbs=[])
_TERMS = Terms(
    nouns=SchemaLibrary(schemas=[NamedSchema(
        name="filing", title="Filing", description="One disclosure a firm sent in.",
        also_written=["disclosure"])]),
    verbs=[Verb(name="flag", definition="Mark a row for a human to decide on.")],
)
_RUN_ID = "20260807T142707"

# What one run of the upstream wrote. `memo` is there so a narrowed read has something
# to narrow away; the amounts are what a case selects between.
_FRAME = pd.DataFrame({
    "amount": [30000.0, 45000.0, 0.0, 111650.94],
    "memo": ["Q1 filing", "Q2 filing", "nil return", "Q2 filing"],
})


def _sources(frame: pd.DataFrame = _FRAME, columns=("amount",)) -> dict[str, InputRows]:
    narrowed = frame[list(columns)].reset_index(drop=True)
    return {"load": InputRows(
        input_id="load", run_id=_RUN_ID, frame=narrowed,
        profile=profile_table(frame_to_table(narrowed), list(columns), max_values=12),
    )}


def _python_stage(*, summary=_SUMMARY, corner_cases=None) -> Stage:
    function = {"kind": "inline", "code": _CODE, "summary": summary}
    if corner_cases is not None:
        function["corner_cases"] = corner_cases
    return parse_stage({
        "id": "double", "description": "Double", "type": "python_row_function",
        "inputs": [{"id": "load"}],
        "function": function,
        "signature": {
            "form": "extends",
            "reads": [
                {
                    "input": "load",
                    "columns": [{"name": "amount", "type": "float", "nullable": False}],
                },
            ],
            "adds": [{"name": "doubled", "type": "float", "nullable": False}],
        },
    })


def _task(stage: Stage | None = None, terms: Terms = _NO_TERMS, **kwargs) -> str:
    return render_generation_task(terms, stage or _python_stage(), _sources(**kwargs))


# ── what the task says ───────────────────────────────────────────────────────

def test_task_contains_the_description_schemas_and_stage_meta():
    task = _task()
    assert _SUMMARY in task
    assert "Double" in task            # stage name rendered
    assert "double" in task            # stage id rendered
    assert "doubled" in task           # output schema rendered
    assert "load" in task              # input id rendered


def test_task_names_the_run_the_rows_come_from_and_how_many_there_are():
    task = _task()
    assert _RUN_ID in task
    assert "4 rows" in task


def test_task_states_what_the_read_column_really_holds():
    """The seed against a case built on a value the column could never carry."""
    task = _task(frame=pd.DataFrame({"amount": [30000.0, 30000.0, 0.0, 111650.94]}))
    assert "3 distinct value(s)" in task
    assert "'30000.0' ×2" in task


def test_a_column_where_nothing_repeats_is_shown_as_a_few_examples():
    """A count of 1 per value states only that. The shape a filter matches is the point."""
    frame = pd.DataFrame({"filing_uuid": [f"uuid-{n}" for n in range(20)]})
    task = _task(_uuid_stage(), columns=("filing_uuid",), frame=frame)
    assert "20 distinct value(s); for example: 'uuid-0', 'uuid-1', 'uuid-10'" in task


def test_task_never_contains_the_methodology_document():
    """An agent that had read the methodology would certify the methodology, not the code."""
    assert "METHODOLOGY" not in _task()


def test_the_task_is_written_in_the_projects_own_words():
    task = _task(terms=_TERMS)
    assert "- filing — One disclosure a firm sent in. Also written: disclosure." in task
    assert "- flag — Mark a row for a human to decide on." in task


def test_a_project_with_no_words_gets_no_terms_heading():
    assert "# Terms" not in _task()


def test_task_never_contains_the_stage_code():
    task = _task()
    assert "def transform" not in task
    assert _CODE not in task


def test_task_never_contains_existing_tests():
    stage = _python_stage()
    stage = parse_stage({**stage.model_dump(by_alias=True, exclude_none=True),
        "tests": [{"name": "stale_case",
                   "inputs": {"load": [{"amount": 1.0}]},
                   "expected": [{"amount": 1.0, "doubled": 2.0}]}]})
    assert "stale_case" not in _task(stage)


def test_stated_corner_cases_are_rendered_with_their_expected_outcome():
    task = _task(_python_stage(corner_cases=[
        {"case": "`amount` is blank", "expected": "the step fails"},
        {"case": "`amount` is negative", "expected": "the row is kept unchanged"},
    ]))
    assert "`amount` is blank" in task
    assert "the step fails" in task
    assert "`amount` is negative" in task
    assert "the row is kept unchanged" in task


def test_no_corner_cases_still_renders_a_task():
    task = _task(_python_stage(corner_cases=[]))
    assert _SUMMARY in task
    assert "corner case" not in task.lower()


def test_a_stage_with_no_summary_cannot_generate_examples():
    with pytest.raises(ValueError, match="has no summary"):
        _task(_python_stage(summary=None))


def test_generator_rejects_non_python_stages():
    bad = parse_stage({
        "id": "pub", "description": "Publish", "type": "report",
        "signature": {"form": "replaces"},
        "inputs": [{"id": "double"}],
        "function": {"kind": "inline", "code": "def transform(df, output_dir):\n    return df\n"},
        "report": {},
    })
    with pytest.raises(ValueError, match="can run them"):
        build_stage_test_generator(_NO_TERMS, bad, _sources())


def test_the_generator_can_search_the_rows_it_selects_from():
    agent = build_stage_test_generator(_NO_TERMS, _python_stage(), _sources())
    assert [spec.name for spec in agent._tools] == [FIND_ROWS_TOOL]


# ── what a submission may say ────────────────────────────────────────────────

def _submit(agent, **case):
    return agent._target_schema.model_validate({"tests": [{
        "name": "doubles_an_amount", "description": "the ordinary case", **case}]})


def _generator(stage: Stage | None = None):
    return build_stage_test_generator(_NO_TERMS, stage or _python_stage(), _sources())


def test_a_selected_case_feeds_in_the_row_the_run_really_holds():
    agent = _generator()
    submitted = _submit(agent, expected=[{"doubled": 90000.0}], selected_rows=[
        {"input": "load", "row": 0, "filter": "amount > 1000"}])

    built = read_selected_rows(
        submitted.tests, find_stage_test_class(type(_python_stage())), _sources())
    assert built[0].inputs == {"load": [{"amount": 30000.0}]}
    assert built[0].expected == [{"doubled": 90000.0}]


def test_a_selection_records_the_run_the_row_and_what_the_filter_reached():
    agent = _generator()
    submitted = _submit(agent, expected=[{"doubled": 90000.0}], selected_rows=[
        {"input": "load", "row": 0, "filter": "amount > 1000"}])

    selection = read_selected_rows(
        submitted.tests, find_stage_test_class(type(_python_stage())), _sources()
    )[0].selections[0]
    assert (selection.run_id, selection.row) == (_RUN_ID, 0)
    assert (selection.matched, selection.scanned) == (3, 4)


def test_a_filter_that_does_not_select_its_own_row_is_refused():
    """Without this the stored filter is decoration: it explains a row it never found."""
    with pytest.raises(ValidationError, match="does not select row 2"):
        _submit(_generator(), expected=[{"doubled": 0.0}], selected_rows=[
            {"input": "load", "row": 2, "filter": "amount > 1000"}])


def test_a_row_number_no_run_holds_is_refused():
    with pytest.raises(ValidationError, match="no rows at all"):
        _submit(_generator(), expected=[{"doubled": 2.0}], selected_rows=[
            {"input": "load", "row": 99, "filter": "amount == 1.0"}])


def test_a_case_selecting_from_an_input_the_step_does_not_read_is_refused():
    with pytest.raises(ValidationError, match="does not read"):
        _submit(_generator(), expected=[{"doubled": 2.0}], selected_rows=[
            {"input": "ghost", "row": 0, "filter": "amount > 1000"}])


def test_a_case_that_selects_nothing_must_say_why_it_wrote_its_rows():
    with pytest.raises(ValidationError, match="why no real row serves it"):
        _submit(_generator(), expected=None,
                authored_rows={"load": [{"amount": -1.0}]})


def test_a_written_row_is_kept_when_the_data_cannot_supply_the_case():
    """A must-fail case can never be selected: such a row would have stopped the run."""
    submitted = _submit(
        _generator(), expected=None, authored_rows={"load": [{"amount": -1.0}]},
        authored_reason="no filing in this data reports a negative amount")

    built = read_selected_rows(
        submitted.tests, find_stage_test_class(type(_python_stage())), _sources())
    assert built[0].inputs == {"load": [{"amount": -1.0}]}
    assert built[0].expected is None
    assert built[0].selections == []
    assert "negative amount" in built[0].authored_reason


def test_a_case_cannot_both_select_a_row_and_write_one():
    with pytest.raises(ValidationError, match="states no authored ones"):
        _submit(_generator(), expected=[{"doubled": 90000.0}],
                selected_rows=[{"input": "load", "row": 0, "filter": "amount > 1000"}],
                authored_rows={"load": [{"amount": -1.0}]},
                authored_reason="a reason it does not get to give")


def test_an_expected_row_naming_a_column_the_step_never_writes_is_refused():
    with pytest.raises(ValidationError, match="memo"):
        _submit(_generator(), expected=[{"doubled": 90000.0, "memo": "rent"}],
                selected_rows=[{"input": "load", "row": 0, "filter": "amount > 1000"}])


# ── the search tool ──────────────────────────────────────────────────────────

def test_the_search_tool_reaches_only_this_steps_own_inputs():
    find_rows = build_find_rows_tool(_sources()).fn
    with pytest.raises(ValueError, match="does not read `elsewhere`"):
        find_rows(input="elsewhere", filter="amount > 1")


def test_the_search_tool_answers_with_the_rows_and_the_counts():
    matches = build_find_rows_tool(_sources()).fn(input="load", filter="amount == 0.0")
    assert (matches.matched, matches.scanned) == (1, 4)
    assert [row.row for row in matches.rows] == [2]


def _uuid_stage() -> Stage:
    return parse_stage({
        "id": "double", "description": "Double", "type": "python_row_function",
        "inputs": [{"id": "load"}],
        "function": {"kind": "inline", "code": _CODE, "summary": _SUMMARY},
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": [
                {"name": "filing_uuid", "type": "str", "nullable": False}]}],
            "adds": [{"name": "doubled", "type": "float", "nullable": False}],
        },
    })
