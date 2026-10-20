# /// script
# requires-python = ">=3.11"
# dependencies = ["tomli-w>=1.0", "pdfplumber>=0.11"]
# ///
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mailkit.cli import main

if __name__ == "__main__":
    # Commands print non-ASCII text (subjects, names); a Windows console must not choke on it.
    # Under uvw.exe, the launcher without a console, there are no streams at all.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8")
    sys.exit(main())
