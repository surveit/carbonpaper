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
  // WHERE THE OPEN CONVERSATION IS WRITTEN DOWN. In the address, and nowhere else.
  //
  // It was localStorage, which outlived what it described: a conversation opened last week
  // reopened on every page this week, on whatever the reader was reading. The job it was
  // really doing is narrower — following a link out of a reply without losing the
  // conversation that offered it — and that is a fact about the LINK, so chat-panel.js
  // writes this parameter onto the links it draws. What is left needs no lifetime rule: a
  // reload, the back button and a URL sent to someone else all agree, because there is one
  // copy of the answer and it is in the address bar.
  // The name itself is set in _chat_rail_head.html, which runs before any script loads.
  function writeAddress(sid) {
    const url = new URL(location.href);
    if (sid) url.searchParams.set(window.ChatRail.PARAM, sid);
    else url.searchParams.delete(window.ChatRail.PARAM);
    history.replaceState(null, "", url);
  }

  document.addEventListener("DOMContentLoaded", function () {
    const rail = document.getElementById("chat-rail");
    const ask = document.getElementById("chat-ask");
    // The chat page is its own host: <head> marked it, and neither control belongs there.
    if (!rail || !ask || document.documentElement.classList.contains("chat-is-the-page")) return;
    const shown = document.documentElement.classList;
    const panel = rail.querySelector(".js-rail-panel");
    const title = rail.querySelector(".js-rail-title");
    const openPage = rail.querySelector(".chat-rail-open-page");

    function show(open) { shown.toggle("chat-rail-open", open); }

    async function mount(pending) {
      const r = await pending;
      // A session the store no longer has is a stale id, not an error to show: drop it and
      // leave the page as if the rail had never been asked for.
      if (!r.ok) { show(false); writeAddress(null); return; }
      // A second mount into the same column is a different conversation, so the panel's own
      // guard has to be cleared along with its markup.
      delete panel.dataset.chatMounted;
      panel.innerHTML = await r.text();
      panel.dataset.chatPanel = "";
      const cfg = JSON.parse(panel.querySelector(".js-chat-config").textContent);
      title.textContent = cfg.title || "Conversation";
      if (cfg.session_id) openPage.href = `/chat/${cfg.session_id}`;
      window.ChatPanel.mount(panel);
    }

    // A draft stores nothing until the reader replies. That reply is the first moment there
    // is a session id for the address to carry, and the panel is what announces it.
    panel.addEventListener("chat-panel:session", (event) => {
      writeAddress(event.detail.sid);
      openPage.href = `/chat/${event.detail.sid}`;
    });

    ask.addEventListener("click", () => {
      show(true);
      mount(fetch("/chat/new/panel"));
    });

    // Closed, not hidden: the address stops naming a conversation, so a reload does not
    // bring this one back. The button below is how the next one starts.
    rail.querySelector(".js-rail-close").addEventListener("click", () => {
      show(false);
      writeAddress(null);
      panel.replaceChildren();
      delete panel.dataset.chatMounted;
    });

    const opening = window.ChatRail.opening || {};
    if (opening.panel) mount(opening.panel);
  });
})();
