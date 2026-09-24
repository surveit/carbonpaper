"""Pipeline two: run an eval item — build blind, then grade against what it expected."""
import json

from evals.rebuild_check.eval_pieces import (
    BUILD_INSTRUCTIONS, COMPARE_CODE, DATA, DIAGNOSE_INSTRUCTIONS, ITEMS, MCP, READ_ANSWER_CODE,
    VERDICT_CODE, VERIFY_CODE, col, render_claims_code, render_resolve_sources_code,
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
            "id": "resolve_sources", "type": "python_row_function", "cache": True,
            "description": "Turn each case's committed source paths into paths on this machine, "
                           "and stop a case whose file is missing.",
            "inputs": [{"id": "items"}],
            "function": {"kind": "inline",
                         "code": render_resolve_sources_code(DATA.parents[1])},
            "signature": {"form": "extends", "reads": [
                {"input": "items", "columns": [col("input_files", "str", True)]}],
                "adds": [col("input_paths", "str", True)], "rewrites": []},
        },
        {
            "id": "blind_build", "type": "llm_transform", "cache": True,
            "description": "Build a pipeline over the given files and fill the requested shape. "
                           "Knows the data and the shape wanted, and nothing else.",
            "inputs": [{"id": "resolve_sources"}],
            "compiler_notes": [
                "The prompt renders only the columns this stage reads; the expected answers "
                "flow past it unread.",
            ],
            "llm": {"prompt_instructions": BUILD_INSTRUCTIONS,
                    "prompt_data_template": (
                        "Source files on disk: {input_paths}\n"
                        "Shape wanted: {target_schema}\n"),
                    "model": "claude-opus-5",
                    "temperature": 0.0, "max_retries": 2, "response_format": "json",
                    "batch_size": 1, "tools": ["Bash", "Read", "Grep"]},
            "signature": {"form": "extends", "reads": [
                {"input": "resolve_sources", "columns": [
                    col("input_paths", "str", True), col("target_schema", "str", True)]}],
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
            "id": "compare", "type": "python_row_function", "cache": True,
            "description": "Compare the answer the run wrote with what the artifact states, "
                           "figure by figure: right type first, then right value.",
            "inputs": [{"id": "read_answer"}],
            "function": {"kind": "inline", "code": COMPARE_CODE},
            "signature": {"form": "extends", "reads": [
                {"input": "read_answer", "columns": [
                    col("expected_json", "str", True), col("results_json", "str", True),
                    col("target_schema", "str", True)]}],
                "adds": [col("comparison_json", "str", False),
                         col("findings_text", "str", False),
                         col("fields_compared", "int", False)],
                "rewrites": []},
        },
        {
            "id": "diagnose", "type": "llm_transform", "cache": True,
            "description": "Say what went wrong: our defect, their defect, or a question the "
                           "source does not settle.",
            "inputs": [{"id": "compare"}],
            "llm": {"prompt_instructions": DIAGNOSE_INSTRUCTIONS,
                    "prompt_data_template": (
                        "Artifact: {artifact_title}\n"
                        "Quantities compared: {fields_compared}\n"
                        "{findings_text}\n\n"
                        "The build's account of its own method: {method}\n"
                        "Citation check: {citation_note}\n"),
                    "model": "claude-sonnet-5",
                    "temperature": 0.2, "max_retries": 3, "response_format": "json",
                    "batch_size": 1},
            "signature": {"form": "extends", "reads": [
                {"input": "compare", "columns": [
                    col("artifact_title", "str", True), col("fields_compared", "int", False),
                    col("findings_text", "str", False), col("method", "str", True),
                    col("citation_note", "str", True)]}],
                "adds": [col("process_ok", "bool", True), col("headline", "str", True),
                         col("diagnosis", "str", True), col("next_step", "str", True)],
                "rewrites": []},
        },
        {
            "id": "verdict", "type": "python_row_function", "cache": True,
            "description": "Fail the case, with a stated reason, unless the citation holds, "
                           "the judge trusted the comparison, every disagreeing figure got a "
                           "valid ruling, and no ruling was our own defect.",
            "inputs": [{"id": "diagnose"}],
            "function": {"kind": "inline", "code": VERDICT_CODE},
            "workflow_outputs": [{
                "kind": "table", "slug": "rebuild-verdicts",
                "label": "Each case's verdict and its per-figure comparison",
                "primary": True,
                "columns": ["item_id", "artifact_title", "passed", "comparison_json"]}],
            "signature": {"form": "extends", "reads": [
                {"input": "diagnose", "columns": [
                    col("citation_holds", "bool", True), col("process_ok", "bool", True),
                    col("diagnosis", "str", True), col("comparison_json", "str", False)]}],
                "adds": [col("passed", "bool", False), col("verdict_reason", "str", False)],
                "rewrites": []},
        },
        {
            "id": "claims", "type": "python_row_function", "cache": True,
            "description": "Claim each rebuilt figure in the page's own words, on the run that "
                           "was graded, and have the reviewers review it. Outreach runs only.",
            "inputs": [{"id": "verdict"}],
            "function": {"kind": "inline", "code": render_claims_code(MCP)},
            "signature": {"form": "extends", "reads": [
                {"input": "verdict", "columns": [
                    col("run_url", "str", True), col("citation_holds", "bool", True),
                    col("process_ok", "bool", True),
                    col("results_json", "str", True), col("expected_quotes_json", "str", True),
                    col("diagnosis", "str", True)]}],
                "adds": [col("claims_json", "str", False), col("claims_skipped_json", "str", False)],
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
