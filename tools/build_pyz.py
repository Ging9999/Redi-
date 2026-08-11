#!/usr/bin/env python3
"""Build a single-file `dist/redi.pyz` with the stdlib `zipapp` (spec B3).

Zero dependencies means this actually works: download one file, run it with any
Python 3.9+ — ``python3 redi.pyz doctor``, ``python3 redi.pyz serve --local``,
etc. Covers people without uv/pipx and anyone who wants to read the whole thing
before running it.
"""

import os
import shutil
import tempfile
import zipapp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "dist", "redi.pyz")


def main() -> int:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with tempfile.TemporaryDirectory() as staging:
        # Copy the package into the staging dir so `redi` is importable inside
        # the archive, and add a top-level __main__ that calls the CLI.
        shutil.copytree(os.path.join(ROOT, "redi"), os.path.join(staging, "redi"))
        with open(os.path.join(staging, "__main__.py"), "w", encoding="utf-8") as fh:
            fh.write("import sys\nfrom redi.cli import main\nsys.exit(main())\n")
        zipapp.create_archive(
            staging, OUT, interpreter="/usr/bin/env python3", compressed=True
        )
    print(f"built {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
