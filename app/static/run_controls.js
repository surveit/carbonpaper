// Keep the run form's fields in sync with the chosen version, and let each path
// field's "Browse…" button pick a file with the browser's own native dialog
// (works on every OS).
//
// Two jobs, one file because they share the same fields:
//
//  1. Version sync — a workflow version can author different input stages / paths
//     than another, so when the version <select> changes we refetch that version's
//     inputs (GET /project/<name>/run-inputs?version_id=) and rebuild the rows.
//     That is what makes the run form "one page": the version you pick and the
//     input paths/caps you set always describe the same version.
//
//  2. File picker — each row picks from the files the project already holds, and its
//     value is a file's sha256. Upload… is the way in for one it does not: the native
//     dialog, then POST /project/<name>/files, then the new file is added to EVERY
//     row's list (a file belongs to the project, not to the step it arrived for) and
//     selected in the row that sent it.
(function () {
  // A rebuilt row is a clone of the form's <template>, which _run_controls.html
  // renders from the same macro as the server-rendered rows — so there is no
  // second copy of the markup here to drift. Only the three attributes that carry
  // the stage's identity, and the path's value, are set.
  function describeBytes(count) {
    var mb = 1024 * 1024;
    if (count >= 1024 * mb) return +(count / (1024 * mb)).toPrecision(3) + "GB";
    if (count >= mb) return +(count / mb).toPrecision(3) + "MB";
    if (count >= 1024) return +(count / 1024).toPrecision(3) + "KB";
    return count + "B";
  }

  function fileOption(file) {
    var option = document.createElement("option");
    option.value = file.sha256;
    option.textContent = file.filename + " — " + describeBytes(file.bytes);
    return option;
  }

  function buildRow(template, row, files) {
    var node = template.content.firstElementChild.cloneNode(true);
    var stageId = row.stage_id;
    var pick = node.querySelector("select.file-pick");
    pick.id = "binding__" + stageId;
    pick.name = pick.id;
    pick.required = !row.authored_path;
    // Blank is "run what the workflow authored", so the first option says which path
    // that is — a row with none cannot run until something here is chosen.
    pick.options[0].textContent = row.authored_path
      ? row.authored_path + " — the path this workflow names"
      : "Choose a file…";
    files.forEach(function (file) { pick.appendChild(fileOption(file)); });
    node.querySelector(".run-input-name").htmlFor = pick.id;
    node.querySelector(".run-input-name code").textContent = stageId;
    node.querySelector('input[type="number"]').name = "limit__" + stageId;
    return node;
  }

  async function refreshInputs(form) {
    var project = form.getAttribute("data-project");
    var select = form.querySelector('select[name="version_id"]');
    var box = form.querySelector(".run-inputs");
    var template = form.querySelector("template.run-input-template");
    if (!project || !select || !box || !template) return;
    try {
      var resp = await fetch(
        "/project/" + encodeURIComponent(project) +
          "/run-inputs?version_id=" + encodeURIComponent(select.value),
        { cache: "no-store" }
      );
      if (!resp.ok) return; // leave the current fields in place on error
      var choices = await resp.json();
      var rows = document.createDocumentFragment();
      choices.inputs.forEach(function (row) {
        rows.appendChild(buildRow(template, row, choices.files));
      });
      box.replaceChildren(rows);
    } catch (e) {
      /* network/parse error: keep whatever fields are shown */
    }
  }

  // ─── Upload a browser-picked file, then select it ───────────────────────
  // The server refuses the same size; this only saves the reader from watching a
  // file upload for minutes before being told it was never going to be accepted.
  function tooLarge(file, form) {
    var ceiling = parseInt(form.getAttribute("data-max-upload-bytes"), 10);
    if (!ceiling || file.size <= ceiling) return false;
    alert('"' + file.name + '" is ' + describeBytes(file.size) + ", over the " +
          describeBytes(ceiling) + " limit for a single input.\n\nThat ceiling is " +
          "what a run on this machine can load into memory. Cut the file down, or " +
          "convert it to parquet.");
    return true;
  }

  // A file belongs to the project, so a new one joins EVERY row's list — then the row
  // that sent it selects it. Adding it only where it was uploaded would hide it from
  // the next step that wants the same file.
  function offerEverywhere(form, file, pick) {
    form.querySelectorAll("select.file-pick").forEach(function (select) {
      if (!select.querySelector('option[value="' + file.sha256 + '"]')) {
        select.appendChild(fileOption(file));
      }
    });
    pick.value = file.sha256;
    pick.dispatchEvent(new Event("change", { bubbles: true }));
  }

  async function uploadFile(file, pick, form, project, btn) {
    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Uploading…";
    try {
      // No stage id: the server stores the file under its own content hash, so the
      // same file picked for two stages is one copy that either may bind.
      var fd = new FormData();
      fd.append("file", file);
      var resp = await fetch(
        "/project/" + encodeURIComponent(project) + "/files",
        { method: "POST", body: fd }
      );
      var data = {};
      try { data = await resp.json(); } catch (e) { /* leave data empty */ }
      if (resp.ok && data.ok && data.sha256) {
        offerEverywhere(form, data, pick);
      } else {
        alert("Upload failed: " + (data.error || ("HTTP " + resp.status)) +
              "\nPlease try again.");
      }
    } catch (e) {
      alert("Upload failed: couldn't reach the server. Please try again.");
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

    // Delegated so buttons/inputs rebuilt by refreshInputs() stay wired.
    form.addEventListener("click", function (e) {
      var btn = e.target.closest(".browse-btn");
      if (!btn || !form.contains(btn)) return;
      e.preventDefault();
      var row = btn.closest(".run-input-row");
      var picker = row && row.querySelector("input.file-input");
      if (picker) picker.click();  // open the browser's native file dialog
    });

    form.addEventListener("change", function (e) {
      var picker = e.target.closest("input.file-input");
      if (!picker || !form.contains(picker) || !picker.files.length) return;
      var row = picker.closest(".run-input-row");
      var pick = row && row.querySelector("select.file-pick");
      var btn = row && row.querySelector(".browse-btn");
      var file = picker.files[0];
      if (pick && btn && !tooLarge(file, form)) uploadFile(file, pick, form, project, btn);
      picker.value = "";  // let the same file be re-picked later
    });
  });
})();
