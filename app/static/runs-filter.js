// Narrows the runs table to the rows whose name, filenames or version message
// contain what was typed. Client-side over what the page already holds: the box
// exists to find one run in a list of dozens, not to query the store.
(function () {
  var box = document.getElementById("runs-filter");
  if (!box) return;
  var count = document.getElementById("runs-filter-count");
  var rows = [].slice.call(document.querySelectorAll(".js-run-row[data-search]"));
  box.addEventListener("input", function () {
    var query = box.value.trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (row) {
      var hit = !query || row.dataset.search.indexOf(query) !== -1;
      row.hidden = !hit;
      if (hit) shown++;
    });
    count.textContent = query ? shown + " of " + rows.length : "";
  });
})();
