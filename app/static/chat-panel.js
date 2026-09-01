// The conversation's client, mounted on a root element rather than on the document, so a
// page and the rail can each draw one. mount() holds every piece of state a panel has —
// nothing here is global, and nothing here knows which agent is on the other end.
//
// The turn itself lives on the server (app/core/agent/turns.py): it is a detached task with
// a replayable event buffer, so a panel that goes away mid-turn is not a turn that stopped.
// Mounting reads `active_turn` off the config and reattaches from event 0.
window.ChatPanel = window.ChatPanel || {};

window.ChatPanel.mount = function (root) {
  if (root.dataset.chatMounted) return;
  root.dataset.chatMounted = "1";

  const cfg = JSON.parse(root.querySelector(".js-chat-config").textContent);
  let SID = cfg.session_id;
  const log = root.querySelector(".js-chat-log");
  const input = root.querySelector(".js-chat-input");
  const sendBtn = root.querySelector(".js-chat-send");
  let streaming = false;

  function el(cls, html) { const d = document.createElement("div"); d.className = cls; if (html !== undefined) d.innerHTML = html; return d; }
  function fmtJson(s) { try { return JSON.stringify(JSON.parse(s), null, 2); } catch (e) { return s || ""; } }
  // The same two shapes the offer_row macro draws, so a turn reads the same live as on
  // reload: a link goes to a page and never replies, and says so with the arrow.
  function offerControl(option) {
    if (!option.url) {
      const button = document.createElement("button");
      button.type = "button"; button.className = "ac-offer"; button.textContent = option.text;
      return button;
    }
    const link = document.createElement("a");
    link.className = "ac-offer ac-offer-link"; link.href = option.url;
    link.textContent = option.text;
    const arrow = el("ac-offer-arrow", "↗");
    arrow.setAttribute("aria-hidden", "true");
    link.appendChild(arrow);
    return link;
  }

  function toolPre(text) { const p = document.createElement("pre"); p.className = "ac-tool-json"; p.textContent = fmtJson(text); return p; }

  // The log is the scrolling element in both hosts. Following only when the reader is
  // ALREADY near the bottom is what keeps a streaming reply from yanking a transcript they
  // scrolled up to read.
  function nearBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 120; }
  function scroll() { if (nearBottom()) log.scrollTop = log.scrollHeight; }

  // Two decisions about a link in a reply, both answered by the same question.
  function isInThisApp(a) {
    try { return new URL(a.href, location.href).origin === location.origin; }
    catch (e) { return false; }
  }

  // WHERE IT OPENS. app.web.markdown_render marks every link target="_blank", which was
  // right while the chat was a page you lost by clicking anything on it. The rail is the
  // change that makes it wrong: an in-app link is no longer navigating away from the
  // conversation, and a new tab is the one thing that still loses it — a tab opened this way
  // carries no reading place, and the panel it lands beside is a second copy, not this one.
  // Undone here rather than at the renderer because origin is a fact about the browser, and
  // the server behind a proxy does not have it. An external link keeps its new tab.
  // An Offer's url is validated to be a path in THIS app, so an offer link is always one
  // of these — which is why it is drawn without a target of its own.
  function keepInAppLinkInPlace(a) {
    if (!isInThisApp(a)) return;
    a.removeAttribute("target");
    a.removeAttribute("rel");
    carryTheConversation(a);
  }

  // WHAT THE LINK CARRIES. The page it opens draws this conversation beside it, because the
  // address is where the rail reads which one is open (_chat_rail_head.html). Written onto
  // the href rather than caught at click time, so what the reader sees on hover, copies, or
  // opens in a new tab is the same address either way.
  function carryTheConversation(a) {
    if (!SID) return;
    const url = new URL(a.href, location.href);
    url.searchParams.set(window.ChatRail.PARAM, SID);
    a.href = url.pathname + url.search + url.hash;
  }

  // A draft's links are drawn before the first reply materializes a session, so they are
  // stamped again the moment there is one to name.
  function readyEveryLink() {
    root.querySelectorAll(".ac-msg.assistant .ac-body a[href], .ac-offer-link")
      .forEach(keepInAppLinkInPlace);
  }

  // HOW IT IS DRAWN. A link the agent wrote on a line of its own is a handover — the thing it
  // wants opened — so it is drawn as a target rather than as underlined words inside a
  // sentence. An outside link is a citation, and following one is not moving around this app.
  function markHandovers(region) {
    region.querySelectorAll("p > a:only-child").forEach((a) => {
      if (a.parentNode.textContent.trim() !== a.textContent.trim()) return;
      if (!isInThisApp(a)) return;
      a.classList.add("ac-goto");
    });
  }

  function readyReplyLinks(region) {
    region.querySelectorAll("a[href]").forEach(keepInAppLinkInPlace);
    markHandovers(region);
  }

  // Mirrors app.web.file_sizes.render_attachment, so a turn looks the same the moment it
  // is sent as it does after a reload. The text is identical either way — this is only how
  // it is drawn, and the agent reads the text.
  const ATTACHMENT_PREFIX = "[file] ";

  function attachmentBody(line) {
    const body = el("ac-body ac-file");
    const fields = line.slice(ATTACHMENT_PREFIX.length).split(" · ");
    const icon = el("ac-file-icon", "▤"); icon.setAttribute("aria-hidden", "true");
    body.append(icon, el("ac-file-name", fields[0]));
    if (fields.length > 1) body.appendChild(el("ac-file-meta", fields[1]));
    return body;
  }

  function addUser(text) {
    const b = el("ac-msg user");
    b.appendChild(el("ac-role", "you"));
    // Attachments lead, one chip each, and whatever the person typed follows them — a
    // turn can carry several files AND words, and neither may swallow the other.
    const lines = text.split("\n");
    let cut = 0;
    // The chips share one row that wraps; the bubble below shrinks to its own words, which
    // it cannot do as a flex item of the same row.
    const files = el("ac-files");
    while (cut < lines.length && lines[cut].startsWith(ATTACHMENT_PREFIX)) {
      files.appendChild(attachmentBody(lines[cut]));
      cut += 1;
    }
    if (cut) b.appendChild(files);
    const said = cut ? lines.slice(cut).join("\n").trim() : text;
    if (said || !cut) { const body = el("ac-body"); body.textContent = said; b.appendChild(body); }
    log.appendChild(b); scroll();
  }

  // Build an assistant bubble whose regions are appended in arrival order: a chunk
  // joins the region already open, and any other kind of event closes it, so the
  // next chunk of its kind opens a new one further down.
  function newAssistant() {
    const b = el("ac-msg assistant");
    b.appendChild(el("ac-role", "assistant"));
    // Spinner from bubble creation until the first content arrives, so the gap
    // while the model thinks (esp. the block-level claude_cli backend) isn't blank.
    const spinner = el("ac-spinner",
      '<span class="ac-dot"></span><span class="ac-dot"></span><span class="ac-dot"></span><span class="ac-sp-label">thinking…</span>');
    b.appendChild(spinner);
    let think = null, thinkText = null, body = null, tools = null, offerRow = null;
    const bodies = [];  // every text region opened, in order — what the swap renders
    const bodyRaw = new Map();  // region → its raw markdown source, for the final match check
    const bodyVersion = new Map();  // region → latest render request issued, so a slow
                                    // response from an earlier chunk can't overwrite a newer one
    const pendingTools = [];  // tool_call disclosures awaiting their tool_result (FIFO)
    log.appendChild(b); scroll();
    function stop() { if (spinner.parentNode) spinner.remove(); }
    function closeRegions() { think = null; thinkText = null; body = null; tools = null; }
    // Fire-and-forget: a slower response from an earlier, shorter chunk must not clobber
    // a newer, longer one that already landed — bodyVersion picks the request that wins.
    async function renderLive(region, raw) {
      const version = (bodyVersion.get(region) || 0) + 1;
      bodyVersion.set(region, version);
      const r = await fetch(`/chat/${SID}/render-markdown`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({text: raw}),
      });
      if (!r.ok || bodyVersion.get(region) !== version) return;
      const data = await r.json();
      if (bodyVersion.get(region) !== version) return;
      region.innerHTML = data.html;
      readyReplyLinks(region);
      region.classList.remove("ac-streaming");
      scroll();
    }
    // The bare name, not the friendly label: the row is furniture, and every row opening
    // the same way is what lets the eye skip the lot. The label is the tooltip, so it
    // costs the page nothing and is still reachable.
    function toolRow(name, args, label) {
      const d = document.createElement("details"); d.className = "ac-tool";
      const sum = document.createElement("summary");
      sum.textContent = "Called a tool: " + name; sum.title = label || name;
      d.appendChild(sum); d.appendChild(toolPre(args));
      return d;
    }
    return {
      thinking(t) {
        stop();
        // A block carrying no text is no thinking to show — an open, empty
        // disclosure reads as content that failed to load. Once the block exists
        // every chunk lands, so interior blank lines are kept.
        if (!think && !t.trim()) return;
        if (!think) {
          closeRegions();
          think = document.createElement("details"); think.className = "ac-think"; think.open = true;
          const s = document.createElement("summary"); s.textContent = "💭 thinking"; think.appendChild(s);
          thinkText = el("ac-think-text"); think.appendChild(thinkText);
          b.appendChild(think);
        }
        thinkText.textContent += t; scroll();
      },
      text(t) {
        stop();
        // Each event already carries a whole TextBlock (sdk_engine.py), not a token
        // delta, so there's no half-arrived "[run](htt" to worry about — only whatever
        // the block itself contains. Re-render the region's full markdown so far on
        // every chunk; ac-streaming's plain-text fallback covers the request's flight time.
        if (!body) { closeRegions(); body = el("ac-body ac-streaming"); bodies.push(body); b.appendChild(body); }
        const raw = (bodyRaw.get(body) || "") + t;
        bodyRaw.set(body, raw);
        body.textContent = raw; scroll();
        renderLive(body, raw);
      },
      // The SECOND call is what makes a group: one call keeps the plain row, and the
      // row already on the page moves inside rather than being drawn twice. The count
      // then stands in for the names, so a turn that calls thirty tools costs one line.
      tool(name, args, label) {
        stop();
        if (!tools) { closeRegions(); tools = {names: [], first: null, body: null}; }
        const row = toolRow(name, args, label);
        tools.names.push(name);
        if (tools.names.length === 1) { tools.first = row; b.appendChild(row); }
        else {
          if (!tools.body) {
            const group = document.createElement("details"); group.className = "ac-tool ac-tools";
            tools.summary = document.createElement("summary"); group.appendChild(tools.summary);
            tools.body = el("ac-tools-body"); group.appendChild(tools.body);
            b.insertBefore(group, tools.first); tools.body.appendChild(tools.first);
          }
          tools.body.appendChild(row);
          tools.summary.textContent = "Called " + tools.names.length + " tools";
          tools.summary.title = [...new Set(tools.names)].join(", ");
        }
        pendingTools.push(row); scroll();
      },
      toolResult(content) {
        stop();
        const d = pendingTools.shift();
        if (d) {
          const lbl = el("ac-tool-label"); lbl.textContent = "result"; d.appendChild(lbl);
          d.appendChild(toolPre(content));
        } else {  // result with no matching call (shouldn't happen) — show standalone
          const d2 = document.createElement("details"); d2.className = "ac-tool";
          const sum = document.createElement("summary"); sum.textContent = "↳ result"; d2.appendChild(sum);
          d2.appendChild(toolPre(content)); b.appendChild(d2);
        }
        scroll();
      },
      // Buttons, not a tool row: the arguments are sentences the reader may send.
      offers(options) {
        stop(); closeRegions();
        // The row this turn already has, if any: a second call replaces what the first
        // offered rather than stacking a second set of buttons under it.
        if (!offerRow) { offerRow = el("ac-offers"); b.appendChild(offerRow); }
        offerRow.replaceChildren();
        options.forEach((option) => offerRow.appendChild(offerControl(option)));
        scroll();
      },
      error(t) { stop(); closeRegions(); const e = el("ac-err"); e.textContent = "⚠ " + t; b.appendChild(e); scroll(); },
      // Turn over: reconcile against the server's markdown of the SAME stored text, so
      // this bubble ends up matching what a page reload renders. Each region is already
      // showing its own live-rendered markdown by now — this is a final consistency
      // check against the persisted transcript, not the first render. Any mismatch
      // leaves the region as its last live-rendered (or plain, if that never landed) state.
      async renderStoredMarkdown() {
        if (!bodies.length) return;
        const r = await fetch(`/chat/${SID}/rendered-reply`);
        if (!r.ok) return;
        const stored = await r.json();
        const segments = stored.segments || [];
        // All or nothing: a stored reply split differently from the streamed one is a
        // different reply, and swapping the segments that happen to line up would
        // reorder the bubble.
        if (segments.length !== bodies.length) return;
        if (!bodies.every((d, i) => (bodyRaw.get(d) ?? d.textContent) === segments[i].text)) return;
        bodies.forEach((d, i) => { d.innerHTML = segments[i].html; readyReplyLinks(d); d.classList.remove("ac-streaming"); });
        scroll();
      },
      stop
    };
  }

  // Back to the message box when the reply lands — but only if nothing else has claimed
  // focus, since in the rail the reader is usually reading the page beside it.
  function refocus() {
    if (input && (document.activeElement === document.body || root.contains(document.activeElement))) input.focus();
  }

  function connect(turnId, fromIndex) {
    streaming = true; if (sendBtn) sendBtn.disabled = true;
    const bubble = newAssistant();
    const es = new EventSource(`/chat/${SID}/turn/${turnId}/stream?from=${fromIndex||0}`);
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      if (ev.kind === "thinking") bubble.thinking(ev.text);
      else if (ev.kind === "text") bubble.text(ev.text);
      else if (ev.kind === "tool_call") bubble.tool(ev.name, ev.args, ev.label);
      else if (ev.kind === "tool_result") bubble.toolResult(ev.content);
      else if (ev.kind === "offer") bubble.offers(ev.options);
      else if (ev.kind === "error") bubble.error(ev.text);
      else if (ev.kind === "done") { bubble.stop(); es.close(); streaming = false; if (sendBtn) sendBtn.disabled = false; refocus(); bubble.renderStoredMarkdown(); }
    };
    es.onerror = () => { bubble.stop(); es.close(); streaming = false; if (sendBtn) sendBtn.disabled = false; };
  }

  // ── Attaching a file ────────────────────────────────────────────────────────
  // The bytes go straight to the server and never through a message, so a 9MB export
  // costs the conversation one line of text. `attached` holds that line until Send.
  const clip = root.querySelector(".js-chat-clip");
  const filePicker = root.querySelector(".js-chat-file");
  const attachedBox = root.querySelector(".js-chat-attached");
  const projectModal = root.querySelector(".js-chat-project-modal");
  let attached = [];       // the lines the next Send carries, one per file
  let pendingFile = null;  // picked, waiting on a project answer

  function describeBytes(n) {
    const mb = 1024 * 1024;
    if (n >= 1024 * mb) return +(n / (1024 * mb)).toPrecision(3) + "GB";
    if (n >= mb) return +(n / mb).toPrecision(3) + "MB";
    if (n >= 1024) return +(n / 1024).toPrecision(3) + "KB";
    return n + "B";
  }

  function drawAttached() {
    attachedBox.replaceChildren();
    attachedBox.hidden = attached.length === 0;
    attached.forEach((line, index) => {
      const chip = el("ac-pending-chip");
      const fields = line.slice(ATTACHMENT_PREFIX.length).split(" · ");
      chip.appendChild(el("ac-file-name", fields[0]));
      if (fields.length > 1) chip.appendChild(el("ac-file-meta", fields[1]));
      const drop = document.createElement("button");
      drop.type = "button";
      drop.className = "ac-unattach";
      drop.textContent = "×";
      // The bytes are already stored by now, and nothing here deletes a file — so this
      // takes it off the message, and says so rather than implying a delete.
      drop.title = "Take this off the message. The file stays in the project's files.";
      drop.addEventListener("click", () => { attached.splice(index, 1); drawAttached(); });
      chip.appendChild(drop);
      attachedBox.appendChild(chip);
    });
    fitComposer();
  }

  function showAttached(line) {
    if (line) attached.push(line);
    drawAttached();
  }

  function askWhereItGoes() {
    const box = root.querySelector(".js-chat-project-choices");
    box.replaceChildren();
    cfg.projects.forEach((project) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "ac-choice"; b.dataset.project = project.id;
      const name = document.createElement("strong");
      name.textContent = project.name;
      const id = document.createElement("span");
      id.className = "ac-muted"; id.textContent = " · " + project.id;
      b.append(name, id);
      box.appendChild(b);
    });
    projectModal.showModal();
  }

  // Materializes a real, stored session on first use (see /chat/agent/{id}/sessions) —
  // a no-op once SID is real, so send() and storeFile() can both call this unconditionally.
  // The host is told rather than acted on: a page rewrites its address, the rail remembers
  // the id, and this does not need to know which one it is inside.
  async function ensureSession() {
    if (SID) return SID;
    const r = await fetch(`/chat/agent/${cfg.draft_agent_id}/sessions`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({context: cfg.draft_context, title: cfg.title})
    });
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || "could not start this chat");
    SID = data.sid;
    readyEveryLink();
    root.dispatchEvent(new CustomEvent("chat-panel:session", {detail: {sid: SID}, bubbles: true}));
    return SID;
  }

  async function storeFile(file, projectId) {
    let sid;
    try { sid = await ensureSession(); }
    catch (e) { const b = newAssistant(); b.error(e.message); return; }
    const fd = new FormData();
    fd.append("file", file);
    fd.append("project_id", projectId || "");
    const r = await fetch(`/chat/${sid}/files`, {method: "POST", body: fd});
    let data = {};
    try { data = await r.json(); } catch (e) { /* leave empty */ }
    if (r.ok && data.ok) { showAttached(data.line); return; }
    const b = newAssistant();
    b.error(data.error || `upload failed (HTTP ${r.status})`);
  }

  function attach(file) {
    if (!file) return;
    if (file.size > cfg.max_upload_bytes) {
      const b = newAssistant();
      b.error(`"${file.name}" is ${describeBytes(file.size)}, over the ` +
              `${describeBytes(cfg.max_upload_bytes)} limit for a single input. That ceiling is ` +
              `what a run on this machine can load into memory. Convert it to parquet, or ` +
              `cut it down.`);
      return;
    }
    // A project the session already works on takes the file with no question asked.
    if (cfg.session_project) { storeFile(file, cfg.session_project); return; }
    if (!cfg.projects.length) { storeFile(file, ""); return; }
    pendingFile = file;
    askWhereItGoes();
  }

  if (clip && filePicker) {
    clip.addEventListener("click", () => filePicker.click());
    filePicker.addEventListener("change", () => {
      attach(filePicker.files[0]);
      filePicker.value = "";   // let the same file be re-picked later
    });
    projectModal.addEventListener("click", (e) => {
      const choice = e.target.closest(".ac-choice");
      if (!choice) return;
      projectModal.close();
      const file = pendingFile; pendingFile = null;
      // A blank project is "New project": the file goes in unclaimed and the agent
      // creates one and adopts it. Nothing here can create a project — that needs a
      // name and a methodology document, which is a form and not a question.
      if (file) storeFile(file, choice.dataset.project);
    });
    // Escape, or the scrim: the file was never sent, so cancelling costs nothing.
    projectModal.addEventListener("close", () => { pendingFile = null; });
    // Drop on the conversation, which is the whole reason the clip is not enough. Bound to
    // the panel and not the document: in the rail the rest of the window is a run or a
    // workflow, and a CSV dropped on one of those is not an attachment to this chat.
    root.addEventListener("dragover", (e) => { e.preventDefault(); });
    root.addEventListener("drop", (e) => {
      e.preventDefault();
      if (e.dataTransfer && e.dataTransfer.files.length) attach(e.dataTransfer.files[0]);
    });
  }

  async function send() {
    const typed = input.value.trim();
    // Attachments lead, one per line, then a blank line, then what was typed. The files
    // ARE the message when nothing was typed: a file arriving is the turn.
    const text = attached.length
      ? attached.join("\n") + (typed ? `\n\n${typed}` : "")
      : typed;
    if (!text || streaming) return;
    input.value = "";
    attached = [];
    drawAttached();
    sendText(text);
  }

  async function sendText(text) {
    if (!text || streaming) return;
    // Claimed here rather than when the stream opens: two quick clicks on the offers are
    // two turns, and the second is not one the reader asked for.
    streaming = true;
    addUser(text);
    let sid;
    try { sid = await ensureSession(); }
    catch (e) { streaming = false; const b = newAssistant(); b.error(e.message); return; }
    const r = await fetch(`/chat/${sid}/message`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text})
    });
    const data = await r.json();
    if (!data.ok) {
      streaming = false;
      const b = newAssistant(); b.error(data.error || "request failed"); return;
    }
    connect(data.turn_id, 0);
  }

  // ── Keeping the reader's place ──────────────────────────────────────────────
  // The rail re-mounts on every page, so without this a reader who follows a link out of a
  // reply is dropped at the bottom of the transcript and has to find their way back.
  //
  // The place is WHICH MESSAGE they were reading, not a pixel offset, because the two hosts
  // are different widths: the same transcript is 1280px wide on the page and 400 in the
  // rail, so the same offset is a different part of the conversation. An index survives the
  // trip, and survives a turn being appended while they were away.
  const PLACE_MARGIN = 4;
  // Tight, and deliberately not the 120px nearBottom() follows a live turn by. That asks
  // "are they keeping up"; this asks "were they at the end" — and at 120 the whole last
  // screenful of a short conversation is recorded as the end, so a reader on the
  // second-to-last message is dropped past it on the next page.
  const AT_END = 8;

  // Read per call, not captured: a draft page has no SID until the reader first replies.
  function placeKey() { return `chat-panel:place:${SID}`; }

  // Where the log's own top edge is on screen — the line a message is "at the top" of.
  function scrollboxTop() { return log.getBoundingClientRect().top; }
  function scrollToEnd() { log.scrollTop = log.scrollHeight; }

  // "<index>:<fraction>" — which message, and how far down it. The fraction rather than a
  // pixel offset for the same reason as the index: at 400px a reply is several times taller
  // than the same reply at 1280, so only its own height is a unit both hosts share.
  //
  // "bottom" when they were already there: a turn that finished while the page was loading
  // must not leave them stranded above its reply.
  function readingPlace() {
    if (log.scrollHeight - log.scrollTop - log.clientHeight < AT_END) return "bottom";
    const top = scrollboxTop();
    const reading = [...log.children].findIndex(
      (msg) => msg.getBoundingClientRect().bottom > top + PLACE_MARGIN);
    if (reading < 0) return "bottom";
    const rect = log.children[reading].getBoundingClientRect();
    const into = rect.height ? (top - rect.top) / rect.height : 0;
    return `${reading}:${Math.min(Math.max(into, 0), 1).toFixed(3)}`;
  }

  function rememberReadingPlace() {
    if (!SID || !log.children.length) return;
    try { sessionStorage.setItem(placeKey(), readingPlace()); } catch (e) { /* private mode */ }
  }

  // What restore left the scrollbox at, so `load` can tell "nothing has moved" from "the
  // reader has taken over" without having to guess at intent.
  let restoredTo = null;

  function restoreReadingPlace() {
    let place = null;
    try { place = SID && sessionStorage.getItem(placeKey()); } catch (e) { /* private mode */ }
    // Nothing remembered: a rail is arriving fresh beside a page and should show the newest
    // turn, but the chat PAGE is a document, and yanking a document on load is the browser's
    // call rather than ours.
    if (place === null) { scrollToEnd(); return; }
    const [index, into] = place.split(":");
    const reading = log.children[Number(index)];
    if (place === "bottom" || !reading) { scrollToEnd(); return; }
    const rect = reading.getBoundingClientRect();
    const delta = rect.top - scrollboxTop() + (Number(into) || 0) * rect.height;
    log.scrollTop += delta;
    restoredTo = log.scrollTop;
  }

  // Anything that lands after mount and changes a height above the reader — a late image, a
  // diagram the page draws for itself — moves the transcript out from under the place we
  // just restored. Re-anchoring on load costs one measurement and is a no-op when nothing
  // moved. Skipped the moment the reader scrolls for themselves, so it cannot fight them.
  window.addEventListener("load", () => {
    if (restoredTo === null || Math.abs(log.scrollTop - restoredTo) > 1) return;
    restoreReadingPlace();
  });

  // rAF-coalesced: a scroll fires per frame and this writes to storage.
  let placePending = false;
  function notePlaceSoon() {
    if (placePending) return;
    placePending = true;
    requestAnimationFrame(() => { placePending = false; rememberReadingPlace(); });
  }
  log.addEventListener("scroll", notePlaceSoon, {passive: true});
  // The last scroll event loses the race with the navigation a click starts, which is what
  // pagehide is for.
  window.addEventListener("pagehide", rememberReadingPlace);

  // A textarea keeps the height it was given, so every edit re-measures: collapse it, then
  // take what the text needs — scrollHeight is content + padding, and the border is the part
  // the box does not report.
  function fitComposer() {
    if (!input) return;
    input.style.height = "auto";
    input.style.height = input.scrollHeight + (input.offsetHeight - input.clientHeight) + "px";
  }

  // The composer exists only when a real agent is bound; a view-only generation session has
  // no input to wire, but must still reattach to stream its live turn.
  if (input && sendBtn) {
    sendBtn.addEventListener("click", send);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
    input.addEventListener("input", fitComposer);
    window.addEventListener("resize", fitComposer);
    // Delegated, so a turn that has just streamed its offers is wired like one the page
    // rendered. A click sends those words and leaves a half-typed draft where it is.
    log.addEventListener("click", (e) => {
      // A button only: an anchor offer is a link, and opening it is the whole of it.
      const offer = e.target.closest("button.ac-offer");
      if (offer) sendText(offer.textContent);
    });
    fitComposer();
  }

  log.querySelectorAll(".ac-msg.assistant .ac-body").forEach(readyReplyLinks);
  log.querySelectorAll(".ac-offer-link").forEach(keepInAppLinkInPlace);
  restoreReadingPlace();
  // Reattach to a turn already running on the server. `from=0` replays it whole: the store
  // holds a turn's blocks only once it has finished, so mid-turn the buffer is the transcript.
  if (cfg.active_turn) connect(cfg.active_turn, 0);
  // A draft page opened from a link that already named the job sends it as the reader's
  // own first message. Only a draft: SID is null until the first reply materializes one.
  if (cfg.opening_message && !SID) sendText(cfg.opening_message);
};

// Self-mounting, so neither host needs a call it could make too early. The rail mounts its
// own copy directly, because it arrives after this has run.
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-chat-panel]").forEach(window.ChatPanel.mount);
});
