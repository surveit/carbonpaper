"""The review queue's pager (app/static/queue-paginate.js), exercised in node.

A card is opaque to the module, so these pass strings where the page passes
elements. The DOM wiring in queue.html is the part these tests do not reach.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_CLIENT = Path(__file__).resolve().parents[1] / "app" / "static" / "queue-paginate.js"

# n undecided cards, in review order, as the page builds them from the template.
_CARDS_JS = """
function cards(n, prefix) {
  var out = [];
  for (var i = 0; i < n; i++) {
    out.push({fingerprint: 'fp' + i, card: (prefix || 'undecided') + '-' + i});
  }
  return out;
}
"""


def _run_in_node(script: str, tmp_path: Path) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to exercise app/static/queue-paginate.js")
    probe = tmp_path / "probe.js"
    probe.write_text(
        f"const client = require({json.dumps(str(_CLIENT))});\n{_CARDS_JS}\n{script}",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(probe)], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise AssertionError(f"node exited {result.returncode}:\n{result.stderr}")
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


def test_the_page_size_is_one_named_constant(tmp_path):
    out = _run_in_node(
        "console.log(JSON.stringify({size: client.QUEUE_PAGE_SIZE}));", tmp_path
    )
    assert out["size"] == 25


def test_a_page_holds_its_own_slice_of_the_queue_in_review_order(tmp_path):
    out = _run_in_node("""
      var pager = client.createQueuePager(cards(60), 25);
      console.log(JSON.stringify({
        first: pager.cardsOnPage(0),
        second: pager.cardsOnPage(1),
        last: pager.cardsOnPage(2),
        pages: pager.pageCount(),
        total: pager.total(),
      }));
    """, tmp_path)

    assert out["pages"] == 3 and out["total"] == 60
    # Page 1 is the FIRST 25 rows of the queue as the server ordered it — which is
    # what a declared queue.sort buys: the rows worth reading first are on it.
    assert out["first"] == [f"undecided-{i}" for i in range(25)]
    assert out["second"] == [f"undecided-{i}" for i in range(25, 50)]
    assert out["last"] == [f"undecided-{i}" for i in range(50, 60)]


@pytest.mark.parametrize(
    "rows, pages",
    [(0, 1), (1, 1), (24, 1), (25, 1), (26, 2), (2000, 80), (2001, 81)],
)
def test_the_last_page_is_a_partial_one_and_an_empty_queue_is_still_one_page(
    tmp_path, rows, pages
):
    out = _run_in_node(f"""
      var pager = client.createQueuePager(cards({rows}), 25);
      console.log(JSON.stringify({{pages: pager.pageCount()}}));
    """, tmp_path)

    assert out["pages"] == pages


def test_a_page_index_past_either_end_lands_on_the_page_that_exists(tmp_path):
    out = _run_in_node("""
      var pager = client.createQueuePager(cards(60), 25);
      console.log(JSON.stringify({
        before: pager.clampPage(-3),
        past: pager.clampPage(99),
        unparseable: pager.clampPage(NaN),
        cardsPast: pager.cardsOnPage(99).length,
      }));
    """, tmp_path)

    assert out == {"before": 0, "past": 2, "unparseable": 0, "cardsPast": 10}


def test_the_readout_counts_rows_the_way_the_card_headers_do(tmp_path):
    """The cards say "Row 26 of 2000"; the readout must not renumber per page."""
    out = _run_in_node("""
      var pager = client.createQueuePager(cards(2000), 25);
      console.log(JSON.stringify({
        first: pager.describePage(0),
        second: pager.describePage(1),
        last: pager.describePage(79),
        empty: client.createQueuePager(cards(0), 25).describePage(0),
      }));
    """, tmp_path)

    assert out["first"]["label"] == "Page 1 of 80"
    assert out["first"]["range"] == "rows 1–25 of 2000"
    assert out["second"]["range"] == "rows 26–50 of 2000"
    assert out["last"] == {
        "page": 80, "pages": 80, "first": 1976, "last": 2000, "total": 2000,
        "label": "Page 80 of 80", "range": "rows 1976–2000 of 2000",
    }
    assert out["empty"]["range"] == "rows 0–0 of 0"


def test_a_decided_card_is_still_decided_after_paging_away_and_back(tmp_path):
    """The composition point: the pager, not the live list, is a card's current state."""
    out = _run_in_node("""
      var pager = client.createQueuePager(cards(60), 25);
      var position = pager.replaceCard('fp3', 'decided-3');
      console.log(JSON.stringify({
        position: position,
        pagedAway: pager.cardsOnPage(1).indexOf('decided-3'),
        pagedBack: pager.cardsOnPage(0)[3],
        neighbours: [pager.cardsOnPage(0)[2], pager.cardsOnPage(0)[4]],
        total: pager.total(),
      }));
    """, tmp_path)

    assert out["position"] == 3          # it keeps its place in the review order
    assert out["pagedAway"] == -1        # and is not smuggled onto another page
    assert out["pagedBack"] == "decided-3"
    assert out["neighbours"] == ["undecided-2", "undecided-4"]
    assert out["total"] == 60            # a swap replaces a card, never adds one


def test_a_card_on_a_page_the_reviewer_is_not_looking_at_can_be_replaced(tmp_path):
    out = _run_in_node("""
      var pager = client.createQueuePager(cards(60), 25);
      pager.replaceCard('fp40', 'decided-40');
      console.log(JSON.stringify({onItsPage: pager.cardsOnPage(1)[15]}));
    """, tmp_path)

    assert out["onItsPage"] == "decided-40"


def test_replacing_a_card_the_queue_does_not_carry_raises(tmp_path):
    """Silence here would read as an undecided row on the reviewer's next visit to the page."""
    out = _run_in_node("""
      var pager = client.createQueuePager(cards(60), 25);
      var message = null;
      try { pager.replaceCard('fp-nope', 'decided'); } catch (err) { message = err.message; }
      console.log(JSON.stringify({message: message}));
    """, tmp_path)

    assert out["message"] is not None and "fp-nope" in out["message"]


def test_a_page_size_that_is_not_a_positive_number_raises(tmp_path):
    out = _run_in_node("""
      var messages = [0, -1, undefined].map(function (size) {
        try { client.createQueuePager(cards(3), size); return null; }
        catch (err) { return err.message; }
      });
      console.log(JSON.stringify({messages: messages}));
    """, tmp_path)

    assert all(message is not None for message in out["messages"])
