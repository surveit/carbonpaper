// One file's page: the shape filters, what a facet's shares are over, the note a
// sampled file owes, and the typed confirmation the delete takes.
(function () {
  document.addEventListener("click", function (event) {
    var scale = event.target.closest(".facet-scale button");
    if (scale) return rescaleFacet(scale);
    var chip = event.target.closest(".shape-filter");
    if (chip) return filterColumns(chip);
  });

  // The bars are drawn against the longest bar SHOWN and the number beside each is its
  // share of the chosen whole, so one denominator is on screen at a time.
  function rescaleFacet(button) {
    var facet = button.closest(".shape-facet");
    var table = facet.querySelector(".facet-values");
    var overFilled = button.dataset.scale === "filled";
    facet.querySelectorAll(".facet-scale button").forEach(function (other) {
      other.classList.toggle("on", other === button);
    });
    var whole = Number(overFilled ? table.dataset.filled : table.dataset.rows);
    var rows = Array.prototype.slice.call(table.querySelectorAll("tr"));
    rows.forEach(function (row) {
      row.hidden = overFilled && row.classList.contains("fv-absent");
    });
    var widest = Math.max.apply(null, rows.filter(function (row) { return !row.hidden; })
      .map(function (row) { return Number(row.dataset.count); }));
    rows.forEach(function (row) {
      var count = Number(row.dataset.count);
      row.querySelector(".fv-bar i").style.width = (100 * count / widest) + "%";
      row.querySelector(".fv-pct").textContent = (100 * count / whole).toFixed(1) + "%";
    });
  }

  function filterColumns(chip) {
    var want = chip.dataset.filter;
    document.querySelectorAll(".shape-filter").forEach(function (other) {
      other.classList.toggle("on", other === chip);
    });
    document.querySelectorAll(".shape-col").forEach(function (column) {
      column.hidden = want !== "all" && column.dataset.group !== want;
    });
  }

  var completeness = document.getElementById("file-completeness");
  var lineage = document.getElementById("file-lineage");
  var hint = document.getElementById("file-lineage-hint");
  if (completeness && lineage && hint) {
    // Said before the save refuses it: a sampled file's note is what says which rows
    // these are, and the server raises without one.
    var showHint = function () {
      hint.hidden = completeness.value !== "sampled" || lineage.value.trim() !== "";
    };
    completeness.addEventListener("change", showHint);
    lineage.addEventListener("input", showHint);
    showHint();
  }

  var confirm = document.getElementById("file-confirm");
  var submit = document.getElementById("file-delete-submit");
  if (confirm && submit) {
    confirm.addEventListener("input", function () {
      submit.disabled = confirm.value.trim() !== confirm.dataset.filename;
    });
  }
})();
