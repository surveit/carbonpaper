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
        try {
            const res = await fetch("/methodology/" + methodology + "/evals/cases-schema", {
                method: "POST", body: params,
            });
            if (!res.ok) return null;
            return await res.json();
        } catch (err) {
            return null;
        }
    }

    function escapeHtml(text) {
        const div = document.createElement("div");
        div.textContent = text;
        return div.innerHTML;
    }

    function renderCasesSchema(body, containerEl, downloadBtn) {
        if (!body) {
            containerEl.innerHTML = "<p class=\"lede\">could not load the required schema.</p>";
            downloadBtn.disabled = true;
            downloadBtn.dataset.columns = "[]";
            return;
        }

        let html = "";
        if (body.problems && body.problems.length) {
            html += "<div class=\"load-issues\"><strong>" + body.problems.length +
                " problem(s):</strong><ul>" +
                body.problems.map(function (p) { return "<li>" + escapeHtml(p) + "</li>"; }).join("") +
                "</ul></div>";
        }
        if (body.warnings && body.warnings.length) {
            html += "<div class=\"cases-warnings\"><strong>" + body.warnings.length +
                " warning(s):</strong><ul>" +
                body.warnings.map(function (w) { return "<li>" + escapeHtml(w) + "</li>"; }).join("") +
                "</ul></div>";
        }
        if (!(body.problems && body.problems.length)) {
            if (body.table_html) {
                html += body.table_html;
            } else {
                html += "<p class=\"lede\">Pick override/target and add expected columns to see the required cases-file schema.</p>";
            }
        }
        containerEl.innerHTML = html;
        downloadBtn.disabled = !(body.ok && body.columns.length);
        downloadBtn.dataset.columns = JSON.stringify(body.columns.map(function (c) { return c.name; }));
    }

    // RFC4180 field quoting: a field containing a comma, double-quote, or
    // newline is wrapped in double quotes with internal quotes doubled;
    // plain names are left bare.
    function csvField(name) {
        if (/[",\n]/.test(name)) {
            return "\"" + name.replace(/"/g, "\"\"") + "\"";
        }
        return name;
    }

    function downloadTemplate(downloadBtn) {
        const names = JSON.parse(downloadBtn.dataset.columns || "[]");
        if (!names.length) return;
        const csv = names.map(csvField).join(",") + "\n";
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
        escapeHtml: escapeHtml,
    };
})(window);
