// Wires _demo_notice.html: the strip's disclosure, and the gate.
//
// The gate opens where a project starts, not on arrival, so reading is never blocked.
// Whether this BROWSER has been shown it — not whether the person read it, and not a
// fact about an account, since there are none. Same as the home page's tour flag.
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
    if (!gate.hasAttribute("data-open-on-load")) return;

    let acknowledged = false;
    try { acknowledged = localStorage.getItem(KEY) === "1"; } catch (e) { /* private mode */ }
    if (acknowledged) return;

    const ack = document.getElementById("demo-gate-ack");
    const go = document.getElementById("demo-gate-continue");
    let continued = false;
    ack.addEventListener("change", function () { go.disabled = !ack.checked; });
    go.addEventListener("click", function () {
        try { localStorage.setItem(KEY, "1"); } catch (e) { /* private mode: gate returns */ }
        continued = true;
        gate.close();
    });
    // Escape and the backdrop close a dialog by default, and a browser may honor a
    // repeated close request even when cancel was prevented. So cancel is prevented,
    // and any close not made by the Continue button reopens the gate.
    gate.addEventListener("cancel", function (event) { event.preventDefault(); });
    gate.addEventListener("close", function () {
        if (!continued) { setTimeout(function () { gate.showModal(); }, 0); }
    });
    gate.showModal();
})();
