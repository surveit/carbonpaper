// The switcher rungs in the header trail. A rung's popover body is fetched from the
// partial named in data-crumb-picker the first time it opens, then kept; the trail
// itself is server-rendered and works without this file, minus the switching.
(function () {
  var open = null;

  function closeOpen() {
    if (!open) return;
    open.button.setAttribute('aria-expanded', 'false');
    open.pop.hidden = true;
    open = null;
  }

  // A failed fetch says so in the popover and leaves the rung usable. Nothing is
  // drawn as if a list had loaded, and the trail's links are unaffected either way.
  function loadInto(pop, url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.text();
    }).then(function (html) {
      pop.innerHTML = html;
      pop.dataset.loaded = '1';
    }).catch(function () {
      pop.innerHTML = '<p class="crumb-pop-empty">Could not read this list.</p>';
    });
  }

  function toggle(button) {
    var anchor = button.closest('.crumb-anchor');
    var pop = anchor.querySelector('.crumb-pop');
    if (!pop) {
      pop = document.createElement('div');
      pop.className = 'crumb-pop';
      pop.hidden = true;
      anchor.appendChild(pop);
    }
    var wasOpen = open && open.pop === pop;
    closeOpen();
    if (wasOpen) return;
    button.setAttribute('aria-expanded', 'true');
    pop.hidden = false;
    open = { button: button, pop: pop };
    if (pop.dataset.loaded) return;
    pop.innerHTML = '<p class="crumb-pop-empty">Loading…</p>';
    var url = button.dataset.crumbPicker;
    var current = button.dataset.crumbCurrent || '';
    loadInto(pop, url + '?current=' + encodeURIComponent(current));
  }

  document.addEventListener('click', function (ev) {
    var button = ev.target.closest('[data-crumb-picker]');
    if (button) { ev.preventDefault(); toggle(button); return; }
    if (open && !ev.target.closest('.crumb-pop')) closeOpen();
  });

  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && open) { open.button.focus(); closeOpen(); }
  });
})();
