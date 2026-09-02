#!/usr/bin/env python3
"""Pack skills/<name>/SKILL.md into .claude/skills/<name>.skill archives.

D4: the root reference documents and skills/<name>/SKILL.md are the source of
truth; the .skill archives are build artifacts. A zip cannot be reviewed — it
does not diff in a pull request and it hides its own drift — so it is generated,
never hand-edited.

Archives are built DETERMINISTICALLY (fixed timestamps, fixed compression, sorted
entries) so that identical content produces a byte-identical archive. Without
that, every rebuild rewrites all eight files and git fills with meaningless
binary churn — which is exactly what happened before this script existed.

Usage:
    python scripts/build-skills.py            # build
    python scripts/build-skills.py --check    # exit 1 if any archive is stale
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "skills"
OUT = ROOT / ".claude" / "skills"

# Fixed epoch for every entry: identical content -> identical bytes.
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)

# Which root document each skill carries as references/<name>.
# One authoritative copy lives at the repo root; it is copied in at build time.
REFERENCES: dict[str, str] = {
    "tracemap-clickhouse-analytics": "schema-and-queries.md",
    "tracemap-anomaly-eval": "eval-harness.md",
    "tracemap-demo-services": "starter-code.md",
}


def build_archive(name: str) -> bytes:
    """Return the deterministic archive bytes for one skill."""
    skill_md = SRC / name / "SKILL.md"
    if not skill_md.is_file():
        raise SystemExit(f"missing source: {skill_md}")

    members: list[tuple[str, bytes]] = [
        (f"{name}/SKILL.md", skill_md.read_bytes())
    ]

    ref = REFERENCES.get(name)
    if ref:
        ref_path = ROOT / ref
        if not ref_path.is_file():
            raise SystemExit(f"missing reference document: {ref_path}")
        members.append((f"{name}/references/{ref}", ref_path.read_bytes()))

    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for arcname, data in sorted(members):
            info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify archives are current; exit 1 if any is stale",
    )
    args = ap.parse_args()

    if not SRC.is_dir():
        raise SystemExit(f"no skills source tree at {SRC}")

    OUT.mkdir(parents=True, exist_ok=True)
    names = sorted(p.name for p in SRC.iterdir() if (p / "SKILL.md").is_file())
    if not names:
        raise SystemExit(f"no skills found under {SRC}")

    stale: list[str] = []
    for name in names:
        want = build_archive(name)
        dest = OUT / f"{name}.skill"
        have = dest.read_bytes() if dest.is_file() else b""

        if have == want:
            status = "unchanged"
        elif args.check:
            status = "STALE"
            stale.append(name)
        else:
            dest.write_bytes(want)
            status = "written"

        digest = hashlib.md5(want).hexdigest()[:10]
        print(f"  {status:<10} {name:<34} {len(want):>6} b  {digest}")

    if stale:
        print(
            f"\n{len(stale)} archive(s) stale: {', '.join(stale)}\n"
            "Run `make skills` and commit the result.",
            file=sys.stderr,
        )
        return 1

    print(f"\n{len(names)} skill archive(s) up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
