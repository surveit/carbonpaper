// Enhance any data-picker native select into the app's popover dropdown, without
// taking ownership of its form value: the select stays the form's field, and every
// choice is written back to it as a bubbling "change".
//
// An option describes itself with three optional attributes — data-name (the line
// the reader picks by, falling back to the option's text), data-side (a short
// right-aligned note) and data-meta (a second line under the name).
//
// A row may carry one extra button beside its option — a preview, say. The builder
// for it is registered under a name by whoever owns that action
// (CarbonPicker.rowActions), and a select asks for it with data-picker-row-action.
// That registry is claimed by whichever of the two files loads first, because a
// page's own scripts run while the body is parsed and this one runs at its foot.
(function () {
  var api = window.CarbonPicker = window.CarbonPicker || { rowActions: {} };
  var openPicker = null;

  function addText(parent, className, text) {
    var span = document.createElement("span");
    span.className = className;
    span.textContent = text;
    parent.appendChild(span);
  }

  function describeOption(option) {
    return {
      name: option.dataset.name || option.textContent.trim(),
      side: option.dataset.side || "",
      meta: option.dataset.meta || "",
    };
  }

  function renderValue(picker) {
    var select = picker.querySelector(".picker-native");
    var value = picker.querySelector(".picker-value");
    var option = select.options[select.selectedIndex] || select.options[0];
    var record = describeOption(option);
    var note = [record.meta, record.side].filter(Boolean).join(" · ");
    value.replaceChildren();
    addText(value, "picker-name", record.name);
    if (note) addText(value, "picker-meta", note);
    picker.querySelector(".picker-trigger").removeAttribute("aria-invalid");
  }

  function chooseOption(picker, option) {
    var select = picker.querySelector(".picker-native");
    select.value = option.value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    closePicker(picker, true);
  }

  function buildRowAction(select, option) {
    var build = api.rowActions[select.dataset.pickerRowAction];
    return build ? build(select, option) : null;
  }

  function buildOption(picker, option) {
    var row = document.createElement("div");
    var item = document.createElement("button");
    var select = picker.querySelector(".picker-native");
    var record = describeOption(option);
    var action = buildRowAction(select, option);
    row.className = "picker-row";
    row.dataset.search = [record.name, record.side, record.meta]
      .filter(Boolean).join(" ").toLocaleLowerCase();
    item.type = "button";
    item.className = "picker-option";
    item.setAttribute("aria-current", option.selected ? "true" : "false");
    item.dataset.value = option.value;
    addText(item, "picker-option-name", record.name);
    if (record.side) addText(item, "picker-option-side", record.side);
    if (record.meta) addText(item, "picker-option-meta", record.meta);
    item.addEventListener("click", function () { chooseOption(picker, option); });
    item.addEventListener("keydown", function (event) { handleOptionKey(picker, event); });
    row.appendChild(item);
    if (action) row.appendChild(action);
    return row;
  }

  function refreshPicker(select) {
    var picker = select.closest("[data-picker]");
    if (!picker) return;
    var list = picker.querySelector(".picker-list");
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
    picker.querySelector(".picker-trigger").setAttribute("aria-expanded", "false");
    picker.querySelector(".picker-popover").hidden = true;
    if (openPicker === picker) openPicker = null;
    if (restoreFocus) picker.querySelector(".picker-trigger").focus();
  }

  function openPopover(picker, direction) {
    if (openPicker && openPicker !== picker) closePicker(openPicker, false);
    refreshPicker(picker.querySelector(".picker-native"));
    picker.classList.add("is-open");
    picker.querySelector(".picker-trigger").setAttribute("aria-expanded", "true");
    picker.querySelector(".picker-popover").hidden = false;
    openPicker = picker;
    var search = picker.querySelector(".picker-search");
    if (search) search.value = "";
    filterOptions(picker);
    var options = visibleOptions(picker);
    if (direction < 0) {
      if (options.length) options[options.length - 1].focus();
    } else if (search) {
      search.focus();
    } else if (options.length) {
      (options.find(function (item) {
        return item.getAttribute("aria-current") === "true";
      }) || options[0]).focus();
    }
  }

  function visibleOptions(picker) {
    return Array.from(picker.querySelectorAll(
      ".picker-row:not([hidden]) .picker-option"
    ));
  }

  function filterOptions(picker) {
    var search = picker.querySelector(".picker-search");
    var query = search ? search.value.trim().toLocaleLowerCase() : "";
    var matches = 0;
    picker.querySelectorAll(".picker-row").forEach(function (row) {
      row.hidden = !!query && !row.dataset.search.includes(query);
      if (!row.hidden) matches += 1;
    });
    var empty = picker.querySelector(".picker-empty");
    if (empty) empty.hidden = matches !== 0;
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
      var select = picker.querySelector(".picker-native");
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
    var select = picker.querySelector(".picker-native");
    var trigger = picker.querySelector(".picker-trigger");
    var list = picker.querySelector(".picker-list");
    var search = picker.querySelector(".picker-search");
    if (!select || !trigger || !list) return;
    trigger.id = select.id + "__button";
    trigger.setAttribute("aria-controls", select.id + "__picker");
    picker.querySelector(".picker-popover").id = select.id + "__picker";
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
      else openPopover(picker, 1);
    });
    trigger.addEventListener("keydown", function (event) {
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
        event.preventDefault();
        openPopover(picker, event.key === "ArrowUp" ? -1 : 1);
      }
    });
    if (search) wireSearch(picker, search);
    picker.addEventListener("focusout", function (event) {
      if (!picker.classList.contains("is-action-open") &&
          !picker.contains(event.relatedTarget)) closePicker(picker, false);
    });
  }

  function wireSearch(picker, search) {
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
  }

  function initPickers(root) {
    var scope = root || document;
    if (scope.matches && scope.matches("[data-picker]")) initPicker(scope);
    scope.querySelectorAll("[data-picker]").forEach(initPicker);
  }

  document.addEventListener("click", function (event) {
    if (openPicker && !openPicker.classList.contains("is-action-open") &&
        !openPicker.contains(event.target)) closePicker(openPicker, false);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && openPicker &&
        !openPicker.classList.contains("is-action-open")) closePicker(openPicker, true);
  });
  document.addEventListener("DOMContentLoaded", function () { initPickers(document); });

  api.init = initPickers;
  api.open = function (picker) { openPopover(picker, 1); };
  api.refresh = refreshPicker;
})();
