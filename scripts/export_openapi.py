"""Writes or checks docs/openapi.json, the committed API contract. A change to the API must change this file on purpose.

    python scripts/export_openapi.py          # rewrite it
    python scripts/export_openapi.py --check  # exit 1 if the API no longer matches the committed file
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from invoiceguard.api import create_app  # noqa: E402
from invoiceguard.config import Settings  # noqa: E402

TARGET = ROOT / "docs" / "openapi.json"


def current() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        schema = create_app(Settings(data_dir=pathlib.Path(tmp))).openapi()
    schema = json.loads(json.dumps(schema))
    schema["info"]["version"] = "see CHANGELOG"   # the version is checked elsewhere; the contract is about the shape
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main(argv) -> int:
    text = current()
    if "--check" in argv:
        if not TARGET.exists() or TARGET.read_text() != text:
            print("docs/openapi.json is out of date. If the API change is intended, run: python scripts/export_openapi.py")
            return 1
        print("API contract matches docs/openapi.json")
        return 0
    TARGET.write_text(text)
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
