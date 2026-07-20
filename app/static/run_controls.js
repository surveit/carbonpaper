// Keep the run form's input-path fields in sync with the chosen version, and
// let each path field's "Browse…" button open the native macOS file dialog.
//
// Two jobs, one file because they share the same fields:
//
//  1. Version sync — a workflow version can author different input stages / paths
//     than another, so when the version <select> changes we refetch that version's
//     inputs (GET /project/<name>/run-inputs?version_id=) and rebuild the path
//     fields. That is what makes the run form "one page": the version you pick and
//     the input paths you set always describe the same version.
//
//  2. File picker — a run reads its input files off the server's disk by absolute
//     path, and this is a local tool (the server is your own Mac), so Browse…
//     POSTs to /project/<name>/pick-file, which pops the real Finder "Choose File"
//     dialog server-side and returns the picked file's absolute path. We write
//     that path into the field — the file is referenced in place, never copied.
//     (A browser <input type=file> can't do this: it hides the real path.)
(function () {
  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
               '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // Must mirror _run_controls.html's field markup exactly, so a field rebuilt
  // here on version change behaves identically to a server-rendered one.
  function fieldHtml(fileInput) {
    var required = fileInput.path ? "" : " required";
    return (
      '<label class="run-input-row">' +
      "<code>" + escapeHtml(fileInput.stage_id) + "</code> — data file " +
      '<span class="path-field">' +
      '<input type="text" name="binding__' + escapeHtml(fileInput.stage_id) +
      '" value="' + escapeHtml(fileInput.path || "") + '" ' +
      'placeholder="absolute path to the data file" size="70"' + required + ">" +
      '<button type="button" class="btn browse-btn">Browse…</button>' +
      "</span></label>"
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

  // ─── Native file picker ─────────────────────────────────────────────────
  // The POST blocks until the user picks or cancels the Finder dialog, so we
  // disable the button and show "Opening…" while it's up.
  async function pickFile(input, project, btn) {
    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Opening…";
    try {
      var resp = await fetch(
        "/project/" + encodeURIComponent(project) + "/pick-file",
        { method: "POST" }
      );
      var data = {};
      try { data = await resp.json(); } catch (e) { /* leave data empty */ }
      if (resp.ok && data.ok && data.path) {
        input.value = data.path;
        input.dispatchEvent(new Event("change", { bubbles: true }));
      } else if (resp.ok && data.ok && data.cancelled) {
        /* user cancelled the dialog — leave the field as-is */
      } else {
        // Native dialog unavailable (e.g. not macOS) or errored — fall back to
        // manual entry and say why.
        input.focus();
        alert("Couldn't open the file dialog: " +
              (data.error || ("HTTP " + resp.status)) +
              "\nType or paste the absolute path instead.");
      }
    } catch (e) {
      input.focus();
      alert("Couldn't reach the server to open the file dialog.\n" +
            "Type or paste the absolute path instead.");
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  // ─── Wire forms ─────────────────────────────────────────────────────────
  document.querySelectorAll("form.run-controls").forEach(function (form) {
    var project = form.getAttribute("data-project");
    var select = form.querySelector('select[name="version_id"]');
    if (select) select.addEventListener("change", function () { refreshInputs(form); });

    // Delegate the Browse click so buttons added by refreshInputs() are covered.
    form.addEventListener("click", function (e) {
      var btn = e.target.closest(".browse-btn");
      if (!btn || !form.contains(btn)) return;
      e.preventDefault();
      var row = btn.closest(".run-input-row");
      var input = row && row.querySelector('input[name^="binding__"]');
      if (input) pickFile(input, project, btn);
    });
  });
})();
