"""The outer pipeline's claims step; its source is the stage's inline code."""
import json
import re
import time
import urllib.error
import urllib.request

# The rendered stage code sets this last; the bare module refuses to call anything.
MCP_URL = None
POLL_SECONDS = 5
REVIEW_DEADLINE_SECONDS = 900
MAX_REVIEWS_IN_FLIGHT = 2


class ToolError(ValueError):
    pass


def transform(row):
    if not row["citation_holds"]:
        return {"claims_json": "[]", "claims_skipped_json": "{}"}
    if not row["process_ok"]:
        answer = json.loads(row["results_json"] or "{}")
        skipped = {field: "the judge did not trust the comparison" for field in sorted(answer)}
        return {"claims_json": "[]", "claims_skipped_json": json.dumps(skipped)}
    match = re.search(r"/project/([^/]+)/runs/([^/?#]+)", str(row["run_url"]))
    if match is None:
        raise ValueError("the run URL names no project and run: " + str(row["run_url"]))
    project, run = match.group(1), match.group(2)
    to_claim, skipped = choose_figures(row)
    claims, in_flight = [], []
    for field, quote in to_claim:
        while len(in_flight) >= MAX_REVIEWS_IN_FLIGHT:
            in_flight = poll_reviews(project, in_flight)
        claim = submit_one(project, run, field, quote)
        claims.append(claim)
        if claim["claim_id"] is not None:
            in_flight.append(claim)
    while in_flight:
        in_flight = poll_reviews(project, in_flight)
    return {"claims_json": json.dumps(claims), "claims_skipped_json": json.dumps(skipped)}


def choose_figures(row):
    answer = json.loads(row["results_json"] or "{}")
    quotes = json.loads(row["expected_quotes_json"] or "{}")
    our_defects = {r["field"] for r in json.loads(row["diagnosis"] or "[]")
                   if r.get("verdict") == "our_defect"}
    to_claim, skipped = [], {}
    for field in sorted(answer):
        if answer[field] is None:
            skipped[field] = "the rebuild wrote no value"
        elif field in our_defects:
            skipped[field] = "ruled our_defect"
        elif not quotes.get(field):
            skipped[field] = "no quote recorded"
        else:
            to_claim.append((field, quotes[field]))
    for field in sorted(set(quotes) - set(answer)):
        skipped[field] = "not in the rebuild's answer"
    return to_claim, skipped


def submit_one(project, run, field, quote):
    slug = field.replace("_", "-")
    try:
        made = call_tool("submit_claim", {"project_id": project, "run_id": run, "slug": slug,
                                          "text": quote, "context": None})
    except ToolError as refusal:
        return {"field": field, "slug": slug, "claim_id": None, "claim_url": None,
                "review": "none", "challenges": [], "error": str(refusal)}
    except (OSError, ValueError) as failure:
        if refuses_connection(failure):
            raise
        return {"field": field, "slug": slug, "claim_id": None, "claim_url": None,
                "review": "none", "challenges": [],
                "error": "the call failed in transit; the claim may exist, so it was not "
                         "resubmitted: " + str(failure)}
    return {"field": field, "slug": slug, "claim_id": made["claim"]["id"],
            "claim_url": made["claim_url"], "review": "running", "challenges": [],
            "error": None, "deadline": time.monotonic() + REVIEW_DEADLINE_SECONDS}


def refuses_connection(error):
    if isinstance(error, ConnectionRefusedError):
        return True
    # urlopen wraps a transport-level refusal in URLError, with the refusal as .reason.
    return isinstance(error, urllib.error.URLError) and isinstance(
        error.reason, ConnectionRefusedError)


def poll_reviews(project, in_flight):
    still = []
    for claim in in_flight:
        try:
            review = call_tool("read_claim_review",
                               {"project_id": project, "claim_id": claim["claim_id"]})
        except (OSError, ValueError) as failure:
            if refuses_connection(failure):
                raise
            claim.update(review="unknown", error=str(failure))
            claim.pop("deadline", None)
            continue
        claim.update(review=review["review"], challenges=review.get("challenges") or [],
                     error=review.get("error"))
        if claim["review"] == "running" and time.monotonic() > claim["deadline"]:
            claim.update(review="timed_out", error="the review was still running at the deadline")
        if claim["review"] == "running":
            still.append(claim)
        else:
            claim.pop("deadline", None)
    if still:
        time.sleep(POLL_SECONDS)
    return still


def call_tool(name, arguments):
    if MCP_URL is None:
        raise ValueError("MCP_URL is unset: render this module with render_claims_code")
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    request = urllib.request.Request(MCP_URL, data=json.dumps(body).encode("utf-8"), headers={
        "content-type": "application/json", "accept": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as reply:
        answer = json.loads(reply.read())
    if "result" not in answer:
        raise ToolError(name + " was refused: " + json.dumps(answer.get("error")))
    if answer["result"].get("isError"):
        raise ToolError(" ".join(p.get("text", "") for p in answer["result"]["content"]))
    return answer["result"]["structuredContent"]
