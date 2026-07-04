# Methodology editing agent — design

**Status: PROPOSED. Not built yet.** This document specifies a feature to build.
Throughout, **[EXISTS]** marks something already in the codebase today and
**[PLANNED]** marks something this design proposes to add. Nothing under
[PLANNED] describes current behaviour.

Written 2026-07-03, after the compiler + reviewable-workflow + chat subsystem
landed. Decisions in §4 were made with the user in the design session that
preceded this doc.

---

## 0. Terms (defined on first use, for a cold read)

- **Methodology** — one data pipeline, stored as a folder under `examples/<name>/`.
  It is the unit a journalist authors. Not "a Pydantic model" and not an LLM.
- **Stage** — one node of the pipeline's DAG (directed acyclic graph). One stage
  = one file `examples/<name>/compiled/NN_<id>.yaml`. There are 7 stage types
  (input_data, llm_transform, python_transform, join, aggregate,
  human_review_queue, publish); each carries a typed `output_schema`.
- **`compiled/`** — the **live working copy** of a methodology's stages. The DAG
  the app renders and runs is read from here.
- **Data model** — the intended entities/columns of a methodology. Today it has
  two faces: (a) each stage's inline `output_schema` (the *executable* contract
  the runtime validates between stages), and (b) an optional `schemas/`
  **reference object** — shared/named type definitions that document intent but
  do **not** by themselves drive execution. See §7.
- **Node review / belief approval** — a human marking a single stage
  `approved` / `rejected` / `needs-changes`, asserting they believe that stage
  is modelled correctly. Distinct from **row review** (`human_review_queue`),
  which halts a *run* to adjudicate data rows. Node review does not halt a run;
  it colours the DAG.
- **Version** — an immutable snapshot of `compiled/` (+ `schemas/`) copied to
  `examples/<name>/versions/<id>/`, freezing the approval coverage at that
  moment. A run can be pinned to a version.
- **The editing agent** — [PLANNED] a chat surface whose LLM is given tools to
  read, compile, edit, and version a methodology by conversing with the user.

---

## 1. Goal

