"""Prompts and stage code shared by the eval generator and the eval runner."""
import os
from pathlib import Path

DATA = Path(__file__).resolve().parent
SOURCES = DATA / "sources"
ITEMS = DATA / "items"
FETCHED = DATA / "fetched"
BASE = "http://127.0.0.1:8944"
MCP = f"{BASE}/mcp"
CHROME_ENV = "REBUILD_CHECK_CHROME"
CHROME_LOCATIONS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
)


def col(name, type_, nullable=True, description=None):
    out = {"name": name, "type": type_, "nullable": nullable}
    if description:
        out["description"] = description
    return out


def resolve_chrome():
    override = os.environ.get(CHROME_ENV)
    if override:
        if not Path(override).is_file():
            raise FileNotFoundError(f"{CHROME_ENV}={override!r} names no file")
        return Path(override)
    for location in CHROME_LOCATIONS:
        if location.is_file():
            return location
    raise FileNotFoundError(
        f"no Chrome found: {CHROME_ENV} is unset and none of these exists: "
        + "; ".join(str(location) for location in CHROME_LOCATIONS))


def render_read_instructions(chrome):
    return f"""You read one published data artifact and write down what it states.

Your place in the system: a separate pipeline will later compute these same quantities from the
artifact's own cited sources, without ever seeing what you write here, and the two will be
compared. So a figure you record wrongly becomes a false accusation against the author. Copy,
never infer: if the page does not state a number, do not supply one.

The artifact is a GitHub repository holding a published page. You have Bash.

## Settle whether this is possible before you do anything else

Do NOT clone yet. List the repository's files over the API, which costs one call and no disk:

  gh api repos/<owner>/<repo>/git/trees/HEAD?recursive=1 --jq '.tree[].path'

Look for committed tabular data — .csv, .tsv, .json, .parquet, .xlsx — that the page's figures
could be computed from. Build manifests and tool configuration are not data, whatever their
extension: `package.json`, `package-lock.json`, `tsconfig.json`, `composer.json`, `bower.json`
and similar files (linter, bundler, editor and CI settings) say how the code is built, not
what the page reports. A JavaScript repository's manifests are not a dataset; if they are the
only .json files it commits, it commits no data.

If there is no data, return immediately with `unusable_because` filled in and every other field
empty. Do not go looking for a way to make it work: a repository that commits no data cannot
produce an item, and hunting for one burns the run for every other artifact in the batch.

## Only then, read the page

1. Fetch only what you need. A shallow clone if you must:

     git clone --depth 1 --filter=blob:none <repo> /tmp/<name>

   but a single raw file is usually enough, and a full clone of a large repository is never
   worth it.
2. Its figures may be computed in the browser and absent from the file, so RENDER it — and cap
   it, because a headless browser that hangs hangs the whole batch:

     timeout 60 "{chrome.as_posix()}" --headless --disable-gpu --no-sandbox \
       --virtual-time-budget=10000 --dump-dom <file-or-url> > /tmp/rendered.html

   then strip tags to read the prose. A number present only after rendering is still a claim
   the page makes.
3. Find the direct URLs of the data files the page is computed from.

Cap every command you run this way. Nothing you do here should take more than a minute; if
something does, it has failed, and a recorded `unusable_because` beats a hung run.

Record every quantity the page states about its own dataset, as a field name you invent that
says what the quantity IS, in snake_case. Record where on the page each appeared, and copy the
text that states it.

Return JSON with exactly these fields:
1. "artifact_title": the page's own title.
2. "claims": an object of your field names to the value each one states, typed as the page
   states it — an integer for a count, a number for a rate, a string for a date or a label.
   Do not coerce: a date is a string, not a number.
3. "claim_locations": an object of the same field names to where each appeared, e.g. "About tab
   prose" or "COTS tab heading". Two places disagreeing about one quantity is worth recording
   as two fields.
4. "claim_quotes": an object of the same field names to the exact text in which the page states
   each figure. Copy it character for character: the sentence, or for a table cell, KPI tile or
   chart label, the label and the figure exactly as displayed. Never paraphrase. Where a figure
   appears in several places, quote the first place listed in `claim_locations`.
5. "unusable_because": one sentence saying why this artifact cannot become an eval item, or
   null where it can. Say no whenever saying yes would be a guess: the repository commits no
   data, the figures are fetched live from a service, the page states no figure of its own,
   or the data is not tabular. A recorded no is a useful result; an item built on a guess is
   worse than nothing, because something downstream will be graded against it.
6. "source_urls": semicolon-separated DIRECT URLs to the machine-readable data files the page
   is computed from — a URL that downloads a .csv, .tsv, .json, .parquet or .xlsx and nothing
   else. Not a repository page, not a releases page, not a README, not documentation, not the
   page itself. A repository is where data lives; it is not data. If the page names a
   repository, find the individual files inside it and give their direct download URLs. If you
   cannot find a direct file URL, return an empty string rather than a page that merely
   mentions the data — a page gets read as a source downstream, and its prose becomes numbers
   nobody computed.

Worked example, for a page whose KPI tile reads "Committees 88" and whose About text reads
"1,204 filings from 88 committees, as of 13 April 2026.":
{{"artifact_title": "Committee Filings Explorer",
  "claims": {{"filing_rows": 1204, "committees_filing": 88, "snapshot_date": "13 April 2026"}},
  "claim_locations": {{"filing_rows": "About tab prose",
                      "committees_filing": "KPI tile; About tab prose",
                      "snapshot_date": "About tab prose"}},
  "claim_quotes": {{"filing_rows": "1,204 filings from 88 committees, as of 13 April 2026.",
                   "committees_filing": "Committees 88",
                   "snapshot_date": "1,204 filings from 88 committees, as of 13 April 2026."}},
  "unusable_because": null,
  "source_urls": "https://raw.githubusercontent.com/org/repo/main/data/filings.csv"}}

Worked example of an artifact that cannot become an item:
{{"artifact_title": "Illegal Tobacco Tracker",
  "claims": {{}},
  "claim_locations": {{}},
  "claim_quotes": {{}},
  "unusable_because": "The repository commits no data; the chart fetches its only dataset from the newsroom CMS at runtime, so no figure here can be recomputed from anything committed.",
  "source_urls": ""}}
"""

