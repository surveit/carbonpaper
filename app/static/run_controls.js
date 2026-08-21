// Keep the run form's fields in sync with the chosen version, and upload files through
// one dialog shared by every input row.
(function () {
  function describeBytes(count) {
    var mb = 1024 * 1024;
    if (count >= 1024 * mb) return +(count / (1024 * mb)).toPrecision(3) + "GB";
    if (count >= mb) return +(count / mb).toPrecision(3) + "MB";
    if (count >= 1024) return +(count / 1024).toPrecision(3) + "KB";
    return count + "B";
  }

  function fileOption(file) {
    var option = document.createElement("option");
    option.value = file.file_id;
    option.dataset.uploadedAt = file.uploaded_at;
    option.dataset.name = file.filename;
    option.dataset.meta = file.uploaded_label;
    option.dataset.side = file.size_label;
    option.textContent = file.label;
    return option;
  }

  function insertFileOption(select, file) {
    var current = select.querySelector('option[value="' + file.file_id + '"]');
    var wasChosen = !!current && current.selected;
    if (current) current.remove();
    var uploadedAt = Date.parse(file.uploaded_at);
    var before = Array.from(select.querySelectorAll("option[data-uploaded-at]")).find(
      function (option) { return Date.parse(option.dataset.uploadedAt) < uploadedAt; }
    );
    var option = fileOption(file);
    // Re-sending bytes the project already holds must not quietly unbind the row
    // that was already reading them.
    option.selected = wasChosen;
    select.insertBefore(option, before || null);
    if (window.CarbonPicker) window.CarbonPicker.refresh(select);
  }

  function buildRow(template, row, files) {
    var node = template.content.firstElementChild.cloneNode(true);
    var stageId = row.stage_id;
    var pick = node.querySelector("select.file-pick");
    pick.id = "binding__" + stageId;
    pick.name = pick.id;
    pick.required = !row.authored_path;
    // The field takes several files, so it holds no blank option: nothing selected is
    // the blank, and the placeholder the trigger shows meanwhile lives on the select.
    pick.replaceChildren();
    pick.dataset.emptyName = row.authored_path || "Choose a file…";
    if (row.authored_path) pick.dataset.emptyMeta = "Workflow path";
    else delete pick.dataset.emptyMeta;
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
      if (!resp.ok) return;
      var choices = await resp.json();
      var rows = document.createDocumentFragment();
      choices.inputs.forEach(function (row) {
        rows.appendChild(buildRow(template, row, choices.files));
      });
      box.replaceChildren(rows);
      // A carried cap names a stage of the version it was copied from; on another
      // version prepare_run refuses it, and no field here could take it back off.
      form.querySelectorAll("input.js-carried-limit").forEach(function (input) {
        input.remove();
      });
      if (window.CarbonPicker) window.CarbonPicker.init(box);
    } catch (e) {
      /* Keep the shown fields after a network or parse failure. */
    }
  }

  function explainOversize(file, form) {
    var ceiling = parseInt(form.getAttribute("data-max-upload-bytes"), 10);
    if (!ceiling || file.size <= ceiling) return "";
    return '"' + file.name + '" is ' + describeBytes(file.size) + ", over the " +
      describeBytes(ceiling) + " limit for a single input. That ceiling is what a " +
      "run on this machine can load into memory. Cut the file down, or convert it " +
      "to parquet.";
  }

  function offerEverywhere(form, file, pick) {
    form.querySelectorAll("select.file-pick").forEach(function (select) {
      insertFileOption(select, file);
    });
    // Added to what the row already holds, not put in its place: uploading the third
    // month of an export is meant to leave the first two bound.
    var option = pick.querySelector('option[value="' + file.file_id + '"]');
    if (pick.multiple && option) option.selected = true;
    else pick.value = file.file_id;
    pick.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function setDialogError(dialog, message) {
    var error = dialog.querySelector(".run-upload-error");
    error.textContent = message;
    error.hidden = !message;
  }

  function wireUploadDialog(form, project) {
    var dialog = form.querySelector(".run-upload-dialog");
    if (!dialog) return;
    var fileInput = dialog.querySelector(".run-upload-input");
    var drop = dialog.querySelector(".run-upload-drop");
    var submit = dialog.querySelector(".run-upload-submit");
    var selection = dialog.querySelector(".run-upload-selection");
    var selectedFile = null;
    var targetPick = null;
    var uploadController = null;

    function resetDialog() {
      selectedFile = null;
      targetPick = null;
      fileInput.value = "";
      selection.hidden = true;
      setDialogError(dialog, "");
      submit.disabled = true;
      submit.textContent = "Upload file";
      drop.classList.remove("is-dragging");
      drop.removeAttribute("aria-disabled");
    }

    function closeDialog() {
      if (dialog.open) dialog.close();
    }

    function chooseFile(file) {
      if (!file || uploadController) return;
      selectedFile = file;
      selection.querySelector(".run-upload-filename").textContent = file.name;
      selection.querySelector(".run-upload-size").textContent = describeBytes(file.size);
      selection.hidden = false;
      var sizeError = explainOversize(file, form);
      setDialogError(dialog, sizeError);
      submit.disabled = !!sizeError;
    }

    function openDialog(row) {
      var pick = row.querySelector("select.file-pick");
      if (!pick) return;
      resetDialog();
      targetPick = pick;
      dialog.querySelector(".run-upload-stage").textContent =
        row.querySelector(".run-input-name code").textContent;
      dialog.showModal();
    }

    async function uploadFile() {
      if (!selectedFile || !targetPick || uploadController) return;
      var file = selectedFile;
      var pick = targetPick;
      uploadController = new AbortController();
      submit.disabled = true;
      submit.textContent = "Uploading…";
      drop.setAttribute("aria-disabled", "true");
      setDialogError(dialog, "");
      try {
        var fd = new FormData();
        fd.append("file", file);
        var resp = await fetch(
          "/project/" + encodeURIComponent(project) + "/files",
          { method: "POST", body: fd, signal: uploadController.signal }
        );
        var data = {};
        try { data = await resp.json(); } catch (e) { /* Leave data empty. */ }
        if (resp.ok && data.ok && data.file_id) {
          offerEverywhere(form, data, pick);
          uploadController = null;
          closeDialog();
        } else {
          setDialogError(dialog, "Upload failed: " +
            (data.error || ("HTTP " + resp.status)) + ". Please try again.");
        }
      } catch (e) {
        if (e.name !== "AbortError") {
          setDialogError(dialog,
            "Upload failed: couldn't reach the server. Please try again.");
        }
      } finally {
        uploadController = null;
        if (dialog.open) {
          submit.disabled = !selectedFile;
          submit.textContent = "Upload file";
          drop.removeAttribute("aria-disabled");
        }
      }
    }

    form.addEventListener("click", function (event) {
      var button = event.target.closest(".browse-btn");
      if (!button || !form.contains(button)) return;
      event.preventDefault();
      var row = button.closest(".run-input-row");
      if (row) openDialog(row);
    });

    drop.addEventListener("click", function () {
      if (!uploadController) fileInput.click();
    });
    fileInput.addEventListener("change", function () {
      chooseFile(fileInput.files[0]);
      fileInput.value = "";
    });
    ["dragenter", "dragover"].forEach(function (name) {
      drop.addEventListener(name, function (event) {
        event.preventDefault();
        if (!uploadController) drop.classList.add("is-dragging");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      drop.addEventListener(name, function () { drop.classList.remove("is-dragging"); });
    });
    drop.addEventListener("drop", function (event) {
      event.preventDefault();
      chooseFile(event.dataTransfer.files[0]);
    });
    submit.addEventListener("click", uploadFile);
    dialog.querySelector(".run-upload-cancel").addEventListener("click", closeDialog);
    dialog.querySelector(".run-upload-close").addEventListener("click", closeDialog);
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) closeDialog();
    });
    dialog.addEventListener("close", function () {
      if (uploadController) uploadController.abort();
      resetDialog();
    });
  }

  document.querySelectorAll("form.run-controls").forEach(function (form) {
    var project = form.getAttribute("data-project");
    var select = form.querySelector('select[name="version_id"]');
    if (select) select.addEventListener("change", function () { refreshInputs(form); });
    wireUploadDialog(form, project);
  });
})();
