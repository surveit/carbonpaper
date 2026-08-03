// Rewrite every <time datetime="ISO"> into the browser's local, human form
// ("Jul 23, 10:29"; adds the year when it isn't this year). A <time> marked
// data-relative gets the relative form instead while it is recent ("4 minutes
// ago", "yesterday, 4:12 PM"), falling back to the absolute form past a week.
// Either way the exact ISO is parked in `title`, so the precise value is one
// hover away, and it stays in the datetime attribute as the no-JS fallback text.
// Naive ISO strings are deliberately read as browser-local time — no TZ math,
// no TZ suffix.
// A MutationObserver catches fragments injected later (run-page polling
// swaps panels via innerHTML), so re-rendered times get formatted too.
(function (global) {
  var BASE = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' };
  var WITH_YEAR = { year: 'numeric', month: 'short', day: 'numeric',
                    hour: 'numeric', minute: '2-digit' };
  var MINUTE_MS = 60000;
  var HOUR_MS = 3600000;
  var DAY_MS = 86400000;
  // Past a week "6 days ago" stops locating anything; the date itself is more use.
  var RELATIVE_DAY_LIMIT = 7;

  // The text to show, or null for a datetime the browser cannot parse — the
  // caller then leaves the element's own text (the raw ISO) alone.
  function describeTime(iso, now, relative) {
    var when = new Date(iso);
    if (isNaN(when.getTime())) return null;
    if (!relative) return formatAbsolute(when, now);
    var elapsed = now - when;
    // A timestamp ahead of the clock is not "ago" — say when, not how long.
    if (elapsed < 0) return formatAbsolute(when, now);
    if (elapsed < MINUTE_MS) return 'just now';
    if (elapsed < HOUR_MS) return pluralise(Math.floor(elapsed / MINUTE_MS), 'minute') + ' ago';
    var days = countCalendarDaysBack(when, now);
    if (days === 0) return pluralise(Math.floor(elapsed / HOUR_MS), 'hour') + ' ago';
    if (days === 1) return 'yesterday, ' + formatClock(when);
    if (days < RELATIVE_DAY_LIMIT) return pluralise(days, 'day') + ' ago';
    return formatAbsolute(when, now);
  }

  function formatAbsolute(when, now) {
    var opts = when.getFullYear() === now.getFullYear() ? BASE : WITH_YEAR;
    return when.toLocaleString(undefined, opts);
  }

  function formatClock(when) {
    return when.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  }

  // Whole days between the two CALENDAR dates, so 23:00 last night is 1 day back
  // at 00:30 rather than 0 — which is what "yesterday" means to a reader.
  function countCalendarDaysBack(when, now) {
    var then = new Date(when.getFullYear(), when.getMonth(), when.getDate());
    var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return Math.round((today - then) / DAY_MS);
  }

  function pluralise(count, unit) {
    return count + ' ' + unit + (count === 1 ? '' : 's');
  }

  function paint(el) {
    var raw = el.getAttribute('datetime');
    var text = describeTime(raw, new Date(), el.hasAttribute('data-relative'));
    if (text === null) return;
    el.textContent = text;
    el.title = raw;               // exact ISO stays one hover away
    el.dataset.localized = '1';
  }

  function sweep(root) {
    if (root.querySelectorAll) {
      root.querySelectorAll('time[datetime]:not([data-localized])').forEach(paint);
    }
  }

  if (typeof document !== 'undefined') {
    sweep(document);
    // A relative label goes stale where an absolute one cannot, so repaint the
    // relative ones on the minute they are counting in.
    setInterval(function () {
      document.querySelectorAll('time[datetime][data-relative]').forEach(paint);
    }, MINUTE_MS);
    new MutationObserver(function (muts) {
      muts.forEach(function (m) {
        m.addedNodes.forEach(function (n) {
          if (n.nodeType !== 1) return;
          if (n.matches && n.matches('time[datetime]:not([data-localized])')) paint(n);
          sweep(n);
        });
      });
    }).observe(document.body, { childList: true, subtree: true });
  }

  global.describeTime = describeTime;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { describeTime: describeTime };
  }
})(typeof window !== 'undefined' ? window : globalThis);
