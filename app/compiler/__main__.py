"""
CLI for the compiler: `python -m app.compiler <input> <out_name> [--out DIR] [--model M]`.

Orchestrates the compile MECHANISM (`app.compiler.compile_methodology`) and the
workflow-writing step of the compilation service (`app.services.compilation`).
"""

from __future__ import annotations

from app.compiler import compile_methodology, read_input
from app.services.compilation import write_methodology


def _cli(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m app.compiler <input file (.jsonl/.md/.txt)> "
              "<out_name> [--out DIR] [--model sonnet]")
        return 2
    input_path = argv[0]
    out_name = argv[1]
    rest = argv[2:]
    # Default scratch location is gitignored (examples/_compiled_*) so CLI output
    # is never accidentally committed.
    out_dir = f"examples/_compiled_{out_name}"
    model = "sonnet"
    i = 0
    while i < len(rest):
        if rest[i] == "--out" and i + 1 < len(rest):
            out_dir = rest[i + 1]
            i += 2
        elif rest[i] == "--model" and i + 1 < len(rest):
            model = rest[i + 1]
            i += 2
        else:
            i += 1

    print(f"[compiler] reading input as prose: {input_path}")
    input_text = read_input(input_path)
    print(f"[compiler]   {len(input_text)} chars")
    print(f"[compiler] calling Claude ({model}) to distill — this can take a minute…")
    result = compile_methodology(input_text, out_name, model=model)

    print(f"\n[compiler] generated {len(result['stages'])} stages:")
    for i, s in enumerate(result["stages"], 1):
        print(f"  {i:02d}. {s.get('id'):<22} {s.get('type')}")

    issues = result["validation"]
    if issues:
        print(f"\n[compiler] validate_workflow_draft: {len(issues)} ISSUE(S):")
        for iss in issues:
            print(f"  - {iss}")
    else:
        print("\n[compiler] validate_workflow_draft: CLEAN ✓ (0 issues)")

    manifest = write_methodology(result, out_dir)
    print(f"\n[compiler] wrote {len(manifest['stage_files'])} stage files to {out_dir}/compiled/")
    print(f"[compiler] methodology_raw → {manifest['methodology_raw']}")
    print(f"[compiler] audit json      → {manifest['audit']}")

    if result["compiler_notes"]:
        print("\n[compiler] compiler_notes (ambiguities):")
        for n in result["compiler_notes"]:
            print(f"  - {n}")

    return 0 if not issues else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
