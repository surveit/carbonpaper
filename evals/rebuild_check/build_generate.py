"""Pipeline one: turn repository links into eval items."""
import json

from evals.rebuild_check.eval_pieces import (
    DATA, FETCH_SOURCES_CODE, ITEMS, SCHEMA_GUARD_CODE, SCHEMA_INSTRUCTIONS, SOURCES, col,
    render_read_instructions, resolve_chrome,
)

EVAL_ITEM_CODE = '''
import hashlib


def transform(row):
    return {
        "usable": not row["unusable_because"] and bool(row["local_source_paths"]),
        "item_id": hashlib.sha256(row["repo_url"].encode()).hexdigest()[:16],
        "artifact": row["repo_url"],
        "input_files": row["local_source_paths"],
        "input_digests": row["source_digests"],
        "expected_json": row["claims_json"],
        "expected_locations_json": row["claim_locations_json"],
        "expected_quotes_json": row["claim_quotes_json"],
    }
'''

WRITE_ITEMS_CODE = '''
import json
import pathlib

import pandas as pd


COLUMNS = ["item_id", "artifact", "artifact_title", "input_files", "input_digests",
           "target_schema", "expected_json", "expected_locations_json", "expected_quotes_json"]
NEWLINE = chr(10)


def transform(df, output_dir):
    items = df[df["usable"]][COLUMNS].to_dict("records")
    refused = df[~df["usable"]][["artifact", "unusable_because"]].to_dict("records")
    body = NEWLINE.join(json.dumps(item) for item in items) + NEWLINE
    destination = pathlib.Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "eval_items.json").write_text(body, encoding="utf-8")
    (destination / "refused.json").write_text(
        json.dumps(refused, indent=2), encoding="utf-8")
    return pd.DataFrame({"items_written": [len(items)], "artifacts_refused": [len(refused)]})
'''


