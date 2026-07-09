// Shared helpers for the "cases file schema + template download" feature.
// Used by the eval authoring form (which refetches as override/target/expected
// picks change) and a saved dataless eval's config page (which fetches once on
// load using the saved config's fixed override/target/expected values). Both
// callers build their own POST payload and own container/button elements;
// this module only knows how to call the cases-schema endpoint, render its
// response, and build the downloadable CSV template.
(function (global) {
    "use strict";

    async function fetchCasesSchema(methodology, params) {
        const res = await fetch("/methodology/" + methodology + "/evals/cases-schema", {
            method: "POST", body: params,
        });
        return res.json();
    }

    function escapeHtml(text) {
        const div = document.createElement("div");
        div.textContent = text;
        return div.innerHTML;
    }

    function renderCasesSchema(body, containerEl, downloadBtn) {
        if (body.problems && body.problems.length) {
            containerEl.innerHTML = "<div class=\"load-issues\"><strong>" + body.problems.length +
                " problem(s):</strong><ul>" +
                body.problems.map(function (p) { return "<li>" + escapeHtml(p) + "</li>"; }).join("") +
                "</ul></div>";
        } else if (!body.columns.length) {
            containerEl.innerHTML = "<p class=\"lede\">Pick override/target and add expected columns to see the required cases-file schema.</p>";
        } else {
            const injected = body.columns.filter(function (c) { return c.role === "injected"; });
            const expected = body.columns.filter(function (c) { return c.role === "expected"; });
            let html = "";
            if (injected.length) {
                html += "<p class=\"lede\"><strong>injected inputs:</strong> " +
                    injected.map(function (c) { return escapeHtml(c.name) + " (" + escapeHtml(c.type) + ")"; }).join(", ") +
                    "</p>";
            }
            if (expected.length) {
                html += "<p class=\"lede\"><strong>expected answers:</strong> " +
                    expected.map(function (c) { return escapeHtml(c.name) + " (" + escapeHtml(c.type) + ")"; }).join(", ") +
                    "</p>";
            }
            containerEl.innerHTML = html;
        }
        downloadBtn.disabled = !(body.ok && body.columns.length);
        downloadBtn.dataset.columns = JSON.stringify(body.columns.map(function (c) { return c.name; }));
    }

    function downloadTemplate(downloadBtn) {
        const names = JSON.parse(downloadBtn.dataset.columns || "[]");
        if (!names.length) return;
        const csv = names.join(",") + "\n";
        const blob = new Blob([csv], {type: "text/csv"});
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "cases-template.csv";
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
    }

    function wireDownloadButton(downloadBtn) {
        downloadBtn.addEventListener("click", function () { downloadTemplate(downloadBtn); });
    }

    function debounce(fn, waitMs) {
        let timer = null;
        return function () {
            const args = arguments;
            if (timer) clearTimeout(timer);
            timer = setTimeout(function () { fn.apply(null, args); }, waitMs);
        };
    }

    global.CasesSchema = {
        fetch: fetchCasesSchema,
        render: renderCasesSchema,
        wireDownloadButton: wireDownloadButton,
        debounce: debounce,
    };
})(window);
