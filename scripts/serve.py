"""Run the web app with no console window; output goes to <data root>/logs/web.log.

Started by the "AVM Web" scheduled task (scripts/install_web_task.ps1), so the
app is up after every logon and comes back on its own if it stops.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from app.config import get_settings  # noqa: E402


def main() -> int:
    settings = get_settings()
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log = (settings.logs_dir / "web.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log

    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
