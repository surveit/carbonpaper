"""
compiler.py — the COMPILER feature of the methodology-DAG platform.

Job: take an UNSTRUCTURED input — a Claude Code agent run captured as a transcript
jsonl (or, in principle, prose) — and DISTILL it into a *draft* DAG: a list of
compiled stage dicts targeting `app.dag_schema`, plus a `methodology_raw.md` and
`compiler_notes` recording ambiguities.

The insight (see examples/palm_osint/research_runs/DISTILLATION.md): an open-ended
agent run's tool-call sequence (search → fetch → parse → extract → report) can be
classified into node types. Most steps are deterministic mechanism
(python_transform / input_data / join); a *few* are genuine judgment
(llm_transform). The compiler's value is collapsing ~40 tool calls into a handful
of stages with the LLM sitting only at the real judgment points.

Pipeline:
    parse_transcript(path)          → compact summary of the run (tool sequence + report)
    compile_from_transcript(path,..) → build an LLM prompt that frames the dag_schema
                                       contract + run summary, call Claude (Agent SDK,
                                       no tools), parse JSON → {stages, methodology_raw,
                                       compiler_notes}
    write_methodology(result, out)  → write compiled/NN_<id>.yaml + methodology_raw.md
    validate(stages)                → dag_schema.validate_methodology issues (self-check)
    harvest_eval_fixtures(parsed)   → candidate (search→url) and (doc→fields) eval rows

Dependency rule (critical, mirrors dag_schema's own): this module imports
`app.dag_schema` from our code and `claude_agent_sdk` directly. It MUST NOT import
`app.runtime.*` — the runner stays ignorant of the compiler; they meet only at the
schema. The CLI-discovery + no-tools query() pattern below is replicated from (not
imported from) app/runtime/llm_agent_sdk.py.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from app import dag_schema

# ─────────────────────────────────────────────────────────────────────────────
# 1. TRANSCRIPT PARSING — extract a compact run summary from a Claude Code jsonl
# ─────────────────────────────────────────────────────────────────────────────


def _iter_records(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # A malformed line is a parse defect we surface, not paper over.
                continue
    return records


def _message_content(rec: dict[str, Any]) -> list[Any]:
    msg = rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _trunc(s: str, n: int) -> str:
    s = s if isinstance(s, str) else str(s)
    s = s.strip()
    return s if len(s) <= n else s[:n] + f"… (+{len(s) - n} chars)"


def parse_transcript(path: str | Path) -> dict[str, Any]:
    """Read a Claude Code transcript jsonl and extract a COMPACT picture of the run.

    Returns a dict with:
      - searches:   [{query}]                      WebSearch queries, in order
      - fetches:    [{url, prompt}]                WebFetch targets, in order
      - commands:   [{command, description}]       Bash/PowerShell shell steps
      - reads:      [file_path, ...]               local Read targets
      - tool_sequence: ["WebSearch", ...]          flat ordered list of tool names
      - report:     str                            the final assistant text (the report)
      - n_records / n_tool_calls                   coarse size signals

    Deliberately compact: the goal is enough signal for the LLM to classify the
    run into node types, not a faithful replay.
    """
    records = _iter_records(path)
    searches: list[dict[str, Any]] = []
    fetches: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    reads: list[str] = []
    tool_sequence: list[str] = []
    assistant_texts: list[str] = []

    for rec in records:
        if rec.get("type") != "assistant":
            continue
        for block in _message_content(rec):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                txt = block.get("text", "")
                if txt and txt.strip():
                    assistant_texts.append(txt)
            elif btype == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {}) or {}
                tool_sequence.append(name)
                if name == "WebSearch":
                    searches.append({"query": inp.get("query", "")})
                elif name == "WebFetch":
                    fetches.append({
                        "url": inp.get("url", ""),
                        "prompt": _trunc(inp.get("prompt", ""), 220),
                    })
                elif name in ("Bash", "PowerShell"):
                    commands.append({
                        "command": _trunc(inp.get("command", ""), 200),
                        "description": inp.get("description", ""),
                    })
                elif name == "Read":
                    fp = inp.get("file_path", "")
                    if fp:
                        reads.append(fp)

    # The final non-empty assistant text block is the report.
    report = assistant_texts[-1] if assistant_texts else ""

    return {
        "source_path": str(path),
        "n_records": len(records),
        "n_tool_calls": len(tool_sequence),
        "tool_sequence": tool_sequence,
        "searches": searches,
        "fetches": fetches,
        "commands": commands,
        "reads": reads,
        "report": report,
    }


def summarize_run(parsed: dict[str, Any], report_chars: int = 6000) -> str:
    """Render parse_transcript output as a compact, LLM-readable brief."""
    lines: list[str] = []
    lines.append(f"## Run summary ({parsed['n_tool_calls']} tool calls, "
                 f"{parsed['n_records']} records)\n")

    # Collapse the tool sequence to a run-length-ish shape so the model sees the
    # search→fetch→parse→extract rhythm without 40 raw rows.
    seq = parsed["tool_sequence"]
    if seq:
        collapsed: list[str] = []
        for name in seq:
            if collapsed and collapsed[-1].split(" x")[0] == name:
                base = collapsed[-1].split(" x")[0]
                count = int(collapsed[-1].split(" x")[1]) if " x" in collapsed[-1] else 1
                collapsed[-1] = f"{base} x{count + 1}"
            else:
                collapsed.append(name)
        lines.append("### Tool call shape (in order)")
        lines.append(" → ".join(collapsed) + "\n")

    if parsed["searches"]:
        lines.append("### WebSearch queries")
        for s in parsed["searches"]:
            lines.append(f"- {s['query']}")
        lines.append("")

    if parsed["fetches"]:
        lines.append("### WebFetch targets (url — extraction intent)")
        for fe in parsed["fetches"]:
            lines.append(f"- {fe['url']}\n    intent: {fe['prompt']}")
        lines.append("")

    if parsed["commands"]:
        lines.append("### Shell commands (local processing)")
        for c in parsed["commands"]:
            desc = f" — {c['description']}" if c["description"] else ""
            lines.append(f"- `{c['command']}`{desc}")
        lines.append("")

    if parsed["reads"]:
        lines.append("### Local files read")
        for r in parsed["reads"]:
            lines.append(f"- {r}")
        lines.append("")

    if parsed["report"]:
        lines.append("### Final report (the run's output — what the DAG must reproduce)")
        lines.append(_trunc(parsed["report"], report_chars))

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 2. THE DAG_SCHEMA CONTRACT, rendered into the prompt
# ─────────────────────────────────────────────────────────────────────────────

def _node_type_contract() -> str:
    """Render the 7 node types + their handle blocks straight from dag_schema, so
    the prompt can never drift from the real contract."""
    out: list[str] = ["The 7 node types (each carries its executable-handle block):\n"]
    for tname, spec in dag_schema.NODE_TYPES.items():
        handle = spec["handle"]
        req = ", ".join(spec["required"]) or "(none)"
        opt = ", ".join(spec.get("optional", [])) or "(none)"
        also = spec.get("also_requires", [])
        also_s = f"; also needs block(s): {', '.join(also)}" if also else ""
        out.append(
            f"- **{tname}** — {spec['summary']}\n"
            f"    handle block: `{handle}:` required fields=[{req}] optional=[{opt}]{also_s}\n"
            f"    min_inputs={spec['min_inputs']}, requires_inputs={spec['requires_inputs']}"
        )
    out.append("")
    out.append("Column types: " + ", ".join(sorted(dag_schema.SCALAR_COLUMN_TYPES))
               + ", or list[<type>].")
    out.append("Connector kinds (input_data.connector.kind): "
               + ", ".join(sorted(dag_schema.CONNECTOR_KINDS)) + ".")
    out.append("python_transform.function.kind ∈ {module, inline} "
               "(module → needs `module`; inline → needs `code`).")
    out.append("join.type ∈ " + ", ".join(sorted(dag_schema.JOIN_TYPES))
               + "; join needs `keys`. publish also needs a `function:` block.")
    return "\n".join(out)


# A single concrete, schema-valid example stage so the model copies the exact key
# layout (handle block, inputs-with-schema, output_schema, snake_case id).
_EXAMPLE_STAGE = {
    "id": "locate",
    "name": "Locate the authoritative most-recent doc (LLM judgment)",
    "type": "llm_transform",
    "source": {"doc": "methodology_raw.md", "section": "§3"},
    "inputs": [
        {
            "id": "build_queries",
            "schema": {
                "primary_key": ["facility_id"],
                "columns": [
                    {"name": "facility_id", "type": "str"},
                    {"name": "name", "type": "str"},
                    {"name": "queries_json", "type": "json"},
                ],
            },
        }
    ],
    "llm": {
        "model": "haiku",
        "temperature": 0.0,
        "response_format": "json",
        "tools": ["WebSearch"],
        "prompt_template": "Find the authoritative most-recent doc for {name}. Return JSON ...",
    },
    "output_schema": {
        "primary_key": ["facility_id", "url"],
        "columns": [
            {"name": "facility_id", "type": "str"},
            {"name": "url", "type": "str"},
            {"name": "doc_type", "type": "str"},
            {"name": "is_primary", "type": "bool"},
        ],
    },
    "compiler_notes": ["JUDGMENT point: which doc is authoritative is not a fixed URL."],
}


def build_compile_prompt(parsed: dict[str, Any], name: str) -> str:
    """Assemble the full distillation prompt: the contract + an example + the run."""
    contract = _node_type_contract()
    example = json.dumps(_EXAMPLE_STAGE, indent=2)
    run_brief = summarize_run(parsed)

    return f"""\
