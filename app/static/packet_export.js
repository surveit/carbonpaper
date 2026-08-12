// The export builds the whole packet before the first byte — 9s on a large run —
// and a plain link shows nothing until the browser is handed a file. The response
// carries a cookie naming the token asked for, so the spinner stops when the file
// is actually on its way rather than after a guessed delay.
(function () {
  const READY_COOKIE = "packet_ready";
  const POLL_MS = 250;
  // Past this the spinner stops claiming to know. The download may still arrive;
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
    const spinner = document.createElement("span");
    spinner.className = "spinner sm";
    spinner.hidden = true;
    spinner.setAttribute("aria-hidden", "true");
    link.insertAdjacentElement("afterend", spinner);

    link.addEventListener("click", function () {
      const token = String(Date.now());
      link.href = link.dataset.href + "?ready=" + token;
      link.setAttribute("aria-busy", "true");
      link.classList.add("is-busy");
      spinner.hidden = false;
      const started = Date.now();
      const timer = setInterval(function () {
        const done = readCookie(READY_COOKIE) === token;
        if (!done && Date.now() - started < GIVE_UP_MS) return;
        clearInterval(timer);
        if (done) clearCookie(READY_COOKIE);
        spinner.hidden = true;
        link.classList.remove("is-busy");
        link.removeAttribute("aria-busy");
      }, POLL_MS);
    });
  }

  document.querySelectorAll(".js-packet-export").forEach(bind);
})();
