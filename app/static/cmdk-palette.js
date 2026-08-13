// The ⌘K bar (templates/_cmdk_palette.html). The index is one GET, fetched on first
// open and kept for the page's life: it is the shape of the workspace, which does not
// change under a reader mid-page. Server order IS the ranking — the query only removes
// rows and re-sorts within it, so an empty box offers where you are, then everywhere else.
(function () {
  var dialog = document.getElementById('cmdk-palette');
  if (!dialog) return;
  var input = dialog.querySelector('.cmdk-input');
  var list = dialog.querySelector('.cmdk-list');
  var count = dialog.querySelector('.cmdk-count');
  var rows = null;
  var shown = [];
  var sel = 0;

  var SHOWN_CAP = 50;

  function readProject() {
    var match = location.pathname.match(/^\/project\/([^/]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  // A failed fetch says so and leaves the bar open; rows stays null, so the next
  // open asks again rather than showing an empty list as if the workspace were empty.
  function loadRows() {
    if (rows) return Promise.resolve(true);
    return fetch('/cmdk_palette/index?project=' + encodeURIComponent(readProject()),
                 { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) { rows = data.rows; return true; })
      .catch(function () { return false; });
  }

  // Subsequence, so "llmc" finds llm_classification: score is where the run starts
  // plus how far it spreads, which lets a contiguous match beat a scattered one.
  function score(row, query) {
    if (!query) return 0;
    var hay = (row.label + ' ' + row.meta).toLowerCase();
    var at = 0, first = -1, last = -1;
    for (var i = 0; i < query.length; i++) {
      at = hay.indexOf(query[i], at);
      if (at < 0) return null;
      if (first < 0) first = at;
      last = at;
      at++;
    }
    return first + (last - first) * 2;
  }

  function render() {
    var query = input.value.trim().toLowerCase();
    shown = (rows || [])
      .map(function (row) { return { row: row, score: score(row, query) }; })
      .filter(function (hit) { return hit.score !== null; })
      // Stable, and the rows arrive ranked, so equal scores keep the server's order.
      .sort(function (a, b) { return a.score - b.score; })
      .slice(0, SHOWN_CAP)
      .map(function (hit) { return hit.row; });
    sel = 0;
    list.textContent = '';
    if (!shown.length) {
      list.appendChild(element('p', 'cmdk-empty',
        rows ? 'Nothing matches.' : 'Could not read the index.'));
      count.textContent = '';
      return;
    }
    shown.forEach(function (row, n) { list.appendChild(draw(row, n === 0)); });
    // The cap is stated rather than silent, on the same reasoning as a picker's "all N".
    count.textContent = shown.length < rows.length
      ? shown.length + ' of ' + rows.length : String(rows.length);
  }

  // Built as nodes, never interpolated: a label here is an id someone authored.
  function draw(row, isSelected) {
    var a = element('a', 'cmdk-row' + (isSelected ? ' sel' : ''));
    a.href = row.href;
    a.appendChild(element('span', 'cmdk-kind', row.kind));
    var label = element(row.is_code ? 'code' : 'span', 'cmdk-label', row.label);
    a.appendChild(label);
    if (row.meta) a.appendChild(element('span', 'cmdk-meta', row.meta));
    return a;
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function move(delta) {
    var drawn = list.querySelectorAll('.cmdk-row');
    if (!drawn.length) return;
    drawn[sel].classList.remove('sel');
    sel = (sel + delta + drawn.length) % drawn.length;
    drawn[sel].classList.add('sel');
    drawn[sel].scrollIntoView({ block: 'nearest' });
  }

  function go(href) {
    var url = new URL(href, location.href);
    if (url.pathname !== location.pathname || !url.hash) { location.href = href; return; }
    // Already on the page the stage lives on. Setting the hash alone would move
    // nothing — the workflow page reads it on load — so hand the id to the loader
    // it exposes, and fall back to a reload where that page is not this one.
    dialog.close();
    if (window._loadStage) { window._loadStage(decodeURIComponent(url.hash.slice(1))); return; }
    location.hash = url.hash;
    location.reload();
  }

  function open() {
    if (dialog.open) return;
    dialog.showModal();
    input.value = '';
    render();
    loadRows().then(render);
  }

  document.addEventListener('keydown', function (ev) {
    if (!(ev.metaKey || ev.ctrlKey) || ev.key.toLowerCase() !== 'k') return;
    ev.preventDefault();
    open();
  });

  input.addEventListener('input', render);

  dialog.addEventListener('keydown', function (ev) {
    if (ev.key === 'ArrowDown') { ev.preventDefault(); move(1); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); move(-1); }
    else if (ev.key === 'Enter') { ev.preventDefault(); if (shown[sel]) go(shown[sel].href); }
  });

  dialog.addEventListener('click', function (ev) {
    var row = ev.target.closest('.cmdk-row');
    if (row) { ev.preventDefault(); go(row.href); return; }
    // The backdrop is the dialog itself, so a click landing on no row closes it.
    if (!ev.target.closest('.cmdk-input, .cmdk-list, .cmdk-foot')) dialog.close();
  });
})();
