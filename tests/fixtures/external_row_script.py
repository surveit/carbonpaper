#!/usr/bin/env python
"""Stands in for the program an `external` stage runs: one JSON row in on stdin,
one JSON row out on stdout. The flags let one script cover every failure the
handler must surface.
"""
from __future__ import annotations

import json
import sys
import time


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "double"
    if mode == "hang":
        time.sleep(60)
    row = json.loads(sys.stdin.read())
    if mode == "fail":
        print("the browser never started", file=sys.stderr)
        return 3
    if mode == "garbage":
        sys.stdout.write("not json at all")
        return 0
    if mode == "not_an_object":
        json.dump([row], sys.stdout)
        return 0
    json.dump({"x": row["x"] * 2}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