You are a METHODOLOGY COMPILER. You are given a transcript summary of an
open-ended Claude Code research run that investigated ONE subject end-to-end
(here: a palm-oil mill named "{name}"). Your job is to DISTILL that ad-hoc run
into a reusable, structured methodology DAG that would reproduce this class of
research deterministically — with the LLM sitting at only the FEW genuine
judgment points, and everything else as deterministic mechanism.

# The output contract (target: app/dag_schema.py)
Emit a methodology as a list of STAGE dicts. Each stage validates against this
contract:

{contract}

Universal stage keys: id (snake_case), name, type, inputs (list of
{{id, schema:{{columns:[{{name,type}}], primary_key:[...]}}}}), output_schema (same
shape), source, compiler_notes (list of strings). The executable-handle block
(connector / llm / function / join / aggregate / queue / publish) is keyed by the
node type as shown above.

Here is ONE complete, valid example stage — copy this exact key layout:

{example}

# The research run to distill
{run_brief}

# How to distill (the core insight)
Classify the run's tool-call sequence into node types:
- The seed identity (what was known going in) → an **input_data** stage.
- Query construction (turning identity into search strings) → **python_transform**.
- Deciding WHICH found document is authoritative + most-recent → **llm_transform**
  (a real judgment point; the "connector needs an LLM").
