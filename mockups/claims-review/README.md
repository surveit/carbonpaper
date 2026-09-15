# Claims review mock — the sentence view

Static mock of the "AI challenges a claim" surface, settled 2026-09-03. Not wired to the app.
Serve it: `python3 -m http.server 8717` in this directory, then open `sentence.html?claim=doccs-terminated`.

- `data.js` — three claims (DOCCS figure 5, LDA outside spend, COMPAS FPR ratio) and their challenges
- `sentence-data.js` — evidence items, grounding, attackers, severities
- `shell.js`, `sentence.html`, `claims.html`, `index.html`, `plan.html` — the pages
- `static/` — copies of the app's stylesheets so the mock renders in the app's shell
