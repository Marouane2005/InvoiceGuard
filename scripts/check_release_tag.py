"""Stops a release whose tag, code version, and release notes disagree.   python scripts/check_release_tag.py v1.4.0"""
from __future__ import annotations

import pathlib
import re
import sys
from typing import Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def problem(tag: str, version: str, changelog: str) -> Optional[str]:
    from release_notes import section
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        return f"the tag {tag!r} must look like v1.2.3"
    if tag != f"v{version}":
        return f"the tag {tag} does not match the code version {version}. Bump the version, commit, then tag that commit"
    if not section(changelog, version):
        return f"CHANGELOG.md has no section for {version}. Write the release notes first"
    return None


def main(argv) -> int:
    if len(argv) != 1:
        print("usage: check_release_tag.py vX.Y.Z", file=sys.stderr)
        return 2
    from invoiceguard import __version__
    error = problem(argv[0], __version__, (ROOT / "CHANGELOG.md").read_text())
    if error:
        print(error, file=sys.stderr)
        return 1
    print(f"{argv[0]} matches the code and the release notes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
