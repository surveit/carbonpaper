// The rail: the host that carries one conversation across pages. It holds a session id and
// mounts whatever GET /chat/<sid>/panel returns — it never learns which agent is on the
// other end, and tests/arch/test_chat_rail_names_no_agent.py fails a build where it does.
//
// The panel does not survive a page load and does not need to: the turn is a detached task
// on the server with a replayable buffer (app/core/agent/turns.py), so this remounts on
// every page and the panel reattaches to whatever is still running.
window.ChatRail = window.ChatRail || {};

(function () {
  const KEY = "chat-rail:session";
  const TITLE_KEY = "chat-rail:title";
  const OPEN_KEY = "chat-rail:open";

  function read(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function write(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } }
  function forget() {
    try { [KEY, TITLE_KEY, OPEN_KEY].forEach((k) => localStorage.removeItem(k)); } catch (e) { /* private mode */ }
  }

  // Called by the full chat page, which is the only surface that opens a session. Opening one
  // there is what puts it in the rail everywhere else. The title comes along so the shut tab
  // can name the conversation without fetching it.
  window.ChatRail.remember = function (sid, title) {
    write(KEY, sid);
    write(TITLE_KEY, title || "Conversation");
    write(OPEN_KEY, "1");
  };

  document.addEventListener("DOMContentLoaded", function () {
    const rail = document.getElementById("chat-rail");
    const tab = document.getElementById("chat-rail-tab");
    if (!rail || !tab) return;
    // The chat page draws its own panel, so the rail stays out of its way rather than
    // showing the same conversation twice.
    if (document.querySelector(".chat-host-page")) return;

    const sid = read(KEY);
    if (!sid) return;
    const name = read(TITLE_KEY) || "Conversation";
    rail.querySelector(".js-rail-title").textContent = name;
    rail.querySelector(".chat-rail-open").href = `/chat/${sid}`;
    tab.textContent = name;

    function show(open) {
      rail.hidden = !open;
      tab.hidden = open;
      document.body.classList.toggle("chat-rail-open", open);
      write(OPEN_KEY, open ? "1" : "0");
    }

    let loaded = false;
    async function load() {
      if (loaded) return;
      const r = await fetch(`/chat/${sid}/panel`);
      // A session the store no longer has is a stale id, not an error to show: drop it and
      // leave the page as if the rail had never been asked for.
      if (!r.ok) { forget(); rail.remove(); tab.remove(); document.body.classList.remove("chat-rail-open"); return; }
      const panel = rail.querySelector(".js-rail-panel");
      panel.innerHTML = await r.text();
      panel.dataset.chatPanel = "";
      loaded = true;
      // The stored title is what the reader last saw it called; the panel carries the
      // current one, so a renamed session corrects itself on the next page.
      const cfg = JSON.parse(panel.querySelector(".js-chat-config").textContent);
      if (cfg.title) { write(TITLE_KEY, cfg.title); rail.querySelector(".js-rail-title").textContent = cfg.title; tab.textContent = cfg.title; }
      window.ChatPanel.mount(panel);
    }

    tab.addEventListener("click", () => { show(true); load(); });
    // Shut, not dismissed: the tab stays, because the conversation is still theirs to come
    // back to. What ends it is opening a different one, which overwrites the id above.
    rail.querySelector(".js-rail-close").addEventListener("click", () => show(false));

    // Shut is remembered, so a reader who closed the rail is not handed it back on every page.
    const wasOpen = read(OPEN_KEY) !== "0";
    show(wasOpen);
    if (wasOpen) load();
  });
})();
