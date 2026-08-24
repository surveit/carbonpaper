// The run page's live log panel: tails the run's event log over SSE and
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
//
//  3. Appending is incremental and batched. Re-rendering the whole buffer per
//     arriving event is O(n²) in formatting AND reparses the panel's innerHTML
//     every time; on a 272k-event run (a row-per-event log of a 135k-row stage)
//     that wedges the tab for good. New events are formatted once, appended
//     with insertAdjacentHTML, and coalesced into one timer tick. The full
//     rebuild is kept for the two things that genuinely invalidate the panel: a
//     filter change and a "load older" prepend.
(function (global) {
  var LEVEL_DETAIL = 1;

  // The log files each event under the ordinal every link here carries; a reader
  // counts rows from 1.
  function rowNumber(ordinal) { return ordinal + 1; }

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
    var loc = [ev.stage, ev.row != null ? "row " + rowNumber(ev.row) : null]
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
      ? "rows " + rowNumber(ev.rows[0]) + "–" + rowNumber(ev.rows[ev.rows.length - 1])
      : "row " + rowNumber(ev.row);
    var head = escapeHtml(
      "    " + label + (ev.stage != null ? "  " + ev.stage + " · " + span : "")
    );
    var body = escapeHtml(ev.text || "").replace(/\n/g, "<br>");
    return '<span class="run-log-detail"><span class="run-log-detail-head">'
      + head + '</span><span class="run-log-detail-body">' + body
      + "</span></span>";
  }

  // Append one query parameter, whichever separator the url already needs.
  function withParam(url, key, value) {
    return url + (url.indexOf("?") < 0 ? "?" : "&")
      + key + "=" + encodeURIComponent(value);
  }

  // options: {url, openStream, schedule, onEvent, onState}. `url` may carry a
  // scope (?stage=…) but never a cursor — this owns from_seq, because the
  // cursor is what makes a reconnect resume rather than replay.
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
      // The FIRST connection carries no cursor, so the server opens on the tail
      // — that default is what keeps a 272k-event log from arriving in full.
      // Every RECONNECT carries one: resuming is what the cursor is for, and a
      // reconnect that fell back to the tail would silently skip the middle.
      stream = options.openStream(
        lastSeq < 0 ? options.url : withParam(options.url, "from_seq", lastSeq + 1)
      );
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
    return {
      highestSeq: function () { return lastSeq; },
      // Abandon the feed for good — a scoped panel that was replaced calls this,
      // and without it every stage the reader clicked would keep a connection.
      close: function () { done = true; if (stream) stream.close(); },
    };
  }

  // A ceiling on what the panel keeps in the DOM. "Load older" is the way to
  // reach further back; without a cap, a long live run walks into the same
  // unbounded-buffer wall the tail default exists to avoid.
  var MAX_BUFFERED_EVENTS = 20000;

  // How long arriving events are pooled before one batched append.
  var FLUSH_INTERVAL_MS = 32;

  // config: {root, base, pageSize, stage, rowTrace}. `root` is the panel element
  // holding this log's controls — the page carries more than one (the whole run
  // at the bottom, the selected stage inside its own panel), so nothing here may
  // reach for a document-wide id. `stage` scopes the feed server-side. `base` is
  // the run's URL prefix, which serves /events and /events/page under it — a
  // production run and an eval's subset run live at different prefixes and are
  // otherwise the same log. `rowTrace` false drops the per-row lineage links,
  // for a run whose prefix serves no trace page.
  function initRunLog(config) {
    var root = config.root;
    var find = function (cls) { return root.querySelector("." + cls); };
    var pre = find("js-run-log");
    var countEl = find("js-run-log-count");
    var stateEl = find("js-run-log-state");
    var errorsOnly = find("js-run-log-errors-only");
    var detail = find("js-run-log-detail");
    var olderBtn = find("js-run-log-older");
    var clearBtn = find("js-run-log-clear");
    var events = [];
    var pending = [];              // arrived since the last flush
    var timer = null;
    var loadingOlder = false;
    var moreAvailable = true;      // until a page fetch says otherwise
    var pageSize = config.pageSize || 500;
    var base = config.base;

    // Both feeds carry the same scope: the SSE tail and the "load older" page
    // must agree on which events exist, or paging back would widen the view.
    function scoped(url) {
      return config.stage ? withParam(url, "stage", config.stage) : url;
    }

    function traceUrl(stage, row) {
      return base + "/stage/" + encodeURIComponent(stage) + "/row/" + row
        + "/trace/view";
    }

    function options() {
      return {
        errorsOnly: errorsOnly.checked, detail: detail.checked,
        traceUrl: config.rowTrace === false ? null : traceUrl,
      };
    }

    // seq only has to be MONOTONIC for this to hold: an event before the oldest
    // one held is older still. The panel deliberately does not turn seq into a
    // count — that would assume it has no gaps.
    function oldestSeq() {
      return events.length && typeof events[0].seq === "number" ? events[0].seq : 0;
    }

    function updateChrome() {
      var detailN = events.filter(function (e) {
        return (e.level || 0) >= LEVEL_DETAIL;
      }).length;
      countEl.textContent = "· " + events.length
        + " event" + (events.length !== 1 ? "s" : "")
        + (detailN ? " (" + detailN + " LLM detail)" : "")
        + (moreAvailable && oldestSeq() > 0 ? " · older not loaded" : "");
      if (!olderBtn) return;
      olderBtn.hidden = !(moreAvailable && oldestSeq() > 0);
      olderBtn.disabled = loadingOlder;
      olderBtn.textContent = loadingOlder ? "loading…" : "load older";
    }

    // Full rebuild. Only for what actually invalidates every line: a filter
    // change, or older events arriving at the FRONT of the buffer.
    function render() {
      pre.innerHTML = renderEvents(events, options());
      pre.scrollTop = pre.scrollHeight;
      updateChrome();
    }

    // The hot path: format only what arrived and append it.
    function flush() {
      timer = null;
      if (!pending.length) return;
      var batch = pending;
      pending = [];
      var html = renderEvents(batch, options());
      // Measured BEFORE the insert — afterwards every position has moved.
      var nearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 60;
      if (html) pre.insertAdjacentHTML("beforeend", html + "\n");
      if (nearBottom) pre.scrollTop = pre.scrollHeight;
      if (events.length > MAX_BUFFERED_EVENTS) {
        events = events.slice(events.length - MAX_BUFFERED_EVENTS);
        render();                  // the buffer moved; the DOM has to follow
        return;
      }
      updateChrome();
    }

    // A timer, not requestAnimationFrame: rAF does not fire in a background
    // tab, so a run opened in one would buffer events and render nothing until
    // it was focused. The interval only has to be long enough to coalesce a
    // burst into one insert — the freeze came from rendering per event, not
    // from rendering per frame.
    function schedule() {
      if (timer !== null) return;
      timer = setTimeout(flush, FLUSH_INTERVAL_MS);
    }

    function loadOlder() {
      var before = oldestSeq();
      if (loadingOlder || before <= 0) return;
      loadingOlder = true;
      updateChrome();
      fetch(scoped(base + "/events/page?before_seq=" + before + "&limit=" + pageSize))
        .then(function (r) { return r.json(); })
        .then(function (page) {
          events = (page.events || []).concat(events);
          moreAvailable = !!page.has_more;
          // Prepending moves everything down by the height of what was added;
          // holding scrollTop steady against that keeps the reader's place.
          var heightBefore = pre.scrollHeight;
          var top = pre.scrollTop;
          pre.innerHTML = renderEvents(events, options());
          pre.scrollTop = top + (pre.scrollHeight - heightBefore);
          updateChrome();
        })
        .catch(function () { stateEl.textContent = "could not load older events"; })
        .then(function () { loadingOlder = false; updateChrome(); });
    }

    errorsOnly.addEventListener("change", render);
    detail.addEventListener("change", render);
    if (olderBtn) olderBtn.addEventListener("click", loadOlder);
    if (clearBtn) clearBtn.addEventListener("click", function () {
      pre.textContent = "";
    });

    stateEl.textContent = "connecting…";
    updateChrome();
    return openRunLogStream({
      url: scoped(base + "/events"),
      openStream: function (url) { return new EventSource(url); },
      schedule: function (fn) { setTimeout(fn, 2000); },
      onEvent: function (ev) { events.push(ev); pending.push(ev); schedule(); },
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
