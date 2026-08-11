// td.cell-clip is clipped to one line by CSS, so a long value never widens the
// column; clicking a cell lets it wrap and shows the whole value in place. The
// title attribute the template writes is the hover fallback.
document.addEventListener('click', function (e) {
    var cell = e.target.closest && e.target.closest('td.cell-clip');
    if (!cell) return;
    if (e.target.tagName === 'A') return;
    cell.classList.toggle('expanded');
});
