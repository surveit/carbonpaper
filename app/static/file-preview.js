// Load a selected project file into the run form's one shared preview dialog.
(function () {
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
    var activeButton = null;
    var request = null;

    function closeDialog() {
      if (dialog.open) dialog.close();
    }

    function resetDialog() {
      title.textContent = "File preview";
      showMessage(body, "Loading preview…", "file-preview-loading", "status");
    }

    async function loadPreview(button) {
      var picker = button.closest("[data-file-picker]");
      var select = picker && picker.querySelector(".file-picker-native");
      var sha256 = select && select.value;
      var project = form.getAttribute("data-project");
      if (!sha256 || !project) return;
      activeButton = button;
      resetDialog();
      dialog.showModal();
      var controller = new AbortController();
      request = controller;
      try {
        var response = await fetch(
          "/project/" + encodeURIComponent(project) + "/files/" +
            encodeURIComponent(sha256) + "/preview",
          { cache: "no-store", signal: controller.signal }
        );
        if (!response.ok) throw new Error("HTTP " + response.status);
        insertPreview(body, await response.text());
        var result = body.querySelector("[data-file-preview-filename]");
        if (result) title.textContent = result.dataset.filePreviewFilename;
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
      if (activeButton && activeButton.isConnected) activeButton.focus();
      activeButton = null;
    });
  }

  document.querySelectorAll("form.run-controls").forEach(wirePreviewDialog);
})();
