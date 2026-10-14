# /// script
# requires-python = ">=3.11"
# dependencies = ["imap-tools>=1.15", "dnspython>=2.6"]
# ///
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mailkit.cli import main

if __name__ == "__main__":
    # Commands print non-ASCII text (mailbox names, mail); a Windows console must not choke on it.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    sys.exit(main())
