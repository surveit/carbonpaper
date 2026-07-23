// Rewrite every <time datetime="ISO"> into the browser's local, human form
// ("Jul 23, 10:29"; adds the year when it isn't this year). Naive ISO strings
// are deliberately read as browser-local time — no TZ math, no TZ suffix.
// A MutationObserver catches fragments injected later (run-page polling
// swaps panels via innerHTML), so re-rendered times get formatted too.
(function () {
  const BASE = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' };
  function fmt(el) {
    const raw = el.getAttribute('datetime');
    const d = new Date(raw);
    if (isNaN(d)) return;
    const opts = d.getFullYear() === new Date().getFullYear() ? BASE : { ...BASE, year: 'numeric' };
    el.textContent = d.toLocaleString(undefined, opts);
    el.title = raw;               // exact ISO stays one hover away
    el.dataset.localized = '1';
  }
  function sweep(root) {
    if (root.querySelectorAll) root.querySelectorAll('time[datetime]:not([data-localized])').forEach(fmt);
  }
  sweep(document);
  new MutationObserver((muts) => muts.forEach((m) => m.addedNodes.forEach((n) => {
    if (n.nodeType !== 1) return;
    if (n.matches && n.matches('time[datetime]:not([data-localized])')) fmt(n);
    sweep(n);
  }))).observe(document.body, { childList: true, subtree: true });
})();
