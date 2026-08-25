// Putting one stage's server-rendered partial into the panel a page keeps for it, and
// outlining that stage in the graph beside it. Three pages hold such a panel — the
// working copy's stage review, a frozen version's, and a run's — and differ in the URL
// they fetch and in what they do once the partial is in.

// innerHTML parses <script> without running it, and every stage partial ships its own
// wiring, so each one is re-created here or the panel arrives inert.
async function loadStagePanel(panel, url, stageId) {
  const answer = await fetch(url);
  if (!answer.ok) {
    panel.innerHTML = `<div class="empty-state"><h2>Error</h2>` +
      `<p>${answer.status} loading stage <code>${stageId}</code></p></div>`;
    return false;
  }
  panel.innerHTML = await answer.text();
  panel.querySelectorAll('script').forEach(function (inert) {
    const live = document.createElement('script');
    if (inert.src) live.src = inert.src; else live.textContent = inert.textContent;
    inert.replaceWith(live);
  });
  if (location.hash !== `#${stageId}`) history.replaceState(null, '', `#${stageId}`);
  return true;
}

// Retries while the svg is missing: mermaid renders asynchronously, so a deep link on
// load asks for the outline before there is a graph to draw it on.
function outlineGraphNode(stageId, tries) {
  const svg = document.querySelector('.diagram-viewport svg');
  if (!svg) {
    if (stageId && (tries || 0) < 12) {
      setTimeout(() => outlineGraphNode(stageId, (tries || 0) + 1), 300);
    }
    return;
  }
  svg.querySelectorAll('g.node.wf-node-active').forEach(n => n.classList.remove('wf-node-active'));
  if (!stageId) return;
  const id = (window.CSS && CSS.escape) ? CSS.escape(stageId) : stageId;
  // mermaid renders each flowchart node as <g class="node …" id="flowchart-<id>-<n>">
  const node = svg.querySelector(`g.node[id^="flowchart-${id}-"]`)
            || svg.querySelector(`g.node[id*="${id}"]`);
  if (node) node.classList.add('wf-node-active');
}