def stages():
    return [
        {
            "id": "repos", "type": "input_data", "cache": True,
            "description": "Name the artifacts to make eval items from, one link each.",
            "connector": {"kind": "file", "params": {
                "paths": [str(SOURCES / "repos.csv")], "format": "csv"}},
            "signature": {"form": "replaces", "produces": [col("repo_url", "str", False)]},
        },
        {
            "id": "read_artifact", "type": "llm_transform", "cache": True,
            "description": "Read what the published page states, and which data files it cites.",
            "inputs": [{"id": "repos"}],
            "llm": {"prompt_instructions": render_read_instructions(resolve_chrome()),
                    "prompt_data_template": "Repository: {repo_url}\n",
                    "temperature": 0.0, "max_retries": 2, "response_format": "json",
                    "batch_size": 1, "tools": ["Bash", "Read", "Grep"]},
            "signature": {"form": "extends", "reads": [
                {"input": "repos", "columns": [col("repo_url", "str", False)]}],
                "adds": [col("artifact_title", "str", True),
                         col("claims_json", "str", True, "Field name to the value stated."),
                         col("claim_locations_json", "str", True, "Field name to where it appears."),
                         col("claim_quotes_json", "str", True,
                             "Field name to the exact text the page states it in."),
                         col("source_urls", "str", True,
                             "Direct URLs to the tabular data files, semicolon separated."),
                         col("unusable_because", "str", True,
                             "Why this artifact cannot become an item; blank where it can.")],
                "rewrites": []},
        },
        {
            "id": "answer_shape", "type": "llm_transform", "cache": True,
            "description": "Turn the quantities the page states into a typed answer shape "
                           "carrying no value.",
            "inputs": [{"id": "read_artifact"}],
            "llm": {"prompt_instructions": SCHEMA_INSTRUCTIONS,
                    "prompt_data_template": "Quantities the page states: {claims_json}\n",
                    "temperature": 0.0, "max_retries": 3, "response_format": "json",
                    "batch_size": 1},
            "signature": {"form": "extends", "reads": [
                {"input": "read_artifact", "columns": [col("claims_json", "str", True)]}],
                "adds": [col("target_schema", "str", True, "JSON Schema for the answer.")],
                "rewrites": []},
        },
        {
            "id": "shape_carries_no_figure", "type": "python_row_function", "cache": True,
            "description": "Stop if a stated figure reached the schema; a value there would "
                           "tell a build what to find.",
            "inputs": [{"id": "answer_shape"}],
            "function": {"kind": "inline", "code": SCHEMA_GUARD_CODE},
            "signature": {"form": "extends", "reads": [
                {"input": "answer_shape", "columns": [
                    col("target_schema", "str", True), col("claims_json", "str", True)]}],
                "adds": [col("schema_is_clean", "bool", False)], "rewrites": []},
        },
        {
            "id": "fetch_sources", "type": "python_row_function", "cache": True,
            "description": "Pull the cited tabular files onto disk and hash each, so an item "
                           "names bytes rather than a URL that may move.",
            "inputs": [{"id": "shape_carries_no_figure"}],
            "function": {"kind": "inline", "code": FETCH_SOURCES_CODE},
            "signature": {"form": "extends", "reads": [
                {"input": "shape_carries_no_figure", "columns": [
                    col("repo_url", "str", False), col("source_urls", "str", True),
                    col("unusable_because", "str", True)]}],
                "adds": [col("local_source_paths", "str", True),
                         col("source_digests", "str", True),
                         col("sources_fetched", "int", False)],
                "rewrites": []},
        },
        {
            "id": "eval_item", "type": "python_row_function", "cache": True,
            "description": "Shape one eval item from the artifact read: its files, its shape, "
                           "and what it expects.",
            "inputs": [{"id": "fetch_sources"}],
            "workflow_outputs": [{
                "kind": "table", "slug": "eval-items",
                "label": "Eval items generated from these repositories",
                "primary": True,
                "columns": ["item_id", "artifact", "artifact_title", "input_files",
                            "target_schema", "expected_json"]}],
            "function": {"kind": "inline", "code": EVAL_ITEM_CODE},
            "signature": {"form": "extends", "reads": [
                {"input": "fetch_sources", "columns": [
                    col("repo_url", "str", False), col("local_source_paths", "str", True),
                    col("source_digests", "str", True), col("claims_json", "str", True),
                    col("claim_locations_json", "str", True),
                    col("claim_quotes_json", "str", True),
                    col("unusable_because", "str", True)]}],
                "adds": [col("usable", "bool", False,
                             "Whether this artifact produced an item worth grading against."),
                         col("item_id", "str", False,
                             "Stable across runs: a digest of the artifact link."),
                         col("artifact", "str", False),
                         col("input_files", "str", True), col("input_digests", "str", True),
                         col("expected_json", "str", True),
                         col("expected_locations_json", "str", True),
                         col("expected_quotes_json", "str", True)],
                "rewrites": []},
        },
        {
            "id": "write_items", "type": "report", "cache": False,
            "description": "Write the item set the eval runner reads.",
            "inputs": [{"id": "eval_item"}],
            "report": {"format": "html_report", "destination": "build/"},
            "function": {"kind": "inline", "code": WRITE_ITEMS_CODE},
            "signature": {"form": "replaces", "reads": [
                {"input": "eval_item", "columns": [
                    col("item_id", "str", False), col("artifact", "str", False),
                    col("artifact_title", "str", True), col("input_files", "str", True),
                    col("input_digests", "str", True), col("target_schema", "str", True),
                    col("expected_json", "str", True),
                    col("expected_locations_json", "str", True),
                    col("expected_quotes_json", "str", True),
                    col("usable", "bool", False), col("unusable_because", "str", True)]}],
                "produces": []},
        },
    ]


def main():
    ITEMS.mkdir(parents=True, exist_ok=True)
    (DATA / "workflow_generate.json").write_text(json.dumps({
        "name": "generate-eval-items",
        "document": (DATA / "methodology_generate.md").read_text(encoding="utf-8"),
        "model": "published data artifacts",
        "source": "https://github.com/surveit/carbonpaper",
        "data_model": {"schemas": []}, "verbs": [], "stages": stages()}, indent=2),
        encoding="utf-8")
    print("wrote workflow_generate.json with", len(stages()), "stages")


if __name__ == "__main__":
    main()