- Downloading a URL, converting PDF→text, grepping fixed anchor keys → each a
  **python_transform** (deterministic mechanism, NOT an LLM stage).
- Reading a document's text into a structured field set → **llm_transform** (EXTRACT).
- Reconciling conflicting figures across documents/years → **llm_transform** (ADJUDICATE)
  or a **human_review_queue** if it is low-volume / high-stakes.
- Merging per-document rows back to one row per subject → **join** or python_transform.
- Rendering the final dossier → **publish**.

Aim for only ~3 llm_transform stages (locate, extract, adjudicate); make the rest
deterministic. Wire `inputs` so the DAG is connected and acyclic: every input id
must be the id of an upstream stage. Keep every id snake_case.

# Output format — RAW JSON ONLY, no prose, no markdown fences:
{{
  "stages": [ <list of stage dicts as above> ],
  "methodology_raw_md": "<a markdown methodology write-up: numbered sections, one
      per stage, describing what it does and why — the human-readable spec>",
  "compiler_notes": [ "<global ambiguities, judgment calls, things a human should
      confirm — e.g. is ADJUDICATE an LLM or a review queue?>" ]
}}

Do not fabricate data values. If a step's behaviour is ambiguous, encode your best
structural guess in the stage and record the ambiguity in compiler_notes. Output
the JSON object now."""


# ─────────────────────────────────────────────────────────────────────────────
# 3. LLM CALL — Agent SDK, no tools, model sonnet (pattern replicated, not imported)
# ─────────────────────────────────────────────────────────────────────────────

_COMPILER_SYSTEM = (
    "You are a methodology compiler. You convert an unstructured research-run "
    "transcript into a structured DAG of typed stages. You have NO tools and NO "
    "web access — work only from the transcript summary the user provides. Respond "
    "with raw JSON exactly matching the shape requested: no prose, no markdown, no "
    "code fences. Never fabricate data values, URLs, or numbers; encode structure "
    "and record uncertainty in compiler_notes instead."
)


def _find_cli() -> str | None:
    """Locate the Claude Code CLI. Same discovery the runtime SDK backend uses (the
    SDK's own search misses the Windows `.local/bin/claude.exe`). Replicated here
    so the compiler does not import the runtime module."""
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "claude.exe",
        home / ".local" / "bin" / "claude",
        home / "AppData" / "Roaming" / "npm" / "claude.cmd",
        home / ".claude" / "local" / "claude",
        home / ".npm-global" / "bin" / "claude",
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return str(c)
    return None


_CLI_PATH = _find_cli()


async def _aquery(prompt: str, model: str, timeout_s: int) -> str:
    """One no-tools query() to Claude via the Agent SDK; returns the raw text."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        TextBlock,
        query,
    )

    opts_kwargs: dict[str, Any] = dict(
        model=model,
        max_turns=1,
        allowed_tools=[],          # no tools → single completion turn
        setting_sources=[],        # ignore inherited CLAUDE.md / settings
        system_prompt=_COMPILER_SYSTEM,
    )
    if _CLI_PATH:
        opts_kwargs["cli_path"] = _CLI_PATH
    options = ClaudeAgentOptions(**opts_kwargs)

    text = ""

    async def _collect() -> None:
        nonlocal text
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text += block.text

    await asyncio.wait_for(_collect(), timeout=timeout_s)
    return text


def _run_sync(coro):
    """Drive a coroutine to completion from sync code. If NO event loop is running
    on this thread (CLI path) use asyncio.run directly; if one IS running (FastAPI
    async route), asyncio.run() would raise, so run on a fresh worker thread with
    its own loop. Mirrors the runtime SDK backend's _run_sync."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


def call_llm(prompt: str, model: str = "sonnet", timeout_s: int = 600) -> str:
    """Synchronous entry point: run the no-tools query to completion → raw text.
    Loop-safe: works from the CLI (no loop) and from inside a FastAPI handler."""
    return _run_sync(_aquery(prompt, model, timeout_s))


# ─────────────────────────────────────────────────────────────────────────────
# 4. JSON PARSING of the model output
# ─────────────────────────────────────────────────────────────────────────────

def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse the model's reply into a dict. Tries straight json.loads first, then
    strips ```json fences, then locates the first balanced {...} block. Raises
    ValueError loudly (with a snippet) on failure — never returns a fake stub."""
    text = (text or "").strip()
    if not text:
        raise ValueError("LLM returned empty text")

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # Strip code fences if present.
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        inner = fenced.group(1).strip()
        try:
            obj = json.loads(inner)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Balanced-brace scan for the first top-level JSON object.
    start = text.find("{")
    if start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break

    raise ValueError(
        "Could not parse a JSON object from the LLM output. First 400 chars:\n"
        + text[:400]
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. TOP-LEVEL COMPILE
# ─────────────────────────────────────────────────────────────────────────────

def compile_from_transcript(
    path: str | Path,
    name: str,
    model: str = "sonnet",
    timeout_s: int = 600,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """End-to-end: parse the transcript, prompt Claude to distill it, parse the
    JSON, and return {name, stages, methodology_raw, compiler_notes, validation,
    parsed, prompt}. Does NOT write files (write_methodology does that).

    LLM JSON output is non-deterministic and the model occasionally emits a single
    bracket/comma slip in a large object. Rather than risk-repairing malformed JSON
    (which could silently corrupt structure), we RE-ASK up to `max_attempts` times
    with a corrective nudge. If every attempt fails we raise loudly with the last
    error — a messy result is never silently passed off as a clean compile."""
    parsed = parse_transcript(path)
    base_prompt = build_compile_prompt(parsed, name)

    obj: dict[str, Any] | None = None
    raw = ""
    last_err: str | None = None
    for attempt in range(1, max_attempts + 1):
        prompt = base_prompt
        if attempt > 1 and last_err:
            prompt = (
                base_prompt
                + f"\n\n# RETRY {attempt}: your previous reply was not valid JSON "
                f"({last_err}). Emit ONLY a single, strictly-valid JSON object — "
                "check every bracket/brace/comma. No prose, no code fences."
            )
        raw = call_llm(prompt, model=model, timeout_s=timeout_s)
        try:
            candidate = _extract_json_object(raw)
            if isinstance(candidate.get("stages"), list) and candidate["stages"]:
                obj = candidate
                break
            last_err = ("no non-empty `stages` list; keys="
                        + ",".join(sorted(candidate.keys())))
        except ValueError as exc:
            last_err = str(exc).splitlines()[0]

    if obj is None:
        raise ValueError(
            f"LLM did not return valid JSON with a `stages` list after "
            f"{max_attempts} attempt(s). Last error: {last_err}"
        )

    stages = obj.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError(
            "LLM output had no non-empty `stages` list. Keys present: "
            + ", ".join(sorted(obj.keys()))
        )

    methodology_raw = obj.get("methodology_raw_md") or obj.get("methodology_raw") or ""
    compiler_notes = obj.get("compiler_notes") or []
    if isinstance(compiler_notes, str):
        compiler_notes = [compiler_notes]

    issues = validate(stages)

    return {
        "name": name,
        "stages": stages,
        "methodology_raw": methodology_raw,
        "compiler_notes": compiler_notes,
        "validation": issues,
        "parsed": parsed,
        "prompt": prompt,
        "raw_llm": raw,
    }


def validate(stages: list[dict[str, Any]]) -> list[str]:
    """Self-check: run the generated stages through the schema's own validator.
    [] means a clean-validating draft DAG."""
    return dag_schema.validate_methodology(stages)


# ─────────────────────────────────────────────────────────────────────────────
# 6. WRITE OUT
# ─────────────────────────────────────────────────────────────────────────────

def write_methodology(result: dict[str, Any], out_dir: str | Path) -> dict[str, Any]:
    """Write the compiled DAG to a folder shaped like an examples/<name>/ artifact:
      <out_dir>/compiled/NN_<id>.yaml   (one per stage, in order)
      <out_dir>/methodology_raw.md
      <out_dir>/compiler_result.json    (raw alongside cooked: full result, audit)
    Returns a manifest of written paths."""
    out_dir = Path(out_dir)
    compiled = out_dir / "compiled"
    compiled.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for i, stage in enumerate(result["stages"], start=1):
        sid = stage.get("id") or f"stage{i}"
        fname = f"{i:02d}_{sid}.yaml"
        fpath = compiled / fname
        with fpath.open("w", encoding="utf-8") as f:
            yaml.safe_dump(stage, f, sort_keys=False, allow_unicode=True, width=100)
        written.append(str(fpath))

    raw_md = out_dir / "methodology_raw.md"
    raw_md.write_text(result.get("methodology_raw") or "", encoding="utf-8")

    # Raw-alongside-cooked: persist the full result (minus the bulky prompt echo)
    # so the compile is auditable and re-sliceable.
    audit = {
        "name": result.get("name"),
        "compiler_notes": result.get("compiler_notes"),
        "validation": result.get("validation"),
        "source_path": (result.get("parsed") or {}).get("source_path"),
        "n_tool_calls": (result.get("parsed") or {}).get("n_tool_calls"),
        "stages": result.get("stages"),
    }
    audit_path = out_dir / "compiler_result.json"
    audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "out_dir": str(out_dir),
        "stage_files": written,
        "methodology_raw": str(raw_md),
        "audit": str(audit_path),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7. EVAL FIXTURE HARVESTING (stretch) — pull candidate eval rows from the run
# ─────────────────────────────────────────────────────────────────────────────

def harvest_eval_fixtures(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """From a parsed transcript, harvest candidate eval rows for the two judgment
    stages, using the run itself as ground truth:

      LOCATE eval  ← (search queries  → the URLs the agent then chose to fetch)
      EXTRACT eval ← (fetched url + extraction intent → ...expected fields)

    The EXTRACT side is only PARTIAL here: this compact parser keeps the fetch
    intent but not the full fetched document text or the agent's parsed fields,
    so we emit the (url, intent) stimulus and mark expected_fields as a TODO to be
    filled by a deeper pass over the raw tool_result blocks. The shape is final;
    only the expected-field population is deferred.

    Returns a list of {fixture_type, ...} dicts ready to write as jsonl.
    """
    fixtures: list[dict[str, Any]] = []

    # LOCATE: the set of search queries → the set of URLs the run actually fetched.
    # This is the (identity+search-hits → chosen authoritative doc) pair.
    if parsed.get("searches") and parsed.get("fetches"):
        fixtures.append({
            "fixture_type": "locate",
            "stimulus": {
                "search_queries": [s["query"] for s in parsed["searches"]],
            },
            "expected": {
                "chosen_urls": [fe["url"] for fe in parsed["fetches"]],
            },
            "provenance": parsed.get("source_path"),
            "note": "chosen_urls = URLs the agent fetched after searching; a proxy "
                    "for 'the authoritative docs it located'.",
        })

    # EXTRACT: each fetched doc + its extraction intent → fields (TODO: populate
    # expected_fields from the tool_result text in a deeper pass).
    for fe in parsed.get("fetches", []):
        fixtures.append({
            "fixture_type": "extract",
            "stimulus": {"url": fe["url"], "extraction_intent": fe["prompt"]},
            "expected_fields": None,  # TODO: harvest from tool_result block in raw jsonl
            "provenance": parsed.get("source_path"),
        })

    return fixtures


def write_eval_fixtures(parsed: dict[str, Any], out_dir: str | Path) -> str:
    """Write harvested fixtures to <out_dir>/eval_fixtures.jsonl. Returns the path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fixtures = harvest_eval_fixtures(parsed)
    path = out_dir / "eval_fixtures.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for row in fixtures:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return str(path)


# ─────────────────────────────────────────────────────────────────────────────
# 7b. COMPILATION OBJECT — persist a compile as a first-class object (parallels a
#     RUN). A compilation lives at <compilations_root>/<compilation_id>/ and holds
#     the manifest ("what compiled, ok/invalid/error"), the what-happened record
#     (parsed tool sequence + LLM prompt + raw response), and the DAG output.
# ─────────────────────────────────────────────────────────────────────────────

def _stage_summary(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact [{id, type}] list for the manifest (parallels a run's stage list)."""
    out: list[dict[str, Any]] = []
    for i, s in enumerate(stages, start=1):
        out.append({"id": s.get("id") or f"stage{i}", "type": s.get("type", "?")})
    return out


def prepare_compilation(
    compilations_root: str | Path,
    input_path: str | Path,
    name: str,
    model: str = "sonnet",
) -> dict[str, Any]:
    """Create the compilation dir + id and write an initial `running` manifest so
    a caller can redirect to the compilation page immediately and poll it while
    the (multi-minute) compile proceeds in the background. Mirrors runner.prepare_run.

    Returns {compilation_id, comp_dir, input_path, name, model}."""
    compilations_root = Path(compilations_root)
    compilation_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    comp_dir = compilations_root / compilation_id
    comp_dir.mkdir(parents=True, exist_ok=True)

    input_path = Path(input_path)
    input_kind = "transcript" if input_path.suffix == ".jsonl" else "prose"

    manifest = {
        "compilation_id": compilation_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "name": name,
        "input": str(input_path),
        "input_kind": input_kind,
        "model": model,
        "status": "running",
        "n_stages": 0,
        "validation_issues": [],
        "stage_summary": [],
        "error": None,
    }
    (comp_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return {
        "compilation_id": compilation_id,
        "comp_dir": comp_dir,
        "input_path": str(input_path),
        "name": name,
        "model": model,
    }


def run_prepared_compilation(prep: dict[str, Any]) -> str:
    """Execute a compilation previously set up by prepare_compilation(). Suitable
    for running in a background thread — the manifest on disk is rewritten to its
    terminal state (ok | invalid | error) when done, and the what_happened.json +
    DAG output + eval fixtures are written alongside.

    The compile itself can fail honestly (bad JSON from the model, an exception in
    parsing): in that case status is `error` and the manifest records the reason —
    we never write a fake-success object. A clean compile with schema issues is
    `invalid`; a clean compile that validates is `ok`.

    Returns the compilation_id."""
    comp_dir: Path = Path(prep["comp_dir"])
    compilation_id: str = prep["compilation_id"]
    input_path: str = prep["input_path"]
    name: str = prep["name"]
    model: str = prep["model"]

    manifest = json.loads((comp_dir / "manifest.json").read_text(encoding="utf-8"))

    try:
        result = compile_from_transcript(input_path, name, model=model)
    except Exception as exc:  # the compile failed — record it honestly, don't fake
        manifest["status"] = "error"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
        (comp_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        # Persist the traceback alongside so the failure is auditable.
        (comp_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return compilation_id

    stages = result["stages"]
    parsed = result["parsed"]
    issues = result["validation"]

    # ── DAG output: compiled/NN_<id>.yaml + methodology_raw.md (+ audit json) ──
    dag_dir = comp_dir / "dag"
    write_methodology(result, dag_dir)

    # ── eval fixtures (reuse the existing harvester) ──
    write_eval_fixtures(parsed, comp_dir)

    # ── what_happened.json: the tool-call summary, the prompt sent, raw response ──
    what_happened = {
        "input": parsed.get("source_path"),
        "tool_sequence_summary": {
            "n_records": parsed.get("n_records"),
            "n_tool_calls": parsed.get("n_tool_calls"),
            "n_searches": len(parsed.get("searches", [])),
            "n_fetches": len(parsed.get("fetches", [])),
            "n_commands": len(parsed.get("commands", [])),
            "n_reads": len(parsed.get("reads", [])),
            "tool_sequence": parsed.get("tool_sequence"),
            "searches": parsed.get("searches"),
            "fetches": parsed.get("fetches"),
            "commands": parsed.get("commands"),
        },
        "prompt": result.get("prompt"),
        "raw_llm_response": result.get("raw_llm"),
        "compiler_notes": result.get("compiler_notes"),
    }
    (comp_dir / "what_happened.json").write_text(
        json.dumps(what_happened, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ── manifest → terminal state ──
    manifest["status"] = "invalid" if issues else "ok"
    manifest["n_stages"] = len(stages)
    manifest["validation_issues"] = issues
    manifest["stage_summary"] = _stage_summary(stages)
    manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
    (comp_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return compilation_id


def run_compilation(
    input_path: str | Path,
    name: str,
    model: str = "sonnet",
    compilations_root: str | Path = "compilations",
) -> str:
    """Synchronous convenience: prepare + run a compilation to completion, writing
    the full compilation object under <compilations_root>/<id>/. Returns the
    compilation_id. (The web app splits these two phases so it can redirect+poll;
    this single-call form is for the CLI / tests.)"""
    prep = prepare_compilation(compilations_root, input_path, name, model)
    return run_prepared_compilation(prep)


def list_compilations(compilations_root: str | Path) -> list[dict[str, Any]]:
    """Read every compilation manifest under compilations_root, newest first, for
    the index page (parallels runner._list_runs)."""
    compilations_root = Path(compilations_root)
    if not compilations_root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for comp in sorted(compilations_root.iterdir(), reverse=True):
        if not comp.is_dir():
            continue
        manifest_path = comp / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            m = {"compilation_id": comp.name, "status": "corrupt"}
        out.append({
            "compilation_id": m.get("compilation_id", comp.name),
            "created_at": m.get("created_at"),
            "name": m.get("name"),
            "input": m.get("input"),
            "model": m.get("model"),
            "status": m.get("status", "unknown"),
            "n_stages": m.get("n_stages", 0),
            "n_validation_issues": len(m.get("validation_issues") or []),
        })
    return out


def load_compilation(compilations_root: str | Path, compilation_id: str) -> dict[str, Any]:
    """Load a single compilation object (manifest + what_happened + DAG stages +
    methodology_raw.md) for the detail page. Raises FileNotFoundError if the
    manifest is missing. Tolerates a still-running / errored compile where the
    what_happened + DAG files do not yet exist."""
    comp_dir = Path(compilations_root) / compilation_id
    manifest_path = comp_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"No compilation '{compilation_id}'")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    what_happened: dict[str, Any] | None = None
    wh_path = comp_dir / "what_happened.json"
    if wh_path.exists():
        what_happened = json.loads(wh_path.read_text(encoding="utf-8"))

    stages: list[dict[str, Any]] = []
    compiled_dir = comp_dir / "dag" / "compiled"
    if compiled_dir.is_dir():
        for yaml_file in sorted(compiled_dir.glob("*.yaml")):
            with yaml_file.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            # build_mermaid_graph needs a fallback _filename; the run loader sets
            # these too. Keep id-bearing stages renderable.
            data["_filename"] = yaml_file.name
            data["_order"] = yaml_file.stem.split("_", 1)[0]
            stages.append(data)

    methodology_raw = ""
    raw_md_path = comp_dir / "dag" / "methodology_raw.md"
    if raw_md_path.exists():
        methodology_raw = raw_md_path.read_text(encoding="utf-8")

    error_text = None
    err_path = comp_dir / "error.txt"
    if err_path.exists():
        error_text = err_path.read_text(encoding="utf-8")

    return {
        "manifest": manifest,
        "what_happened": what_happened,
        "stages": stages,
        "methodology_raw": methodology_raw,
        "error_text": error_text,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 8. CLI
# ─────────────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m app.compiler <transcript.jsonl> <out_name> "
              "[--out DIR] [--model sonnet]")
        return 2
    transcript = argv[0]
    out_name = argv[1]
    rest = argv[2:]
    out_dir = f"examples/_compiled_{out_name}"
    model = "sonnet"
    i = 0
    while i < len(rest):
        if rest[i] == "--out" and i + 1 < len(rest):
            out_dir = rest[i + 1]
            i += 2
        elif rest[i] == "--model" and i + 1 < len(rest):
            model = rest[i + 1]
            i += 2
        else:
            i += 1

    print(f"[compiler] parsing transcript: {transcript}")
    parsed = parse_transcript(transcript)
    print(f"[compiler]   {parsed['n_tool_calls']} tool calls, "
          f"{len(parsed['searches'])} searches, {len(parsed['fetches'])} fetches, "
          f"{len(parsed['commands'])} shell cmds; report {len(parsed['report'])} chars")
    print(f"[compiler] calling Claude ({model}) to distill — this can take a minute…")
    result = compile_from_transcript(transcript, out_name, model=model)

    print(f"\n[compiler] generated {len(result['stages'])} stages:")
    for i, s in enumerate(result["stages"], 1):
        print(f"  {i:02d}. {s.get('id'):<22} {s.get('type')}")

    issues = result["validation"]
    if issues:
        print(f"\n[compiler] validate_methodology: {len(issues)} ISSUE(S):")
        for iss in issues:
            print(f"  - {iss}")
    else:
        print("\n[compiler] validate_methodology: CLEAN ✓ (0 issues)")

    manifest = write_methodology(result, out_dir)
    eval_path = write_eval_fixtures(parsed, out_dir)
    print(f"\n[compiler] wrote {len(manifest['stage_files'])} stage files to {out_dir}/compiled/")
    print(f"[compiler] methodology_raw → {manifest['methodology_raw']}")
    print(f"[compiler] audit json      → {manifest['audit']}")
    print(f"[compiler] eval fixtures   → {eval_path}")

    if result["compiler_notes"]:
        print("\n[compiler] compiler_notes (ambiguities):")
        for n in result["compiler_notes"]:
            print(f"  - {n}")

    return 0 if not issues else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
