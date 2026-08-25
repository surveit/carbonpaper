// A data table's cell opens that cell's lineage, so the table carries no column
// of links. Delegated from the document, like cell-expand.js, so a table of any
// size costs one listener: the row holds the href and the header holds the name.
//
// CAPTURE phase, because cell-expand.js also answers a click on td.cell-clip.
// Here `expanded` still reads as it did BEFORE this click, which is what lets a
// clipped cell reveal itself on the first click and leave on the second.
document.addEventListener('click', function (event) {
    if (!event.target.closest) return;
    if (event.target.closest('a, button, input, label')) return;
    var cell = event.target.closest('td');
    var row = cell && cell.closest('tr[data-href]');
    if (!row) return;
    if (cell.classList.contains('cell-clip') && !cell.classList.contains('expanded')) return;
    var href = row.dataset.href;
    var head = row.closest('table').tHead;
    var column = head && head.rows[0].cells[cell.cellIndex];
    if (column && column.dataset.column) {
        href += (href.indexOf('?') < 0 ? '?' : '&')
              + 'column=' + encodeURIComponent(column.dataset.column);
    }
    if (event.metaKey || event.ctrlKey) window.open(href, '_blank', 'noopener');
    else location.href = href;
}, true);
