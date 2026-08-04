/* diagram_viewport.js — make a mermaid diagram BIG + EXPANDABLE.
 *
 * Wraps a <pre class="mermaid"> in a scrollable, zoomable, pannable, fullscreen-able
 * viewport. Shared by the data-model (ER) and workflow (graph) sections so both render
 * at a legible size instead of being shrunk by mermaid's useMaxWidth inside a narrow
 * column.
 *
 * Expected markup:
 *   <div class="diagram-block">
 *     <div class="diagram-tools">
 *       <button data-zoom="in">+</button> <button data-zoom="out">−</button>
 *       <button data-zoom="reset">reset</button> <button class="diagram-fs">⛶ fullscreen</button>
 *     </div>
 *     <div class="diagram-viewport"><pre class="mermaid">...</pre></div>
 *   </div>
 *
 * The page must call mermaid.initialize({ startOnLoad: false, ... }) BEFORE loading this
 * file; boot() renders the diagrams then wires each viewport. A caller that re-renders a
 * diagram in place (e.g. the workflow belief-recolor) should call viewport._refit() after
 * mermaid.run() so the new svg is re-measured + re-fit.
 */
(function () {
  "use strict";

  function initDiagramViewport(vp) {
    if (!vp || vp._dvInit) return;
    vp._dvInit = true;
    var block = vp.closest(".diagram-block") || document;
    var svg = null, baseW = 1000, scale = 1;

    function grabSvg() {
      svg = vp.querySelector("svg");
      if (!svg) return false;
      var vb = (svg.getAttribute("viewBox") || "0 0 1000 800").split(/\s+/).map(Number);
      baseW = vb[2] || svg.getBoundingClientRect().width || 1000;
      svg.style.maxWidth = "none";   // defeat mermaid useMaxWidth so zoom is real
      svg.style.height = "auto";
      return true;
    }
    function apply() { if (svg) svg.style.width = (baseW * scale) + "px"; }
    function fit() {
      if (!svg) return;
      // Collapse the svg FIRST so the diagram's own width can't inflate the viewport
      // (its container is content-sized in some layouts); then measure the real width.
      svg.style.width = "0px";
      scale = Math.min(1, (vp.clientWidth - 24) / baseW);
      apply();
    }
    function zoom(f) {
      if (!svg) return;
      var cx = vp.scrollLeft + vp.clientWidth / 2, cy = vp.scrollTop + vp.clientHeight / 2;
      scale = Math.min(8, Math.max(0.1, scale * f)); apply();
      vp.scrollLeft = cx * f - vp.clientWidth / 2; vp.scrollTop = cy * f - vp.clientHeight / 2;
    }

    // Re-grab the (possibly new) svg after a re-render and fit to width again.
    vp._refit = function () { if (grabSvg()) fit(); };

    // Outline one node and bring it into view, zooming in first when the diagram is
    // fitted so small that the node is unreadable. Returns false when the id names no
    // node — the caller may be racing a re-render and want to retry.
    vp._focusNode = function (stageId, minScale) {
      if (!svg && !grabSvg()) return false;
      svg.querySelectorAll("g.node.wf-node-active")
        .forEach(function (n) { n.classList.remove("wf-node-active"); });
      if (!stageId) return true;
      var node = findNode(stageId);
      if (!node) return false;
      node.classList.add("wf-node-active");
      if (scale < (minScale || 1)) { scale = minScale || 1; apply(); }
      // Measure AFTER any zoom: the box moves when the svg is rescaled. clientWidth,
      // not the rect's width, so a visible scrollbar does not shift the centre.
      var box = node.getBoundingClientRect(), port = vp.getBoundingClientRect();
      vp.scrollTo({
        left: vp.scrollLeft + (box.left + box.width / 2) - (port.left + vp.clientWidth / 2),
        top: vp.scrollTop + (box.top + box.height / 2) - (port.top + vp.clientHeight / 2),
        behavior: "smooth",
      });
      pulse(node);
      return true;
    };

    // mermaid ids a flowchart node "mermaid-<salt>-flowchart-<stageId>-<n>", where the
    // salt is a per-render timestamp. Anchoring the stage id between "flowchart-" and a
    // trailing index matches the whole id, so a stage whose id is a substring of another
    // ("route" vs "solicitation_route") cannot win the lookup.
    function findNode(stageId) {
      var escaped = stageId.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      var wanted = new RegExp("(?:^|-)flowchart-" + escaped + "-\\d+$");
      var nodes = svg.querySelectorAll("g.node");
      for (var i = 0; i < nodes.length; i++) {
        if (wanted.test(nodes[i].id)) return nodes[i];
      }
      return null;
    }

    // Restart the arrival pulse even when the same node is focused twice in a row:
    // re-adding a class the element already carries does not replay its animation.
    function pulse(node) {
      node.classList.remove("wf-node-arriving");
      void node.getBoundingClientRect();          // force a style flush between removal and re-add
      node.classList.add("wf-node-arriving");
    }

    if (grabSvg()) fit();

    block.querySelectorAll("[data-zoom]").forEach(function (b) {
      b.addEventListener("click", function () {
        var k = b.dataset.zoom;
        if (k === "reset") fit(); else zoom(k === "in" ? 1.25 : 0.8);
      });
    });
    var fsBtn = block.querySelector(".diagram-fs");
    if (fsBtn) fsBtn.addEventListener("click", function () {
      if (document.fullscreenElement) document.exitFullscreen();
      else if (vp.requestFullscreen) vp.requestFullscreen();
    });
    document.addEventListener("fullscreenchange", function () {
      if (document.fullscreenElement === vp) fit();   // fullscreen changed clientWidth
    });

    // ⌘/Ctrl + wheel = zoom; plain wheel = native scroll.
    vp.addEventListener("wheel", function (e) {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault(); zoom(e.deltaY < 0 ? 1.1 : 0.9);
    }, { passive: false });

    // Drag to pan — WITHOUT stealing clicks. Panning engages only after the pointer
    // moves past a small threshold, and captures the pointer only THEN. A plain click
    // (no drag) is never captured, so it reaches the mermaid node's own click handler.
    // (Capturing on every pointerdown is what broke node → review navigation.)
    var down = false, dragging = false, sx = 0, sy = 0, sl = 0, st = 0, pid = null;
    vp.addEventListener("pointerdown", function (e) {
      if (e.button !== 0) return;                       // left button only
      down = true; dragging = false; pid = e.pointerId;
      sx = e.clientX; sy = e.clientY; sl = vp.scrollLeft; st = vp.scrollTop;
    });
    vp.addEventListener("pointermove", function (e) {
      if (!down) return;
      var dx = e.clientX - sx, dy = e.clientY - sy;
      if (!dragging) {
        if (Math.abs(dx) + Math.abs(dy) < 5) return;    // below threshold: still a click
        dragging = true;
        vp.classList.add("grabbing");
        try { vp.setPointerCapture(pid); } catch (_) {}
      }
      vp.scrollLeft = sl - dx; vp.scrollTop = st - dy;
    });
    function endPan() { down = false; if (dragging) vp.classList.remove("grabbing"); }
    vp.addEventListener("pointerup", endPan);
    vp.addEventListener("pointercancel", endPan);
    // Swallow the click a drag would otherwise synthesize (so panning never opens a
    // node); a plain click (dragging=false) passes straight through.
    vp.addEventListener("click", function (e) {
      if (dragging) { e.stopPropagation(); e.preventDefault(); dragging = false; }
    }, true);
  }

  async function boot() {
    var vps = Array.prototype.slice.call(document.querySelectorAll(".diagram-viewport"));
    if (!vps.length) return;
    if (window.mermaid) {
      try { await mermaid.run({ querySelector: ".diagram-viewport .mermaid" }); }
      catch (e) { console.error("mermaid render failed", e); }
    }
    vps.forEach(initDiagramViewport);
  }

  window.initDiagramViewport = initDiagramViewport;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
