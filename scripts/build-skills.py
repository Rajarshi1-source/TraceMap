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


def archive_members(data: bytes) -> dict[str, bytes]:
    """The {arcname: bytes} an archive carries.

    Staleness is a question about CONTENT, not about compressed bytes. DEFLATE
    output at a given level is not guaranteed identical across zlib builds, so
    comparing the raw archive made `--check` a test of the runner's zlib: the
    same commit with byte-identical inputs reported all eight archives stale in
    CI and current on the machine that built them.

    Comparing members catches the thing the guard is actually for -- a SKILL.md
    edited without rebuilding -- and is indifferent to how the bytes were
    squeezed.
    """
    import io

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n) for n in sorted(z.namelist())}


def _normalised(path: Path) -> bytes:
    """File bytes with line endings normalised to LF.

    Without this the archive is not reproducible across platforms, and the
    failure is baffling rather than obvious. Git checks these files out with
    CRLF on Windows and LF on a Linux runner, so the same commit produced two
    different archives: `--check` passed locally and reported every skill STALE
    in CI, on a tree nobody had edited.

    The fixed timestamp and pinned compression level above already exist to
    make the archive byte-identical everywhere. They are pointless if the
    INPUT differs, which it silently did.
    """
    return path.read_bytes().replace(b"\r\n", b"\n")


def build_archive(name: str) -> bytes:
    """Return the deterministic archive bytes for one skill."""
    skill_md = SRC / name / "SKILL.md"
    if not skill_md.is_file():
        raise SystemExit(f"missing source: {skill_md}")

    members: list[tuple[str, bytes]] = [
        (f"{name}/SKILL.md", _normalised(skill_md))
    ]

    ref = REFERENCES.get(name)
    if ref:
        ref_path = ROOT / ref
        if not ref_path.is_file():
            raise SystemExit(f"missing reference document: {ref_path}")
        members.append((f"{name}/references/{ref}", _normalised(ref_path)))

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

        # Compared by CONTENT, not by compressed bytes -- see archive_members().
        try:
            current = bool(have) and archive_members(have) == archive_members(want)
        except zipfile.BadZipFile:
            current = False

        if current:
            # Byte-identical is not required, so an archive whose contents
            # already match is left alone rather than rewritten. Rewriting it
            # would churn a binary in every diff for no gain.
            status = "unchanged"
        elif args.check:
            status = "STALE"
            stale.append(name)
        else:
            dest.write_bytes(want)
            status = "written"

        # Digest over the CONTENT, so the same sources print the same digest on
        # any machine. A digest of the compressed bytes varies with zlib and is
        # therefore useless for the one thing a digest is for here: telling two
        # people whether they are looking at the same skill.
        digest = hashlib.md5(
            b"".join(k.encode() + v for k, v in sorted(archive_members(want).items()))
        ).hexdigest()[:10]
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
