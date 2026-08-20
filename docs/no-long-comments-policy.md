# No long comments in new code

CI runs `scripts/check_added_comment_length.py` on every push and pull request. It looks
only at lines a diff *adds* — existing code is untouched — and fails the job if any newly
added comment or docstring carries more than 100 characters of prose.

## What counts

- A docstring (module, class, function) is measured by its cleaned text.
- A run of consecutive `#` comment lines is measured as one block (its marker and a
  single leading space are stripped before counting).

## What's exempt

- A tool directive a line depends on to run correctly: `# noqa`, `# type: ignore`,
  `# pragma: no cover`, `# pyright: ignore`.
- A comment or docstring whose entire content is a single link, in one of two forms:
  - `docs/<path>.md` (a reference to a file in this directory)
  - `https://github.com/<org>/<repo>/issues/<n>` (a GitHub issue)

## What to do instead

The default is no comment at all. A better function, variable, or class name usually
replaces what a comment would have explained. If the code genuinely needs a paragraph of
context, write it in a new file under `docs/` and leave only the link behind — or file a
GitHub issue and link that instead. Either way, the comment in the code stays one line.

## Scope

Only `app/` and `tests/` are checked. The rule is diff-scoped by design: it never
requires a one-time sweep of the existing codebase, and it never grows an
exception list — touch a long pre-existing comment and it's judged the same as a new one.
