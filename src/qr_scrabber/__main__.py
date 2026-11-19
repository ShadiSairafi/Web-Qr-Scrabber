"""Module execution entry point (`python -m qr_scrabber`)."""

from __future__ import annotations

import sys

from qr_scrabber.cli import main

if __name__ == "__main__":
    sys.exit(main())
