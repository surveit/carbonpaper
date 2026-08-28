// The rail: the host that carries one conversation across pages. It holds a session id and
// mounts whatever GET /chat/<sid>/panel returns — it never learns which agent is on the
// other end, and tests/arch/test_chat_rail_names_no_agent.py fails a build where it does.
//
// The panel does not survive a page load and does not need to: the turn is a detached task
// on the server with a replayable buffer (app/core/agent/turns.py), so this remounts on
// every page and the panel reattaches to whatever is still running.
//
// Whether the rail is there, and the request for its contents, are both decided in <head>
// (_chat_rail_head.html). What is left here runs at DOMContentLoaded, because it needs the
// markup: filling the column in, and the two controls that change which state it is in.
window.ChatRail = window.ChatRail || {};

(function () {
  const KEY = "chat-rail:session";
  const TITLE_KEY = "chat-rail:title";
  const OPEN_KEY = "chat-rail:open";

  function write(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } }
  function forget() {
    try { [KEY, TITLE_KEY, OPEN_KEY].forEach((k) => localStorage.removeItem(k)); } catch (e) { /* private mode */ }
  }

  // localStorage, and NOT sessionStorage, though that leaves the two halves of this with
  // different lifetimes. A tab opened from outside the browser inherits no session storage,
  // and links arrive that way constantly — from another app, a bookmark, a restored window.
  // Per-tab the rail is therefore absent on most arrivals, which is the one thing it exists
  // not to be. WHICH conversation is the reader's; WHERE they had got to in it belongs to
  // the view, and stays per-tab in chat-panel.js.
  //
  // Called by the full chat page, which is the only surface that opens a session. Opening one
  // there is what puts it in the rail everywhere else. The title comes along so the shut tab
  // can name the conversation without fetching it.
  window.ChatRail.remember = function (sid, title) {
    write(KEY, sid);
    write(TITLE_KEY, title || "Conversation");
    write(OPEN_KEY, "1");
  };

  // Takes a session someone else opened, so a page can put a conversation in the rail
  // without this learning whose it is. See tests/arch/test_chat_rail_names_no_agent.py.
  window.ChatRail.open = function (sid, title) {
    window.ChatRail.remember(sid, title);
    wire(sid, title, fetch(`/chat/${sid}/panel`), true);
  };

  function wire(sid, title, pendingPanel, showNow) {
    const rail = document.getElementById("chat-rail");
    const tab = document.getElementById("chat-rail-tab");
    if (!rail || !tab) return;
    const shown = document.documentElement.classList;

    const name = title || "Conversation";
    rail.querySelector(".js-rail-title").textContent = name;
    rail.querySelector(".chat-rail-open-page").href = `/chat/${sid}`;
    tab.textContent = name;

    function show(open) {
      shown.toggle("chat-rail-open", open);
      shown.toggle("chat-rail-shut", !open);
      write(OPEN_KEY, open ? "1" : "0");
    }

    let pending = pendingPanel;
    async function load() {
      if (!pending) return;
      const response = pending;
      pending = null;
      const r = await response;
      // A session the store no longer has is a stale id, not an error to show: drop it and
      // leave the page as if the rail had never been asked for.
      if (!r.ok) { forget(); shown.remove("chat-rail-open", "chat-rail-shut"); return; }
      const panel = rail.querySelector(".js-rail-panel");
      panel.innerHTML = await r.text();
      panel.dataset.chatPanel = "";
      // The stored title is what the reader last saw it called; the panel carries the
      // current one, so a renamed session corrects itself on the next page.
      const cfg = JSON.parse(panel.querySelector(".js-chat-config").textContent);
      if (cfg.title && cfg.title !== name) {
        write(TITLE_KEY, cfg.title);
        rail.querySelector(".js-rail-title").textContent = cfg.title;
        tab.textContent = cfg.title;
      }
      window.ChatPanel.mount(panel);
    }

    tab.onclick = () => {
      show(true);
      // Shut at <head> time means no request was made, so opening asks for it now.
      if (!pending && !rail.querySelector(".js-chat-config")) pending = fetch(`/chat/${sid}/panel`);
      load();
    };
    // Shut, not dismissed: the tab stays, because the conversation is still theirs to come
    // back to. What ends it is opening a different one, which overwrites the id above.
    rail.querySelector(".js-rail-close").onclick = () => show(false);

    if (showNow) show(true);
    load();
  }

  document.addEventListener("DOMContentLoaded", function () {
    const opening = window.ChatRail.opening || {};
    // No session, or the chat page's own panel — <head> drew neither state, so there is
    // nothing here to fill in.
    if (!opening.sid) return;
    wire(opening.sid, opening.title, opening.panel, false);
  });
})();