SCHEMA_INSTRUCTIONS = """You turn a list of quantities into the shape of an answer.

Your place in the system: a pipeline that has never seen this artifact will be handed your
schema and its cited sources, and asked to fill the schema in. Your schema is the ONLY thing
telling it which quantities matter. It must therefore carry no figure, no range, no example
value and no hint of magnitude — a number anywhere in it tells the build what to find, and the
comparison downstream becomes worthless.

Write a JSON Schema object with one property per quantity, keeping the field names you are
given EXACTLY as given, and a `description` for each saying what to compute in plain words.
Give each the JSON Schema type matching the value it was given — `integer` for a whole count,
`number` for a rate, `string` for a date or label. Mark them all required.

The names are not yours to improve. They are the key the answer is compared on, so renaming a
quantity makes it uncomparable even when the figure is right.

Return JSON with exactly this field:
1. "target_schema": the JSON Schema, as a JSON string.

Worked example, given fields filing_rows and committees_filing:
{"target_schema": "{\\"type\\": \\"object\\", \\"properties\\": {\\"filing_rows\\": {\\"type\\": \\"integer\\", \\"description\\": \\"Rows in the filings file.\\"}, \\"committees_filing\\": {\\"type\\": \\"integer\\", \\"description\\": \\"Distinct committees appearing in it.\\"}}, \\"required\\": [\\"filing_rows\\", \\"committees_filing\\"]}"}
"""

SCHEMA_GUARD_CODE = '''
import json
import re


def transform(row):
    schema, claims = row["target_schema"], json.loads(row["claims_json"])
    leaked = sorted({str(v) for v in claims.values() if str(v) in re.findall(r"\\d+", schema)})
    if leaked:
        raise ValueError(
            f"the target schema carries claimed value(s) {leaked}; it would tell the build "
            f"what to find")
    return {"schema_is_clean": True}
'''

