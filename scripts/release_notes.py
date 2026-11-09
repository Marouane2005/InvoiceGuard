"""Prints the CHANGELOG section for one version. Used by the release workflow:  python scripts/release_notes.py 1.4.0"""
from __future__ import annotations

import pathlib
import re
import sys
from typing import Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]


def section(changelog: str, version: str) -> Optional[str]:
    match = re.search(rf"^## {re.escape(version)}(?=[ \t]|$)[^\n]*\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    return match.group(1).strip() if match else None


def main(argv) -> int:
    if len(argv) != 1:
        print("usage: release_notes.py VERSION   (for example 1.4.0)", file=sys.stderr)
        return 2
    text = section((ROOT / "CHANGELOG.md").read_text(), argv[0])
    if not text:
        print(f"CHANGELOG.md has no section for {argv[0]}. Write the release notes before tagging.", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
