// The Rows & columns tab: the run as a canvas of stages and the sheets they wrote,
// the figure's own rows leading each sheet. Every panel the drawer opens is the
// RUN PAGE'S OWN, fetched cut to the rows behind this figure — this file draws none.
window.Canvas = window.Canvas || (function(){
  const esc = s => String(s).replace(/[&<>"]/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const fmt = n => window.Figures.text(n);

  const BOX_W = 210, BOX_H = 34, STEM = 6, SHEET_H = 96, FOOT = 30, LINE_H = 11;
  const LEVEL_DX = 300, LANE_DY = 40, LANE_TOP = 60, LEVEL_LEFT = 40;
  // The table under a box is the app's own, drawn at this fraction of its size.
  const MINI = 0.62;
  const WIRE_MIN = 2, WIRE_MAX = 30, CORE_MIN = 2.5;
  const ZOOM_MIN = 0.3, ZOOM_MAX = 3, ZOOM_STEP = 1.1;
  const DRAG_SLACK = 3;

  function mount(root){
    if(root.canvasTeardown) root.canvasTeardown();
    const pane = root.querySelector('.canvas');
    if(!pane) return;
    const nav = JSON.parse(pane.querySelector('.canvas-nav').textContent);
    const order = listDrawOrder(nav);
    const view = {
      pane, nav, order,
      typeLabels: JSON.parse(pane.querySelector('.canvas-type-labels').textContent),
      stages: placeStages(nav, order),
      widest: Math.max(0, ...nav.sheets.map(sheet => sheet.rows_out)),
      svg: pane.querySelector('.canvas-svg'),
      world: pane.querySelector('.canvas-world'),
      drawer: pane.querySelector('.canvas-drawer'),
      at: {x: 30, y: 20, k: 0.85},
      picked: null,
      panels: {},
      listeners: new AbortController(),
    };
    root.canvasTeardown = () => view.listeners.abort();
    draw(view);
    wire(view);
    return view;
  }

  // ── layout ──────────────────────────────────────────────────────────────
  // Sheets come in run order; a stage that wrote no frame follows them.
  function listDrawOrder(nav){
    const withSheet = nav.sheets.map(sheet => sheet.stage_id);
    const rest = nav.nodes.map(node => node.stage_id).filter(id => !withSheet.includes(id));
    return withSheet.concat(rest);
  }

  function placeStages(nav, order){
    const sheetOf = Object.fromEntries(nav.sheets.map(sheet => [sheet.stage_id, sheet]));
    const inputs = {};
    nav.edges.forEach(edge => (inputs[edge.to_stage] ||= []).push(edge.from_stage));
    const stages = {};
    nav.nodes.forEach(node => stages[node.stage_id] = {
      id: node.stage_id, glyph: node.glyph,
      on: node.on_walk || node.rows_behind > 0,
      sheet: sheetOf[node.stage_id] || null,
      inputs: inputs[node.stage_id] || [],
    });
    layOut(stages, order, nav.steps);
    return stages;
  }

  // The figure's own stages take the top lane, so its path reads as one line.
  function layOut(stages, order, steps){
    const level = measureLevels(stages, order);
    const lanes = {};
    order.forEach(id => (lanes[level[id]] ||= []).push(id));
    const rank = id => steps.includes(id) ? 0 : 1;
    Object.entries(lanes).forEach(([column, ids]) => {
      let y = LANE_TOP;
      ids.sort((p, q) => rank(p) - rank(q)).forEach(id => {
        const stage = stages[id];
        stage.x = LEVEL_LEFT + column * LEVEL_DX;
        stage.y = y;
        y += stackH(stage) + LANE_DY;
      });
    });
  }

  // Longest path from the sources; a source with no inputs slides right to one
  // column before its first consumer.
  function measureLevels(stages, order){
    const level = {};
    const measure = id => {
      if(!(id in level)){
        level[id] = -1;
        level[id] = Math.max(-1, ...stages[id].inputs.map(measure)) + 1;
      }
      return level[id];
    };
    order.forEach(measure);
    const consumers = {};
    order.forEach(id => stages[id].inputs.forEach(
      input => (consumers[input] ||= []).push(id)));
    order.forEach(id => {
      if(!stages[id].inputs.length && consumers[id])
        level[id] = Math.min(...consumers[id].map(consumer => level[consumer])) - 1;
    });
    return level;
  }

  const stackH = stage => BOX_H + (stage.sheet
    ? STEM + SHEET_H + FOOT + LINE_H * (sayShape(stage.sheet).length - 1) : 0);

  // ── drawing ─────────────────────────────────────────────────────────────
  function draw(view){
    view.world.innerHTML = view.nav.edges.map(edge => drawWire(view, edge)).join('')
      + view.order.map(id => drawBox(view, view.stages[id])
                           + drawSheet(view, view.stages[id])).join('');
    place(view);
  }

  function place(view){
    view.world.setAttribute(
      'transform', `translate(${view.at.x},${view.at.y}) scale(${view.at.k})`);
  }

  function drawBox(view, stage){
    const type = stage.sheet ? view.typeLabels[stage.id] : '';
    return `<g class="canvas-box${stage.on ? ' on' : ' aside'}" data-stage="${esc(stage.id)}">
      <rect class="box" x="${stage.x}" y="${stage.y}" width="${BOX_W}" height="${BOX_H}" rx="6"/>
      <text class="title" x="${stage.x + 10}" y="${stage.y + 15}">${esc(stage.glyph)} ${esc(stage.id)}</text>
      ${type ? `<text class="type" x="${stage.x + 10}" y="${stage.y + 27}">${esc(type)}</text>` : ''}
      <rect class="hit" x="${stage.x}" y="${stage.y}" width="${BOX_W}" height="${BOX_H}"/></g>`;
  }

  function drawSheet(view, stage){
    const sheet = stage.sheet;
    if(!sheet) return '';
    const top = stage.y + BOX_H + STEM;
    const middle = stage.x + BOX_W / 2;
    return `<g class="canvas-sheet" data-sheet="${esc(stage.id)}">
      <line class="stem" x1="${middle}" y1="${stage.y + BOX_H}" x2="${middle}" y2="${top}"/>
      <rect class="paper" x="${stage.x}" y="${top}" width="${BOX_W}" height="${SHEET_H}"/>
      <foreignObject x="${stage.x + 1}" y="${top + 1}" width="${BOX_W - 2}" height="${SHEET_H - 2}">
        <div xmlns="http://www.w3.org/1999/xhtml" class="canvas-mini"
             style="transform:scale(${MINI});width:${100 / MINI}%;height:${(SHEET_H - 2) / MINI}px">${drawMini(view, sheet)}</div>
      </foreignObject>
      ${drawBar(sheet, stage.x, top + SHEET_H + 5)}
      ${drawCaption(sheet, stage.x, top + SHEET_H + 23)}
      <rect class="hit" x="${stage.x}" y="${top}" width="${BOX_W}" height="${SHEET_H}"/></g>`;
  }

  // The figure's rows first, the rest dimmed; a filter's dropped rows in place,
  // labelled in words the way the stage diff labels them.
  function drawMini(view, sheet){
    const labelled = sheet.rows.some(row => row.dropped);
    const cited = sheet.stage_id === view.nav.cited_stage;
    const head = (labelled ? '<th class="diff-kept-col"></th>' : '')
      + sheet.columns.map(name => `<th>${esc(name)}</th>`).join('');
    const cell = (value, i, mine) => `<td${
      mine && cited && sheet.columns[i] === view.nav.column ? ' class="diff-col-cited"' : ''
    }>${esc(value)}</td>`;
    const line = row => `<tr class="${
      row.dropped ? 'diff-row-dropped' : row.mine ? 'diff-row-mine' : 'canvas-dim'}">${
      labelled ? `<td class="diff-kept-col">${row.dropped ? 'Dropped row' : ''}</td>` : ''
    }${row.cells.map((value, i) => cell(value, i, row.mine)).join('')}</tr>`;
    return `<div class="stage-diff"><table class="data-preview"><thead><tr>${head}</tr></thead>`
      + `<tbody>${sheet.rows.map(line).join('')}</tbody></table></div>`;
  }

  // The rows that came in, at full width: the figure's in blue from the left, the
  // dropped ones in red from the right, the other kept rows the grey between.
  function drawBar(sheet, x, top){
    const share = rows => sheet.rows_in ? BOX_W * rows / sheet.rows_in : 0;
    const behind = sheet.rows_behind ? Math.max(2, share(sheet.rows_behind)) : 0;
    const dropped = share(sheet.rows_dropped);
    return `<g class="canvas-bar" transform="translate(${x},${top})">
      <rect class="track" width="${BOX_W}" height="6" rx="1"/>
      ${behind ? `<rect class="thumb" width="${behind}" height="6" rx="1"/>` : ''}
      ${dropped ? `<rect class="cut" x="${BOX_W - dropped}" width="${dropped}" height="6" rx="1"/>` : ''}
    </g>`;
  }

  const plural = (n, word) => `${fmt(n)} ${word}${n === 1 ? '' : 's'}`;
  const sayFrame = (rows, columns) => `${plural(rows, 'row')} × ${plural(columns, 'column')}`;

  // A statement per line: the whole caption is wider than the sheet it sits under.
  function drawCaption(sheet, x, top){
    return sayShape(sheet).map((line, i) =>
      `<text class="n" x="${x}" y="${top + i * LINE_H}">${line}</text>`).join('');
  }

  // The frame the stage wrote, then the figure's part of it, then what it dropped.
  // A count of zero is left out rather than written: a cited column that counts
  // rows came through no column of this stage, and an aside stage no row of it.
  function sayShape(sheet){
    const columns = (sheet.columns_behind || []).length;
    const said = [sayFrame(sheet.rows_out, sheet.columns.length)];
    if(sheet.rows_behind) said.push((columns ? sayFrame(sheet.rows_behind, columns)
      : plural(sheet.rows_behind, 'row')) + ' behind this figure');
    if(sheet.rows_dropped) said.push(`${plural(sheet.rows_dropped, 'row')} dropped`);
    return said;
  }

  // Width on a square-root scale of the rows down the wire, over the widest
  // sheet in the run; the blue core is the rows behind the figure, the same way.
  function wireWidth(view, rows){
    if(!view.widest) return WIRE_MIN;
    return WIRE_MIN + (WIRE_MAX - WIRE_MIN) * Math.sqrt(rows / view.widest);
  }

  function drawWire(view, edge){
    const from = view.stages[edge.from_stage], to = view.stages[edge.to_stage];
    if(!from || !to) return '';
    const total = wireWidth(view, from.sheet ? from.sheet.rows_out : 0);
    const core = edge.rows > 0 ? Math.max(CORE_MIN, wireWidth(view, edge.rows)) : 0;
    const y0 = from.y + BOX_H / 2, y1 = to.y + BOX_H / 2;
    const x0 = from.x + BOX_W, x1 = to.x, mid = (x0 + x1) / 2;
    const d = y0 === y1 ? `M${x0},${y0}H${x1}`
                        : `M${x0},${y0}C${mid},${y0} ${mid},${y1} ${x1},${y1}`;
    return `<g class="canvas-wire"><path class="total" d="${d}" stroke-width="${total}"/>${
      core ? `<path class="core" d="${d}" stroke-width="${core}"/>` : ''}</g>`;
  }

  function markPicked(view){
    const picked = view.picked;
    view.world.querySelectorAll('[data-stage]').forEach(box => box.classList.toggle(
      'picked', Boolean(picked) && picked.kind === 'stage' && picked.id === box.dataset.stage));
    view.world.querySelectorAll('[data-sheet]').forEach(sheet => sheet.classList.toggle(
      'picked', Boolean(picked) && picked.kind === 'sheet' && picked.id === sheet.dataset.sheet));
  }

  // ── the drawer: the run page's own stage panel, cut to the figure's rows ──
  async function openPanel(view, kind, id, tab, rows){
    view.picked = {kind, id, rows: rows || 'figure'};
    markPicked(view);
    view.drawer.hidden = false;
    const body = view.pane.querySelector('.canvas-drawer-body');
    body.innerHTML = '<p class="muted">loading…</p>';
    const html = await fetchPanel(view, id, view.picked.rows);
    if(!view.picked || view.picked.id !== id) return;
    body.innerHTML = html;
    // The panel ships its own wiring, which innerHTML does not run.
    body.querySelectorAll('script').forEach(source => {
      const run = document.createElement('script');
      run.textContent = source.textContent;
      source.replaceWith(run);
    });
    const bar = body.querySelector('.stage-tabs');
    if(bar) selectTab(bar, tab);
    view.drawer.scrollTop = 0;
  }

  // Which tab the reader had open, so asking for other rows does not move them.
  function openTabOf(view){
    const on = view.drawer.querySelector('.stage-tabs [data-tab].active');
    return on ? on.dataset.tab : 'data';
  }

  function fetchPanel(view, id, rows){
    const held = `${id}|${rows}`;
    if(!view.panels[held]){
      const query = new URLSearchParams({
        stage: view.nav.cited_stage, row: view.pane.dataset.row,
        column: view.nav.column, rows});
      view.panels[held] = fetch(`${runBase(view)}/stage/${encodeURIComponent(id)}/traced?${query}`)
        .then(answer => answer.ok ? answer.text()
          : `<p class="muted">could not load ${esc(id)} (${answer.status})</p>`)
        .catch(failure => {
          delete view.panels[held];
          return `<p class="muted">error: ${esc(failure)}</p>`;
        });
    }
    return view.panels[held];
  }

  function runBase(view){
    return `/project/${encodeURIComponent(view.pane.dataset.project)}`
         + `/runs/${encodeURIComponent(view.pane.dataset.run)}`;
  }

  function closeDrawer(view){
    view.drawer.hidden = true;
    view.picked = null;
    markPicked(view);
  }

  // ── pointer: drag pans, wheel zooms about the pointer, a still click picks ──
  function wire(view){
    const signal = view.listeners.signal;
    // The panel's own control: which rows it holds is the reader's to pick.
    view.drawer.addEventListener('click', event => {
      const pick = event.target.closest('[data-rows]');
      if(pick && view.picked) openPanel(view, view.picked.kind, view.picked.id,
                                        openTabOf(view), pick.dataset.rows);
    }, {signal});
    let drag = null;
    view.svg.addEventListener('mousedown', event => {
      drag = {x: event.clientX, y: event.clientY, atX: view.at.x, atY: view.at.y, moved: false};
      event.preventDefault();
    });
    window.addEventListener('mousemove', event => {
      if(!drag) return;
      const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
      if(Math.abs(dx) + Math.abs(dy) > DRAG_SLACK) drag.moved = true;
      view.at.x = drag.atX + dx;
      view.at.y = drag.atY + dy;
      place(view);
    }, {signal});
    window.addEventListener('mouseup', event => {
      if(!drag) return;
      const moved = drag.moved;
      drag = null;
      if(moved) return;
      const box = event.target.closest('[data-stage]');
      const sheet = event.target.closest('[data-sheet]');
      if(box) openPanel(view, 'stage', box.dataset.stage, 'transform');
      else if(sheet) openPanel(view, 'sheet', sheet.dataset.sheet, 'data');
      else closeDrawer(view);
    }, {signal});
    view.svg.addEventListener('wheel', event => {
      event.preventDefault();
      zoomAbout(view, event);
    }, {passive: false});
    document.addEventListener('keydown', event => {
      if(event.key === 'Escape' && view.pane.offsetParent !== null) closeDrawer(view);
    }, {signal});
    view.pane.querySelector('.canvas-close').addEventListener(
      'click', () => closeDrawer(view));
  }

  function zoomAbout(view, event){
    const k = Math.min(ZOOM_MAX, Math.max(
      ZOOM_MIN, view.at.k * (event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP)));
    const frame = view.svg.getBoundingClientRect();
    const px = event.clientX - frame.left, py = event.clientY - frame.top;
    view.at.x = px - (px - view.at.x) * k / view.at.k;
    view.at.y = py - (py - view.at.y) * k / view.at.k;
    view.at.k = k;
    place(view);
  }

  return {mount};
})();
