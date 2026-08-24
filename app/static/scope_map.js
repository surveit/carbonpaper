/* scope_map.js — draw the scope of one figure.
 *
 * Reads the ScopeMap the route embeds in #scope-payload and draws, left to right in
 * run order, one column per stage that told these rows apart. A node is a SET of
 * branches, not one branch, so a row holding two arms of a split lands in its own
 * node and every row is in exactly one. docs/scope-map.md.
 *
 * Clicking a cut stub redraws for the rows behind it: the reason a set of rows is
 * missing is usually upstream of the stage that removed them.
 */
(function () {
  "use strict";

  var host = document.getElementById("scope-payload");
  if (!host) return;

  var D = JSON.parse(host.textContent);
  var BACK = [];
  var cols = [];
  var pickedNode = null;
  var pickedRow = null;
  var showAll = false;

  var SEP = " ";
  var TABLE = '<table class="data-preview">';
  var COLUMN = 300, BAR = 11, HEIGHT = 520, TOP = 52, GAP = 16;
  var LABEL_PITCH = 30, STUB = 26;

  var num = function (n) { return Number(n).toLocaleString("en-US"); };
  // A cited cell is not always a number — a group key is a figure a reader may cite
  // too, and Number("Facebook") prints NaN where the frame holds a name.
  var figure = function (value) {
    if (value === null || value === undefined) return "(empty)";
    return typeof value === "number"
      ? value.toLocaleString("en-US", { maximumFractionDigits: 2 })
      : String(value);
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
  var cell = function (value) {
    var text = String(value == null ? "" : value);
    var short = clip(text, 34);
    return short === text
      ? "<td>" + esc(text) + "</td>"
      : '<td data-tip="' + esc(text) + '">' + esc(short) + "</td>";
  };
  var byId = function (id) { return document.getElementById(id); };

  // ── which columns to draw ────────────────────────────────────────────────

  function branchesOn(path, stageId) {
    return (D.branch_paths[path] || []).filter(function (b) {
      return D.branches[b] && D.branches[b].stage_id === stageId;
    });
  }

  function branchesAt(i, stageId) {
    return branchesOn(D.branch_path_index[i], stageId);
  }

  function nodeKey(column, i) {
    return column.id + SEP + branchesAt(i, column.id).join(",");
  }

  function nodeKeyForPath(stageId, path) {
    return stageId + SEP + branchesOn(path, stageId).join(",");
  }

  function tally(column) {
    var seen = new Map();
    D.covers.ordinals.forEach(function (ordinal, i) {
      var key = nodeKey(column, i);
      var node = seen.get(key) || { key: key, branches: branchesAt(i, column.id),
                                    rows: 0, on: [] };
      node.rows += 1;
      node.on.push(ordinal);
      seen.set(key, node);
    });
    return Array.from(seen.values()).sort(function (a, b) { return b.rows - a.rows; });
  }

  // A stub is for rows that LEFT here: removed from the frame, or grouped elsewhere.
  // An arm none of these rows took is not a loss — those rows carried on.
  function leftHere(branch) {
    var role = (D.branches[branch] || {}).role;
    return role === "removes" || (D.excluded_merges || []).indexOf(branch) >= 0;
  }

  function columns() {
    var drawn = D.stages.filter(function (stage) {
      return D.covers.ordinals.some(function (_, i) {
        return branchesAt(i, stage.id).length;
      });
    });
    return drawn.map(function (stage) {
      var nodes = tally(stage);
      if (stage.id === D.citation.stage_id && !D.drilled) {
        nodes.forEach(function (n) { n.isFigure = true; n.drawnRows = 1; });
      }
      var held = new Set(nodes.reduce(function (all, n) {
        return all.concat(n.branches);
      }, []));
      var gone = (D.reach || []).filter(function (r) {
        return D.branches[r.branch] && D.branches[r.branch].stage_id === stage.id &&
               !held.has(r.branch) && leftHere(r.branch);
      }).map(function (r) { return { branch: r.branch, rows: r.taken }; })
        .sort(function (a, b) { return b.rows - a.rows; });
      return Object.assign({}, stage, { nodes: nodes, gone: gone });
    }).filter(function (c) { return c.nodes.length || c.gone.length; });
  }

  // A column adds nothing when every row it separates was already separated. The
  // cited cell's own stage is exempt: it is the figure, not a distinction, and
  // dropping it leaves the last frame drawn looking like the answer.
  function keepInformative(all) {
    var running = D.covers.ordinals.map(function () { return ""; });
    var kept = [];
    all.forEach(function (column) {
      var next = D.covers.ordinals.map(function (_, i) {
        return running[i] + "|" + branchesAt(i, column.id).join(",");
      });
      if (column.id === D.citation.stage_id || column.gone.length ||
          new Set(next).size > new Set(running).size) {
        kept.push(column);
        running = next;
      }
    });
    return kept;
  }

  function shape() {
    cols = columns();
    // Inside a cut the question is what these rows DID, not how they differ from one
    // another — dropping a column here would hide the arm that got them removed.
    if (!showAll && !D.drilled) cols = keepInformative(cols);
    render();
  }

  // ── the drawing ──────────────────────────────────────────────────────────

  function labelOf(node) {
    if (node.isFigure) {
      return D.citation.column + " = " + figure(D.citation.value) +
        ", merged from " + num(node.rows);
    }
    var labels = node.branches.map(function (b) { return D.branches[b].label; });
    return labels.length ? labels.join(" + ") : "—";
  }

  function draw() {
    var total = D.covers.ordinals.length || 1;
    var scale = Math.min.apply(null, cols.map(function (c) {
      return (HEIGHT - (c.nodes.length - 1) * GAP) / total;
    }).concat([HEIGHT / total]));
    var x = 0;
    cols.forEach(function (c) {
      var y = TOP;
      c.nodes.forEach(function (n) {
        n.h = Math.max(2, (n.drawnRows == null ? n.rows : n.drawnRows) * scale);
        n.y = y;
        y += n.h + GAP;
      });
      c.x = x;
      c.bottom = y;
      c.nodes.forEach(function (n) { n.fromY = n.y; n.intoY = n.y; });
      placeLabels(c.nodes);
      x += COLUMN;
    });

    var ribbons = gatherRibbons(scale);
    var goneRows = Math.max.apply(null, cols.map(function (c) {
      return c.gone.length;
    }).concat([0]));
    var height = Math.max.apply(null, cols.map(function (c) {
      return c.bottom;
    }).concat([TOP])) + 14 + goneRows * 17;

    var svg = byId("scope-svg");
    svg.setAttribute("width", String(x));
    svg.setAttribute("height", String(height));
    svg.setAttribute("viewBox", "0 0 " + x + " " + height);
    svg.innerHTML =
      ribbons.map(drawRibbon).join("") +
      cols.map(function (c) {
        return c.nodes.map(function (n) { return drawNode(c, n); }).join("") +
               drawHead(c);
      }).join("");
    svg.querySelectorAll("[data-node]").forEach(function (el) {
      el.onclick = function (event) { event.stopPropagation(); pick(el.dataset.node); };
    });
    svg.querySelectorAll("[data-cut]").forEach(function (el) {
      el.onclick = function (event) { event.stopPropagation(); pick(el.dataset.cut); };
    });
    svg.onclick = clearPick;
  }

  function gatherRibbons(scale) {
    var seen = new Map();
    for (var ci = 0; ci < cols.length - 1; ci++) {
      for (var i = 0; i < D.covers.ordinals.length; i++) {
        var a = nodeAt(ci, i), b = nodeAt(ci + 1, i);
        if (!a || !b) continue;
        var key = ci + a.key + ">" + b.key;
        var ribbon = seen.get(key) || { ci: ci, a: a, b: b, rows: 0, scale: scale };
        ribbon.rows += 1;
        seen.set(key, ribbon);
      }
    }
    return Array.from(seen.values());
  }

  function nodeAt(ci, i) {
    var want = nodeKey(cols[ci], i);
    return cols[ci].nodes.find(function (n) { return n.key === want; });
  }

  function drawRibbon(r) {
    var x0 = cols[r.ci].x + BAR, x1 = cols[r.ci + 1].x;
    var lit = isLit(r.a) && isLit(r.b);
    // Each end is a share of ITS OWN node's height, so a ribbon into a node drawn
    // shorter than its inputs tapers — which is what an aggregate collapsing rows
    // looks like.
    var h0 = r.a.h * (r.rows / Math.max(1, r.a.rows));
    var h1 = r.b.h * (r.rows / Math.max(1, r.b.rows));
    var y0 = r.a.fromY, y1 = r.b.intoY;
    r.a.fromY += h0;
    r.b.intoY += h1;
    var m = (x0 + x1) / 2;
    return '<path class="scope-ribbon' + (lit ? " is-lit" : "") + '" d="' +
      "M" + x0 + "," + y0 + "C" + m + "," + y0 + " " + m + "," + y1 + " " + x1 +
      "," + y1 + "v" + h1 + "C" + m + "," + (y1 + h1) + " " + m + "," + (y0 + h0) +
      " " + x0 + "," + (y0 + h0) + 'Z"/>';
  }

  function isLit(node) {
    return !pickedNode || node.key === pickedNode;
  }

  function placeLabels(nodes) {
    var y = TOP;
    nodes.forEach(function (n) {
      n.labelY = Math.max(y, n.y + n.h / 2);
      y = n.labelY + LABEL_PITCH;
    });
  }

  function drawNode(c, n) {
    var room = COLUMN - BAR - 14;
    var implied = n.branches.every(function (b) {
      return D.branches[b].reason !== "code";
    });
    var dim = pickedNode && n.key !== pickedNode;
    var mark = '<rect class="scope-bar-mark' + (implied ? " is-implied" : "") +
      (dim ? " is-dim" : "") + '" data-node="' + esc(n.key) + '" data-tip="' +
      esc(labelOf(n) + " — " + num(n.rows) + " row" + (n.rows === 1 ? "" : "s")) +
      '" x="' + c.x + '" y="' + n.y + '" width="' + BAR + '" height="' + n.h + '"/>';
    var mid = n.y + n.h / 2;
    var leader = Math.abs(n.labelY - mid) > 2
      ? '<path class="scope-leader" d="M' + (c.x + BAR) + "," + mid + "H" +
        (c.x + BAR + 6) + "V" + n.labelY + "H" + (c.x + BAR + 10) + '"/>'
      : "";
    var tx = c.x + BAR + 12;
    var full = labelOf(n);
    var short = clip(full, Math.floor(room / 6.1));
    return mark + leader +
      '<text class="scope-node" data-tip="' + esc(full) + '" x="' + tx +
      '" y="' + (n.labelY + 3) + '">' +
      esc(short) + "</text>" +
      '<text class="scope-count" x="' + tx + '" y="' + (n.labelY + 14) + '">' +
      num(n.rows) + "</text>";
  }

  function drawHead(c) {
    var room = COLUMN - 14;
    var about = c.id + " — " + (c.description || c.type);
    return '<text class="scope-head" data-tip="' + esc(about) + '" x="' + c.x +
      '" y="15">' + esc(clip(c.id, Math.floor(room / 7))) + "</text>" +
      '<text class="scope-head scope-head-note" data-tip="' + esc(about) +
      '" x="' + c.x + '" y="27">' +
      esc(clip(c.description || c.type, Math.floor(room / 5.6))) + "</text>" +
      drawScale(c, room) +
      c.gone.map(function (g, i) { return drawStub(c, g, i); }).join("");
  }

  // The frame here held rows this figure has no ancestor among. A count, never a
  // ribbon: 45,061 drawn beside 40 to scale is the scale-mixing that makes a lie.
  function drawScale(c, room) {
    var step = (D.scale || []).find(function (s) { return s.stage === c.id; });
    if (!step || !step.rows_count || step.included_rows_count >= step.rows_count) return "";
    var label = num(step.included_rows_count) + " of " + num(step.rows_count) + " rows here";
    return '<text class="scope-out" data-tip="' + esc(c.id + " holds " +
      num(step.rows_count) + " rows; this figure descends from " + num(step.included_rows_count)) +
      '" x="' + c.x + '" y="39">' + esc(clip(label, Math.floor(room / 5.6))) +
      "</text>";
  }

  function drawStub(c, gone, i) {
    var y = c.bottom + 4 + i * 17;
    var fact = D.branches[gone.branch];
    var budget = Math.floor((COLUMN - BAR - STUB - 10) / 6.3);
    return '<line class="scope-stub" x1="' + c.x + '" y1="' + y + '" x2="' +
      (c.x + STUB) + '" y2="' + y + '"/>' +
      '<text class="scope-stub-label" data-cut="' + esc(c.id + SEP + gone.branch) +
      '" data-tip="' + esc(fact.label + " — " + num(gone.rows) + " row" +
      (gone.rows === 1 ? "" : "s") + ", none of them in this figure") +
      '" x="' + (c.x + STUB + 5) + '" y="' + (y + 3) + '">' +
      esc(clip(num(gone.rows) + " " + fact.label, budget)) + "</text>";
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

  function pickedCut() {
    if (!pickedNode) return null;
    var branch = pickedNode.slice(pickedNode.indexOf(SEP) + 1);
    var known = cols.some(function (c) {
      return c.nodes.some(function (n) { return n.key === pickedNode; });
    });
    return !known && D.branches[branch] ? branch : null;
  }

  function selected() {
    if (pickedRow != null) return [pickedRow];
    if (!pickedNode) return D.covers.ordinals;
    var node = cols.reduce(function (all, c) { return all.concat(c.nodes); }, [])
      .find(function (n) { return n.key === pickedNode; });
    return node ? node.on : [];
  }

  // ── drilling into a cut ──────────────────────────────────────────────────
  //
  // A drilled view is one branch id, so `?cut=` addresses it. The rows behind a cut
  // arrive as counts per path; the synthetic index below sizes the ribbons and is
  // never a name for a row.

  function enterCut(branch) {
    if (!openCut(branch)) return;
    history.pushState({ cut: branch }, "", addressOf(branch));
  }
  window.scopeEnterCut = enterCut;

  function leaveCut() {
    if (!closeCut()) return;
    history.pushState({ cut: null }, "", addressOf(null));
  }
  window.scopeLeaveCut = leaveCut;

  function addressOf(branch) {
    var query = new URLSearchParams(location.search);
    if (branch) query.set("cut", branch); else query.delete("cut");
    return location.pathname + "?" + query.toString();
  }

  window.addEventListener("popstate", function () {
    var wanted = new URLSearchParams(location.search).get("cut");
    if (wanted === (D.drilled || {}).branch) return;
    if (wanted) openCut(wanted); else closeCut();
  });

  function openCut(branch) {
    var cut = (D.cuts || {})[branch];
    if (!cut) return false;
    if (D.drilled) closeCut();
    BACK.push({ map: D, node: pickedNode });
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
      drilled: { branch: branch, label: D.branches[branch].label,
                 stage: D.branches[branch].stage_id, total: cut.total },
    });
    pickedNode = null;
    pickedRow = null;
    shape();
    return true;
  }

  function closeCut() {
    var previous = BACK.pop();
    if (!previous) return false;
    D = previous.map;
    pickedNode = previous.node;
    pickedRow = null;
    shape();
    return true;
  }

  // ── the words under the drawing ──────────────────────────────────────────

  function render() {
    draw();
    renderHere();
    renderBar();
    renderDetail();
    renderTable();
    var stubs = cols.reduce(function (all, c) {
      return all.concat(c.gone.map(function (g) { return g.rows; }));
    }, [0]);
    var widest = Math.max.apply(null, stubs);
    byId("scope-legend").textContent = widest
      ? "A dashed stub is a branch none of these rows took. Its count is true and its " +
        "width is not — the biggest is " + num(widest) + " against " +
        num(D.covers.ordinals.length) + " rows. Click one to draw those rows instead."
      : "";
  }

  function renderHere() {
    var here = byId("scope-here");
    var note = byId("scope-drilled");
    var section = document.querySelector(".scope");
    if (!D.drilled) {
      here.hidden = true;
      section.removeAttribute("data-drilled");
      note.textContent = "";
      return;
    }
    here.hidden = false;
    here.textContent = "back to " + D.citation.stage_id + "." + D.citation.column;
    here.onclick = leaveCut;
    section.setAttribute("data-drilled", "");
    note.innerHTML = "<b>" + num(D.drilled.total) + "</b> row" +
      (D.drilled.total === 1 ? "" : "s") + " that <code>" + esc(D.drilled.stage) +
      "</code> " + esc(D.drilled.label) + ", drawn over <code>" +
      esc(D.covers.at_stage) + "</code> where they were still present. Their path is " +
      "why they left: whatever they did differently is upstream of that stage.";
  }

  function renderBar() {
    var bar = byId("scope-bar");
    var cut = pickedCut();
    if (cut) {
      var known = (D.cuts || {})[cut];
      var left = (D.reach.find(function (r) { return r.branch === cut; }) || {}).taken || 0;
      bar.innerHTML = "<span><b>" + num(left) + "</b> row" + (left === 1 ? "" : "s") +
        " took <code>" + esc(D.branches[cut].label) +
        "</code>. None of them are in this figure.</span>" +
        (known ? ' <span class="scope-clear" data-enter="' + esc(cut) +
          '">draw these rows instead</span>' : "");
    } else if (pickedFigure()) {
      bar.innerHTML = "<span><b>1</b> row of <code>" + esc(D.citation.stage_id) +
        "</code> — row " + D.cited_row.ordinal + ", merged from " +
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
    bar.querySelectorAll("[data-enter]").forEach(function (el) {
      el.onclick = function () { enterCut(el.dataset.enter); };
    });
  }

  function renderDetail() {
    var host = byId("scope-detail");
    if (!pickedNode || pickedCut()) { host.innerHTML = ""; return; }
    var stageId = pickedNode.split(SEP)[0];
    var held = pickedNode.slice(stageId.length + 1);
    var facts = (held ? held.split(",") : []).map(function (b) {
      return D.branches[b];
    }).filter(Boolean);
    var stage = D.stages.find(function (s) { return s.id === stageId; });
    var source = facts.length && facts[0].reason !== "code"
      ? facts[0].source : (stage || {}).code;
    if (!source) { host.innerHTML = ""; return; }
    var lit = new Set(facts.reduce(function (all, f) {
      return all.concat(lineRange(f));
    }, []).filter(Boolean));
    host.innerHTML = '<pre class="scope-source">' + source.split("\n")
      .map(function (line, i) {
        var text = esc(line) || " ";
        return lit.has(i + 1) ? '<span class="is-lit">' + text + "</span>" : text;
      }).join("\n") + "</pre>";
  }

  function renderTable() {
    var cut = pickedCut();
    if (cut) { renderCutTable(cut); return; }
    if (pickedFigure()) { renderCitedRow(); return; }
    var wanted = new Set(selected());
    var head = headOf(D.columns);
    var body = D.rows.filter(function (r) {
      // Drilled, the drawn ordinals are synthetic counts; a sample row is placed by
      // the path it is on instead.
      if (!D.drilled) return wanted.has(r.ordinal);
      if (!pickedNode) return true;
      return nodeKeyForPath(pickedNode.split(SEP)[0], r.branch_path_index) === pickedNode;
    }).map(function (r) {
      return '<tr class="' + (r.ordinal === pickedRow ? "is-on" : "") +
        '" data-row="' + r.ordinal + '">' + rowOf(r);
    }).join("");
    var host = byId("scope-table");
    host.innerHTML = TABLE + "<thead>" + head + "</thead><tbody>" + body +
      "</tbody></table>";
    host.querySelectorAll("tbody tr").forEach(function (tr) {
      tr.onclick = function () {
        var ordinal = Number(tr.dataset.row);
        pickedRow = pickedRow === ordinal ? null : ordinal;
        render();
      };
    });
  }

  // The figure's node is ONE output row, not a slice of what fed it.
  function pickedFigure() {
    if (!pickedNode || D.drilled) return false;
    return cols.some(function (c) {
      return c.id === D.citation.stage_id &&
        c.nodes.some(function (n) { return n.key === pickedNode; });
    });
  }

  function headOf(columns) {
    return '<tr><th class="scope-num">row</th>' +
      columns.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("") + "</tr>";
  }

  // Cells are positional against the map's `columns`, the way every table here is.
  function rowOf(r) {
    return '<td class="scope-num">' + r.ordinal + "</td>" +
      r.cells.map(cell).join("") + "</tr>";
  }

  function renderCitedRow() {
    var row = D.cited_row;
    byId("scope-table").innerHTML = TABLE + "<thead>" + headOf(row.columns) +
      "</thead><tbody><tr>" + rowOf(row) + "</tbody></table>";
  }

  function renderCutTable(branch) {
    var cut = (D.cuts || {})[branch];
    var host = byId("scope-table");
    if (!cut) {
      host.innerHTML = "";
      return;
    }
    var head = headOf(cut.columns);
    var body = cut.rows.map(function (r) { return "<tr>" + rowOf(r); }).join("");
    host.innerHTML = TABLE + "<thead>" + head + "</thead><tbody>" + body +
      "</tbody></table>";
  }

  var every = byId("scope-every-stage");
  if (every) {
    every.onchange = function () { showAll = every.checked; shape(); };
  }
  shape();
  var asked = new URLSearchParams(location.search).get("cut");
  if (asked) openCut(asked);
})();
