# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "beancount>=3.2,<4", "pyyaml>=6", "matplotlib>=3.8",
#   "openpyxl>=3.1", "xlrd>=2.0", "pdfplumber>=0.11",
# ]
# ///
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ledgerkit.cli import main

if __name__ == "__main__":
    # Commands print non-ASCII text (names, bill rows); a Windows console must not choke on it.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    sys.exit(main())
