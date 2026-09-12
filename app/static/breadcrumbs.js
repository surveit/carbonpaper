// The header trail: what a rung opens, and the fold that keeps the trail on one line.
//
// A rung's popover is either fetched from the partial named in data-crumb-picker the
// first time it opens, or written into the page beside the rung (data-crumb-pop). The
// trail is server-rendered and works without this file, minus the switching and the fold.
(function () {
  var open = null;
  var pending = null;

  document.addEventListener('click', function (ev) {
    var button = ev.target.closest('[data-crumb-picker], [data-crumb-pop], .crumb-fold');
    if (button) { ev.preventDefault(); toggle(button); return; }
    if (open && !ev.target.closest('.crumb-pop')) closeOpen();
  });

  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && open) { open.button.focus(); closeOpen(); }
  });

  // Every trail on the page, remeasured whenever the room it has can have changed.
  // A web font landing after first paint moves every rung, so wait for that too.
  function refitEveryTrail() {
    document.querySelectorAll('.crumbs').forEach(refit);
    if (open && open.button.classList.contains('crumb-fold')) {
      drawFoldedList(open.pop, open.button.closest('.crumbs'));
    }
  }

  function scheduleRefit() {
    if (pending !== null) cancelAnimationFrame(pending);
    pending = requestAnimationFrame(function () { pending = null; refitEveryTrail(); });
  }

  addEventListener('resize', scheduleRefit);
  if (document.fonts) document.fonts.ready.then(scheduleRefit);
  // Straight away rather than on a frame: this file is loaded from <head>, and a
  // trail that folds only once a frame has been painted folds in front of the reader.
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', refitEveryTrail);
  else refitEveryTrail();

  // ── what a rung opens ──────────────────────────────────────────────────────

  function toggle(button) {
    var anchor = button.closest('.crumb-anchor');
    var pop = anchor.querySelector('.crumb-pop') || attachPop(anchor);
    var wasOpen = open && open.pop === pop;
    closeOpen();
    if (wasOpen) return;
    button.setAttribute('aria-expanded', 'true');
    pop.hidden = false;
    open = { button: button, pop: pop };
    if (button.classList.contains('crumb-fold')) { drawFoldedList(pop, button.closest('.crumbs')); return; }
    if (pop.dataset.loaded) return;
    fetchList(pop, button);
  }

  function closeOpen() {
    if (!open) return;
    open.button.setAttribute('aria-expanded', 'false');
    open.pop.hidden = true;
    open = null;
  }

  function attachPop(anchor) {
    var pop = document.createElement('div');
    pop.className = 'crumb-pop';
    pop.hidden = true;
    anchor.appendChild(pop);
    return pop;
  }

  function fetchList(into, button) {
    into.innerHTML = '<p class="crumb-pop-empty">Loading…</p>';
    var current = button.dataset.crumbCurrent || '';
    return loadInto(into, button.dataset.crumbPicker + '?current=' + encodeURIComponent(current));
  }

  // A failed fetch says so in the popover and leaves the rung usable. Nothing is
  // drawn as if a list had loaded, and the trail's links are unaffected either way.
  function loadInto(into, url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.text();
    }).then(function (html) {
      into.innerHTML = html;
      into.dataset.loaded = '1';
    }).catch(function () {
      into.innerHTML = '<p class="crumb-pop-empty">Could not read this list.</p>';
    });
  }

  // ── the fold ───────────────────────────────────────────────────────────────

  function refit(crumbs) {
    var fold = crumbs.querySelector('.crumb-fold');
    if (!fold) return;
    unfold(crumbs, fold);
    crumbs.classList.add('measuring');
    var rungs = crumbs.querySelectorAll('[data-foldable]');
    var folded = 0;
    for (var i = 0; i < rungs.length && overflows(crumbs); i++) {
      fold.hidden = false;
      foldRung(rungs[i]);
      folded++;
    }
    crumbs.classList.remove('measuring');
    fold.hidden = folded === 0;
  }

  // Measured with shrinking off (see .crumbs.measuring), so this asks whether the
  // rungs fit at their own widths. With shrinking on nothing ever overflows.
  function overflows(crumbs) {
    return crumbs.scrollWidth > crumbs.clientWidth + 1;
  }

  function foldRung(rung) {
    rung.setAttribute('data-folded', '');
    var before = rung.previousElementSibling;
    if (before && before.classList.contains('crumb-sep')) before.setAttribute('data-folded', '');
  }

  function unfold(crumbs, fold) {
    crumbs.querySelectorAll('[data-folded]').forEach(function (el) {
      el.removeAttribute('data-folded');
    });
    fold.hidden = true;
  }

  function foldedRungs(crumbs) {
    return Array.prototype.filter.call(
      crumbs.querySelectorAll('[data-folded]'),
      function (el) { return el.hasAttribute('data-foldable'); }
    );
  }

  // What the fold swallowed, one row per rung. A rung that switches opens its own
  // list here rather than sending the reader to the page first.
  function drawFoldedList(pop, crumbs) {
    pop.replaceChildren(popHeading('Folded out of the trail'));
    foldedRungs(crumbs).forEach(function (rung) {
      pop.appendChild(foldedRow(pop, crumbs, rung));
    });
  }

  function foldedRow(pop, crumbs, rung) {
    var switcher = rung.querySelector('[data-crumb-picker]');
    var row = document.createElement('a');
    row.className = 'crumb-pop-row';
    row.href = switcher ? '#' : rung.getAttribute('href');
    var id = document.createElement('span');
    id.className = 'crumb-pop-id';
    id.appendChild(labelOf(rung));
    row.appendChild(id);
    if (!switcher) return row;
    id.appendChild(caret('▸'));
    // This row is about to replace the popover's own contents, which detaches it
    // before the outside-click handler can ask whether the click was inside.
    row.addEventListener('click', function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      drillInto(pop, crumbs, switcher);
    });
    return row;
  }

  function drillInto(pop, crumbs, switcher) {
    var body = document.createElement('div');
    pop.replaceChildren(backToFoldedList(pop, crumbs), body);
    fetchList(body, switcher);
  }

  function backToFoldedList(pop, crumbs) {
    var back = document.createElement('button');
    back.type = 'button';
    back.className = 'crumb-pop-back';
    back.textContent = '‹ back';
    back.addEventListener('click', function (ev) {
      ev.stopPropagation();
      drawFoldedList(pop, crumbs);
    });
    return back;
  }

  // The rung's own words, without the caret it wears in the trail.
  function labelOf(rung) {
    var label = document.createElement('span');
    var source = rung.querySelector('.crumb-switch') || rung;
    Array.prototype.forEach.call(source.childNodes, function (node) {
      if (node.nodeType === 1 && node.classList.contains('crumb-caret')) return;
      label.appendChild(node.cloneNode(true));
    });
    return label;
  }

  function popHeading(text) {
    var head = document.createElement('p');
    head.className = 'crumb-pop-head';
    head.textContent = text;
    return head;
  }

  function caret(glyph) {
    var mark = document.createElement('span');
    mark.className = 'crumb-caret';
    mark.textContent = glyph;
    return mark;
  }
})();
