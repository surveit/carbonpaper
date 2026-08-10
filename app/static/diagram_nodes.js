/* diagram_nodes.js — the ONE binding between a mermaid stage node and the page.
 *
 * build_mermaid_graph (app/web/diagrams.py) emits, for every node:
 *     click <stage_id> call dvNode("<stage_id>") "Open stage"
 * mermaid resolves that callback BY NAME, at click time, and silently no-ops when
 * the name is missing. That used to mean every page rendering the graph had to
 * define its own `window.loadStage` global; a page that forgot got nodes that
 * looked live (mermaid styles them .clickable regardless) and did nothing at all
 * — no error, nothing in the console. version_detail.html was exactly that.
 *
 * So `dvNode` is defined HERE, once, and loaded from base.html — it always exists.
 * It re-broadcasts as a DOM event; pages subscribe with onDiagramNode(). Two
 * consequences worth keeping:
 *   · a page that never subscribes gets a real no-op AND a console warning, so the
 *     failure is loud instead of silent;
 *   · subscribing is what marks the document node-clickable, and the pointer-cursor
 *     CSS keys off that same mark — affordance and handler can no longer diverge.
 */
(function () {
  "use strict";

  var EVENT = "diagram:node";
  var subscribed = false;

  // Called by mermaid's `click … call dvNode(…)` binding. Always defined.
  window.dvNode = function (stageId) {
    if (!subscribed) {
      console.warn(
        "diagram_nodes: node '" + stageId + "' clicked, but this page never called " +
        "onDiagramNode() — nothing is listening for stage clicks."
      );
      return;
    }
    document.dispatchEvent(new CustomEvent(EVENT, { detail: { id: stageId } }));
  };

  // Subscribe to stage-node clicks. Also marks the document node-clickable, which
  // is what turns on the pointer cursor (see .mermaid .clickable in diagram.css).
  window.onDiagramNode = function (handler) {
    subscribed = true;
    document.documentElement.setAttribute("data-diagram-clickable", "1");
    document.addEventListener(EVENT, function (e) { handler(e.detail.id); });
  };
})();
