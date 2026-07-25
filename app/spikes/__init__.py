"""Runnable prototypes for open design questions — never production paths.

A spike answers "would this substrate/approach actually work?" with code you can
run and measure, rather than a document that asserts it would. The rules that
keep it from rotting into a second implementation of the app:

- **Nothing imports `app.spikes`.** The runtime, services and web layers never
  reach in; a spike module is reached only by its own tests. That is why no
  import-linter contract names it — it has no importers to whitelist.
- **It never replaces the path it prototypes.** The production handler it stands
  next to stays exactly as it was until the spike's question is decided.
- **It is temporary.** A spike either graduates into the layer it prototypes or
  is deleted with the issue that motivated it. The module docstring names that
  issue so a reader always knows which decision the code is evidence for.
"""
