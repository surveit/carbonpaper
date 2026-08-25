// The app's tab strip: a `.stage-tabs` bar and the `.panes` block right after it.
//
// The markup decides which pane opens — the active button and the one pane without
// `hidden` — so a first paint needs no script, and this file only ever moves that
// choice. Delegated from the document, so a strip that arrives by innerHTML (the run
// and workflow stage panels both do) is live without being wired.
function selectTab(scope, name) {
  const bar = scope.matches(".stage-tabs") ? scope : scope.querySelector(".stage-tabs");
  const panes = bar && bar.nextElementSibling;
  if (!panes || !panes.classList.contains("panes")) return;
  bar.querySelectorAll("[data-tab]").forEach(function (button) {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  panes.querySelectorAll(":scope > .pane").forEach(function (pane) {
    pane.hidden = pane.dataset.pane !== name;
  });
}

document.addEventListener("click", function (event) {
  const button = event.target.closest(".stage-tabs [data-tab]");
  if (button) selectTab(button.closest(".stage-tabs"), button.dataset.tab);
});