FETCH_SOURCES_CODE = '''
TABULAR_SUFFIXES = (".csv", ".tsv", ".json", ".parquet", ".xlsx", ".xls")


def refuse_non_tabular(urls):
    for url in urls:
        path = urllib.parse.urlparse(url).path.lower()
        if not path.endswith(TABULAR_SUFFIXES):
            raise ValueError(
                "source " + url + " does not name a tabular data file; a page read as a "
                "source turns its prose into numbers nobody computed")


import hashlib
import pathlib
import re
import urllib.parse
import urllib.request

''' + f"FETCH_ROOT = pathlib.Path({FETCHED.as_posix()!r})\n" + '''

def transform(row):
    if row["unusable_because"]:
        return nothing_fetched()
    urls = [part.strip() for part in (row["source_urls"] or "").split(";") if part.strip()]
    if not urls:
        return nothing_fetched()
    refuse_non_tabular(urls)
    directory = FETCH_ROOT / re.sub(r"[^A-Za-z0-9]+", "_", row["repo_url"]).strip("_")
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    digests = []
    for position, url in enumerate(urls):
        body = fetch(url)
        path = directory / name_file(position, url)
        path.write_bytes(body)
        paths.append(str(path))
        digests.append(hashlib.sha256(body).hexdigest())
    return {"local_source_paths": ";".join(paths),
            "source_digests": ";".join(digests),
            "sources_fetched": len(paths)}


def nothing_fetched():
    return {"local_source_paths": None, "source_digests": None, "sources_fetched": 0}


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "carbon-paper-blind-fetch"})
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read()
    if not body:
        raise ValueError(url + " answered with an empty body; a stub is not a source")
    return body


def name_file(position, url):
    tail = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    return "%02d_%s" % (position, re.sub(r"[^A-Za-z0-9._-]+", "_", tail) or "source")
'''

BUILD_INSTRUCTIONS = f"""You are a reporter sitting down with a dataset for the first time.

You are given the paths of source files already sitting on this machine's disk, and a JSON
schema describing the shape of the answer wanted from them. Work out each field from those
files and return it. That is the whole job.

Nobody has told you what any of these numbers should be, and nothing you produce is being
compared to a figure you have not seen. Do not look for one, and do not shape an answer towards
what seems plausible. Compute what the files support.

## What you are actually producing

Not the answer. The answer is the cheap part and it is not what anyone opens.

What a journalist opens is the pipeline. They click a figure, and walk back stage by stage to
the rows in the source file it was computed from. That walk is the entire product. A figure
they cannot walk back is worth nothing to them however right it is, and a figure they can walk
back is useful even when it turns out to be wrong, because they can see where it went wrong.

So the pipeline must BEGIN at the source files themselves: an `input_data` stage reading the
given paths directly, and every figure computed from there by later stages. Not a file you
computed and saved and then loaded. If the first stage loads your own answer, the walk back
ends at arithmetic nobody can see, and what you have built is a picture of a result rather
than the working that produced one — which is worse than returning nothing, because it looks checked.

If a stage type fights you, that is the work. Read its schema, look at how an existing project
uses it, and keep going. Computing the answer another way and wrapping it in a pipeline is the
one outcome that fails.

## How

The source files are already on disk. An earlier stage fetched them and handed you their
paths, so there is nothing to download. Those files are the ONLY data you may read: do not
fetch anything over the network, do not clone or browse a repository, do not search the web,
and do not open any URL. Your Bash is for two things — reading the files at the given paths,
and driving the MCP endpoint below. Point your pipeline's `input_data` stage at those paths.

Reaching past the given files is the one thing that voids this work, whatever it turns up.

The workspace speaks MCP over HTTP at {MCP}:

  curl -s -X POST {MCP} -H 'content-type: application/json' -H 'accept: application/json' \\
    -d '{{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{{"name":"<tool>","arguments":{{...}}}}}}'

Before your first call, list what the workspace offers and read the argument schema of every
tool you mean to use:

  curl -s -X POST {MCP} -H 'content-type: application/json' -H 'accept: application/json' \\
    -d '{{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{{}}}}'

Each tool carries an `inputSchema` naming its required arguments and the shape of each. Read
those rather than guessing and correcting from error messages: a validation error names one
missing field at a time and some arguments nest several levels, so guessing will not converge.

You will need to create a project, open it for editing, add its stages, save a version, run it,
and read the output rows back. Look at what the source files hold before writing a stage over
one — never guess a column name.

Every number you return must be read back out of a stage that ran.

## When the data does not settle it

Some fields turn on a judgement the files do not make for you. Choose, compute, and say plainly
in `method` what you chose. Return one figure per field.

## You do not hand back the figures

You hand back the run. Nothing you type is read as an answer — the figures are read out of your
pipeline afterwards, by machine. A number you worked out another way has nowhere to go.

So your pipeline's LAST stage must be called `answer`, and must output exactly ONE row whose
columns are the field names in the schema,
each holding the type the schema declares for that field. That row is the deliverable. If the
run that writes it did not finish, there is no answer, whatever you have in a scratch file.

If you cannot get there, say so. Returning `run_url` as null with `blocked_by` filled in is a
legitimate outcome and a useful one — a stage type that will not take your data, a column the
file does not have, a schema field the sources cannot answer. Report the wall you hit. Do not
report a run that did not finish, and do not quietly narrow the schema to make one finish.

Return JSON with exactly these fields:
1. "run_url": the full URL of the finished run whose `answer` stage holds the figures, as
   {BASE}/project/<project_id>/runs/<run_id> — or null if you could not build one.
2. "method": two or three sentences on how the pipeline computes them, naming any judgement you
   had to make. Where you are blocked, say what you tried.
3. "blocked_by": one sentence naming what stopped you, or null where the run finished.

Worked example of a finished build:
{{"run_url": "{BASE}/project/20260917T090102.334455/runs/20260917T090108.771122",
  "method": "Loaded filings.csv, counted rows, and counted distinct committee_id in the answer stage. 41 committee names differ only by a trailing 'Inc.'; I kept them separate because the file gives no identifier saying otherwise.",
  "blocked_by": null}}

Worked example of a blocked build:
{{"run_url": null,
  "method": "Loaded filings.csv and tried to count distinct committees per quarter.",
  "blocked_by": "The file has no quarter column and no date I could compute one from, so committees_per_quarter cannot be answered from the sources given."}}
"""

