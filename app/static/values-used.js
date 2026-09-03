// The Relevant columns tab's walk: which stage is on screen, and every way the
// reader moves between them. Each stage's panel is the RUN PAGE'S OWN, fetched
// scoped to the rows behind this figure — this file never draws one.
window.ValuesUsed = window.ValuesUsed || (function(){
  const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

  function mount(root){
    const pane = root.querySelector('.vu');
    if(!pane) return;
    const nav = JSON.parse(root.querySelector('.vu-nav').textContent);
    const view = {
      pane, nav, trail: [nav.cited_stage],
      viewport: pane.querySelector('.diagram-viewport'),
      scroll: pane.querySelector('.vu-scroll'),
      loaded: {},
    };
    wire(view);
    drawGraph(view);
    show(view);
    return view;
  }

  const at = view => view.trail[view.trail.length - 1];

  function show(view){
    const here = at(view);
    view.pane.querySelectorAll('.vu-step').forEach(
      step => step.classList.toggle('vu-on', step.dataset.step === here));
    // The pane owns its scroll, so the document never moves when a step changes;
    // the reader still wants the new sheet from the top.
    view.scroll.scrollTop = 0;
    // An arrow that would do nothing says so, rather than swallowing the click.
    view.pane.querySelector('.vu-arrow[data-go="back"]').disabled =
      !carrying(view, here).length;
    view.pane.querySelector('.vu-arrow[data-go="fwd"]').disabled = view.trail.length < 2;
    focusNode(view, here);
    loadPanel(view, here);
  }

  // Back only ever moves along a wire that carried rows: a parent none of this
  // figure's rows came through is a stage the value did not take.
  function carrying(view, stageId){
    return (view.nav.sources[stageId] || []).filter(source => source.rows > 0);
  }

  // Along an edge, so the step is already on the route.
  function goTo(view, stageId){ view.trail.push(stageId); show(view); }

  // A click lands anywhere on the map, so the trail is re-laid as the route from the
  // figure to that stage. Pushing a jump made it a click history instead, leaving
  // stages the value never came through lit as walked.
  function jumpTo(view, stageId){
    const seen = view.trail.indexOf(stageId);
    if(seen >= 0) view.trail = view.trail.slice(0, seen + 1);
    else if(carrying(view, at(view)).some(s => s.stage_id === stageId))
      view.trail.push(stageId);
    else view.trail = findRoute(view, stageId);
    show(view);
  }

  // Breadth-first over the sources, so a jump lands on the shortest route the
  // value took from the figure to that stage. A stage off every route — which the
  // minimap can hold, since it draws the whole walk — stands alone.
  function findRoute(view, stageId){
    const start = view.nav.cited_stage, cameFrom = {}, queue = [start];
    cameFrom[start] = null;
    while(queue.length){
      const here = queue.shift();
      if(here === stageId){
        const route = [];
        for(let step = stageId; step !== null; step = cameFrom[step]) route.unshift(step);
        return route;
      }
      for(const source of carrying(view, here)){
        if(source.stage_id in cameFrom) continue;
        cameFrom[source.stage_id] = here;
        queue.push(source.stage_id);
      }
    }
    return [stageId];
  }

  // Two parents both carrying rows is a review decision, so it is left to the
  // reader: both wires are already drawn hot, and they click the one they want.
  function stepBack(view){
    const sources = carrying(view, at(view));
    if(sources.length === 1) goTo(view, sources[0].stage_id);
  }

  function stepForward(view){
    if(view.trail.length < 2) return;
    view.trail.pop();
    show(view);
  }

  function runBase(view){
    return `/project/${encodeURIComponent(view.pane.dataset.project)}`
         + `/runs/${encodeURIComponent(view.pane.dataset.run)}`;
  }

  // The run page's own panel for this stage, cut to the rows behind the figure.
  async function loadPanel(view, stageId){
    const slot = view.pane.querySelector(`.vu-panel[data-panel="${CSS.escape(stageId)}"]`);
    if(!slot || view.loaded[stageId]) return;
    view.loaded[stageId] = true;
    const query = new URLSearchParams({
      stage: view.nav.cited_stage, row: view.pane.dataset.row,
      column: view.nav.column,
    });
    slot.innerHTML = '<p class="muted">loading…</p>';
    try {
      const answer = await fetch(
        `${runBase(view)}/stage/${encodeURIComponent(stageId)}/traced?${query}`);
      slot.innerHTML = answer.ok ? await answer.text()
        : `<p class="muted">could not load ${esc(stageId)} (${answer.status})</p>`;
      // The panel ships its own wiring, which innerHTML does not run.
      slot.querySelectorAll('script').forEach(source => {
        const run = document.createElement('script');
        run.textContent = source.textContent;
        source.replaceWith(run);
      });
    } catch(failure){
      view.loaded[stageId] = false;
      slot.innerHTML = `<p class="muted">error: ${esc(failure)}</p>`;
    }
  }

  // The same renderer, band and viewport the run page's minimap uses. This pane
  // arrives by fetch, after diagram_viewport.js has already booted on nothing.
  function drawGraph(view){
    if(!view.viewport || !window.renderDiagramViewports) return;
    window.renderDiagramViewports(view.pane).then(() => focusNode(view, at(view)));
  }

  function focusNode(view, stageId){
    if(view.viewport && view.viewport._focusNode) view.viewport._focusNode(stageId);
  }

  const CONTROLS = '.vu-arrow, th.diff-col-jump';

  function wire(view){
    // A stage node is clicked through the shared dvNode binding, not a handler of
    // this pane's own — the graph is the workflow graph everywhere it is drawn.
    if(window.onDiagramNode) window.onDiagramNode(stageId => {
      if(view.pane.offsetParent !== null) jumpTo(view, stageId);
    });
    // Focusing a control inside a horizontally scrolling strip makes the browser
    // scroll to reveal it. Taking the default focus away is what stops that.
    view.pane.addEventListener('mousedown', event => {
      if(event.target.closest(CONTROLS)) event.preventDefault();
    });
    view.pane.addEventListener('click', event => {
      const control = event.target.closest(CONTROLS);
      if(control) control.focus({preventScroll: true});
      const header = event.target.closest('th.diff-col-jump');
      if(header) return jumpTo(view, header.dataset.jump);
      const arrow = event.target.closest('.vu-arrow');
      if(arrow) return arrow.dataset.go === 'back' ? stepBack(view) : stepForward(view);
    });
    document.addEventListener('keydown', event => {
      if(view.pane.offsetParent === null) return;
      const focused = document.activeElement;
      if(focused && focused.closest('input, textarea, [contenteditable]')) return;
      if(event.key === 'ArrowLeft') stepBack(view);
      if(event.key === 'ArrowRight') stepForward(view);
    });
  }

  return {mount};
})();
