/* scope_map.js — draw the scope of one figure.
 *
 * Reads the ScopeMap the route embeds in #scope-payload and draws, left to right in
 * run order, one column per stage that told these rows apart. A node is a SET of
 * branches, not one branch, so a row holding two arms of a split lands in its own
 * node and every row is in exactly one. docs/scope-map.md.
 *
 * Clicking a removal count redraws for the rows behind it: the reason a set of rows
 * is missing is usually upstream of the stage that removed them.
 */
(function () {
  "use strict";

  var host = document.getElementById("scope-payload");
  if (!host) return;

  var D = JSON.parse(host.textContent);
  var pickedNode = null;
  var pickedRow = null;
  var showAll = false;
  var openTab = "rows";
  var panels = {};
  var rowTables = {};

  var SEP = " ";
  // Says the link leaves this page, the way it does anywhere else.
  var OUTWARD = "\u00a0\u2197";
  // Gutter mark against a branch the drawn rows are here by. A glyph, not a tint:
  // highlight.js owns the code element's markup and rewrites it wholesale.
  var MARK = "\u25B8";
  var tableOf = function (stageId) {
    return '<table class="data-preview" data-stage="' + esc(stageId) + '">';
  };
  var COLUMN = 300, BAR = 11, HEAD = 52, GAP = 16, CUT_LINE = 11;
  // Clear of the head: flush against it, the ribbons read as hanging off the text.
  var BAND_GAP = 14;
  // HEAD plus a line per removal the widest column names above its bar.
  var TOP = HEAD + BAND_GAP;
  var LABEL_PITCH = 30;
  var SHORTEST_BAND = 260, TALLEST_BAND = 620, BAND_PER_NODE = 72, SWEEPS = 4;

  var num = function (n) { return window.Figures.text(Number(n)); };
  // A cited cell is not always a number — a group key is a figure a reader may cite
  // too, and Number("Facebook") prints NaN where the frame holds a name.
  var figure = function (value) {
    if (value === null || value === undefined) return "(empty)";
    return window.Figures.text(value);
  };
  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
    });
  };
  var clip = function (s, n) {
    return s.length <= n ? s : s.slice(0, Math.max(0, n - 1)) + "…";
  };
  var lineRange = function (fact) {
    var first = fact.first_body_line_number, last = fact.last_body_line_number;
    var lines = [fact.test_line_number];
    for (var n = first; n && last && n <= last; n++) lines.push(n);
    return lines;
  };
  // `column` names the cell so it can be opened; a table drawn without one is
  // read-only, which is what leaving it off says.
  var cell = function (value, column) {
    var text = String(value == null ? "" : value);
    var short = clip(text, 34);
    var at = column === undefined ? "" : ' data-col="' + esc(column) + '"';
    return short === text
      ? "<td" + at + ">" + esc(text) + "</td>"
      : "<td" + at + ' data-tip="' + esc(text) + '">' + esc(short) + "</td>";
  };
  var byId = function (id) { return document.getElementById(id); };

  // ── the drawing ──────────────────────────────────────────────────────────
  //
  // Every position here was decided in app/web/scope_drawing.py and arrives on the
  // payload. Nothing below measures anything: it turns numbers into SVG and binds
  // the clicks. See docs/scope-map.md.

  function branchesOn(path, stageId) {
    return (D.branch_paths[path] || []).filter(function (b) {
      return D.branches[b] && D.branches[b].stage_id === stageId;
    });
  }

  function nodeKeyForPath(stageId, path) {
    return stageId + SEP + branchesOn(path, stageId).join(",");
  }

  function drawing() {
    if (D.drilled) return (D.cuts[D.drilled.branch] || {}).drawn;
    return showAll ? D.drawn_every_stage : D.drawn;
  }

  function bars() {
    return drawing().columns.reduce(function (all, c) {
      return all.concat(c.bars);
    }, []);
  }

  function shape() {
    render();
  }

  function draw() {
    var g = drawing();
    var svg = byId("scope-svg");
    svg.setAttribute("width", String(g.width));
    svg.setAttribute("height", String(g.height));
    svg.setAttribute("viewBox", "0 0 " + g.width + " " + g.height);
    svg.innerHTML =
      g.ribbons.map(function (r) { return drawRibbon(g, r); }).join("") +
      g.columns.map(function (c) {
        return c.bars.map(function (b) { return drawBar(g, b); }).join("") +
               drawHead(g, c);
      }).join("");
    bindTheDrawing(svg);
  }

  function bindTheDrawing(svg) {
    svg.querySelectorAll("[data-node]").forEach(function (el) {
      el.onclick = function (event) { event.stopPropagation(); pick(el.dataset.node); };
    });
    svg.querySelectorAll("[data-cut]").forEach(function (el) {
      el.onclick = function (event) {
        event.stopPropagation();
        window.open(scopePageFor(el.dataset.cut), "_blank");
      };
    });
    svg.querySelectorAll("[data-expand]").forEach(function (el) {
      el.onclick = function (event) {
        event.stopPropagation();
        location.href = expandUrl(el.dataset.expand, el.dataset.want === "1");
      };
    });
    svg.onclick = clearPick;
  }

  // Each end is a share of ITS OWN bar's height, so a ribbon into a bar drawn
  // shorter than its inputs tapers — which is what an aggregate collapsing rows
  // looks like.
  function drawRibbon(g, r) {
    var lit = isLit(r.from_key) && isLit(r.into_key);
    var m = (r.x0 + r.x1) / 2;
    return '<path class="scope-ribbon' + (lit ? " is-lit" : "") + '" d="' +
      "M" + r.x0 + "," + r.y0 + "C" + m + "," + r.y0 + " " + m + "," + r.y1 + " " +
      r.x1 + "," + r.y1 + "v" + r.h1 + "C" + m + "," + (r.y1 + r.h1) + " " + m +
      "," + (r.y0 + r.h0) + " " + r.x0 + "," + (r.y0 + r.h0) + 'Z"/>';
  }

  function isLit(key) {
    return !pickedNode || key === pickedNode;
  }

  function drawBar(g, b) {
    var dim = pickedNode && b.key !== pickedNode;
    var edge = b.x + g.bar_width;
    var leader = b.leader_at == null ? ""
      : '<path class="scope-leader" d="M' + edge + "," + b.leader_at + "H" +
        (edge + 6) + "V" + b.label_y + "H" + (edge + 10) + '"/>';
    return '<rect class="scope-bar-mark' + (b.implied ? " is-implied" : "") +
      (dim ? " is-dim" : "") + '" data-node="' + esc(b.key) + '" data-tip="' +
      esc(b.tip) + '" x="' + b.x + '" y="' + b.y + '" width="' + g.bar_width +
      '" height="' + b.height + '"/>' + leader +
      '<text class="scope-node" data-node="' + esc(b.key) + '" data-tip="' +
      esc(b.tip) + '" x="' + b.text_x + '" y="' + (b.label_y + 3) + '">' +
      esc(b.label) + "</text>" +
      '<text class="scope-count" x="' + b.text_x + '" y="' + (b.label_y + 14) +
      '">' + num(b.rows) + "</text>";
  }

  function drawHead(g, c) {
    return '<text class="scope-head" data-tip="' + esc(c.head_tip) + '" x="' + c.x +
      '" y="15">' + esc(c.head_label) + "</text>" +
      '<text class="scope-head scope-head-note" data-tip="' + esc(c.head_tip) +
      '" x="' + c.x + '" y="27">' + esc(c.head_note) + "</text>" +
      (c.scale_label ? '<text class="scope-out" data-tip="' + esc(c.scale_tip) +
        '" x="' + c.x + '" y="39">' + esc(c.scale_label) + "</text>" : "") +
      c.removals.map(function (r) { return drawRemoval(g, c, r); }).join("") +
      drawMergeControl(g, c);
  }

  function drawRemoval(g, c, r) {
    return '<text class="scope-out-gone" data-cut="' + esc(r.branch) +
      '" data-tip="' + esc(r.tip) + '" x="' + c.x + '" y="' +
      (39 + (r.line + 1) * 11) + '">' + esc(r.label) + OUTWARD + "</text>";
  }

  // Aliased or expanded, a merge stage offers the other reading of itself here.
  function drawMergeControl(g, c) {
    if (!c.merge_label) return "";
    return '<text class="scope-expand" data-expand="' + esc(c.stage.id) +
      '" data-want="' + (c.merge_wants ? "1" : "0") + '" x="' +
      (c.x + g.bar_width + 12) + '" y="' + (c.bottom + 7) + '">' +
      esc(c.merge_label) + "</text>";
  }

  // The standalone scope page, never `location`: drawn inside the lineage page's
  // frame this link leaves that page's figure behind, so it may not address it.
  function scopePageFor(branch) {
    var query = new URLSearchParams({
      stage: D.citation.stage_id, row: String(D.citation.row_ordinal),
      column: D.citation.column, cut: branch,
    });
    return "/project/" + encodeURIComponent(D.project_id) + "/runs/" +
      encodeURIComponent(D.run_id) + "/scope?" + query.toString();
  }

  // ── what is picked ───────────────────────────────────────────────────────

  function pick(key) {
    pickedNode = pickedNode === key ? null : key;
    pickedRow = null;
    render();
  }

  function clearPick() {
    pickedNode = null;
    pickedRow = null;
    render();
  }
  window.scopeClearPick = clearPick;

  function selected() {
    if (pickedRow != null) return [pickedRow];
    if (!pickedNode) return D.covers.ordinals;
    var bar = bars().find(function (b) { return b.key === pickedNode; });
    return bar ? bar.on : [];
  }

  // ── drilling into a cut ──────────────────────────────────────────────────
  //
  // A cut is a page of its own, addressed by `?cut=`, and this reads it once on
  // load. The rows behind it arrive as counts per path; the synthetic index below
  // sizes the ribbons and is never a name for a row.

  function openCut(branch) {
    var cut = (D.cuts || {})[branch];
    if (!cut) return false;
    var index = [];
    cut.rows_per_branch_path.forEach(function (rows, path) {
      for (var n = 0; n < rows; n++) index.push(path);
    });
    D = Object.assign({}, D, {
      covers: { at_stage: cut.at_stage,
                ordinals: index.map(function (_, i) { return i; }),
                merges_walked_down: [] },
      branch_paths: cut.branch_paths, branch_path_index: index, rows: cut.rows, columns: cut.columns,
      stages: cut.stages, reach: [], scale: [], sampled_from: cut.total,
      aliased_merges: cut.aliased_merges, resolved_merges: cut.resolved_merges,
      nearest_merge: cut.nearest_merge,
      // A cut's rows arrive as counts per path, which say no frame each was a row of.
      came_through: [], came_through_index: [],
      drilled: { branch: branch, label: D.branches[branch].label,
                 stage: D.branches[branch].stage_id, total: cut.total },
    });
    pickedNode = null;
    pickedRow = null;
    shape();
    return true;
  }

  // ── expanding a merge ────────────────────────────────────────────────────
  //
  // A merge's groups are aliased into one node by default. Expanding one asks the
  // route to resolve it, so the drawing splits into a node per group.

  function expandUrl(stageId, want) {
    var query = new URLSearchParams(location.search);
    query.delete("expand");
    (D.resolved_merges || []).concat(want ? [stageId] : []).forEach(function (id) {
      if (id !== D.nearest_merge && (want || id !== stageId)) query.append("expand", id);
    });
    return location.pathname + "?" + query.toString();
  }

  // ── the words under the drawing ──────────────────────────────────────────

  function render() {
    draw();
    renderHere();
    renderBar();
    renderTable();
    renderTabs();
    var cut = drawing().columns.some(function (c) { return c.removals.length; });
    byId("scope-legend").textContent =
      (cut ? "An underlined count is rows that stage took out of the workflow — click " +
        "one to draw them in a new tab. Nothing in the drawing is scaled to it. " : "") +
      sayWhatIsNotDrawn();
  }

  // The whole lookup side is left out of the drawing, so it is named here instead.
  function sayWhatIsNotDrawn() {
    var lookups = D.lookup_tables || [];
    if (!lookups.length || D.drilled) return "";
    return "This figure also read " + (lookups.length === 1 ? "a lookup table, " :
      lookups.length + " lookup tables, ") + lookups.join(", ") +
      ". Neither they nor the stages behind them are drawn yet: a row that matched " +
      "one leaves as one row, which no ribbon here carries.";
  }

  function renderHere() {
    var note = byId("scope-drilled");
    var section = document.querySelector(".scope");
    if (!D.drilled) {
      section.removeAttribute("data-drilled");
      note.textContent = "";
      return;
    }
    section.setAttribute("data-drilled", "");
    note.innerHTML = "<b>" + num(D.drilled.total) + "</b> row" +
      (D.drilled.total === 1 ? "" : "s") + " that <code>" + esc(D.drilled.stage) +
      "</code> " + esc(D.drilled.label) + ", drawn over <code>" +
      esc(D.covers.at_stage) + "</code> where they were still present. Their path is " +
      "why they left: whatever they did differently is upstream of that stage.";
  }

  function renderBar() {
    var bar = byId("scope-bar");
    if (pickedFigure()) {
      bar.innerHTML = "<span><b>1</b> row of <code>" + esc(D.citation.stage_id) +
        "</code> — row " + D.cited_row.number + ", merged from " +
        num(D.covers.ordinals.length) + " rows of <code>" +
        esc(D.covers.at_stage) + "</code>.</span>";
    } else {
      var rows = selected();
      bar.innerHTML = "<span><b>" + num(rows.length) + "</b> of " +
        num(D.covers.ordinals.length) + " rows</span>" +
        (pickedNode || pickedRow != null
          ? ' <span class="muted">click the chart background to clear</span>' : "") +
        (D.covers.sampled_from && !pickedNode && pickedRow == null
          ? ' <span class="muted">the table below lists ' + num(D.rows.length) +
            " of them</span>" : "");
    }
    bar.querySelectorAll("[data-clear]").forEach(function (el) {
      el.onclick = clearPick;
    });
  }

  // ── the transform behind the picked rows ─────────────────────────────────
  //
  // The rows and the step that made them are the same question asked twice, so they
  // are two tabs over one pick. The transform is the run page's own panel, fetched
  // per stage, with the arm these rows took lit inside its code block.

  function renderTabs() {
    var stage = pickedStage();
    if (!stage) openTab = "rows";
    byId("scope-tabs").hidden = !stage;
    byId("scope-table").hidden = openTab !== "rows";
    byId("scope-transform").hidden = openTab !== "transform";
    document.querySelectorAll("#scope-tabs [data-tab]").forEach(function (button) {
      button.classList.toggle("active", button.dataset.tab === openTab);
    });
    if (stage && openTab === "transform") showTransform(stage);
  }

  function pickedStage() {
    return pickedNode ? pickedNode.split(SEP)[0] : null;
  }

  function showTransform(stageId) {
    var host = byId("scope-transform");
    if (host.dataset.node === pickedNode) return;
    host.dataset.node = pickedNode;
    var wanted = pickedNode;
    host.textContent = "reading " + stageId + "…";
    loadPanel(stageId).then(function (html) {
      if (host.dataset.node !== wanted) return;
      host.innerHTML = html;
      lightTheArm(host, stageId);
    }, function () {
      if (host.dataset.node !== wanted) return;
      host.textContent = "This run kept no transform for " + stageId + ".";
    });
  }

  function loadPanel(stageId) {
    if (!panels[stageId]) {
      panels[stageId] = fetch(panelAddress(stageId)).then(function (reply) {
        if (!reply.ok) throw new Error(String(reply.status));
        return reply.text();
      });
    }
    return panels[stageId];
  }

  function panelAddress(stageId) {
    // lineage_panel takes a row; nothing in the transform it renders varies by one.
    var row = D.rows.length ? D.rows[0].ordinal : 0;
    return "/project/" + encodeURIComponent(D.project_id) + "/runs/" +
      encodeURIComponent(D.run_id) + "/stage/" + encodeURIComponent(stageId) +
      "/lineage_panel?row=" + row;
  }

  // Which arm ran is what this page knows and the panel does not, so the panel's own
  // code block gives way to the same source, marked. Rebuilt from `stage.code`: the
  // arms' line numbers are counted against that, and a panel that resolved a module
  // reference to find its source need not be showing it.
  function lightTheArm(host, stageId) {
    var lit = new Set(armsTaken().reduce(function (all, fact) {
      return all.concat(lineRange(fact));
    }, []).filter(Boolean));
    var block = host.querySelector(".code-block pre.code");
    var stage = D.stages.find(function (s) { return s.id === stageId; });
    if (!block || !lit.size || !stage || !stage.code) return;
    var lines = stage.code.split("\n");
    block.outerHTML =
      '<pre class="code scope-lit"><span class="scope-gutter" aria-hidden="true">' +
      lines.map(function (_, i) { return lit.has(i + 1) ? MARK : " "; }).join("\n") +
      '</span><code class="language-python">' + esc(stage.code) + "</code></pre>" +
      '<p class="muted scope-lit-legend">' + MARK + " a branch on these rows' path.</p>";
  }

  function armsTaken() {
    if (!pickedNode) return [];
    var held = pickedNode.slice(pickedStage().length + 1);
    return (held ? held.split(",") : []).map(function (b) {
      return D.branches[b];
    }).filter(function (fact) { return fact && fact.reason === "code"; });
  }

  // The rows are the run page's own table for the picked stage, fetched whole. The
  // colours, the marks and the +/- are that table's; nothing here re-decides them.
  function renderTable() {
    if (pickedFigure()) { renderCitedRow(); return; }
    var stage = pickedStage() || D.covers.at_stage;
    var host = byId("scope-table");
    if (host.dataset.stage === stage) return;
    host.dataset.stage = stage;
    host.innerHTML = '<p class="muted">loading\u2026</p>';
    loadRows(stage).then(function (html) {
      if (host.dataset.stage !== stage) return;
      host.innerHTML = html;
    }).catch(function (failure) {
      if (host.dataset.stage !== stage) return;
      host.innerHTML = '<p class="muted">could not read ' + esc(stage) +
        " (" + esc(failure.message) + ")</p>";
    });
  }

  function loadRows(stageId) {
    if (!rowTables[stageId]) {
      rowTables[stageId] = fetch(rowsAddress(stageId)).then(function (reply) {
        if (!reply.ok) throw new Error(String(reply.status));
        return reply.text();
      });
    }
    return rowTables[stageId];
  }

  function rowsAddress(stageId) {
    var query = new URLSearchParams({
      stage: D.citation.stage_id, row: D.citation.row_ordinal,
      column: D.citation.column, at: stageId });
    return "/project/" + encodeURIComponent(D.project_id) + "/runs/" +
      encodeURIComponent(D.run_id) + "/scope/rows?" + query.toString();
  }

  // The figure's node is ONE output row, not a slice of what fed it.
  function pickedFigure() {
    if (!pickedNode || D.drilled) return false;
    return bars().some(function (b) {
      return b.is_figure && b.key === pickedNode;
    });
  }

  function headOf(columns) {
    return '<tr><th class="scope-num">row</th>' +
      columns.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("") + "</tr>";
  }

  // Cells are positional against the map's `columns`, the way every table here is.
  function rowOf(r, columns) {
    return '<td class="scope-num">' + r.number + "</td>" +
      r.cells.map(function (value, i) { return cell(value, columns[i]); }).join("") +
      "</tr>";
  }

  function renderCitedRow() {
    var row = D.cited_row;
    byId("scope-table").dataset.stage = "";
    byId("scope-table").innerHTML = tableOf(D.citation.stage_id) + "<thead>" +
      headOf(row.columns) + '</thead><tbody><tr data-row="' + row.ordinal + '">' +
      rowOf(row, row.columns) + "</tbody></table>";
  }

  // A cell is one figure's coordinate — stage, row, column — so it opens that
  // figure's lineage. Drawn inside the lineage page's frame, the page that moves
  // is the one holding the frame; opened on its own, that is this page.
  byId("scope-table").addEventListener("click", function (event) {
    var td = event.target.closest("td[data-col]");
    if (!td) return;
    var stage = td.closest("table").dataset.stage;
    var ordinal = td.closest("tr").dataset.row;
    if (!stage || ordinal === undefined) return;
    window.top.location.href = "/project/" + encodeURIComponent(D.project_id) +
      "/runs/" + encodeURIComponent(D.run_id) +
      "/stage/" + encodeURIComponent(stage) +
      "/row/" + encodeURIComponent(ordinal) +
      "/trace/view?column=" + encodeURIComponent(td.dataset.col);
  });

  byId("scope-tabs").onclick = function (event) {
    var button = event.target.closest("[data-tab]");
    if (!button) return;
    openTab = button.dataset.tab;
    renderTabs();
  };

  var every = byId("scope-every-stage");
  if (every) {
    // The markup owns the default, and a reload restoring the box agrees with it.
    showAll = every.checked;
    every.onchange = function () { showAll = every.checked; shape(); };
  }
  shape();
  var asked = new URLSearchParams(location.search).get("cut");
  if (asked) openCut(asked);
})();
