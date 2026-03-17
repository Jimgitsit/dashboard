"""Launcher: python -m dashboard.run"""

import os
import sys
import uvicorn
from pathlib import Path
from dotenv import load_dotenv
from .db import init_db

load_dotenv()

# Enable upsonic logging
os.environ.setdefault("UPSONIC_LOG_LEVEL", "DEBUG")
os.environ.setdefault("UPSONIC_LOG_FORMAT", "detailed")

# Redirect upsonic's Rich console output to a log file
_log_path = Path(__file__).parent.parent / "upsonic.log"
_log_file = open(_log_path, "a", buffering=1)  # line-buffered
try:
    from rich.console import Console
    import upsonic.utils.printing as _up
    _up.console = Console(file=_log_file, force_terminal=False, width=200)
except Exception:
    pass
sys.stderr = _log_file

if __name__ == "__main__":
    init_db()
    uvicorn.run(
        "dashboard.api:app",
        host="0.0.0.0",
        port=8765,
        reload=False,
    )
