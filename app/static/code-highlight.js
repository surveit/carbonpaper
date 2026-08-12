/* Colours the authored-code blocks (_stage_code.html) with highlight.js.
 *
 * Watches for insertions rather than painting once: the workflow and run pages fetch
 * their stage panel and swap it in with innerHTML long after load, so a one-shot pass
 * would colour every page EXCEPT the two that show code most.
 *
 * Only the inner <code> is touched, so pre.code keeps the palette's ground and a block
 * still reads as code when this never runs. Starlark takes the python grammar: it is a
 * Python dialect, and hljs ships no starlark. */
const UNPAINTED = 'pre.code > code:not(.hljs)';

document.addEventListener('DOMContentLoaded', function () {
    paintWithin(document);
});

new MutationObserver(function (records) {
    records.forEach(function (record) {
        record.addedNodes.forEach(paintAdded);
    });
}).observe(document.documentElement, { childList: true, subtree: true });

function paintAdded(node) {
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.matches(UNPAINTED)) hljs.highlightElement(node);
    paintWithin(node);
}

function paintWithin(root) {
    root.querySelectorAll(UNPAINTED).forEach(function (block) {
        hljs.highlightElement(block);
    });
}
