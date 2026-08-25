// The Input files tab: one file at a time, and two toggles that decide which rows
// and which columns of it every panel below is about.
//
// Mounted by the row lineage page after it swaps the panel in, so it claims its
// namespace idempotently and re-runs against whatever root it is handed.
window.InputFiles = window.InputFiles || (function () {
  function mount(root) {
    var tab = root.querySelector(".if-tab");
    if (!tab) return;
    if (window.CarbonPicker) window.CarbonPicker.init(tab);
    bindFilePicker(tab);
    bindToggles(tab);
    paint(tab);
  }

  function bindFilePicker(tab) {
    var picker = tab.querySelector(".if-file");
    if (!picker) return;
    picker.addEventListener("change", function () {
      tab.querySelectorAll(".if-file-pane").forEach(function (pane) {
        pane.classList.toggle("hidden", pane.dataset.file !== picker.value);
      });
      var by = tab.querySelector(".if-stage");
      if (by) by.textContent = picker.value;
      paint(tab);
    });
  }

  function bindToggles(tab) {
    tab.querySelectorAll(".if-tg").forEach(function (group) {
      group.addEventListener("click", function (event) {
        var button = event.target.closest("button");
        if (!button) return;
        group.querySelectorAll("button").forEach(function (other) {
          other.classList.toggle("on", other === button);
        });
        tab.dataset[group.dataset.axis] = button.dataset.pick;
        paint(tab);
      });
    });
  }

  function shownPane(tab) {
    return tab.querySelector(".if-file-pane:not(.hidden)");
  }

  // Every count a reader sees is counted off the rows and columns actually shown,
  // so a state the toggles reach can never be described by a stale number.
  function paint(tab) {
    var pane = shownPane(tab);
    if (!pane) return;
    var rows = pane.querySelectorAll("tbody tr:not([hidden])");
    var shown = 0;
    rows.forEach(function (row) { if (row.offsetParent !== null) shown += 1; });
    var columns = 0;
    pane.querySelectorAll("thead th[data-relevant]").forEach(function (head) {
      if (head.offsetParent !== null) columns += 1;
    });
    var count = pane.querySelector(".if-count");
    if (count) count.textContent = countOf(shown, "row") + " × " + countOf(columns, "column");
    paintTake(tab, pane, shown, columns);
  }

  function paintTake(tab, pane, rows, columns) {
    var link = pane.querySelector(".if-download");
    if (!link) return;
    link.textContent = "Download " + countOf(rows, "row") + " × " +
      countOf(columns, "column") + " (CSV)";
    link.href = urlFor(tab, pane, "slice.csv");
    var checked = pane.querySelector(".if-checked");
    if (checked) { checked.hidden = true; checked.textContent = ""; }
    check(tab, pane);
  }

  function urlFor(tab, pane, leaf) {
    var query = new URLSearchParams({
      stage: tab.dataset.stage, row: tab.dataset.row, column: tab.dataset.column,
      input: pane.dataset.file, rows: tab.dataset.rows,
      columns: tab.dataset.columns,
    });
    return "/project/" + encodeURIComponent(tab.dataset.project) + "/runs/" +
      encodeURIComponent(tab.dataset.run) + "/input-files/" + leaf + "?" + query;
  }

  // The comparison re-reads the file, so it is asked for after the panel is drawn
  // rather than made part of the page load.
  function check(tab, pane) {
    var asked = urlFor(tab, pane, "check.json");
    pane.dataset.asked = asked;
    fetch(asked).then(function (answer) {
      return answer.ok ? answer.json() : null;
    }).then(function (report) {
      if (pane.dataset.asked !== asked) return;
      say(pane.querySelector(".if-checked"), report);
    }).catch(function () { /* the line stays hidden; nothing is claimed */ });
  }

  function say(line, report) {
    if (!line || !report) return;
    line.hidden = false;
    line.classList.toggle("if-differs", report.mismatches > 0);
    var mark = report.mismatches ? "✗ " + report.mismatches.toLocaleString() +
      " cells differ." : "✓ checked.";
    line.innerHTML = "<b>" + mark + "</b> " + countOf(report.cells, "cell") + " — " +
      countOf(report.rows, "row") + " × " + countOf(report.columns, "column") +
      " — read back from " + escapeText(report.filename) +
      " as the run read it, rows located by " + escapeText(report.located_by) + ".";
  }

  function countOf(many, thing) {
    return many.toLocaleString() + " " + thing + (many === 1 ? "" : "s");
  }

  function escapeText(text) {
    var box = document.createElement("span");
    box.textContent = text;
    return box.innerHTML;
  }

  return { mount: mount };
})();
