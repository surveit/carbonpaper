// Shared chrome, state and vocabulary for the three option pages.
(function () {
  const OPTIONS = [["sentence", "B4 · What this run says"], ["claims", "Claims list"], ["a", "A · Ledger"], ["b", "B · The sentence"], ["c", "C · The register"]];
  const CLS = {
    data: ["data", "a defect in the file or its reading"],
    choice: ["a choice made", "one reading taken where another was open"],
    omission: ["a decision never made", "no branch reads it, so the default decided"],
    coverage: ["coverage", "who is in the count and who is not"],
    semantic: ["what it means", "the same number, a different sentence"],
    causal: ["framing", "a reading the arithmetic does not support"],
    gap: ["not examined", "the probe did not reach this"]
  };
  const COST = {
    free: "free rerun", person: "a person's call", outside: "outside data",
    editorial: "editorial", settled: "settled in the methodology"
  };
  const MOVES = { moves: "moves the figure", meaning: "changes the sentence", unpriced: "cannot be told yet", none: "does not move it" };

  function q(k, d) { const u = new URL(location.href); return u.searchParams.get(k) || d; }
  function href(opt, claim, view) { return `${opt}.html?claim=${claim}&view=${view}`; }

  const claimId = q("claim", CLAIMS[0].id);
  const view = q("view", "author");
  const claim = CLAIMS.find(c => c.id === claimId) || CLAIMS[0];

  // Author decisions. Dismissal is unrecorded: it lives only in this browser.
  const KEY = "claims-review-state:" + claim.id;
  let state = { accepted: [], dismissed: [], adopted: [] };
  try { state = Object.assign(state, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch (e) {}
  function save() { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} }
  const S = {
    is(list, id) { return state[list].includes(id); },
    add(list, id) { if (!S.is(list, id)) state[list].push(id); save(); render(); },
    del(list, id) { state[list] = state[list].filter(x => x !== id); save(); render(); },
    reset() { state = { accepted: [], dismissed: [], adopted: [] }; save(); render(); }
  };

  function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
  function nums(s) { return esc(s).replace(/(\$?[\d][\d,.]*[%mk]?(?:\s?→\s?\$?[\d][\d,.]*[%mk]?)?)/g, '<span class="n">$1</span>'); }

  function live() { return claim.challenges.filter(c => !S.is("dismissed", c.id)); }
  function bands() {
    const cs = live();
    return {
      open: cs.filter(c => !c.settled && (c.moves.kind === "moves" || c.moves.kind === "meaning")),
      unpriced: cs.filter(c => !c.settled && c.moves.kind === "unpriced"),
      quiet: cs.filter(c => c.settled || c.moves.kind === "none")
    };
  }

  function claimsBadge() {
    const st = CLAIMS.map(c => { try { return (JSON.parse(localStorage.getItem("sentence-v3:" + c.id) || "{}").status) || (window.DEFAULT_STATUS || {})[c.id] || "submitted"; } catch (e) { return "submitted"; } });
    const review = st.filter(x => x === "submitted").length, made = st.filter(x => x === "approved").length;
    return review ? `<span class="nav-badge todo">${review} to review</span>` : `<span class="nav-badge">${made}</span>`;
  }
  function shell(optKey, body) {
    const opt = OPTIONS.find(o => o[0] === optKey);
    document.body.innerHTML = `
<header>
  <div class="crumbs">
    <a class="brand" href="index.html"><span class="wordmark">Carbon <span class="wordmark-transfer">Paper</span></span></a>
    <span class="crumb-sep">/</span><a class="crumb-link" href="#">${esc(claim.project)}</a>
    ${optKey === "claims" ? "" : `<span class="crumb-sep">/</span><a class="crumb-link" href="#">Runs</a><span class="crumb-sep">/</span><a class="crumb-link" href="#"><code>${esc(claim.run)}</code></a>`}
    <span class="crumb-sep">/</span><span class="crumb-here">${optKey === "sentence" ? "What it says" : optKey === "claims" ? "Claims" : esc(claim.label)}</span>
  </div>
  <a class="back-link header-right" href="#">Chats</a><a class="back-link" href="#">Admin</a>
</header>
<main>
<div class="app-shell">
  <aside class="app-side">
    <div class="app-side-head"><a class="app-side-brand" href="#"><span class="app-side-kicker">project</span><span class="app-side-name">${esc(claim.project)}</span></a></div>
    <nav class="app-side-nav">
      <a class="app-nav-item" href="#">Overview</a>
      <a class="app-nav-item trail" href="#">Workflow</a>
      <div class="app-nav-children">
        <a class="app-nav-item child" href="#">Versions</a>
        <a class="app-nav-item child current" href="#">Runs</a>
        <a class="app-nav-item child" href="#">Evals</a>
      </div>
      <a class="app-nav-item ${optKey === "claims" ? "current" : ""}" href="claims.html">Claims ${claimsBadge()}</a>
      <a class="app-nav-item" href="#">Files</a>
      <a class="app-nav-item" href="#">Documentation</a>
    </nav>
  </aside>
  <section>
    <div class="mock-strip"><b>mock</b> <a href="index.html">the three, compared</a>
      ${OPTIONS.map(o => `<a class="${o[0] === optKey ? "cur" : ""}" href="${href(o[0], claim.id, view)}">${o[1]}</a>`).join("")}
      <span class="right">
        ${optKey === "sentence" || optKey === "claims" ? "" : `<span class="seg">
          <button class="${view === "author" ? "on" : ""}" onclick="location.href='${href(optKey, claim.id, "author")}'">as the author</button>
          <button class="${view === "reader" ? "on" : ""}" onclick="location.href='${href(optKey, claim.id, "reader")}'">as a reader</button>
        </span>`}
        <button class="btn quiet" onclick="CR.S.reset()" title="Forget every accept and dismiss on this claim">reset</button>
      </span>
    </div>
    <div class="claim-tabs">${CLAIMS.map(c => `<a class="${c.id === claim.id ? "on" : ""}" href="${href(optKey, c.id, view)}">${esc(c.value)} <code>${esc(c.stage)}</code></a>`).join("")}</div>
    <div id="page">${body}</div>
  </section>
</div>
</main>`;
  }

  function claimCard(opts = {}) {
    const accepted = live().filter(c => S.is("accepted", c.id) && c.qualifier);
    const quals = claim.qualifiers.map(x => `<li>${esc(x)}</li>`)
      .concat(accepted.map(c => `<li class="qual-added">${esc(c.qualifier)}${view === "author" ? '<span class="tag">added</span>' : ""}</li>`)).join("");
    const alts = live().filter(c => c.alt && S.is("adopted", c.id));
    return `
<article class="mint-fig">
  <div class="mint-top">
    <div class="mint-kicker">Claimed · run <code>${esc(claim.run)}</code></div>
    <div class="mint-claim"><a class="mint-value" href="#" title="Open the row this value was read from">${esc(claim.value)}</a><span class="mint-label">${esc(claim.label)}</span></div>
    <div class="mint-row"><span class="mint-row-k">coverage</span><span>${esc(claim.coverage)}</span></div>
    <div class="mint-row"><span class="mint-row-k">context</span><span class="mint-dim">${esc(claim.context)}</span></div>
    <div class="mint-quals"><span class="mint-row-k">qualifiers</span><ul>${quals}</ul></div>
  </div>
</article>
${alts.map(c => `
<article class="mint-fig also">
  <div class="mint-top">
    <div class="mint-kicker">Also claimed · the same run</div>
    <div class="mint-claim"><a class="mint-value small" href="#">${esc(c.alt.value)}</a><span class="mint-label">${esc(c.alt.label)}</span></div>
    <div class="mint-quals"><span class="mint-row-k">why both</span><ul><li>${esc(c.alt.note)}</li></ul></div>
  </div>
</article>`).join("")}`;
  }

  function lede() {
    return `<div class="lede-ai"><span class="mark" title="Written by the AI">AI</span><p style="margin:0">${esc(claim.lede)}</p></div>`;
  }

  function clsCell(c, withStage) {
    const [w, why] = CLS[c.cls];
    return `<div class="ch-cls">${w}<small>${why}</small>${withStage ? `<small>at <code>${esc(c.stage)}</code></small>` : ""}</div>`;
  }
  function moveChip(c) { return `<span class="mv ${c.moves.kind}"><b>${MOVES[c.moves.kind]}</b>${c.moves.detail ? " · " + esc(c.moves.detail) : ""}</span>`; }
  function costLine(c) {
    const shared = claim.shared_stages[c.stage];
    return `<span class="cost">to settle: <b>${COST[c.cost]}</b></span>${c.cross ? `<span class="cross">moves <code>${esc(c.cross)}</code></span>` : ""}${shared ? `<span class="cross" title="This stage feeds other claims too">shared with ${shared} other claim${shared > 1 ? "s" : ""}</span>` : ""}`;
  }
  function actions(c) {
    if (c.settled) return `<span class="muted">${esc(c.settled_by)}</span>`;
    const acc = S.is("accepted", c.id), adopted = S.is("adopted", c.id);
    const parts = [];
    if (c.alt) parts.push(adopted
      ? `<span class="muted">Claimed beside it.</span> <button class="btn quiet" onclick="CR.S.del('adopted','${c.id}')">undo</button>`
      : `<button class="btn primary" onclick="CR.S.add('adopted','${c.id}')">Claim this beside it</button>`);
    if (c.qualifier) parts.push(acc
      ? `<span class="muted">On the claim as a qualifier.</span> <button class="btn quiet" onclick="CR.S.del('accepted','${c.id}')">undo</button>`
      : `<button class="btn ${c.alt ? "" : "primary"}" onclick="CR.S.add('accepted','${c.id}')">Add to the claim</button>`);
    if (!c.alt && !c.qualifier) parts.push(`<span class="muted">Nothing to add to the claim; a rerun or a person settles it.</span>`);
    parts.push(`<button class="btn quiet" onclick="CR.S.add('dismissed','${c.id}')" title="Unrecorded. It leaves this page and nothing else.">Dismiss</button>`);
    return parts.join(" ");
  }
  function card(c, compact) {
    return `<div class="ch ${compact ? "compact" : ""}">
  <div class="ch-top">${clsCell(c, true)}
    <div><div class="ch-text">${esc(c.text)}</div><div class="ch-ev">${nums(c.evidence)}</div>${c.raised ? `<div class="ch-raised">raised by ${esc(c.raised)}</div>` : ""}</div>
    <div class="ch-side">${moveChip(c)}${costLine(c)}</div>
  </div>
  <div class="ch-act">${actions(c)}</div>
</div>`;
  }
  function quietRow(c) {
    return `<div class="quiet-row">${clsCell(c, true)}<div>${esc(c.text)} <span style="color:var(--muted-dim)">${nums(c.evidence)}</span></div><div>${c.settled ? `<span class="mv none">settled: ${esc(c.settled_by)}</span>` : moveChip(c)} ${c.cross ? `<span class="cross">moves <code>${esc(c.cross)}</code></span>` : ""}</div></div>`;
  }

  function sentenceHTML(withCounts, filterPhrase) {
    const byPhrase = {};
    live().forEach(c => { (byPhrase[c.phrase] = byPhrase[c.phrase] || []).push(c); });
    return claim.sentence.map((tok, i) => {
      if (!claim.phrases.includes(i)) return esc(tok);
      const cs = byPhrase[i] || [];
      const hot = cs.some(c => !c.settled && c.moves.kind === "moves");
      const mean = cs.some(c => !c.settled && c.moves.kind === "meaning");
      const cls = ["ph", hot ? "hot" : mean ? "mean" : "", cs.length ? "" : "none-ph", filterPhrase === i ? "on" : ""].join(" ");
      return `<span class="${cls}" data-ph="${i}">${esc(tok)}${withCounts && cs.length ? `<span class="cnt">${cs.length}</span>` : ""}</span>`;
    }).join("");
  }
  function altSentence(c) {
    return claim.sentence.map((tok, i) => {
      const rep = c.alt.replace && c.alt.replace[i];
      const join = c.alt.join && i in c.alt.join ? c.alt.join[i] : null;
      if (rep !== undefined) return `<del>${esc(tok)}</del> <ins>${esc(rep)}</ins>`;
      if (join === null) return esc(tok);
      // Connective words change silently; only a load-bearing phrase is shown struck or inserted.
      if (!claim.phrases.includes(i)) return esc(join);
      return join === "" ? `<del>${esc(tok)}</del>` : `<del>${esc(tok)}</del> <ins>${esc(join)}</ins>`;
    }).join("");
  }

  let render = () => {};
  window.CR = { claim, view, S, esc, nums, live, bands, shell, claimCard, lede, card, quietRow, clsCell, moveChip, costLine, actions, sentenceHTML, altSentence, CLS, COST, MOVES, setRender(fn) { render = fn; fn(); } };
})();
