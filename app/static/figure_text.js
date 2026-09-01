// Mirrors app/web/figure_text.py so a figure reads the same on both sides.
window.Figures = window.Figures || (function () {
  var GROUPS_FROM = 10000;
  return {
    text: function (value) {
      if (typeof value !== "number" || !isFinite(value)) return String(value);
      if (Math.abs(value) < GROUPS_FROM) return String(value);
      return value.toLocaleString("en-US", { maximumFractionDigits: 20 });
    }
  };
})();
