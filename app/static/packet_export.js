// The export builds the whole packet before the first byte — 9s on a large run —
// and a plain download link fires no event to hang a spinner on. The response
// carries a cookie naming the token asked for, so the button stops spinning when
// the file is actually on its way rather than after a guessed delay.
(function () {
  const READY_COOKIE = "packet_ready";
  const POLL_MS = 250;
  // Past this the button stops claiming to know. The download may still arrive;
  // what has expired is this page's evidence about it, not the request.
  const GIVE_UP_MS = 10 * 60 * 1000;

  function readCookie(name) {
    return document.cookie
      .split("; ")
      .map((pair) => pair.split("="))
      .filter(([key]) => key === name)
      .map(([, value]) => value)[0];
  }

  function clearCookie(name) {
    document.cookie = name + "=; Max-Age=0; path=/";
  }

  function bind(link) {
    const icon = link.querySelector(".js-packet-icon");
    if (!icon) return;
    const glyph = icon.textContent;

    function release() {
      icon.textContent = glyph;
      link.removeAttribute("aria-disabled");
      link.removeAttribute("aria-busy");
      delete link.dataset.busy;
    }

    link.addEventListener("click", function (event) {
      // One build at a time: a second click starts a second nine-second export.
      if (link.dataset.busy) {
        event.preventDefault();
        return;
      }
      const token = String(Date.now());
      link.href = link.dataset.href + "?ready=" + token;
      link.dataset.busy = "1";
      link.setAttribute("aria-disabled", "true");
      link.setAttribute("aria-busy", "true");
      icon.textContent = "";
      icon.appendChild(
        Object.assign(document.createElement("span"), { className: "spinner" })
      );

      const started = Date.now();
      const timer = setInterval(function () {
        const ready = readCookie(READY_COOKIE) === token;
        if (!ready && Date.now() - started < GIVE_UP_MS) return;
        clearInterval(timer);
        if (ready) clearCookie(READY_COOKIE);
        release();
      }, POLL_MS);
    });
  }

  document.querySelectorAll(".js-packet-export").forEach(bind);
})();
