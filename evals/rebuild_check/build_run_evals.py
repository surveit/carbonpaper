"""Pipeline two: run an eval item — build blind, then grade against what it expected."""
import json

from evals.rebuild_check.eval_pieces import (
    BUILD_INSTRUCTIONS, DATA, DIAGNOSE_INSTRUCTIONS, FIELD_NAMES_CODE, FIELD_VALUES_CODE,
    FIELD_VERDICT_CODE, ITEMS, READ_ANSWER_CODE, RENDER_CODE, VERIFY_CODE, col,
)

ITEM_COLUMNS = [
    col("item_id", "str", False), col("artifact", "str", False),
    col("artifact_title", "str", True), col("input_files", "str", True),
    col("input_digests", "str", True), col("target_schema", "str", True),
    col("expected_json", "str", True), col("expected_locations_json", "str", True),
    col("expected_quotes_json", "str", True),
]


def stages():
    return [
        {
            "id": "items", "type": "input_data", "cache": True,
            "description": "Read the eval items: input files, the shape wanted, and what the "
                           "artifact expects.",
            "connector": {"kind": "file", "params": {
                "paths": [str(ITEMS / "eval_items.json")], "format": "json"}},
            "signature": {"form": "replaces", "produces": ITEM_COLUMNS},
        },
        {
            "id": "build_brief", "type": "aggregate", "cache": True,
            "description": "Cut the item down to what a build may see. This stage exists for "
                           "what it drops: the expected answers are not on its output.",
            "inputs": [{"id": "items"}],
            "aggregate": {"group_by": ["item_id", "input_files", "target_schema"],
                          "aggregations": [{"output_column": "rows_briefed", "formula": "count"}]},
            "signature": {"form": "replaces", "reads": [
                {"input": "items", "columns": [
                    col("item_id", "str", False), col("input_files", "str", True),
                    col("target_schema", "str", True)]}],
                "produces": [col("item_id", "str", False), col("input_files", "str", True),
                             col("target_schema", "str", True),
                             col("rows_briefed", "int", False)]},
        },
        {
            "id": "blind_build", "type": "llm_transform", "cache": True,
            "description": "Build a pipeline over the given files and fill the requested shape. "
                           "Knows the data and the shape wanted, and nothing else.",
            "inputs": [{"id": "build_brief"}],
            "compiler_notes": [
                "Its input is build_brief, whose schema has no expected answer on it, so the "
                "prompt could not inject one even if it named it.",
            ],
            "llm": {"prompt_instructions": BUILD_INSTRUCTIONS,
                    "prompt_data_template": (
                        "Source files on disk: {input_files}\n"
                        "Shape wanted: {target_schema}\n"),
                    "model": "claude-opus-5",
                    "temperature": 0.0, "max_retries": 2, "response_format": "json",
                    "batch_size": 1, "tools": ["Bash", "Read", "Grep"]},
            "signature": {"form": "extends", "reads": [
                {"input": "build_brief", "columns": [
                    col("input_files", "str", True), col("target_schema", "str", True)]}],
                "adds": [col("run_url", "str", True,
                             "Full URL of the finished run whose answer stage holds the figures."),
                         col("method", "str", True),
                         col("blocked_by", "str", True,
                             "What stopped the build; blank where the run finished.")],
                "rewrites": []},
        },
        {
            "id": "verify_citation", "type": "python_row_function", "cache": True,
            "description": "Ask the workspace whether the cited run finished, and name the link "
                           "that breaks if one does.",
            "inputs": [{"id": "blind_build"}],
            "function": {"kind": "inline", "code": VERIFY_CODE},
            "signature": {"form": "extends", "reads": [
                {"input": "blind_build", "columns": [
                    col("run_url", "str", True), col("blocked_by", "str", True)]}],
                "adds": [col("citation_holds", "bool", False),
                         col("citation_failure", "str", True),
                         col("citation_note", "str", False)],
                "rewrites": []},
        },
        {
            "id": "read_answer", "type": "python_row_function", "cache": True,
            "description": "Read the figures out of the cited run's answer stage. They come "
                           "from the run or they do not come at all.",
            "inputs": [{"id": "verify_citation"}],
            "function": {"kind": "inline", "code": READ_ANSWER_CODE},
            "signature": {"form": "extends", "reads": [
                {"input": "verify_citation", "columns": [
                    col("run_url", "str", True), col("citation_holds", "bool", False)]}],
                "adds": [col("results_json", "str", True,
                             "The answer row, as JSON, read back out of the run."),
                         col("answer_stage_rows", "int", True)],
                "rewrites": []},
        },
        {
            "id": "answers_with_expected", "type": "enrich", "cache": True,
            "description": "Bring the expected answers back. The first stage where both exist "
                           "on one row.",
            "inputs": [{"id": "read_answer"}, {"id": "items"}],
            "join": {"keys": [{"left": "item_id", "right": "item_id"}],
                     "enrich_with": {"expected_json": "expected_json",
                                     "expected_locations_json": "expected_locations_json",
                                     "artifact": "artifact", "artifact_title": "artifact_title"}},
            "signature": {"form": "extends", "reads": [
                {"input": "read_answer", "columns": [col("item_id", "str", False)]},
                {"input": "items", "columns": ITEM_COLUMNS}],
                "adds": [col("expected_json", "str", True),
                         col("expected_locations_json", "str", True),
                         col("artifact", "str", True), col("artifact_title", "str", True)],
                "rewrites": []},
        },
        {
            "id": "field_names", "type": "python_row_function", "cache": True,
            "description": "List every quantity either side named, as one array on the row it "
                           "came from, ready to explode into rows that still trace back.",
            "inputs": [{"id": "answers_with_expected"}],
            "function": {"kind": "inline", "code": FIELD_NAMES_CODE},
            "signature": {"form": "extends", "reads": [
                {"input": "answers_with_expected", "columns": [
                    col("expected_json", "str", True), col("results_json", "str", True)]}],
                "adds": [col("field", "list[str]", True)], "rewrites": []},
        },
        {
            "id": "field_rows", "type": "explode",
            "description": "Fan out to one row per quantity. Explode records where each row "
                           "came from, which a frame function doing the same would not.",
            "inputs": [{"id": "field_names"}],
            "explode": {"column": "field", "keep_empty": True},
            "signature": {"form": "extends", "reads": [
                {"input": "field_names", "columns": [col("field", "list[str]", True)]}],
                "adds": [], "rewrites": [col("field", "str", True)]},
        },
        {
            "id": "field_values", "type": "python_row_function", "cache": True,
            "description": "Read this quantity from both sides, and check the computed value "
                           "against the type the schema declared for it.",
            "inputs": [{"id": "field_rows"}],
            "function": {"kind": "inline", "code": FIELD_VALUES_CODE},
            "signature": {"form": "extends", "reads": [
                {"input": "field_rows", "columns": [
                    col("field", "str", True), col("expected_json", "str", True),
                    col("results_json", "str", True), col("target_schema", "str", True)]}],
                "adds": [col("expected_value", "str", True),
                         col("computed_value", "str", True),
                         col("declared_type", "str", True),
                         col("type_matches", "bool", True)],
                "rewrites": []},
        },
        {
            "id": "field_verdict", "type": "starlark_row_function", "cache": True,
            "description": "Grade this quantity: right type first, then right value.",
            "inputs": [{"id": "field_values"}],
            "workflow_outputs": [{
                "kind": "table", "slug": "expected-against-rebuild",
                "label": "What the artifact expects, against a rebuild that never saw it",
                "primary": True,
                "columns": ["field", "expected_value", "computed_value", "declared_type",
                            "verdict"]}],
            "starlark": {
                "summary": "Compares the expected and computed values for one quantity.",
                "corner_cases": [
                    {"case": "the computed value is of the declared type and equal",
                     "expected": "agrees"},
                    {"case": "the schema declared integer and a string came back",
                     "expected": "wrong_type, not a value difference"},
                    {"case": "the rebuild produced nothing for the quantity",
                     "expected": "not_computed rather than a difference against nothing"}],
                "code": FIELD_VERDICT_CODE},
            "signature": {"form": "extends", "reads": [
                {"input": "field_values", "columns": [
                    col("expected_value", "str", True), col("computed_value", "str", True),
                    col("type_matches", "bool", True)]}],
                "adds": [col("verdict", "str", False)], "rewrites": []},
        },
        {
            "id": "item_findings", "type": "aggregate", "cache": True,
            "description": "Gather every quantity back onto the item it belongs to.",
            "inputs": [{"id": "field_verdict"}],
            "aggregate": {"group_by": ["item_id"], "aggregations": [
                {"output_column": "fields", "formula": "list", "value_column": "field"},
                {"output_column": "expected_values", "formula": "list", "value_column": "expected_value"},
                {"output_column": "computed_values", "formula": "list", "value_column": "computed_value"},
                {"output_column": "verdicts", "formula": "list", "value_column": "verdict"},
                {"output_column": "fields_compared", "formula": "count"}]},
            "signature": {"form": "replaces", "reads": [
                {"input": "field_verdict", "columns": [
                    col("item_id", "str", False), col("field", "str", True),
                    col("expected_value", "str", True), col("computed_value", "str", True),
                    col("verdict", "str", False)]}],
                "produces": [col("item_id", "str", False),
                             col("fields", "list[str]", True),
                             col("expected_values", "list[str]", True),
                             col("computed_values", "list[str]", True),
                             col("verdicts", "list[str]", True),
                             col("fields_compared", "int", False)]},
        },
        {
            "id": "findings_with_context", "type": "enrich", "cache": True,
            "description": "Put the build's own method and the citation check beside the "
                           "quantity results.",
            "inputs": [{"id": "item_findings"}, {"id": "answers_with_expected"}],
            "join": {"keys": [{"left": "item_id", "right": "item_id"}],
                     "enrich_with": {"artifact_title": "artifact_title", "method": "method",
                                     "run_url": "run_url", "citation_holds": "citation_holds",
                                     "citation_note": "citation_note"}},
            "signature": {"form": "extends", "reads": [
                {"input": "item_findings", "columns": [col("item_id", "str", False)]},
                {"input": "answers_with_expected", "columns": [
                    col("item_id", "str", False), col("artifact_title", "str", True),
                    col("method", "str", True), col("run_url", "str", True),
                    col("citation_holds", "bool", False), col("citation_note", "str", False)]}],
                "adds": [col("artifact_title", "str", True), col("method", "str", True),
                         col("run_url", "str", True), col("citation_holds", "bool", True),
                         col("citation_note", "str", True)],
                "rewrites": []},
        },
        {
            "id": "findings_rendered", "type": "starlark_row_function", "cache": True,
            "description": "Write the quantity results out as lines, which is what the "
                           "diagnosis reads.",
            "inputs": [{"id": "findings_with_context"}],
            "starlark": {
                "summary": "Renders one line per compared quantity.",
                "corner_cases": [
                    {"case": "five quantities were compared", "expected": "five lines"}],
                "code": RENDER_CODE},
            "signature": {"form": "extends", "reads": [
                {"input": "findings_with_context", "columns": [
                    col("fields", "list[str]", True),
                    col("expected_values", "list[str]", True),
                    col("computed_values", "list[str]", True),
                    col("verdicts", "list[str]", True)]}],
                "adds": [col("findings_text", "str", False)], "rewrites": []},
        },
        {
            "id": "diagnose", "type": "llm_transform", "cache": True,
            "description": "Say what went wrong: our defect, their defect, or a question the "
                           "source does not settle.",
            "inputs": [{"id": "findings_rendered"}],
            "llm": {"prompt_instructions": DIAGNOSE_INSTRUCTIONS,
                    "prompt_data_template": (
                        "Artifact: {artifact_title}\n"
                        "Quantities compared: {fields_compared}\n"
                        "{findings_text}\n\n"
                        "The build's account of its own method: {method}\n"
                        "Citation check: {citation_note}\n"),
                    "temperature": 0.2, "max_retries": 3, "response_format": "json",
                    "batch_size": 1},
            "signature": {"form": "extends", "reads": [
                {"input": "findings_rendered", "columns": [
                    col("artifact_title", "str", True), col("fields_compared", "int", False),
                    col("findings_text", "str", False), col("method", "str", True),
                    col("citation_note", "str", True)]}],
                "adds": [col("process_ok", "bool", True), col("headline", "str", True),
                         col("diagnosis", "str", True), col("next_step", "str", True)],
                "rewrites": []},
        },
    ]


def main():
    (DATA / "workflow_run_evals.json").write_text(json.dumps({
        "name": "run-evals",
        "document": (DATA / "methodology_run_evals.md").read_text(encoding="utf-8"),
        "model": "eval items",
        "source": "https://github.com/surveit/carbonpaper",
        "data_model": {"schemas": []}, "verbs": [], "stages": stages()}, indent=2),
        encoding="utf-8")
    print("wrote workflow_run_evals.json with", len(stages()), "stages")


if __name__ == "__main__":
    main()