Let a journalist **author and refine a methodology by talking to it**: paste or
point at a source document, get a first data model + DAG, then iterate
("tighten stage 5's threshold", "add a dedup stage", "redo the whole thing from
the doc"). The agent proposes; the human's belief-approval and versioning gate
what becomes trusted. It is an authoring *partner* alongside the existing web
review UI, not a replacement for human judgment.

---

## 2. The substrate that already exists (what the agent orchestrates) [EXISTS]

The agent is mostly **orchestration** — nearly every primitive already exists:

| Capability | Where [EXISTS] | Notes |
|---|---|---|
| Compile prose → DAG | `app/compiler/` (`compile_methodology`), CLI `python -m app.compiler` | Re-asks the LLM up to 3× on parse/validation failure. **CLI only — no web route yet.** |
| Write stages to disk | `app/services/compilation.py` (`write_methodology`) | Emits `compiled/NN_<id>.yaml` + `methodology_raw.md` + `compiler_result.json` (audit). |
| Validate a stage | `app/models` (`validate_stage` / `validate_methodology`) | The node-edit path rejects an invalid spec; the file is never touched. |
| Edit one stage | `app/web/routers` node-edit route | The **only writer** to `compiled/` after compile; validates first. |
| Node belief approval | `app/services/node_review.py`; `node_decisions.parquet` | States: `approved`, `unreviewed`, `rejected`, `edited_stale`. Approval keys on a content-hash of the stage spec, so **editing a stage auto-drops it to `edited_stale`** — no separate dirty flag. |
| Version snapshot | `app/services/versioning.py` (`create_version`); `versions/<id>/version.json` | Copies live `compiled/` (+ `schemas/` if present); freezes coverage. |
| Chat spine | `app/chat/` — `turns.py` (detached turn + SSE + replay), `store.py` (transcript persistence), `templates/chat.html` (renders thinking / text / tool / tool-result), `router.py` | Engine-agnostic; consumes a **normalized event** stream (see §5). |
| Claude event mapping | `app/runtime/llm_agent_sdk.py` | Already maps `claude-agent-sdk` blocks (Thinking / Text / ToolUse / ToolResult) → the same normalized events the FE renders. |

**Bonus already-wired behaviour:** the methodology DAG view **polls node-review
status and re-colours nodes**. So when the agent edits a stage, that stage turns
amber (`edited_stale`) in the UI on its own — no new FE streaming needed for the
effect.

---

## 3. The three flows

```
 AUTHOR (cold start)                         [PLANNED]
   user → fetch_document → [doc on DISK]
        → compile(doc_path, name) → data model + DAG written to compiled/,
          every node unreviewed (amber)

 EDIT (incremental)                          [PLANNED]
   "change stage 5's threshold" → read_stage → edit_stage (validate, write)
        → that node auto-drops to edited_stale (amber) → human re-approves in UI

 REGENERATE (from scratch, on request)       [PLANNED]
   "redo it all" → create_version (snapshot the old state first!)
        → re-fetch doc to disk → compile() → overwrite compiled/, all amber

 Human-only, in the existing web UI: approve / reject nodes.
 Agent may: create_version. Agent may NOT: approve nodes.
```

---

## 4. Design decisions (locked with the user)

1. **Tool mechanism: SDK-MCP.** Tools are in-process MCP functions registered on
   `claude-agent-sdk`'s `ClaudeAgentOptions`, so they work on the **Claude
   subscription (CLI) backend with no API key**. Consequence in §5.
2. **The agent CAN `create_version`, CANNOT `approve_node`.** Versioning is a
   mechanical freeze — safe for the agent, and it lets the agent snapshot before
   a destructive regenerate. Approval is a human belief act; an agent approving
   its own edits would defeat the point.
3. **Data model = both.** The agent edits **both** the `schemas/` reference
   object **and** each stage's `output_schema`, because the reference object
   alone is not sufficient to define an executable workflow (§7).
4. **Doc stays on disk, never in the agent's context** (§6.1). The source
   document is assumed too large to hold in context.
5. **Doc is input-only and may go stale.** The agent edits `compiled/` directly;
   we accept that `compiled/` can diverge from the prose doc (the stage
   `source:` back-refs may become approximate). We do not keep them in lock-step.

---

## 5. Architecture: two engines, one reusable spine

Choosing SDK-MCP (decision 4.1) has a real consequence: **PydanticAI's tool
mechanism cannot drive the subscription backend** — the Claude CLI runs its
*own* agent loop. So the editing agent is wired **claude-agent-sdk-native**, and
PydanticAI steps aside for this surface.

```
   REUSABLE SPINE (engine-agnostic, via the normalized event shape) [EXISTS]
   turns.py (detached turn + SSE + replay) · store.py (transcript) · chat.html (FE)
            ▲                                              ▲
            │  normalized events: {thinking | text | tool_call | tool_result}
   ┌────────┴──────────┐                     ┌────────────┴─────────────────┐
   │ PydanticAI engine │  [EXISTS]           │ claude-agent-sdk engine      │  [PLANNED]
   │ API path; typed   │                     │ subscription (CLI) backend;  │
   │ tools; token-     │                     │ SDK-MCP tools; the CLI runs  │
   │ streamed thinking │                     │ the tool loop; reuse the     │
   └───────────────────┘                     │ llm_agent_sdk event mapping  │
                                             └──────────────────────────────┘
```

**What PydanticAI buys us, stated plainly:** on the **API/ship path** it earns
its keep — typed/validated tools, provider-agnosticism, forced token-streamed
thinking. On the **subscription editing path it does not** — the CLI does the
tools, so we bypass it and lean on `claude-agent-sdk` directly. The FE and
transport don't care which engine ran: they consume normalized events. This is
additive, not a rewrite of the existing PydanticAI chat.

**One small refactor [PLANNED]:** `store.py` currently persists PydanticAI
`ModelMessage` objects. Make its stored transcript **engine-neutral** (role +
parts as plain JSON) so the claude-agent-sdk engine can persist through the same
store. The FE's `summarize()` reads from this neutral shape.

---

## 6. Tools [PLANNED]

In-process SDK-MCP functions. They call the existing **service functions
directly** (in-process), not the HTTP routes — no localhost round-trip.

| Tool | Wraps [EXISTS] | R/W | Context-safety |
|---|---|---|---|
| `fetch_document(src) → {path, outline}` | writes doc to disk | write(disk) | returns a **path + cheap outline** (size, section headers), never the text |
| `read_section(path, section)` / `grep_doc(path, query)` | file read | read | returns a **bounded slice**, never the whole doc |
| `list_methodologies()` | examples scan | read | names only |
| `load_methodology(name) → DAG summary` | stage loader | read | stage ids/types/edges + review state; not full YAML |
| `read_stage(name, stage_id) → yaml` | stage loader | read | one stage (small) |
| `edit_stage(name, stage_id, yaml)` | node-edit service | write | `validate_stage` first; reject → file untouched; success → node auto-`edited_stale` |
| `edit_data_model(name, ...)` | `schemas/` writer | write | edits the reference object (§7) |
| `compile(doc_path, name)` | `compile_methodology` + `write_methodology` | write | reads doc from **disk**; writes `compiled/` |
| `create_version(name, message)` | `create_version` | write | snapshot; **allowed** |

**Not provided:** `approve_node` (human-only, decision 4.2).

### 6.1 The doc-on-disk principle
The source document never enters the agent's context. `fetch_document` lands it
on disk and returns a handle + outline; `compile` consumes the path; targeted
reads (`read_section` / `grep_doc`) return bounded slices. This keeps large docs
out of the window and keeps subscription token cost bounded.

---

## 7. Data model: why the agent edits both

- **`output_schema` (per stage) [EXISTS]** — the executable contract. The runtime
  validates each stage's output against it and checks that downstream inputs
  conform. This is what actually defines the workflow. Edited via `edit_stage`
  (it is a field of the stage YAML).
- **`schemas/` reference object [EXISTS, optional]** — shared/named type
  definitions that document the intended data model and are snapshotted by
  versioning. Useful as a reference and for review, but **not sufficient** to run
  a pipeline on its own. Edited via `edit_data_model`.

So a "data-model agent" edits the reference object; a "methodology agent" edits
stages (including their `output_schema`). Same engine, different tools + system
prompt. **To confirm before building `edit_data_model`:** exactly what the
compiler emits for `schemas/` and what its on-disk edit surface is (see §10).

---

## 8. Safety / invariants

- **Every edit validates.** `edit_stage` runs `validate_stage` and refuses to
  write an invalid spec (mirrors the existing node-edit route). Never write a
  spec that would break the DAG.
- **Regenerate snapshots first.** A from-scratch `compile` overwrites `compiled/`
  and destroys prior approvals/edits. The agent must `create_version` first (it
  can) and should confirm with the user before overwriting reviewed work.
- **No self-approval.** The agent cannot mark nodes approved; its edits land as
  `edited_stale` (amber) for a human to review.
- **Never fabricate a spec.** If the agent lacks the information to fill a stage
  (e.g. a real column, a source), it asks — it does not invent plausible values.

---

## 9. System prompts [PLANNED] (sketch)

- **Methodology agent:** "You help author/refine a methodology (a DAG of typed
  stages) for `<name>`. Read before you edit. Every edit is validated and lands
  as *unreviewed* for a human to approve — you cannot approve. Snapshot a version
  before regenerating from scratch. Never invent columns, sources, or values;
  ask."
