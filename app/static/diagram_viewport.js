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
 * data-zoom-floor on the viewport sets the smallest scale fit-to-width may choose, so a
 * short band can hold a wide graph at a readable size and pan instead of shrinking it.
 * Fullscreen ignores the floor and fits the whole graph on both axes, centred.
 *
 * boot() configures mermaid from the palette, renders the diagrams, then wires each
 * viewport — the page supplies neither theme nor mermaid.initialize call. A caller that
 * re-renders a diagram in place (e.g. the workflow graph after a spec edit) should call
 * viewport._refit() after mermaid.run() so the new svg is re-measured + re-fit.
 */
(function () {
  "use strict";

  function initDiagramViewport(vp) {
    if (!vp || vp._dvInit) return;
    vp._dvInit = true;
    var block = vp.closest(".diagram-block") || document;
    // zoomFloor is the smallest scale this viewport considers legible: fit-to-width
    // will not go below it, and focusing a node lifts a smaller scale up to it.
    var svg = null, baseW = 1000, baseH = 800, scale = 1;
    var zoomFloor = Number(vp.dataset.zoomFloor) || 0;
    var MIN_SCALE = 0.1, MAX_SCALE = 8, PAD = 24;

    function grabSvg() {
      svg = vp.querySelector("svg");
      if (!svg) return false;
      var vb = (svg.getAttribute("viewBox") || "0 0 1000 800").split(/\s+/).map(Number);
      baseW = vb[2] || svg.getBoundingClientRect().width || 1000;
      baseH = vb[3] || svg.getBoundingClientRect().height || 800;
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
      scale = Math.max(zoomFloor, Math.min(1, (vp.clientWidth - PAD) / baseW));
      apply();
    }
    // Fullscreen is the survey: fit the whole graph on BOTH axes and park it in the
    // middle. The zoom floor is not honoured here — it exists to stop a 200px band
    // shrinking a graph below legibility, and a screen that holds all of it has no
    // such trade to make.
    function fitWholeGraph() {
      if (!svg) return;
      svg.style.width = "0px";
      scale = Math.max(MIN_SCALE, Math.min(
        1, (vp.clientWidth - PAD) / baseW, (vp.clientHeight - PAD) / baseH));
      apply();
      centreInViewport();
    }
    // Vertical centring of a graph SMALLER than the box is CSS (align-content on the
    // fullscreened viewport); this handles the other case, where it still overflows.
    function centreInViewport() {
      vp.scrollLeft = (vp.scrollWidth - vp.clientWidth) / 2;
      vp.scrollTop = (vp.scrollHeight - vp.clientHeight) / 2;
    }
    function zoom(f) {
      if (!svg) return;
      var cx = vp.scrollLeft + vp.clientWidth / 2, cy = vp.scrollTop + vp.clientHeight / 2;
      scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale * f)); apply();
      vp.scrollLeft = cx * f - vp.clientWidth / 2; vp.scrollTop = cy * f - vp.clientHeight / 2;
    }

    // Re-grab the (possibly new) svg after a re-render and fit to width again.
    vp._refit = function () { if (grabSvg()) fit(); };

    // Outline one node and bring it into view, lifting the zoom to zoomFloor first when
    // the diagram is fitted so small that the node is unreadable.
    // opts.pulse plays the arrival animation, which is for a jump the reader did not
    // aim (a deep link, a step in the guide rail) — clicking the node needs no cue,
    // the pointer is already on it. opts.select:false scrolls to the node WITHOUT
    // outlining it, for a caller parking the view somewhere the reader has not
    // chosen; opts.animate:false jumps instead of scrolling. Returns false when the
    // id names no node — the caller may be racing a re-render and want to retry.
    vp._focusNode = function (stageId, opts) {
      var o = opts || {};
      if (!svg && !grabSvg()) return false;
      svg.querySelectorAll("g.node.wf-node-active")
        .forEach(function (n) { n.classList.remove("wf-node-active"); });
      if (!stageId) return true;
      var node = findNode(stageId);
      if (!node) return false;
      if (o.select !== false) node.classList.add("wf-node-active");
      if (scale < zoomFloor) { scale = zoomFloor; apply(); }
      // Measure AFTER any zoom: the box moves when the svg is rescaled. clientWidth,
      // not the rect's width, so a visible scrollbar does not shift the centre.
      var box = node.getBoundingClientRect(), port = vp.getBoundingClientRect();
      vp.scrollTo({
        left: vp.scrollLeft + (box.left + box.width / 2) - (port.left + vp.clientWidth / 2),
        top: vp.scrollTop + (box.top + box.height / 2) - (port.top + vp.clientHeight / 2),
        // Smooth only when the reader is being MOVED from somewhere they were looking.
        // Arriving at the page — parked, or on a deep link — has no from, and animating
        // it plays a scroll nobody asked for from a position nobody saw.
        behavior: o.animate === false ? "auto" : "smooth",
      });
      if (o.pulse) pulse(node);
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
    // The BLOCK goes fullscreen, not the viewport, so whatever the block wraps around
    // the graph — its controls, and on the run page a coverage line — comes with it.
    var fsEl = block instanceof Element ? block : vp;
    var fsBtn = block.querySelector(".diagram-fs");
    if (fsBtn) fsBtn.addEventListener("click", function () {
      if (document.fullscreenElement) document.exitFullscreen();
      else if (fsEl.requestFullscreen) fsEl.requestFullscreen();
    });
    document.addEventListener("fullscreenchange", function () {
      // Both dimensions change, so re-fit either way. On the way out the band would
      // otherwise keep a scale chosen for a whole screen, which is unreadable in 200px.
      if (document.fullscreenElement === fsEl) fitWholeGraph();
      else if (!document.fullscreenElement) fit();
    });

    // ⌘/Ctrl + wheel = zoom; plain wheel = native scroll.
    //
    // The step follows the gesture's SIZE, not only its sign: a trackpad pinch fires a
    // stream of small-delta events, and a flat per-event step shot past the scale being
    // reached for. A firm scroll moves about 20%, a light one a fraction of that.
    var ZOOM_PER_WHEEL_PIXEL = 0.006, WHEEL_PIXEL_CAP = 40;
    vp.addEventListener("wheel", function (e) {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      var px = readWheelPixels(e);
      zoom(Math.exp(-Math.max(-WHEEL_PIXEL_CAP, Math.min(WHEEL_PIXEL_CAP, px))
                    * ZOOM_PER_WHEEL_PIXEL));
    }, { passive: false });

    // deltaY is in pixels only when deltaMode is 0. A line- or page-mode wheel reports 3
    // or 1, which a size-sensitive step would otherwise read as a barely-there nudge.
    function readWheelPixels(e) {
      if (e.deltaMode === 1) return e.deltaY * 16;                // lines
      if (e.deltaMode === 2) return e.deltaY * vp.clientHeight;    // pages
      return e.deltaY;
    }

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

  // Every palette value the graph spends, read off :root so mermaid follows the
  // stylesheet instead of carrying a second copy of it.
  var THEME_TOKENS = ["ui", "fg", "bg", "border", "sunk-deep", "diagram-edge"];

  function readPaletteTokens() {
    var css = getComputedStyle(document.documentElement);
    var tokens = {};
    THEME_TOKENS.forEach(function (t) { tokens[t] = css.getPropertyValue("--" + t).trim(); });
    return tokens;
  }

  // A page served without the palette resolves every token to "", which mermaid would
  // take literally and draw invisible text in.
  function dropUnset(vars) {
    Object.keys(vars).forEach(function (k) { if (!vars[k]) delete vars[k]; });
    return vars;
  }

  // themeVariables reach the "base" theme only — under "default" mermaid ignores them
  // and draws its own typeface and edge colour over the app's.
  function initMermaidTheme() {
    var p = readPaletteTokens();
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "loose",
      theme: "base",
      themeVariables: dropUnset({
        fontFamily: p.ui,
        fontSize: "15px",
        primaryColor: p.bg,
        primaryBorderColor: p.border,
        primaryTextColor: p.fg,
        textColor: p.fg,
        lineColor: p["diagram-edge"],
        edgeLabelBackground: p["sunk-deep"],
      }),
      // useMaxWidth:false — this file sizes the svg itself, so zoom is real.
      flowchart: {
        curve: "basis", padding: 18, nodeSpacing: 28, rankSpacing: 58, useMaxWidth: false,
      },
    });
  }

  async function boot() {
    var vps = Array.prototype.slice.call(document.querySelectorAll(".diagram-viewport"));
    if (!vps.length) return;
    if (window.mermaid) {
      initMermaidTheme();
      try { await mermaid.run({ querySelector: ".diagram-viewport .mermaid" }); }
      catch (e) { console.error("mermaid render failed", e); }
    }
    vps.forEach(initDiagramViewport);
  }

  window.initDiagramViewport = initDiagramViewport;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
