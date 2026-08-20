"""docs/no-long-comments-policy.md"""
from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROSE_CHAR_CEILING = 100
_GOVERNED_PREFIXES = ("app/", "tests/")

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_TOOL_DIRECTIVE = re.compile(r"^(noqa\b|type:\s*ignore\b|pragma:\s*no cover\b|pyright:\s*ignore\b)")
_DOCS_LINK = re.compile(r"^(?:See )?docs/[\w./-]+\.md\.?$")
_ISSUE_LINK = re.compile(r"^(?:See )?https://github\.com/[\w.-]+/[\w.-]+/issues/\d+\.?$")


@dataclass(frozen=True)
class ProseSpan:
    path: str
    start_line: int
    end_line: int
    text: str


def find_added_lines(diff_text: str) -> dict[str, set[int]]:
    added: dict[str, set[int]] = {}
    current_file: str | None = None
    current_line: int | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            current_file = _target_path(line[4:])
            continue
        hunk = _HUNK_HEADER.match(line)
        if hunk:
            current_line = int(hunk.group(1))
            continue
        if current_file is None or current_line is None:
            continue
        if line.startswith("+"):
            added.setdefault(current_file, set()).add(current_line)
            current_line += 1
        elif not line.startswith(("-", "\\")):
            current_line += 1
    return added


def _target_path(raw: str) -> str | None:
    if raw == "/dev/null":
        return None
    return raw[2:] if raw.startswith("b/") else raw


def find_prose_spans(path: str, content: str) -> list[ProseSpan]:
    return _docstring_spans(path, content) + _comment_spans(path, content)


def is_exempt(text: str) -> bool:
    stripped = text.strip()
    return bool(_TOOL_DIRECTIVE.match(stripped) or _DOCS_LINK.fullmatch(stripped) or _ISSUE_LINK.fullmatch(stripped))


def find_violations_from_diff(diff_text: str, read_file: Callable[[str], str | None]) -> list[str]:
    added_lines_by_file = find_added_lines(diff_text)
    violations: list[str] = []
    for file in sorted(added_lines_by_file):
        if not file.endswith(".py") or not file.startswith(_GOVERNED_PREFIXES):
            continue
        content = read_file(file)
        if content is None:
            continue
        added_lines = added_lines_by_file[file]
        for span in find_prose_spans(file, content):
            if not any(span.start_line <= line <= span.end_line for line in added_lines):
                continue
            if is_exempt(span.text) or len(span.text) <= _PROSE_CHAR_CEILING:
                continue
            violations.append(_describe(span))
    return violations


def find_violations(base: str, head: str, repo_root: Path) -> list[str]:
    diff_text = _run_git_diff(base, head, repo_root)
    return find_violations_from_diff(diff_text, lambda file: _read_file_at_ref(head, file, repo_root))


# --- AST/tokenize span extraction -------------------------------------------


def _docstring_spans(path: str, content: str) -> list[ProseSpan]:
    spans: list[ProseSpan] = []
    for node in ast.walk(ast.parse(content)):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        docstring = ast.get_docstring(node, clean=True)
        if docstring is None:
            continue
        statement = node.body[0]
        spans.append(ProseSpan(path, statement.lineno, statement.end_lineno or statement.lineno, docstring))
    return spans


def _comment_spans(path: str, content: str) -> list[ProseSpan]:
    tokens = [
        (token.start[0], _strip_marker(token.string))
        for token in tokenize.generate_tokens(io.StringIO(content).readline)
        if token.type == tokenize.COMMENT
    ]
    spans: list[ProseSpan] = []
    run: list[tuple[int, str]] = []
    for line, text in [*tokens, (-1, "")]:
        if run and line != run[-1][0] + 1:
            spans.append(ProseSpan(path, run[0][0], run[-1][0], " ".join(t for _, t in run)))
            run = []
        if line != -1:
            run.append((line, text))
    return spans


def _strip_marker(raw: str) -> str:
    body = raw[1:]
    return body[1:] if body.startswith(" ") else body


def _describe(span: ProseSpan) -> str:
    preview = span.text[:80]
    return (
        f"{span.path}:{span.start_line} adds {len(span.text)} chars of comment/docstring prose "
        f"(> {_PROSE_CHAR_CEILING}), not a tool directive or a docs/GitHub-issue link: {preview!r}"
        " — name the thing instead of explaining it, or move the explanation to a docs/*.md "
        "file (or a GitHub issue) and leave only that link here"
    )


# --- git plumbing ------------------------------------------------------------


def _run_git_diff(base: str, head: str, repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "--no-color", "-U0", f"{base}...{head}", "--", "app", "tests"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _read_file_at_ref(ref: str, path: str, repo_root: Path) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"], cwd=repo_root, capture_output=True, text=True
    )
    return None if result.returncode != 0 else result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/master")
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)
    violations = find_violations(args.base, args.head, _REPO_ROOT)
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
