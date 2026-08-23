// Group the Files table by data shape, and put it back. The rows carry their group,
// its name and its size; nothing is fetched and no row is rewritten, only moved.
(function () {
  var toggle = document.getElementById("group-by-shape");
  var table = document.getElementById("files-table");
  if (!toggle || !table) return;
  var body = table.tBodies[0];
  var asListed = Array.prototype.slice.call(body.rows);

  function headingFor(row, first) {
    var head = document.createElement("tr");
    var cell = document.createElement("td");
    head.className = "files-group-head" + (first ? " first" : "");
    cell.colSpan = table.tHead.rows[0].cells.length;
    cell.textContent = row.dataset.shapeLabel;
    head.appendChild(cell);
    return head;
  }

  // Commonest shape first: the set a reader is comparing is the one with files in it.
  function orderGroups() {
    var byKey = {};
    var keys = [];
    asListed.forEach(function (row) {
      var key = row.dataset.shapeKey || "";
      if (!byKey[key]) { byKey[key] = []; keys.push(key); }
      byKey[key].push(row);
    });
    keys.sort(function (a, b) {
      var size = byKey[b].length - byKey[a].length;
      return size !== 0 ? size : keys.indexOf(a) - keys.indexOf(b);
    });
    return keys.map(function (key) { return byKey[key]; });
  }

  function group() {
    orderGroups().forEach(function (rows, i) {
      body.appendChild(headingFor(rows[0], i === 0));
      rows.forEach(function (row) { body.appendChild(row); });
    });
    table.classList.add("grouped");
  }

  function ungroup() {
    Array.prototype.slice.call(body.querySelectorAll(".files-group-head"))
      .forEach(function (head) { head.remove(); });
    asListed.forEach(function (row) { body.appendChild(row); });
    table.classList.remove("grouped");
  }

  toggle.addEventListener("change", function () {
    if (toggle.checked) group(); else ungroup();
  });
})();
