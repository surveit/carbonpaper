// Enhance any data-file-picker native select without taking ownership of its form value.
(function () {
  var openPicker = null;

  function addText(parent, className, text) {
    var span = document.createElement("span");
    span.className = className;
    span.textContent = text;
    parent.appendChild(span);
  }

  function describeOption(option) {
    var uploaded = option.dataset.uploadedLabel || "";
    var size = option.dataset.sizeLabel || "";
    return {
      filename: option.dataset.filename || option.textContent,
      meta: [uploaded, size].filter(Boolean).join(" · ") || option.dataset.detail || "",
      uploaded: uploaded,
      size: size,
    };
  }

  function renderValue(picker) {
    var select = picker.querySelector(".file-picker-native");
    var value = picker.querySelector(".file-picker-value");
    var option = select.options[select.selectedIndex] || select.options[0];
    var record = describeOption(option);
    value.replaceChildren();
    addText(value, "file-picker-name", record.filename);
    if (record.meta) addText(value, "file-picker-meta", record.meta);
    picker.querySelector(".file-picker-trigger").removeAttribute("aria-invalid");
  }

  function chooseOption(picker, option) {
    var select = picker.querySelector(".file-picker-native");
    select.value = option.value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    closePicker(picker, true);
  }

  function buildPreviewAction(select, option) {
    var wrap = document.createElement("span");
    var button = document.createElement("button");
    var filename = option.dataset.filename || option.textContent;
    wrap.className = "file-picker-preview-wrap";
    button.type = "button";
    button.className = "file-picker-preview file-preview-open";
    button.textContent = "Preview";
    if (option.value) {
      button.dataset.fileSha = option.value;
      button.setAttribute("aria-label", "Preview " + filename);
      button.setAttribute("aria-haspopup", "dialog");
      button.setAttribute("aria-controls", "run-file-preview");
      wrap.appendChild(button);
      return wrap;
    }
    var tooltip = document.createElement("span");
    var tooltipId = select.id + "__preview_help";
    button.setAttribute("aria-disabled", "true");
    button.setAttribute("aria-label", "Preview unavailable for " + filename);
    button.setAttribute("aria-describedby", tooltipId);
    tooltip.id = tooltipId;
    tooltip.className = "file-picker-tooltip";
    tooltip.setAttribute("role", "tooltip");
    tooltip.textContent = "Only project files can be previewed.";
    wrap.appendChild(button);
    wrap.appendChild(tooltip);
    return wrap;
  }

  function buildOption(picker, option) {
    var row = document.createElement("div");
    var item = document.createElement("button");
    var select = picker.querySelector(".file-picker-native");
    var record = describeOption(option);
    row.className = "file-picker-row";
    row.dataset.search = [record.filename, record.uploaded, record.size, record.meta]
      .filter(Boolean).join(" ").toLocaleLowerCase();
    item.type = "button";
    item.className = "file-picker-option";
    item.setAttribute("aria-current", option.selected ? "true" : "false");
    item.dataset.value = option.value;
    addText(item, "file-picker-option-name", record.filename);
    if (record.size) addText(item, "file-picker-option-size", record.size);
    if (record.uploaded || record.meta) {
      addText(item, "file-picker-option-time", record.uploaded || record.meta);
    }
    item.addEventListener("click", function () { chooseOption(picker, option); });
    item.addEventListener("keydown", function (event) { handleOptionKey(picker, event); });
    row.appendChild(item);
    row.appendChild(buildPreviewAction(select, option));
    return row;
  }

  function refreshPicker(select) {
    var picker = select.closest("[data-file-picker]");
    if (!picker) return;
    var list = picker.querySelector(".file-picker-list");
    var items = document.createDocumentFragment();
    Array.from(select.options).forEach(function (option) {
      items.appendChild(buildOption(picker, option));
    });
    list.replaceChildren(items);
    renderValue(picker);
  }

  function closePicker(picker, restoreFocus) {
    if (!picker || !picker.classList.contains("is-open")) return;
    picker.classList.remove("is-open");
    picker.querySelector(".file-picker-trigger").setAttribute("aria-expanded", "false");
    picker.querySelector(".file-picker-popover").hidden = true;
    if (openPicker === picker) openPicker = null;
    if (restoreFocus) picker.querySelector(".file-picker-trigger").focus();
  }

  function openFilePicker(picker, direction) {
    if (openPicker && openPicker !== picker) closePicker(openPicker, false);
    refreshPicker(picker.querySelector(".file-picker-native"));
    picker.classList.add("is-open");
    picker.querySelector(".file-picker-trigger").setAttribute("aria-expanded", "true");
    picker.querySelector(".file-picker-popover").hidden = false;
    openPicker = picker;
    var search = picker.querySelector(".file-picker-search");
    search.value = "";
    filterOptions(picker);
    if (direction < 0) {
      var options = visibleOptions(picker);
      if (options.length) options[options.length - 1].focus();
    } else {
      search.focus();
    }
  }

  function visibleOptions(picker) {
    return Array.from(picker.querySelectorAll(
      ".file-picker-row:not([hidden]) .file-picker-option"
    ));
  }

  function filterOptions(picker) {
    var query = picker.querySelector(".file-picker-search").value.trim().toLocaleLowerCase();
    var matches = 0;
    picker.querySelectorAll(".file-picker-row").forEach(function (row) {
      row.hidden = !!query && !row.dataset.search.includes(query);
      if (!row.hidden) matches += 1;
    });
    picker.querySelector(".file-picker-empty").hidden = matches !== 0;
  }

  function moveOption(picker, current, offset) {
    var options = visibleOptions(picker);
    var index = options.indexOf(current);
    var target = options[Math.max(0, Math.min(options.length - 1, index + offset))];
    if (target) target.focus();
  }

  function handleOptionKey(picker, event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      moveOption(picker, event.currentTarget, event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      var options = visibleOptions(picker);
      var target = event.key === "Home" ? options[0] : options[options.length - 1];
      if (target) target.focus();
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      var select = picker.querySelector(".file-picker-native");
      var option = Array.from(select.options).find(function (item) {
        return item.value === event.currentTarget.dataset.value;
      });
      if (option) chooseOption(picker, option);
    } else if (event.key === "Escape") {
      event.preventDefault();
      closePicker(picker, true);
    }
  }

  function initPicker(picker) {
    if (picker.classList.contains("is-ready")) return;
    var select = picker.querySelector(".file-picker-native");
    var trigger = picker.querySelector(".file-picker-trigger");
    var list = picker.querySelector(".file-picker-list");
    var search = picker.querySelector(".file-picker-search");
    if (!select || !trigger || !list || !search) return;
    trigger.id = select.id + "__button";
    trigger.setAttribute("aria-controls", select.id + "__picker");
    picker.querySelector(".file-picker-popover").id = select.id + "__picker";
    document.querySelectorAll("label").forEach(function (label) {
      if (label.htmlFor === select.id) label.htmlFor = trigger.id;
    });
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    trigger.hidden = false;
    picker.classList.add("is-ready");
    refreshPicker(select);
    select.addEventListener("change", function () { refreshPicker(select); });
    select.addEventListener("invalid", function (event) {
      event.preventDefault();
      trigger.setAttribute("aria-invalid", "true");
      trigger.focus();
    });
    trigger.addEventListener("click", function () {
      if (picker.classList.contains("is-open")) closePicker(picker, false);
      else openFilePicker(picker, 1);
    });
    trigger.addEventListener("keydown", function (event) {
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
        event.preventDefault();
        openFilePicker(picker, event.key === "ArrowUp" ? -1 : 1);
      }
    });
    search.addEventListener("input", function () { filterOptions(picker); });
    search.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        var options = visibleOptions(picker);
        var target = event.key === "ArrowDown" ? options[0] : options[options.length - 1];
        if (target) target.focus();
      } else if (event.key === "Enter") {
        event.preventDefault();
        var first = visibleOptions(picker)[0];
        if (first) {
          first.click();
        }
      } else if (event.key === "Escape") {
        event.preventDefault();
        closePicker(picker, true);
      }
    });
    picker.addEventListener("focusout", function (event) {
      if (!picker.classList.contains("is-previewing") &&
          !picker.contains(event.relatedTarget)) closePicker(picker, false);
    });
  }

  function initFilePickers(root) {
    var scope = root || document;
    if (scope.matches && scope.matches("[data-file-picker]")) initPicker(scope);
    scope.querySelectorAll("[data-file-picker]").forEach(initPicker);
  }

  document.addEventListener("click", function (event) {
    if (openPicker && !openPicker.classList.contains("is-previewing") &&
        !openPicker.contains(event.target)) closePicker(openPicker, false);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && openPicker &&
        !openPicker.classList.contains("is-previewing")) closePicker(openPicker, true);
  });
  document.addEventListener("DOMContentLoaded", function () { initFilePickers(document); });

  window.CarbonFilePicker = {
    init: initFilePickers,
    open: function (picker) { openFilePicker(picker, 1); },
    refresh: refreshPicker,
  };
})();