- **Data-model agent:** same spine, scoped to the reference schemas + stage
  `output_schema`; "the reference object documents intent but does not run the
  pipeline — a stage's `output_schema` is the executable contract."

The host builds a `ChatEngine`/agent with the `<name>` bound and only that
context's tools — the "tools plugged in per embedding context" pattern.

---

## 10. Open questions / to confirm before coding

1. **`schemas/` edit surface** — does the compiler emit `schemas/`, and what is
   its exact on-disk shape? Decides whether `edit_data_model` is a real tool or a
   focused prompt over `edit_stage`'s `output_schema`.
2. **`store.py` neutral transcript** — settle the engine-neutral stored shape so
   both engines persist through one store.
3. **Where `compile` writes** — straight into live `examples/<name>/compiled/`
   (all amber, review state *is* the staging) vs a staging dir the human
   promotes. Leaning: straight into `compiled/`, since the amber review state
   already serves as staging.

---

## 11. Build plan (phased)

1. **Engine + tool host.** Add the claude-agent-sdk engine (SDK-MCP server
   registration, CLI tool loop, reuse `llm_agent_sdk` event mapping). Neutralize
   `store.py`. Reuse `turns.py` + `chat.html` unchanged.
2. **Read + edit tools.** `read_stage`, `edit_stage` (→ node-edit service),
   `edit_data_model`, `load_methodology`, `list_methodologies`. Verify an edit
   turns the node amber in the DAG view.
3. **Author + regenerate.** `fetch_document` (doc-on-disk), `read_section` /
   `grep_doc`, `compile`, `create_version`. Wire the destructive-regenerate
   guard.
4. **Embed in context.** Mount the methodology and data-model agents as
   per-methodology chat embeddings with only their context's tools + prompt.
