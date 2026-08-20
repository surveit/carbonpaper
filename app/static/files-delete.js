// One dialog serves the whole table: the trash button that opened it carries the row's
// name, hash and run count, so the warning is about the file being deleted rather than a
// generic one. Escape or Cancel closes it, and nothing is sent until the name matches.
(function () {
  var modal = document.getElementById("delete-modal");
  if (!modal) return;
  var form = document.getElementById("delete-form");
  var confirmField = document.getElementById("delete-confirm");
  var submit = document.getElementById("delete-submit");
  var project = modal.getAttribute("data-project");
  var expected = "";

  // The server checks this too and answers 400, but nobody should ever meet that: a
  // typo is a button that will not press, on the page where the name is in front of you.
  function gateSubmit() {
    var matches = confirmField.value.trim() === expected;
    submit.disabled = !matches;
    submit.title = matches ? "" : "Type the file's name to enable this";
  }

  confirmField.addEventListener("input", gateSubmit);

  function warn(runCount, filename) {
    var runs = parseInt(runCount, 10) || 0;
    if (!runs) return "No run has read " + filename + ". Deleting it cannot be undone.";
    if (runs === 1) {
      return "1 run has read " + filename + ". Its manifest keeps saying so, but " +
        "re-running it stops working.";
    }
    return runs + " runs have read " + filename + ". Their manifests keep saying so, " +
      "but re-running them stops working.";
  }

  document.querySelectorAll(".files-trash").forEach(function (button) {
    button.addEventListener("click", function () {
      var filename = button.getAttribute("data-filename");
      document.getElementById("delete-name").textContent = filename;
      document.getElementById("delete-warning").textContent =
        warn(button.getAttribute("data-runs"), filename);
      expected = filename;
      confirmField.value = "";
      confirmField.placeholder = filename;
      gateSubmit();
      form.action = "/project/" + encodeURIComponent(project) + "/files/" +
        button.getAttribute("data-file-id") + "/delete";
      modal.showModal();
      confirmField.focus();
    });
  });

  document.getElementById("delete-cancel").addEventListener("click", function () {
    modal.close();
  });
})();
