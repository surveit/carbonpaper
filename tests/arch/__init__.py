"""Architecture tests: executable, red/green statements about code structure.

Kept outside the packages they inspect so a content scan never matches its own
source. Import-graph boundaries live in pyproject [tool.importlinter]; these files
hold the content-level checks that operate on the AST.
"""
