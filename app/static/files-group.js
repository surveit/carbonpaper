// Group the Files table by data shape, and put it back. The rows carry their group
// and its name; nothing is fetched and no row is rewritten, only moved.
(function () {
  var toggle = document.getElementById("group-by-shape");
  var table = document.getElementById("files-table");
  if (!toggle || !table) return;
  var body = table.tBodies[0];
  var asListed = Array.prototype.slice.call(body.rows);

  function headingFor(row) {
    var head = document.createElement("tr");
    var cell = document.createElement("td");
    head.className = "files-group-head";
    cell.colSpan = table.tHead.rows[0].cells.length;
    cell.textContent = row.dataset.shapeLabel;
    head.appendChild(cell);
    return head;
  }

  function group() {
    var seen = [];
    var byKey = {};
    asListed.forEach(function (row) {
      var key = row.dataset.shapeKey || "";
      if (!byKey[key]) { byKey[key] = []; seen.push(key); }
      byKey[key].push(row);
    });
    seen.forEach(function (key) {
      body.appendChild(headingFor(byKey[key][0]));
      byKey[key].forEach(function (row) { body.appendChild(row); });
    });
  }

  function ungroup() {
    Array.prototype.slice.call(body.querySelectorAll(".files-group-head"))
      .forEach(function (head) { head.remove(); });
    asListed.forEach(function (row) { body.appendChild(row); });
  }

  toggle.addEventListener("change", function () {
    if (toggle.checked) group(); else ungroup();
  });
})();
