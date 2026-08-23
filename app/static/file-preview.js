// Load a selected project file into the run form's one shared preview dialog, and
// give every file row in a picker the button that opens it.
(function () {
  // The picker builds its rows from the select's options and knows nothing about
  // files, so the button a file row carries is registered here, where the dialog
  // it opens lives. Only a stored project file has something to show: the option
  // standing for the workflow-authored path has no id to fetch, and says so.
  function buildPreviewAction(select, option) {
    var wrap = document.createElement("span");
    var button = document.createElement("button");
    var filename = option.dataset.name || option.textContent.trim();
    wrap.className = "picker-row-action";
    button.type = "button";
    button.className = "picker-action file-preview-open";
    button.textContent = "Preview";
    if (option.value) {
      button.dataset.fileId = option.value;
      button.setAttribute("aria-label", "Preview " + filename);
      button.setAttribute("aria-haspopup", "dialog");
      button.setAttribute("aria-controls", "run-file-preview");
      wrap.appendChild(button);
      // The dialog holds rows and nothing else; the page holds the shape, the note,
      // and what has read it. A reader deciding between two exports needs that.
      var open = filePageLink(select, option.value, filename);
      if (open) wrap.appendChild(open);
      return wrap;
    }
    button.setAttribute("aria-disabled", "true");
    button.setAttribute("aria-label", "Preview unavailable for " + filename);
    button.setAttribute("data-tip", "Only project files can be previewed.");
    wrap.appendChild(button);
    return wrap;
  }

  function filePageLink(select, fileId, filename) {
    var form = select.closest("form.run-controls");
    var project = form && form.getAttribute("data-project");
    if (!project) return null;
    var link = document.createElement("a");
    link.className = "picker-action file-page-open";
    link.href = "/project/" + encodeURIComponent(project) + "/files/" +
      encodeURIComponent(fileId);
    link.textContent = "Open";
    link.setAttribute("aria-label", "Open the page for " + filename);
    return link;
  }

  function showMessage(body, message, className, role) {
    var status = document.createElement("p");
    status.className = className;
    status.setAttribute("role", role);
    status.textContent = message;
    body.replaceChildren(status);
  }

  function insertPreview(body, html) {
    var parsed = document.createElement("template");
    parsed.innerHTML = html;
    body.replaceChildren(parsed.content);
  }

  function wirePreviewDialog(form) {
    var dialog = form.querySelector(".file-preview-dialog");
    if (!dialog) return;
    var body = dialog.querySelector(".file-preview-body");
    var title = dialog.querySelector(".file-preview-title");
    var openPage = dialog.querySelector(".file-preview-page-link");
    var activeButton = null;
    var activePicker = null;
    var request = null;

    function closeDialog() {
      if (dialog.open) dialog.close();
    }

    function resetDialog() {
      title.textContent = "File preview";
      if (openPage) openPage.hidden = true;
      showMessage(body, "Loading preview…", "file-preview-loading", "status");
    }

    async function loadPreview(button) {
      var fileId = button.dataset.fileId;
      var project = form.getAttribute("data-project");
      if (!fileId || button.getAttribute("aria-disabled") === "true") return;
      if (!project) return;
      activeButton = button;
      activePicker = button.closest("[data-picker]");
      if (activePicker) activePicker.classList.add("is-action-open");
      resetDialog();
      dialog.showModal();
      var controller = new AbortController();
      request = controller;
      try {
        var response = await fetch(
          "/project/" + encodeURIComponent(project) + "/files/" +
            encodeURIComponent(fileId) + "/preview",
          { cache: "no-store", signal: controller.signal }
        );
        if (!response.ok) throw new Error("HTTP " + response.status);
        insertPreview(body, await response.text());
        var result = body.querySelector("[data-file-preview-filename]");
        if (result) title.textContent = result.dataset.filePreviewFilename;
        if (openPage) {
          openPage.href = "/project/" + encodeURIComponent(project) + "/files/" +
            encodeURIComponent(fileId);
          openPage.hidden = false;
        }
      } catch (error) {
        if (error.name !== "AbortError") {
          showMessage(
            body,
            "The server could not read this file (" + error.message + ").",
            "file-preview-error",
            "alert"
          );
        }
      } finally {
        if (request === controller) request = null;
      }
    }

    form.addEventListener("click", function (event) {
      var button = event.target.closest(".file-preview-open");
      if (!button || !form.contains(button)) return;
      event.preventDefault();
      loadPreview(button);
    });
    dialog.querySelector(".file-preview-close").addEventListener("click", closeDialog);
    dialog.addEventListener("cancel", function (event) {
      event.preventDefault();
      closeDialog();
    });
    dialog.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeDialog();
    });
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) closeDialog();
    });
    dialog.addEventListener("close", function () {
      if (request) request.abort();
      resetDialog();
      if (activePicker) activePicker.classList.remove("is-action-open");
      if (activeButton && activeButton.isConnected) activeButton.focus();
      activeButton = null;
      activePicker = null;
    });
  }

  var api = window.CarbonPicker = window.CarbonPicker || { rowActions: {} };
  api.rowActions["file-preview"] = buildPreviewAction;
  document.querySelectorAll("form.run-controls").forEach(wirePreviewDialog);
})();
