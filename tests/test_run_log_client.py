"""The run page's log client (app/static/run_log.js), exercised in node.

Two things only the client can get wrong: which events a filter combination
shows, and what a reconnect re-requests. Both are pure functions here — the DOM
wiring in initRunLog() is the only part these tests do not reach.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_CLIENT = Path(__file__).resolve().parents[1] / "app" / "static" / "run_log.js"


def _run_in_node(script: str, tmp_path: Path) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to exercise app/static/run_log.js")
    probe = tmp_path / "probe.js"
    probe.write_text(
        f"const client = require({json.dumps(str(_CLIENT))});\n{script}",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(probe)], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise AssertionError(f"node exited {result.returncode}:\n{result.stderr}")
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


_EVENTS_JS = """
const events = [
  {seq: 0, kind: 'row_ok', stage: 's', row: 0, level: 0, source: 'computed'},
  {seq: 1, kind: 'llm_error', stage: 's', row: 1, level: 1, text: 'model refused'},
  {seq: 2, kind: 'row_error', stage: 's', row: 1, level: 0, text: 'row blew up'},
  {seq: 3, kind: 'llm_prompt', stage: 's', row: 2, level: 1, text: 'score it'},
];
"""


def test_errors_only_surfaces_an_llm_error_while_llm_detail_is_off(tmp_path):
    """An llm_error is a detail-level event, so filtering by detail first would hide every one."""
    out = _run_in_node(_EVENTS_JS + """
      console.log(JSON.stringify({
        html: client.renderEvents(events, {errorsOnly: true, detail: false}),
      }));
    """, tmp_path)

    assert "model refused" in out["html"]
    assert "row blew up" in out["html"]
    assert "score it" not in out["html"]      # a non-error detail event stays hidden
    assert "row_ok" not in out["html"]


def test_the_default_view_hides_detail_and_keeps_the_lifecycle_spine(tmp_path):
    out = _run_in_node(_EVENTS_JS + """
      console.log(JSON.stringify({
        kinds: client.selectVisibleEvents(events, {errorsOnly: false, detail: false})
          .map(e => e.kind),
        withDetail: client.selectVisibleEvents(events, {errorsOnly: false, detail: true})
          .map(e => e.kind),
      }));
    """, tmp_path)

    assert out["kinds"] == ["row_ok", "row_error"]
    assert out["withDetail"] == ["row_ok", "llm_error", "row_error", "llm_prompt"]


_STREAM_JS = """
const opened = [];
const scheduled = [];
const received = [];
const states = [];
let current = null;

function openStream(url) {
  opened.push(url);
  current = {onmessage: null, onerror: null, closed: false,
             addEventListener() {}, close() { this.closed = true; }};
  return current;
}
const send = (seq, kind) => current.onmessage(
  {data: JSON.stringify({seq: seq, kind: kind || 'row_ok', level: 0})});

client.openRunLogStream({
  url: '/events', openStream: openStream,
  schedule: (fn) => scheduled.push(fn),
  onEvent: (ev) => received.push(ev.seq),
  onState: (s) => states.push(s),
});
"""


def test_a_reconnect_resumes_at_the_cursor_and_never_duplicates(tmp_path):
    """An EventSource retries the URL it was built with, so a fixed from_seq=0 replays the log."""
    out = _run_in_node(_STREAM_JS + """
      send(0); send(1);
      current.onerror();          // the connection drops
      scheduled.shift()();        // ...and the client reconnects
      send(0); send(1); send(2);  // a server replaying from 0 anyway
      console.log(JSON.stringify({opened: opened, received: received, states: states}));
    """, tmp_path)

    # First connect carries no cursor, so the server opens on the tail; the
    # reconnect carries one, because resuming is what the cursor is for.
    assert out["opened"] == ["/events", "/events?from_seq=2"]
    assert out["received"] == [0, 1, 2]
    assert out["states"][-1] == "live"


def test_a_drop_before_any_event_reconnects_to_the_tail_again(tmp_path):
    out = _run_in_node(_STREAM_JS + """
      current.onerror();
      scheduled.shift()();
      console.log(JSON.stringify({opened: opened}));
    """, tmp_path)

    assert out["opened"] == ["/events", "/events"]


def test_a_scoped_feed_keeps_its_scope_across_a_reconnect(tmp_path):
    out = _run_in_node("""
      const opened = [];
      let current = null;
      client.openRunLogStream({
        url: '/events?stage=load',
        openStream: (url) => {
          opened.push(url);
          current = {onmessage: null, onerror: null, addEventListener() {}, close() {}};
          return current;
        },
        schedule: (fn) => fn(),
        onEvent: () => {}, onState: () => {},
      });
      current.onmessage({data: JSON.stringify({seq: 7, kind: 'row_ok', level: 0})});
      current.onerror();
      console.log(JSON.stringify({opened: opened}));
    """, tmp_path)

    assert out["opened"] == ["/events?stage=load", "/events?stage=load&from_seq=8"]


def test_run_done_is_rendered_and_then_ends_the_stream(tmp_path):
    out = _run_in_node(_STREAM_JS + """
      send(0); send(1, 'run_done');
      console.log(JSON.stringify({received: received, states: states,
                                  closed: current.closed, opened: opened.length}));
    """, tmp_path)

    # The terminal marker is the panel's "run finished" line, so it is rendered
    # like any other event — the panel's count then matches the file's.
    assert out["received"] == [0, 1]
    assert out["states"][-1] == "complete" and out["closed"] is True
    assert out["opened"] == 1
