// The overview's agent controls. A row names a job; clicking it opens that job as a
// conversation in the rail, on this page, rather than sending the reader to /chat.
//
// The anchor keeps its href, so a failed POST — or no JS at all — still lands them in
// the same conversation on its own page.
(function () {
  const AGENT = "editing";

  async function openInTheRail(link) {
    const task = link.dataset.agentTask;
    const project = link.dataset.project;
    const r = await fetch(`/chat/agent/${AGENT}/sessions`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({context: {project_id: project, task}, title: task})
    });
    const session = await r.json();
    if (!session.ok) throw new Error(session.error || "could not start this chat");
    // Sent before the panel mounts: the panel reads the running turn off the session and
    // reattaches to it, which is the same path a reload takes.
    await fetch(`/chat/${session.sid}/message`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text: task})
    });
    window.ChatRail.open(session.sid, task);
  }

  document.addEventListener("click", function (e) {
    const link = e.target.closest("[data-agent-task]");
    if (!link || !window.ChatRail || !window.ChatRail.open) return;
    e.preventDefault();
    link.classList.add("is-opening");
    openInTheRail(link)
      .catch(() => { window.location.href = link.href; })
      .finally(() => link.classList.remove("is-opening"));
  });
})();
