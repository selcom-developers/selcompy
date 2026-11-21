"""Refresh, or check, the copy of the spec vectors in ``tests/vectors``.

    python scripts/refresh_vectors.py           # replace the copy with the files at SPEC_VERSION
    python scripts/refresh_vectors.py --check   # fail if the copy differs, byte for byte

The tag to use is the first line of ``SPEC_VERSION`` at the repository root.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SPEC_URL = "https://github.com/selcom-developers/selcom-spec"
ROOT = Path(__file__).resolve().parent.parent


def _differences(left: Path, right: Path) -> list[str]:
    result: list[str] = []
    cmp = filecmp.dircmp(left, right)
    result += [f"only in spec: {name}" for name in cmp.right_only]
    result += [f"only in copy: {name}" for name in cmp.left_only]
    for name in cmp.common_files:
        if (left / name).read_bytes() != (right / name).read_bytes():
            result.append(f"differs: {name}")
    for sub in cmp.common_dirs:
        result += [f"{sub}/{item}" for item in _differences(left / sub, right / sub)]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="only compare, never write")
    args = parser.parse_args()

    tag = (ROOT / "SPEC_VERSION").read_text(encoding="utf-8").splitlines()[0].strip()
    copy = ROOT / "tests" / "vectors"
    with tempfile.TemporaryDirectory() as tmp:
        checkout = Path(tmp) / "spec"
        subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", "--branch", tag, SPEC_URL, str(checkout)],
            check=True,
        )
        source = checkout / "vectors"
        if args.check:
            problems = _differences(copy, source) if copy.is_dir() else ["copy is missing"]
            if problems:
                print(f"tests/vectors is not identical to selcom-spec {tag}:", file=sys.stderr)
                for line in problems:
                    print(f"  {line}", file=sys.stderr)
                return 1
            print(f"tests/vectors is byte-identical to selcom-spec {tag}")
            return 0
        if copy.exists():
            shutil.rmtree(copy)
        shutil.copytree(source, copy)
        print(f"tests/vectors refreshed from selcom-spec {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