VERIFY_CODE = '''
import json
import re
import urllib.request


def transform(row):
    url = row["run_url"]
    if not url:
        return refuse("not_built", row["blocked_by"] or "the build reported no run")
    match = re.search(r"/project/([^/]+)/runs/([^/?#]+)", str(url))
    if match is None:
        return refuse("unparseable_url", "the run URL does not name a project and a run")
    project, run = match.group(1), match.group(2)
    if project == run:
        return refuse("run_is_project_id", "the URL repeats the project id as the run id")
    status = read_status(project, run)
    if status is None:
        return refuse("no_run", "no run answers at that URL")
    if status.get("status") != "ok":
        return refuse("run_did_not_finish",
                      "the cited run ended " + str(status.get("status")))
    return {"citation_holds": True, "citation_failure": None,
            "citation_note": "the cited run finished ok"}


def read_status(project, run):
    address = ("http://127.0.0.1:8944/project/" + project + "/runs/" + run + "/status")
    try:
        with urllib.request.urlopen(address, timeout=30) as reply:
            return json.loads(reply.read())
    except urllib.error.HTTPError:
        return None


def refuse(kind, note):
    return {"citation_holds": False, "citation_failure": kind, "citation_note": note}
'''

READ_ANSWER_CODE = f"MCP_URL = {MCP!r}\n" + '''
import json
import re
import urllib.request


def transform(row):
    if not row["citation_holds"]:
        return {"results_json": None, "answer_stage_rows": None}
    match = re.search(r"/project/([^/]+)/runs/([^/?#]+)", str(row["run_url"]))
    answer = read_answer_stage(match.group(1), match.group(2))
    if answer["row_count"] != 1:
        raise ValueError(
            "the `answer` stage wrote " + str(answer["row_count"]) + " rows; it must write "
            "exactly one")
    # Each value arrives JSON-typed as the answer stage wrote it: an int stays an int.
    record = answer["rows"][0]["values"]
    return {"results_json": json.dumps(record), "answer_stage_rows": answer["row_count"]}


# The server the build ran on finds the run under its own projects root.
def read_answer_stage(project, run):
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "read_stage_output_rows",
        "arguments": {"project_id": project, "run_id": run, "stage_id": "answer"}}}
    request = urllib.request.Request(
        MCP_URL, data=json.dumps(call).encode("utf-8"),
        headers={"content-type": "application/json", "accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as reply:
        body = json.loads(reply.read())
    if "result" not in body:
        raise ValueError("the workspace refused the read: " + json.dumps(body.get("error")))
    if body["result"].get("isError"):
        raise ValueError(
            "the cited run finished but its `answer` stage could not be read, so the figures "
            "have nowhere to come from: "
            + " ".join(part.get("text", "") for part in body["result"]["content"]))
    return body["result"]["structuredContent"]
'''

