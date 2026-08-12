// A table row that opens what it describes. Delegated from the document, like
// cell-expand.js, so any page with `.js-run-row[data-href]` rows gets it without
// wiring — the runs index and one eval's run history draw the same table.
// A click that landed on a link is that link's; ⌘/Ctrl opens a new tab.
document.addEventListener("click", function (event) {
  var row = event.target.closest(".js-run-row[data-href]");
  if (!row || event.target.closest("a")) return;
  var href = row.dataset.href;
  if (event.metaKey || event.ctrlKey) window.open(href, "_blank", "noopener");
  else location.href = href;
});
