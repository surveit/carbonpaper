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
    picker.querySelector(".file-preview-open").disabled = !option.value;
  }

  function chooseOption(picker, option) {
    var select = picker.querySelector(".file-picker-native");
    select.value = option.value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    closePicker(picker, true);
  }

  function buildOption(picker, option) {
    var item = document.createElement("button");
    var record = describeOption(option);
    item.type = "button";
    item.className = "file-picker-option";
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", String(option.selected));
    item.dataset.value = option.value;
    item.dataset.kind = option.dataset.fileKind || "upload";
    item.dataset.search = [record.filename, record.uploaded, record.size, record.meta]
      .filter(Boolean).join(" ").toLocaleLowerCase();
    item.tabIndex = -1;
    addText(item, "file-picker-option-name", record.filename);
    if (record.size) addText(item, "file-picker-option-size", record.size);
    if (record.uploaded || record.meta) {
      addText(item, "file-picker-option-time", record.uploaded || record.meta);
    }
    item.addEventListener("click", function () { chooseOption(picker, option); });
    item.addEventListener("keydown", function (event) { handleOptionKey(picker, event); });
    return item;
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
    return Array.from(picker.querySelectorAll(".file-picker-option:not([hidden])"));
  }

  function filterOptions(picker) {
    var query = picker.querySelector(".file-picker-search").value.trim().toLocaleLowerCase();
    var matches = 0;
    picker.querySelectorAll(".file-picker-option").forEach(function (option) {
      option.hidden = !!query && !option.dataset.search.includes(query);
      if (!option.hidden) matches += 1;
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
    } else if (event.key === "Tab") {
      closePicker(picker, false);
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
    trigger.setAttribute("aria-controls", select.id + "__listbox");
    list.id = select.id + "__listbox";
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
      } else if (event.key === "Tab") {
        closePicker(picker, false);
      }
    });
  }

  function initFilePickers(root) {
    var scope = root || document;
    if (scope.matches && scope.matches("[data-file-picker]")) initPicker(scope);
    scope.querySelectorAll("[data-file-picker]").forEach(initPicker);
  }

  document.addEventListener("click", function (event) {
    if (openPicker && !openPicker.contains(event.target)) closePicker(openPicker, false);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && openPicker) closePicker(openPicker, true);
  });
  document.addEventListener("DOMContentLoaded", function () { initFilePickers(document); });

  window.CarbonFilePicker = { init: initFilePickers, refresh: refreshPicker };
})();
