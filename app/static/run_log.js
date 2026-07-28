// The run page's live log panel: tails runs/<id>/events.jsonl over SSE and
// renders the lifecycle spine, with two client-side filters over the same feed.
//
// Two behaviours are worth knowing before editing:
//
//  1. Reconnect. An EventSource retries a dropped connection against the URL it
//     was constructed with, so a fixed `?from_seq=0` replays the whole log into
//     the panel on every blip (measured: three connections, every event three
//     times). This module drives reconnection itself — close on error, reopen
//     one past the highest seq seen — and drops any event whose seq it has
//     already seen, so a replay can never duplicate a line.
//
//  2. Filters. "errors only" is applied FIRST, over every event: an llm_error
//     is a LEVEL_DETAIL (1) event, so filtering by detail level first would
//     strip exactly the errors the checkbox exists to surface.
(function (global) {
  var LEVEL_DETAIL = 1;

  function escapeHtml(value) {
    return String(value).replace(/[&<>]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
    });
  }

  function isErrorEvent(ev) {
    return ev.kind === "row_error" || ev.kind === "llm_error"
      || (ev.kind === "stage_done" && ev.status === "error");
  }

  // "errors only" wins outright: it is the tightest filter, and an error is
  // worth seeing whatever tier it was logged at.
  function selectVisibleEvents(events, options) {
    if (options.errorsOnly) return events.filter(isErrorEvent);
    if (options.detail) return events.slice();
    return events.filter(function (e) { return (e.level || 0) < LEVEL_DETAIL; });
  }

  function renderEvents(events, options) {
    return selectVisibleEvents(events, options)
      .map(function (ev) { return formatEvent(ev, options.traceUrl); })
      .join("\n");
  }

  function formatEvent(ev, traceUrl) {
    if ((ev.level || 0) >= LEVEL_DETAIL) return formatDetailEvent(ev);
    var ts = (ev.ts || "").slice(11, 23);        // HH:MM:SS.mmm
    var loc = [ev.stage, ev.row != null ? "row " + ev.row : null]
      .filter(Boolean).join(" · ");
    var tail = ev.text ? " — " + ev.text : (ev.status ? " — " + ev.status : "");
    var src = ev.source ? "  [" + ev.source + "]" : "";
    var line = escapeHtml(
      ts + "  " + (ev.kind || "").padEnd(11) + " " + loc + src + tail
    );
    var cls = ev.source === "cached" ? " run-log-cached" : "";
    // A per-row event links to that row's lineage/trace. Opens in a new tab so
    // the live feed keeps running.
    if (traceUrl && ev.stage != null && ev.row != null) {
      return '<a class="run-log-row' + cls + '" href="'
        + traceUrl(ev.stage, ev.row) + '" target="_blank" rel="noopener">'
        + line + "</a>";
    }
    return '<span class="' + cls.trim() + '">' + line + "</span>";
  }

  // A detail (level 1) event carries the model's own prompt/thinking/response
  // and can be long: rendered nested under its row, dimmed, whitespace kept, and
  // clipped unless scrolled so the lifecycle spine stays scannable. `rows` names
  // every row one prompt covered — a batched chunk's detail belongs to all of
  // them, not to the single row it is filed under.
  function formatDetailEvent(ev) {
    var label = (ev.kind || "").replace(/^llm_/, "");
    var span = (ev.rows && ev.rows.length > 1)
      ? "rows " + ev.rows[0] + "–" + ev.rows[ev.rows.length - 1]
      : "row " + ev.row;
    var head = escapeHtml(
      "    " + label + (ev.stage != null ? "  " + ev.stage + " · " + span : "")
    );
    var body = escapeHtml(ev.text || "").replace(/\n/g, "<br>");
    return '<span class="run-log-detail"><span class="run-log-detail-head">'
      + head + '</span><span class="run-log-detail-body">' + body
      + "</span></span>";
  }

  // options: {url, openStream, schedule, onEvent, onState}. `url` carries no
  // query — this owns from_seq, because the cursor is what makes a reconnect
  // resume rather than replay.
  function openRunLogStream(options) {
    var lastSeq = -1;
    var stream = null;
    var done = false;

    function finish() {
      done = true;
      if (stream) stream.close();
      options.onState("complete");
    }

    function receive(message) {
      var ev;
      try { ev = JSON.parse(message.data); } catch (e) { return; }
      if (typeof ev.seq === "number") {
        if (ev.seq <= lastSeq) return;   // a replayed event: already rendered
        lastSeq = ev.seq;
      }
      // run_done is rendered like any other event — it is the panel's "run
      // finished" line — and only then ends the stream.
      options.onEvent(ev);
      if (ev.kind === "run_done") { finish(); return; }
      options.onState("live");
    }

    function connect() {
      stream = options.openStream(options.url + "?from_seq=" + (lastSeq + 1));
      stream.onmessage = receive;
      stream.addEventListener("done", finish);
      stream.onerror = function () {
        if (done) return;
        options.onState("disconnected");
        stream.close();          // cancel the EventSource's own replay-from-0 retry
        options.schedule(connect);
      };
    }

    connect();
    return { highestSeq: function () { return lastSeq; } };
  }

  function initRunLog(config) {
    var pre = document.getElementById("run-log");
    var countEl = document.getElementById("run-log-count");
    var stateEl = document.getElementById("run-log-state");
    var errorsOnly = document.getElementById("run-log-errors-only");
    var detail = document.getElementById("run-log-detail");
    var events = [];
    var base = "/project/" + encodeURIComponent(config.project)
      + "/runs/" + encodeURIComponent(config.runId);

    function traceUrl(stage, row) {
      return base + "/stage/" + encodeURIComponent(stage) + "/row/" + row
        + "/trace/view";
    }

    function render() {
      pre.innerHTML = renderEvents(events, {
        errorsOnly: errorsOnly.checked, detail: detail.checked,
        traceUrl: traceUrl,
      });
      var detailN = events.filter(function (e) {
        return (e.level || 0) >= LEVEL_DETAIL;
      }).length;
      countEl.textContent = "· " + events.length + " event"
        + (events.length !== 1 ? "s" : "") + (detailN ? " (" + detailN + " LLM detail)" : "");
      var nearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 60;
      if (nearBottom) pre.scrollTop = pre.scrollHeight;
    }

    errorsOnly.addEventListener("change", render);
    detail.addEventListener("change", render);
    document.getElementById("run-log-clear").addEventListener("click", function () {
      pre.textContent = "";
    });

    stateEl.textContent = "connecting…";
    openRunLogStream({
      url: base + "/events",
      openStream: function (url) { return new EventSource(url); },
      schedule: function (fn) { setTimeout(fn, 2000); },
      onEvent: function (ev) { events.push(ev); render(); },
      onState: function (state) { stateEl.textContent = state; },
    });
  }

  global.initRunLog = initRunLog;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      isErrorEvent: isErrorEvent,
      selectVisibleEvents: selectVisibleEvents,
      renderEvents: renderEvents,
      openRunLogStream: openRunLogStream,
    };
  }
})(typeof window !== "undefined" ? window : globalThis);
