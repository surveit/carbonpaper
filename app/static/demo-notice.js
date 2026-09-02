// Wires _demo_notice.html: the strip's disclosure, and the gate.
//
// The gate opens where content goes in, not on arrival: on the chat that starts a
// project, and before a file leaves the reader's machine. Reading is never blocked.
//
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

    function acknowledged() {
        try { return localStorage.getItem(KEY) === "1"; } catch (e) { return false; }
    }

    const gate = document.getElementById("demo-gate");
    const usable = gate && typeof gate.showModal === "function";

    // An event rather than a global: a page holding an uploader need not also load this
    // one, and an unanswered ask is one nothing cancelled. Preventing it takes the turn.
    document.addEventListener("demo-gate:ask", function (event) {
        if (!usable || acknowledged()) return;
        event.preventDefault();
        open(event.detail.proceed);
    });

    if (!usable) return;

    const ack = document.getElementById("demo-gate-ack");
    const go = document.getElementById("demo-gate-continue");
    let insist = false;
    let onContinue = null;

    ack.addEventListener("change", function () { go.disabled = !ack.checked; });
    go.addEventListener("click", function () {
        try { localStorage.setItem(KEY, "1"); } catch (e) { /* private mode: gate returns */ }
        const proceed = onContinue;
        onContinue = null;
        insist = false;
        gate.close();
        // Run from here rather than the close event: a dialog that dispatches none would
        // strand the upload the reader just agreed to.
        if (proceed) proceed();
    });
    // Escape and the backdrop close a dialog by default. While `insist` holds there is
    // nothing behind the gate to go back to, so cancel is refused and a close that got
    // through anyway reopens it. A backstop instead drops what it interrupted.
    gate.addEventListener("cancel", function (event) {
        if (insist) { event.preventDefault(); return; }
        onContinue = null;
    });
    gate.addEventListener("close", function () {
        if (insist) setTimeout(function () { gate.showModal(); }, 0);
    });

    function open(proceed) {
        insist = !proceed;
        onContinue = proceed || null;
        ack.checked = false;
        go.disabled = true;
        gate.showModal();
    }

    if (gate.hasAttribute("data-open-on-load") && !acknowledged()) open(null);
})();
