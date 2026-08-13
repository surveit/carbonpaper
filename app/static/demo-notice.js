// Wires _demo_notice.html: the strip's disclosure, and the gate over a first visit.
//
// Whether this BROWSER has been shown the gate — not whether the person read it, and
// not a fact about an account, since there are none. Same reasoning as the tour flag
// on the home page: nothing server-side records either.
(function () {
    const KEY = "carbonpaper.demo-notice.acknowledged";

    const toggle = document.getElementById("demo-strip-toggle");
    const more = document.getElementById("demo-strip-more");
    if (toggle && more) {
        toggle.addEventListener("click", function () {
            const opening = more.hasAttribute("hidden");
            more.toggleAttribute("hidden", !opening);
            toggle.setAttribute("aria-expanded", String(opening));
            toggle.textContent = opening ? "hide" : "what that means";
        });
    }

    const gate = document.getElementById("demo-gate");
    if (!gate || typeof gate.showModal !== "function") return;

    let acknowledged = false;
    try { acknowledged = localStorage.getItem(KEY) === "1"; } catch (e) { /* private mode */ }
    if (acknowledged) return;

    const ack = document.getElementById("demo-gate-ack");
    const go = document.getElementById("demo-gate-continue");
    ack.addEventListener("change", function () { go.disabled = !ack.checked; });
    go.addEventListener("click", function () {
        try { localStorage.setItem(KEY, "1"); } catch (e) { /* private mode: gate returns */ }
        gate.close();
    });
    // Escape and the backdrop close a dialog by default. Here they would walk past the
    // one thing this page exists to say, so the two ways out are the box and the link.
    gate.addEventListener("cancel", function (event) { event.preventDefault(); });
    gate.showModal();
})();