FIELD_NAMES_CODE = '''
import json


def transform(row):
    expected = json.loads(row["expected_json"]) if row["expected_json"] else {}
    computed = json.loads(row["results_json"]) if row["results_json"] else {}
    return {"field": sorted(set(expected) | set(computed))}
'''

FIELD_VERDICT_CODE = '''
def transform(row):
    if row["computed_value"] == None:
        return {"verdict": "not_computed"}
    if row["expected_value"] == None:
        return {"verdict": "not_expected"}
    if row["type_matches"] == False:
        return {"verdict": "wrong_type"}
    if row["expected_value"] == row["computed_value"]:
        return {"verdict": "agrees"}
    return {"verdict": "differs"}
'''

FIELD_VALUES_CODE = '''
import json


def transform(row):
    field = row["field"]
    expected = json.loads(row["expected_json"]) if row["expected_json"] else {}
    computed = json.loads(row["results_json"]) if row["results_json"] else {}
    declared = declared_type(row["target_schema"], field)
    return {"expected_value": render(expected.get(field)),
            "computed_value": render(computed.get(field)),
            "declared_type": declared,
            "type_matches": fits(computed.get(field), declared)}


def declared_type(schema_text, field):
    schema = json.loads(schema_text) if schema_text else {}
    return ((schema.get("properties") or {}).get(field) or {}).get("type")


def fits(value, declared):
    if value is None or declared is None:
        return None
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "string":
        return isinstance(value, str)
    return None


def render(value):
    return None if value is None else json.dumps(value)
'''

RENDER_CODE = '''
def transform(row):
    lines = []
    for i in range(len(row["fields"])):
        lines.append("- " + row["fields"][i]
                     + ": expected " + str(row["expected_values"][i])
                     + ", rebuild " + str(row["computed_values"][i])
                     + " -> " + row["verdicts"][i])
    return {"findings_text": chr(10).join(lines)}
'''

DIAGNOSE_INSTRUCTIONS = """You read the whole picture of one rebuild and say what went wrong.

Your place in the system: a published artifact states figures. A pipeline computed the same
quantities from the artifact's own cited sources without ever seeing those figures. Deterministic
stages compared them and checked whether the run the rebuild cites actually exists. You see all
of it. Nothing downstream re-checks you, and a person reads this to decide whether to contact
the author — so a wrong call costs someone's attention and possibly their goodwill.

Every disagreement resolves exactly three ways, and you must not prefer one:
- OUR DEFECT: the rebuild is wrong — misread a column, took a reading the data does not support,
  joined on the wrong key, or did not really run.
- THEIR DEFECT: the artifact states something its own cited source does not support.
- GENUINE AMBIGUITY: both readings are defensible and the source does not settle it.

Two things to weigh:
- Read the rebuild's method before judging. A difference whose cause is stated plainly there is
  usually ambiguity or our defect, not theirs.
- Read WHERE each figure appears. A page that states one number in its prose and a different one
  in a table is disagreeing with itself, and that is evidence of their defect rather than of
  ambiguity.

If the cited run wrote no stage output, the numbers came from no reviewable pipeline and nothing
here may be reported about the author, whatever the figures say. Say that first and stop.

Return JSON with exactly these fields:
1. "process_ok": true only if the citation holds and the comparison can be trusted.
2. "headline": one sentence a person reads first.
3. "diagnosis": an array, one object per disagreeing field, each with "field", "verdict"
   (our_defect | their_defect | genuine_ambiguity) and "why" in one sentence. Where process_ok
   is false, return an empty array.
4. "next_step": what the person should do, in one sentence. Name whether the next move is on our
   side or theirs.

Worked example:
{"process_ok": true,
 "headline": "One figure looks like a slip in their prose and one is a definitional question.",
 "diagnosis": [
   {"field": "committees_filing", "verdict": "their_defect",
    "why": "The cited file holds 88 distinct committees and the About prose says 89, with nothing in the method to explain the extra one."},
   {"field": "withheld_rows", "verdict": "genuine_ambiguity",
    "why": "The rebuild counted rows marked Other as withheld and the page did not; the column does not say which is right."}],
 "next_step": "Theirs: ask which of the two withheld readings they intended, and flag the 89."}
"""


