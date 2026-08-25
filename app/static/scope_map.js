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
  var BACK = [];
  var cols = [];
  var pickedNode = null;
  var pickedRow = null;
  var showAll = false;
  var openTab = "rows";
  var panels = {};

  var SEP = " ";
  // Gutter mark against a branch the drawn rows are here by. A glyph, not a tint:
  // highlight.js owns the code element's markup and rewrites it wholesale.
  var MARK = "\u25B8";
  var tableOf = function (stageId) {
    return '<table class="data-preview" data-stage="' + esc(stageId) + '">';
  };
  var COLUMN = 300, BAR = 11, TOP = 52, GAP = 16;
  var LABEL_PITCH = 30;
  var SHORTEST_BAND = 260, TALLEST_BAND = 620, BAND_PER_NODE = 72, SWEEPS = 4;

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

  // A removal is rows that LEFT here. An arm none of these rows took is not a
  // loss — those rows carried on.
  function leftHere(branch) {
    return (D.branches[branch] || {}).role === "removes";
  }

  function aliasAt(stageId) {
    return (D.aliased_merges || {})[stageId] || null;
  }

  function columns() {
    var drawn = D.stages.filter(function (stage) {
      return showAll || aliasAt(stage.id) ||
        D.covers.ordinals.some(function (_, i) {
          return branchesAt(i, stage.id).length;
        });
    });
    return drawn.map(function (stage) {
      var nodes = tally(stage);
      var alias = aliasAt(stage.id);
      // One node: the groups are aliased, so nothing here tells these rows apart.
      if (alias) nodes.forEach(function (n) { n.aliasOf = alias; });
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
      var expanded = !alias && stage.id !== D.nearest_merge &&
        (D.resolved_merges || []).indexOf(stage.id) >= 0;
      return Object.assign({}, stage,
        { nodes: nodes, gone: gone, alias: alias, expanded: expanded });
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
      if (column.id === D.citation.stage_id || column.gone.length || column.alias ||
          column.expanded ||
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
    if (node.aliasOf) {
      return num(node.aliasOf.on_route_groups_count) + " of " +
        num(node.aliasOf.groups_count) + " groups, by " +
        (node.aliasOf.group_by.join(", ") || "the whole frame");
    }
    if (node.isFigure) {
      return D.citation.column + " = " + figure(D.citation.value) +
        ", merged from " + num(node.rows);
    }
    var labels = node.branches.map(function (b) { return D.branches[b].label; });
    return labels.length ? labels.join(" + ") : "—";
  }

  function nodeTip(n) {
    if (!n.aliasOf) {
      return labelOf(n) + " — " + num(n.rows) + " row" + (n.rows === 1 ? "" : "s");
    }
    return n.aliasOf.stage_id + " grouped " + num(n.aliasOf.rows_count) +
      " rows into " + num(n.aliasOf.groups_count) + ". These " + num(n.rows) +
      " came through " + num(n.aliasOf.on_route_groups_count) +
      " of them. Click to list the groups.";
  }

  function draw() {
    var total = D.covers.ordinals.length || 1;
    var band = measureBand();
    var scale = Math.min.apply(null, cols.map(function (c) {
      return (band - (c.nodes.length - 1) * GAP) / total;
    }).concat([band / total]));
    var ribbons = gatherRibbons();
    orderNodes(ribbons);
    var x = 0;
    cols.forEach(function (c) {
      var y = TOP;
      c.nodes.forEach(function (n) {
        n.h = Math.max(2, (n.drawnRows == null ? n.rows : n.drawnRows) * scale);
        n.y = y;
        y += n.h + GAP;
      });
      c.x = x;
      c.bottom = Math.max(y, placeLabels(c.nodes));
      x += COLUMN;
    });
    stackRibbons(ribbons);
    var foot = cols.some(function (c) { return c.alias || c.expanded; }) ? 1 : 0;
    var height = Math.max.apply(null, cols.map(function (c) {
      return c.bottom;
    }).concat([TOP])) + 14 + foot * 17;

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
    svg.querySelectorAll("[data-expand]").forEach(function (el) {
      el.onclick = function (event) {
        event.stopPropagation();
        location.href = expandUrl(el.dataset.expand, el.dataset.want === "1");
      };
    });
    svg.onclick = clearPick;
  }

  // A ribbon travelling further up or down than it runs across bulges rather than
  // flows, so the busiest column sets the height and COLUMN caps it.
  function measureBand() {
    var most = Math.max.apply(null, cols.map(function (c) {
      return c.nodes.length;
    }).concat([1]));
    return Math.min(TALLEST_BAND, Math.max(SHORTEST_BAND, most * BAND_PER_NODE));
  }

  function gatherRibbons() {
    var seen = new Map();
    for (var ci = 0; ci < cols.length - 1; ci++) {
      for (var i = 0; i < D.covers.ordinals.length; i++) {
        var a = nodeAt(ci, i), b = nodeAt(ci + 1, i);
        if (!a || !b) continue;
        var key = ci + a.key + ">" + b.key;
        var ribbon = seen.get(key) || { ci: ci, a: a, b: b, rows: 0 };
        ribbon.rows += 1;
        seen.set(key, ribbon);
      }
    }
    return Array.from(seen.values());
  }

  // ── keeping the ribbons apart ────────────────────────────────────────────
  //
  // Two ribbons cross when the nodes they run between sit in the opposite order in
  // the two columns, or when they leave one node in a different order from the one
  // they arrive in. Both are ordering, not geometry, so both are fixable here.

  // Each column takes the average position of the columns either side, weighted by
  // rows, until the sweeps settle. The barycentre heuristic, and cheap at this size.
  function orderNodes(ribbons) {
    for (var pass = 0; pass < SWEEPS; pass++) {
      for (var down = 1; down < cols.length; down++) sortColumn(down, down - 1, ribbons);
      for (var up = cols.length - 2; up >= 0; up--) sortColumn(up, up + 1, ribbons);
    }
  }

  function sortColumn(ci, neighbour, ribbons) {
    var place = new Map();
    cols[neighbour].nodes.forEach(function (n, i) { place.set(n.key, i); });
    var held = new Map();
    cols[ci].nodes.forEach(function (n, i) { held.set(n.key, i); });
    var pull = new Map();
    ribbons.forEach(function (r) {
      if (r.ci !== Math.min(ci, neighbour)) return;
      var here = ci < neighbour ? r.a : r.b;
      var there = ci < neighbour ? r.b : r.a;
      if (!place.has(there.key)) return;
      var got = pull.get(here.key) || { sum: 0, rows: 0 };
      got.sum += place.get(there.key) * r.rows;
      got.rows += r.rows;
      pull.set(here.key, got);
    });
    cols[ci].nodes.sort(function (p, q) {
      return meanPlace(pull, held, p) - meanPlace(pull, held, q);
    });
  }

  // A node with nothing running to that neighbour has no opinion, so it stays put.
  function meanPlace(pull, held, node) {
    var got = pull.get(node.key);
    return got ? got.sum / got.rows : held.get(node.key);
  }

  // Each end is stacked in the order of the node at the OTHER end, so ribbons sharing
  // a node fan out instead of swapping over.
  function stackRibbons(ribbons) {
    ribbons.forEach(function (r) {
      r.h0 = r.a.h * (r.rows / Math.max(1, r.a.rows));
      r.h1 = r.b.h * (r.rows / Math.max(1, r.b.rows));
    });
    stackEnd(ribbons, "a", "b", "y0", "h0");
    stackEnd(ribbons, "b", "a", "y1", "h1");
  }

  function stackEnd(ribbons, end, other, edge, depth) {
    var groups = new Map();
    ribbons.forEach(function (r) {
      var group = groups.get(r[end].key) || [];
      group.push(r);
      groups.set(r[end].key, group);
    });
    groups.forEach(function (group) {
      group.sort(function (p, q) { return p[other].y - q[other].y; });
      var y = group[0][end].y;
      group.forEach(function (r) { r[edge] = y; y += r[depth]; });
    });
  }

  function nodeAt(ci, i) {
    var want = nodeKey(cols[ci], i);
    return cols[ci].nodes.find(function (n) { return n.key === want; });
  }

  // Each end is a share of ITS OWN node's height, so a ribbon into a node drawn
  // shorter than its inputs tapers — which is what an aggregate collapsing rows
  // looks like.
  function drawRibbon(r) {
    var x0 = cols[r.ci].x + BAR, x1 = cols[r.ci + 1].x;
    var lit = isLit(r.a) && isLit(r.b);
    var h0 = r.h0, h1 = r.h1, y0 = r.y0, y1 = r.y1;
    var m = (x0 + x1) / 2;
    return '<path class="scope-ribbon' + (lit ? " is-lit" : "") + '" d="' +
      "M" + x0 + "," + y0 + "C" + m + "," + y0 + " " + m + "," + y1 + " " + x1 +
      "," + y1 + "v" + h1 + "C" + m + "," + (y1 + h1) + " " + m + "," + (y0 + h0) +
      " " + x0 + "," + (y0 + h0) + 'Z"/>';
  }

  function isLit(node) {
    return !pickedNode || node.key === pickedNode;
  }

  // Returns where the label stack ends, which outruns the bars once they are thin.
  function placeLabels(nodes) {
    var y = TOP;
    nodes.forEach(function (n) {
      n.labelY = Math.max(y, n.y + n.h / 2);
      y = n.labelY + LABEL_PITCH;
    });
    return y;
  }

  function drawNode(c, n) {
    var room = COLUMN - BAR - 14;
    var implied = n.branches.every(function (b) {
      return D.branches[b].reason !== "code";
    });
    var dim = pickedNode && n.key !== pickedNode;
    var mark = '<rect class="scope-bar-mark' + (implied ? " is-implied" : "") +
      (dim ? " is-dim" : "") + '" data-node="' + esc(n.key) + '" data-tip="' +
      esc(nodeTip(n)) + '" x="' + c.x + '" y="' + n.y + '" width="' + BAR +
      '" height="' + n.h + '"/>';
    var mid = n.y + n.h / 2;
    var leader = Math.abs(n.labelY - mid) > 2
      ? '<path class="scope-leader" d="M' + (c.x + BAR) + "," + mid + "H" +
        (c.x + BAR + 6) + "V" + n.labelY + "H" + (c.x + BAR + 10) + '"/>'
      : "";
    var tx = c.x + BAR + 12;
    var full = labelOf(n);
    var short = clip(full, Math.floor(room / 6.1));
    return mark + leader +
      '<text class="scope-node" data-node="' + esc(n.key) +
      '" data-tip="' + esc(nodeTip(n)) + '" x="' + tx +
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
      drawMergeControl(c);
  }

  // Two facts about this stage on one line: how much of the frame here the figure
  // descends from — a blank reads as missing, not as all of them — and what the
  // stage took out of the workflow on the way in.
  function drawScale(c, room) {
    var step = (D.scale || []).find(function (s) {
      return s.stage === c.id && s.rows_count;
    });
    var budget = Math.floor(room / 5.6);
    var label = step
      ? num(step.included_rows_count) + " of " + num(step.rows_count) +
        (step.rows_count === 1 ? " row here" : " rows here")
      : "";
    var cut = c.gone.map(function (gone) {
      return drawRemoval(c, gone, Math.max(12, budget - label.length - 3));
    }).join("");
    if (!label && !cut) return "";
    var tip = step
      ? c.id + " holds " + num(step.rows_count) + " rows; this figure descends from " +
        num(step.included_rows_count)
      : c.id;
    return '<text class="scope-out" data-tip="' + esc(tip) + '" x="' + c.x +
      '" y="39">' + esc(clip(label, budget)) + cut + "</text>";
  }

  // A count, never a ribbon: 44,963 drawn beside 40 to scale is the scale-mixing
  // that makes a lie. It counts rows of the frame this stage READ, one column left.
  function drawRemoval(c, gone, room) {
    var label = num(gone.rows) + (gone.rows === 1 ? " row" : " rows") + " filtered here";
    return '<tspan class="scope-out-gone" data-cut="' + esc(c.id + SEP + gone.branch) +
      '" data-tip="' + esc(num(gone.rows) + (gone.rows === 1 ? " row was" : " rows were") +
      " dropped from the workflow at this stage") + '">' +
      esc("\u00a0\u00b7\u00a0" + clip(label, room)) + "</tspan>";
  }

  // Aliased or expanded, a merge stage offers the other reading of itself here.
  function drawMergeControl(c) {
    if (!c.alias && !c.expanded) return "";
    var y = c.bottom + 4;
    var want = c.alias ? "1" : "0";
    var text = c.alias
      ? "split into " + num(c.alias.on_route_groups_count) + " groups"
      : "fold " + num(c.nodes.length) + " groups back";
    var tip = c.alias
      ? c.id + " grouped " + num(c.alias.rows_count) + " rows into " +
        num(c.alias.groups_count) + ". Draw a node per group these rows went into."
      : "Draw " + c.id + " as one node again.";
    return '<text class="scope-expand" data-expand="' + esc(c.id) + '" data-want="' +
      want + '" data-tip="' + esc(tip) + '" x="' + (c.x + BAR + 12) + '" y="' +
      (y + 3) + '">' + esc(text) + "</text>";
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
    var cut = cols.some(function (c) { return c.gone.length; });
    byId("scope-legend").textContent = cut
      ? "The red count beside a column is rows that stage took out of the workflow — " +
        "click one to draw them. Nothing in the drawing is scaled to it."
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
    bar.querySelectorAll("[data-enter]").forEach(function (el) {
      el.onclick = function () { enterCut(el.dataset.enter); };
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
        '" data-row="' + r.ordinal + '">' + rowOf(r, D.columns);
    }).join("");
    var host = byId("scope-table");
    host.innerHTML = tableOf(D.covers.at_stage) + "<thead>" + head + "</thead><tbody>" +
      body + "</tbody></table>";
    // The ordinal draws the row's path on the map above; the cells beside it
    // leave for the lineage of what they hold.
    host.querySelectorAll("tbody tr .scope-num").forEach(function (num) {
      num.onclick = function () {
        var ordinal = Number(num.parentNode.dataset.row);
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
  function rowOf(r, columns) {
    return '<td class="scope-num">' + r.number + "</td>" +
      r.cells.map(function (value, i) { return cell(value, columns[i]); }).join("") +
      "</tr>";
  }

  function renderCitedRow() {
    var row = D.cited_row;
    byId("scope-table").innerHTML = tableOf(D.citation.stage_id) + "<thead>" +
      headOf(row.columns) + '</thead><tbody><tr data-row="' + row.ordinal + '">' +
      rowOf(row, row.columns) + "</tbody></table>";
  }

  function renderCutTable(branch) {
    var cut = (D.cuts || {})[branch];
    var host = byId("scope-table");
    if (!cut) {
      host.innerHTML = "";
      return;
    }
    var head = headOf(cut.columns);
    var body = cut.rows.map(function (r) {
      return '<tr data-row="' + r.ordinal + '">' + rowOf(r, cut.columns);
    }).join("");
    host.innerHTML = tableOf(cut.at_stage) + "<thead>" + head + "</thead><tbody>" +
      body + "</tbody></table>";
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
