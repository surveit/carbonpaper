"""One complete stage, shown to every authoring surface so the shapes in the type
catalog do not have to be described in prose. tests/test_worked_example.py parses it
with the real loader and runs its own corner case, so it cannot drift into a shape
that would be refused on write.
"""
from __future__ import annotations

WORKED_STAGE_EXAMPLE = """\
```json
{
  "id": "normalize_spend",
  "description": "Normalize spend",
  "type": "starlark_row_function",
  "inputs": [{"id": "filings", "schema": {"columns": [
    {"name": "filing_id", "type": "str", "nullable": false},
    {"name": "reported_amount", "type": "str", "nullable": true}
  ]}}],
  "signature": {
    "form": "extends",
    "reads": [{"input": "filings", "columns": [
      {"name": "reported_amount", "type": "str", "nullable": true}
    ]}],
    "adds": [{"name": "amount_usd", "type": "float", "nullable": true}]
  },
  "starlark": {
    "summary": "Reads `reported_amount` as US dollars, leaving it blank when there is none.",
    "corner_cases": [
      {"case": "reported_amount is blank", "expected": "amount_usd is blank too"},
      {"case": "reported_amount is not in dollars", "expected": "the step refuses the row"}
    ],
    "code": "def transform(row):\\n    reported = row['reported_amount']\\n    if reported == None:\\n        return dict(row, amount_usd = None)\\n    if not reported.startswith('$'):\\n        refuse('reported_amount %s is not US dollars' % reported)\\n    return dict(row, amount_usd = float(reported[1:].replace(',', '')))\\n"
  }
}
```"""
