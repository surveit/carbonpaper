// The Relevant columns tab's replay: which stage's sheet is on screen, and every way
// the reader moves between them. The sheets themselves are drawn by the server
// (_values_panel.html); this file only ever changes which one is shown.
window.ValuesUsed = window.ValuesUsed || (function(){
  const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

  function mount(root){
    const pane = root.querySelector('.vu');
    if(!pane) return;
    const nav = JSON.parse(root.querySelector('.vu-nav').textContent);
    const view = {
      pane, nav, trail: [nav.cited_stage], tab: 'data',
      map: pane.querySelector('.vu-map'),
      scroll: pane.querySelector('.vu-scroll'),
      modal: pane.querySelector('.vu-modal'),
      loaded: {},
    };
    wire(view);
    show(view);
    window.addEventListener('resize', () => drawWires(view));
    return view;
  }

  const at = view => view.trail[view.trail.length - 1];

  function show(view){
    const here = at(view), seen = new Set(view.trail);
    const next = new Set((view.nav.sources[here] || []).map(s => s.stage_id));
    view.pane.querySelectorAll('.vu-step').forEach(
      step => step.classList.toggle('vu-on', step.dataset.step === here));
    view.pane.querySelectorAll('.vu-node').forEach(node => {
      const id = node.dataset.node;
      node.classList.toggle('vu-on', id === here);
      node.classList.toggle('vu-seen', id !== here && seen.has(id));
      node.classList.toggle('vu-next', next.has(id));
    });
    // The pane owns its scroll, so the document never moves when a step changes;
    // the reader still wants the new sheet from the top.
    view.scroll.scrollTop = 0;
    // An arrow that would do nothing says so, rather than swallowing the click.
    view.pane.querySelector('.vu-arrow[data-go="back"]').disabled =
      !(view.nav.sources[here] || []).length;
    view.pane.querySelector('.vu-arrow[data-go="fwd"]').disabled = view.trail.length < 2;
    drawWires(view);
    applyTabs(view);
  }

  function applyTabs(view){
    view.pane.querySelectorAll('.vu-steptabs button').forEach(
      button => button.classList.toggle('on', button.dataset.tab === view.tab));
    view.pane.querySelectorAll('.vu-pane').forEach(
      pane => pane.classList.toggle('vu-off', pane.dataset.tab !== view.tab));
    if(view.tab === 'transform') loadTransform(view, at(view));
  }

  // Along an edge, so the step is already on the route.
  function goTo(view, stageId){ view.trail.push(stageId); show(view); }

  // A click lands anywhere on the map, so the trail is re-laid as the route from the
  // figure to that stage. Pushing a jump made it a click history instead, leaving
  // stages the value never came through lit as walked.
  function jumpTo(view, stageId){
    const seen = view.trail.indexOf(stageId);
    if(seen >= 0) view.trail = view.trail.slice(0, seen + 1);
    else if((view.nav.sources[at(view)] || []).some(s => s.stage_id === stageId))
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
      for(const source of view.nav.sources[here] || []){
        if(source.stage_id in cameFrom) continue;
        cameFrom[source.stage_id] = here;
        queue.push(source.stage_id);
      }
    }
    return [stageId];
  }

  function stepBack(view){
    const sources = view.nav.sources[at(view)] || [];
    if(!sources.length) return;
    if(sources.length === 1) return goTo(view, sources[0].stage_id);
    openFork(view, at(view), sources);
  }

  function stepForward(view){
    if(view.trail.length < 2) return;
    view.trail.pop();
    show(view);
  }

  // A fork is a review decision, so it is asked rather than picked.
  function openFork(view, stageId, sources){
    view.modal.querySelector('.vu-modal-title').textContent =
      `${stageId} takes rows from ${sources.length} sources.`;
    view.modal.querySelector('.vu-forklist').innerHTML = sources.map(source =>
      `<button class="vu-forkopt" type="button" data-fork="${esc(source.stage_id)}">`
      + `<b>${esc(source.stage_id)}</b><span>${esc(source.columns.slice(0, 6).join(', '))}`
      + `${source.columns.length > 6 ? ` +${source.columns.length - 6} more` : ''}`
      + `</span></button>`).join('');
    view.modal.hidden = false;
  }

  function closeFork(view){ view.modal.hidden = true; }

  async function loadTransform(view, stageId){
    const slot = view.pane.querySelector(`.vu-transform[data-transform="${CSS.escape(stageId)}"]`);
    if(!slot || view.loaded[stageId]) return;
    view.loaded[stageId] = true;
    const base = `/project/${encodeURIComponent(view.pane.dataset.project)}`
               + `/runs/${encodeURIComponent(view.pane.dataset.run)}`;
    const url = `${base}/stage/${encodeURIComponent(stageId)}`
              + `/lineage_panel?row=${encodeURIComponent(view.pane.dataset.row)}`;
    slot.innerHTML = '<p class="muted">loading…</p>';
    try {
      const answer = await fetch(url);
      slot.innerHTML = answer.ok ? await answer.text()
        : `<p class="muted">could not load ${esc(stageId)} (${answer.status})</p>`;
    } catch(failure){
      view.loaded[stageId] = false;
      slot.innerHTML = `<p class="muted">error: ${esc(failure)}</p>`;
    }
  }

  // Every edge is measured off the laid-out chips: the columns are a topological
  // order, so where a chip lands says nothing about which one feeds which.
  function drawWires(view){
    const map = view.map, svg = map.querySelector('.vu-wires');
    const seen = new Set(view.trail), origin = map.getBoundingClientRect();
    const chip = id => map.querySelector(`.vu-node[data-node="${CSS.escape(id)}"]`);
    svg.setAttribute('width', map.scrollWidth);
    svg.setAttribute('height', map.scrollHeight);
    svg.innerHTML = view.nav.edges.map(edge => {
      const from = chip(edge.from_stage), to = chip(edge.to_stage);
      if(!from || !to) return '';
      const a = from.getBoundingClientRect(), b = to.getBoundingClientRect();
      const x1 = a.right - origin.left + map.scrollLeft, y1 = a.top - origin.top + a.height / 2;
      const x2 = b.left - origin.left + map.scrollLeft, y2 = b.top - origin.top + b.height / 2;
      const mid = (x1 + x2) / 2;
      const hot = seen.has(edge.from_stage) && seen.has(edge.to_stage) ? ' class="vu-hot"' : '';
      return `<path${hot} d="M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}"/>`;
    }).join('');
  }

  const CONTROLS = '.vu-node, .vu-arrow, .vu-forkopt, .vu-modal-close, th.diff-col-jump, .vu-steptabs button';

  function wire(view){
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
      const tab = event.target.closest('.vu-steptabs button');
      if(tab){ view.tab = tab.dataset.tab; return applyTabs(view); }
      const node = event.target.closest('.vu-node');
      if(node) return jumpTo(view, node.dataset.node);
      const fork = event.target.closest('.vu-forkopt');
      if(fork){ closeFork(view); return goTo(view, fork.dataset.fork); }
      const arrow = event.target.closest('.vu-arrow');
      if(arrow) return arrow.dataset.go === 'back' ? stepBack(view) : stepForward(view);
      if(event.target.closest('.vu-modal-close') || event.target === view.modal) closeFork(view);
    });
    document.addEventListener('keydown', event => {
      if(view.pane.offsetParent === null) return;
      if(event.key === 'Escape') return closeFork(view);
      const focused = document.activeElement;
      if(focused && focused.closest('input, textarea, [contenteditable]')) return;
      if(event.key === 'ArrowLeft') stepBack(view);
      if(event.key === 'ArrowRight') stepForward(view);
    });
  }

  return {mount};
})();
