// Every `data-tip` on the page, through one node on <body>. See static/tooltip.css for
// why it is not drawn inside its trigger.
(function () {
  // Long enough that sweeping the pointer across a toolbar opens nothing, short enough
  // that pointing at something reads as asking. The browser's own wait is about a second.
  var DELAY_MS = 120;
  var GAP = 6;

  var tip = null, timer = null, open = null;

  function tooltipNode() {
    if (tip) return tip;
    tip = document.createElement("div");
    tip.className = "tooltip";
    tip.setAttribute("role", "tooltip");
    tip.id = "app-tooltip";
    document.body.appendChild(tip);
    return tip;
  }

  function place(target) {
    var box = target.getBoundingClientRect();
    var node = tooltipNode();
    node.style.left = "0px";
    node.style.top = "0px";
    var width = node.offsetWidth, height = node.offsetHeight;
    var left = Math.min(box.left, window.innerWidth - width - GAP);
    var top = box.bottom + GAP;
    if (top + height > window.innerHeight) top = box.top - height - GAP;
    node.style.left = Math.max(GAP, left) + "px";
    node.style.top = Math.max(GAP, top) + "px";
  }

  function show(target) {
    var text = target.getAttribute("data-tip");
    if (!text) return;
    var node = tooltipNode();
    node.textContent = text;
    node.classList.add("is-open");
    place(target);
    // Announced to a screen reader as the trigger's description, which the visual
    // tooltip alone would not be.
    target.setAttribute("aria-describedby", node.id);
    open = target;
  }

  function hide() {
    clearTimeout(timer);
    if (open) open.removeAttribute("aria-describedby");
    if (tip) tip.classList.remove("is-open");
    open = null;
  }

  function arm(event) {
    var target = event.target.closest("[data-tip]");
    if (!target || target === open) return;
    clearTimeout(timer);
    timer = setTimeout(function () { show(target); }, DELAY_MS);
  }

  document.addEventListener("mouseover", arm);
  document.addEventListener("mouseout", function (event) {
    var to = event.relatedTarget;
    if (open && !(to && to.closest && to.closest("[data-tip]"))) hide();
  });
  // No delay on focus: a keyboard reader asked for this outright rather than
  // happening to pass over it.
  document.addEventListener("focusin", function (event) {
    var target = event.target.closest("[data-tip]");
    if (target) show(target);
  });
  document.addEventListener("focusout", hide);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") hide();
  });
  // Capture: a scroll inside any container moves the trigger out from under it.
  window.addEventListener("scroll", hide, true);
  window.addEventListener("resize", hide);
})();
