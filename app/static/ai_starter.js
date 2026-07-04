/* ai_starter.js — minimal "start the AI assistant" trigger for empty sections.
 *
 * The rich authoring chat is a separate surface. An empty project needs a way to invoke
 * the AI: this opens the existing authoring SSE endpoint, streams a LIVE preview of what
 * the AI is writing (so it's visibly running through the long "thinking" phase), counts
 * schemas/stages as they're persisted, and reloads once done so the output renders
 * server-side. A starter marked data-ai-autostart fires once on load.
 *
 * Markup:
 *   <div class="ai-starter" data-ai-start data-ai-endpoint="/…/stream"
 *        data-ai-verb="Authoring the data model" [data-ai-autostart]>
 *     <button class="btn primary ai-start-btn">✦ Generate…</button>
 *     <div class="ai-start-status" hidden>
 *       <span class="ai-start-spin"></span> <span class="ai-start-msg"></span>
 *     </div>
 *     <pre class="ai-start-preview" hidden></pre>
 *   </div>
 */
(function () {
  "use strict";

  function wire(root) {
    var btn = root.querySelector(".ai-start-btn");
    var status = root.querySelector(".ai-start-status");
    var msgEl = root.querySelector(".ai-start-msg");
    var previewEl = root.querySelector(".ai-start-preview");
    var endpoint = root.getAttribute("data-ai-endpoint");
    var verb = root.getAttribute("data-ai-verb") || "Working";
    if (!endpoint) return;
    var running = false, es = null, done = false, n = 0, acc = "";

    function msg(t) { if (msgEl) msgEl.textContent = t; }
    function pushPreview(t) {
      acc += t;
      if (acc.length > 4000) acc = acc.slice(-4000);   // keep the tail; cap the DOM
      if (previewEl) {
        previewEl.hidden = false;
        previewEl.textContent = acc;
        previewEl.scrollTop = previewEl.scrollHeight;   // follow the stream
      }
    }

    function start() {
      if (running) return;
      running = true; done = false; n = 0; acc = "";
      if (btn) { btn.hidden = true; btn.classList.remove("ai-start-error"); }
      if (status) status.hidden = false;
      if (previewEl) { previewEl.hidden = true; previewEl.textContent = ""; }
      msg(verb + " — the AI is thinking…");
      // Empty message → the endpoint seeds from the project document (server-side).
      es = new EventSource(endpoint);
      es.onmessage = function (e) {
        var ev; try { ev = JSON.parse(e.data); } catch (_) { return; }
        if (ev.type === "assistant_delta") {
          if (ev.text) pushPreview(ev.text);            // ← liveness during the long stream
        } else if (ev.type === "schema_emitted") {
          n++; msg(verb + "… " + n + " schema(s) proposed");
        } else if (ev.type === "stage_emitted") {
          n++; msg(verb + "… " + n + " stage(s) proposed");
        } else if (ev.type === "done" || ev.type === "data_model_proposed") {
          done = true; msg("Done" + (n ? " — " + n + " emitted" : "") + " — refreshing…"); shut();
          setTimeout(function () { location.reload(); }, 800);
        } else if (ev.type === "error") {
          fail(ev.message || "the AI stream failed");
        }
      };
      es.onerror = function () {
        // EventSource also fires onerror on a normal close; only a close we did not
        // initiate (and before 'done') is a real failure — surface it, never silently.
        if (es && es.readyState === EventSource.CLOSED && running && !done) {
          fail("lost connection to the AI stream");
        }
      };
    }

    function shut() { if (es) { es.close(); es = null; } running = false; }

    function fail(m) {
      shut();
      if (status) status.hidden = true;
      if (btn) {
        btn.hidden = false;
        btn.textContent = "⚠ " + m + " — retry";
        btn.classList.add("ai-start-error");
      }
    }

    if (btn) btn.addEventListener("click", start);
    if (root.hasAttribute("data-ai-autostart")) start();
  }

  function boot() {
    document.querySelectorAll(".ai-starter[data-ai-start]").forEach(wire);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
