// Keep the run form's input-path fields in sync with the chosen version.
//
// A workflow version can author different input stages / paths than another, so
// when the version <select> changes we refetch that version's inputs
// (GET /project/<name>/run-inputs?version_id=) and rebuild the path fields. This
// is what makes the run form "one page": the version you pick and the input
// paths you set always describe the same version.
(function () {
  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
               '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fieldHtml(fileInput) {
    var required = fileInput.path ? "" : " required";
    return (
      '<label style="display:block; margin:0.35rem 0;">' +
      "<code>" + escapeHtml(fileInput.stage_id) + "</code> — data file " +
      '<input type="text" name="binding__' + escapeHtml(fileInput.stage_id) +
      '" value="' + escapeHtml(fileInput.path || "") + '" ' +
      'placeholder="absolute path to the data file" size="70"' + required + ">" +
      "</label>"
    );
  }

  async function refreshInputs(form) {
    var project = form.getAttribute("data-project");
    var select = form.querySelector('select[name="version_id"]');
    var box = form.querySelector(".run-inputs");
    if (!project || !select || !box) return;
    try {
      var resp = await fetch(
        "/project/" + encodeURIComponent(project) +
          "/run-inputs?version_id=" + encodeURIComponent(select.value),
        { cache: "no-store" }
      );
      if (!resp.ok) return; // leave the current fields in place on error
      var inputs = await resp.json();
      box.innerHTML = inputs.map(fieldHtml).join("");
    } catch (e) {
      /* network/parse error: keep whatever fields are shown */
    }
  }

  document.querySelectorAll("form.run-controls").forEach(function (form) {
    var select = form.querySelector('select[name="version_id"]');
    if (select) select.addEventListener("change", function () { refreshInputs(form); });
  });
})();
