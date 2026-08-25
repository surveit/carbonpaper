// td.cell-clip is clipped to one line by CSS, so a long value never widens the
// column; clicking a cell lets it wrap and shows the whole value in place. The
// title attribute the template writes is the hover fallback.
document.addEventListener('click', function (e) {
    var cell = e.target.closest && e.target.closest('td.cell-clip');
    if (!cell) return;
    if (e.target.tagName === 'A') return;
    // In a table whose rows open their own lineage the click is already spoken
    // for; the title attribute shows the whole value on hover instead.
    if (cell.closest('tr[data-href]')) return;
    cell.classList.toggle('expanded');
});
