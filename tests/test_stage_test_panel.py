"""The node review partial renders the stage's tests as a skimmable report."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace
from stage_seed import add_stage

_AMOUNT = {"name": "amount", "type": "float", "nullable": False}
# `note` is carried through: in the input schema, read by nothing, written by nothing.
_IN_SCHEMA = {"columns": [_AMOUNT, {"name": "note", "type": "str", "nullable": False}]}


def _seed_project(root: Path) -> None:
    compiled = root / "alpha"
    compiled.mkdir(parents=True, exist_ok=True)
    add_stage(compiled, {
        "id": "load", "description": "Load", "type": "input_data",
        "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]},
    })
    add_stage(compiled, {
        "id": "double", "description": "Double", "type": "python_row_function",
        "inputs": [{"id": "load"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": [_AMOUNT]}],
            "adds": [{"name": "doubled", "type": "float", "nullable": False}],
        },
        "function": {"kind": "inline",
                     "code": "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"},
        # The rows state their columns in neither schema's order, so the table's order
        # can only come from the schema.
        "tests": [
            {"name": "doubles_two", "description": "The basic doubling contract.",
             "inputs": {"load": [{"note": "opening balance", "amount": 2.0}]},
             "expected": [{"doubled": 4.0, "note": "opening balance", "amount": 2.0}]},
            {"name": "expects_wrong_value",
             "inputs": {"load": [{"note": "closing balance", "amount": 3.0}]},
             "expected": [{"doubled": 7.0, "note": "closing balance", "amount": 3.0}]},
            {"name": "expects_refusal",
             "inputs": {"load": [{"note": "any balance", "amount": 5.0}]},
             "expected": None},
        ],
    })


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    workspace.set_projects_dir(tmp_path)
    return TestClient(app)


def test_panel_shows_each_test_with_status(client: TestClient, tmp_path: Path) -> None:
    _seed_project(tmp_path)
    response = client.get("/project/alpha/node/double/panel")
    assert response.status_code == 200
    html = response.text
    assert "Tests" in html
    assert "doubles_two" in html and "The basic doubling contract." in html
    assert "expects_wrong_value" in html


def test_only_a_case_needing_review_is_marked(client: TestClient, tmp_path: Path) -> None:
    _seed_project(tmp_path)
    html = client.get("/project/alpha/node/double/panel").text
    # Two of the three seeded cases do not match; the third carries nothing, because a
    # mark on every case is a mark that means nothing.
    assert html.count("sev-ico-warning") == 2
    assert ">passed<" not in html and ">failed<" not in html


def test_the_verdict_is_one_line_naming_who_expected(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_project(tmp_path)
    html = " ".join(client.get("/project/alpha/node/double/panel").text.split())
    assert 'class="sparkle"' in html  # the agent mark, drawn by _sparkle.html
    assert "✓ This matches what an independent agent expected from the " \
           "description alone." in html
    assert "✗ This outcome is different from what an independent agent expected " \
           "from the description alone. Further review is recommended." in html
    # It carries no heading; the outcome above it is the last thing headed.
    assert "against the description" not in html


def test_only_a_differing_case_shows_what_the_agent_expected(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_project(tmp_path)
    html = client.get("/project/alpha/node/double/panel").text
    assert html.count("The agent expected the step to return:") == 1
    assert "The agent expected the step to refuse this input" in html


def test_a_case_leads_with_what_the_step_actually_returned(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_project(tmp_path)
    html = client.get("/project/alpha/node/double/panel").text
    assert html.index("actual outcome") < html.index("independent agent expected")
    # `expects_refusal` returned a row instead of refusing; the row itself is on the
    # page, not only the count that used to stand in for it.
    assert '<td class="test-col-written">10.0</td>' in html
    assert "got 1 row(s)" not in html


def test_a_matching_case_does_not_repeat_the_rows_as_an_expectation(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_project(tmp_path)
    html = client.get("/project/alpha/node/double/panel").text
    assert html.count('<td class="test-col-written">4.0</td>') == 1
    # The disagreeing case still states what the description asked for.
    assert '<td class="test-col-written">7.0</td>' in html


def test_the_actual_table_marks_the_columns_the_step_writes(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_project(tmp_path)
    html = client.get("/project/alpha/node/double/panel").text
    # `doubled` is what the signature writes; `amount` and `note` are carried through.
    assert '<th class="test-col-written">doubled</th>' in html
    assert '<td class="test-col-written">4.0</td>' in html
    assert "<th>amount</th>" in html and "<td>opening balance</td>" in html
    assert "<code>doubled</code>" in html  # named in the caption, not colour alone


def test_a_carried_through_column_sits_where_the_schema_puts_it(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_project(tmp_path)
    html = client.get("/project/alpha/node/double/panel").text
    # Both rows were authored `note` before `amount`; every table shows the schema's
    # order, so a carried-through column is under the same heading in each.
    input_table = html.split("<h3>input")[1].split("<h3>")[0]
    assert input_table.index("<th>amount</th>") < input_table.index("<th>note</th>")
    outcome_table = html.split("<h3>actual outcome</h3>")[1].split("</table>")[0]
    assert outcome_table.index("<th>amount</th>") < outcome_table.index("<th>note</th>")
    assert outcome_table.index("<th>note</th>") < outcome_table.index(
        '<th class="test-col-written">doubled</th>'
    )
    expected_table = html.split("The agent expected the step to return:")[1].split("</table>")[0]
    assert expected_table.index("<th>amount</th>") < expected_table.index("<th>note</th>")


def test_panel_without_tests_has_no_tests_section(client: TestClient, tmp_path: Path) -> None:
    _seed_project(tmp_path)
    response = client.get("/project/alpha/node/load/panel")
    assert response.status_code == 200
    assert "test-report" not in response.text


# ── the three sections ───────────────────────────────────────────────────────
# A case belongs to one of them by what it already carries: a selected row, or no row
# and a step that must stop, or no row and an expected value. Nothing else decides.

_SECTIONED = [
    {"name": "doubles_a_real_amount", "description": "the ordinary case",
     "inputs": {"load": [{"note": "opening balance", "amount": 2.0}]},
     "expected": [{"doubled": 4.0, "note": "opening balance", "amount": 2.0}],
     "selections": [{"input": "load", "run_id": "20260101T000000", "row": 3,
                     "filter": "amount > 1", "matched": 7, "scanned": 98}]},
    {"name": "a_negative_amount_stops_the_run", "description": "nobody has decided",
     "inputs": {"load": [{"note": "refund", "amount": -5.0}]},
     "expected": None,
     "authored_reason": "No filing reports a negative amount."},
    {"name": "a_zero_amount_is_doubled_to_zero", "description": "already decided",
     "inputs": {"load": [{"note": "nil return", "amount": 0.0}]},
     "expected": [{"doubled": 0.0, "note": "nil return", "amount": 0.0}],
     "authored_reason": "No filing reports a plain zero."},
]


def _seed_sectioned(root: Path) -> None:
    _seed_project(root)
    path = root / "alpha" / "compiled" / "02_double.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["tests"] = _SECTIONED
    path.write_text(json.dumps(spec), encoding="utf-8")


def test_the_panel_leads_with_the_rows_that_came_out_of_a_run(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_sectioned(tmp_path)
    html = client.get("/project/alpha/node/double/panel").text
    assert "Examples from your data" in html
    assert html.index("Examples from your data") < html.index("your data changes")


def test_a_case_with_no_row_is_sorted_by_what_the_step_would_do(
    client: TestClient, tmp_path: Path
) -> None:
    """Must-stop leaves the decision open; an expected value is a decision already taken."""
    _seed_sectioned(tmp_path)
    html = client.get("/project/alpha/node/double/panel").text
    rejected = html.index("Examples to reject and defer")
    decide = html.index("Examples to decide now")
    assert rejected < html.index("a_negative_amount_stops_the_run") < decide
    assert decide < html.index("a_zero_amount_is_doubled_to_zero")


def test_a_written_row_states_no_reason_beside_itself(
    client: TestClient, tmp_path: Path
) -> None:
    """The section it sits in has already said the row was written."""
    _seed_sectioned(tmp_path)
    # On the class, not the prose: the stored reason still reaches the page inside the
    # spec editor's raw JSON, which is not a rendering of it.
    html = client.get("/project/alpha/node/double/panel").text
    assert "test-authored" not in html


def test_a_suite_with_nothing_written_shows_only_the_first_section(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_sectioned(tmp_path)
    path = tmp_path / "alpha" / "compiled" / "02_double.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["tests"] = _SECTIONED[:1]
    path.write_text(json.dumps(spec), encoding="utf-8")

    html = client.get("/project/alpha/node/double/panel").text
    assert "Examples from your data" in html
    assert "What happens when your data changes" not in html


def test_the_first_section_names_the_run_its_rows_came_out_of(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_sectioned(tmp_path)
    html = " ".join(client.get("/project/alpha/node/double/panel").text.split())
    assert "1 row out of run 20260101T000000, and what this step did to it." in html
