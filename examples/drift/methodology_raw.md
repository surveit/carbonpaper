# Drift methodology

Narrative-discovery pipeline. For each member of Congress with material
in two time windows, asks the LLM to identify drift: topical shifts,
stance changes, new talking points, abandoned ones. Ranks members by
notability and surfaces the top candidates as story leads with cited
quotes.

This is NOT a scoring pipeline. There are no benchmarks, no per-issue
scores, no per-entity profile pages. The terminal artifact is a ranked
list of story candidates.

## §1 Universe selection

Members of Congress active in BOTH windows (≥5 press releases each).
This excludes new members and retired ones — drift can only be
detected against a baseline.

## §2 Two-window press corpora

Press releases from the methodology's earlier window (e.g., 2025-09) and
recent window (e.g., 2026-01). Both are scraped JSONL records keyed by
`bioguide_id`. Window dates are configurable per run.

## §3 Per-member input pairing

A python_transform builds one row per member containing:
- member identity (name, party, state, chamber)
- list of {title, body excerpt, date, url} for the early window (up to 30, sorted by date)
- same for the recent window

Bodies are truncated to keep total prompt size bounded. Title + first
1500 chars suffices to detect drift.

## §4 Drift detection (LLM)

For each member, the LLM reads both windows and emits a structured JSON
report:

- `notability_score`: 0-10, the LLM's assessment of how
  newsworthy/surprising the drift is. 0 = no meaningful change. 10 =
  hard pivot or self-contradiction.
- `topical_drift`: list of topics whose mention frequency changed
  significantly between windows. Each entry has direction (added /
  dropped / intensified / diminished) and a one-line explanation.
- `stance_drift`: list of topics where the *position* changed, with
  before-text and after-text quotes for evidence.
- `new_talking_points`: phrases that appear in recent that didn't in
  early. Up to 5.
- `abandoned_talking_points`: phrases that appeared early but vanished.
- `story_hypothesis`: one paragraph proposing a journalistic question
  worth investigating, or "no meaningful drift" if score is low.

The LLM is told explicitly: do NOT speculate beyond the supplied
quotes; only assert drift if you can cite specific evidence.

## §5 Notability ranking

A python_transform sorts members by `notability_score` descending and
attaches metadata (party, chamber, total releases). Output is the
ranked list, top-N selected for publish.

## §6 Publish ranked stories

For each story candidate above a notability threshold:
- Render a card with member, score, story hypothesis, and the cited
  quotes
- Build an index page that sorts/filters by score, party, chamber

The index is the front door — not per-member profiles. A journalist
picks a story from the index, reads the card, and drills into the
quotes to verify.
