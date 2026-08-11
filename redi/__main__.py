"""`python3 -m redi ...` entry point — same as the installed `redi` console script."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
