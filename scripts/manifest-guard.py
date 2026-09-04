#!/usr/bin/env python3
"""M10 and M18, as a script rather than as a grep inside the workflow.

Both checks look for a forbidden string, and a grep that looks for a forbidden
string is itself a file containing that string. Inlined in `ci.yml` the M10
check matched the workflow that defined it and failed on a clean tree -- the
same self-matching failure F11 recorded against the Phase 3 criterion grep, so
this is the second time the shape has bitten.

A script can exclude itself, which a grep pattern written inline cannot. It can
also say *why* a hit matters, and be tested.

    python scripts/manifest-guard.py

  M10  no Jaeger v1 manifests. v1 has been EOL since 31 Dec 2025 and quoting a
       v1 config is disqualifying. v2 is one binary with YAML roles.
  M18  no EOL base images. Node 20 went EOL in April 2026.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()

# Documentation is allowed to discuss what not to do -- that is what makes it
# documentation. Only files that a machine would ACT on are checked.
DOC_SUFFIXES = {".md", ".markdown", ".html", ".txt", ".rst"}

# The guard's own definitions, and the plan that explains them, necessarily
# contain the forbidden strings.
EXCLUDED = {
    "scripts/manifest-guard.py",
    ".github/workflows/ci.yml",
}

M10_PATTERNS = [
    ("jaegertracing/all-in-one",
     "the v1 all-in-one image; v2 is jaegertracing/jaeger with a YAML config"),
    ("apiVersion: jaegertracing.io/v1",
     "a v1 operator CRD; the v2 operator uses a different group/version"),
]

M18_PATTERNS = [
    ("node:20", "Node 20 reached end of life in April 2026; use node:24"),
    ("node:18", "Node 18 is long EOL; use node:24"),
    ("python:3.9", "Python 3.9 is EOL; this project targets 3.14"),
]


def tracked_files() -> list[Path]:
    """Only what git tracks.

    Scanning the working tree walks node_modules and .venv, which is slow
    enough that the guard gets skipped, and would flag a vendored dependency
    that nobody in this repository chose.
    """
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    return [ROOT / p for p in out.stdout.split("\0") if p]


def scan(patterns: list[tuple[str, str]], label: str) -> list[str]:
    hits: list[str] = []
    for path in tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in EXCLUDED or path.resolve() == SELF:
            continue
        if path.suffix.lower() in DOC_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for i, raw in enumerate(text.splitlines(), 1):
            # Comments are skipped, and this is not a loophole -- it is the
            # difference between a manifest and a note about a manifest. The
            # first line of every Node Dockerfile here reads
            #   # Node.js 24 LTS. NOT node:20 - end of life April 2026 (M18).
            # which exists precisely BECAUSE the rule exists. A guard that
            # fails on the comment explaining it teaches people to delete the
            # comment, which is the opposite of what it is for.
            if raw.lstrip().startswith("#"):
                continue
            for needle, why in patterns:
                if needle in raw:
                    hits.append(f"{rel}:{i}  {needle!r} -- {why}")
    if hits:
        print(f"  {label:<52} FAIL")
        for h in hits:
            print(f"      {h}")
    else:
        print(f"  {label:<52} OK")
    return hits


def main() -> int:
    print()
    print("  manifest guard")
    print()
    failures = scan(M10_PATTERNS, "M10 no Jaeger v1 manifests")
    failures += scan(M18_PATTERNS, "M18 no EOL base images")
    print()
    print("  MANIFEST GUARD PASS" if not failures else "  MANIFEST GUARD FAIL")
    print()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
