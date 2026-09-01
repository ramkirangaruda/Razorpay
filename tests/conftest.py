"""
Loads .env into os.environ before the test session starts, if the file exists.

No new dependency for two lines of KEY=VALUE - .env is gitignored (see HANDOFF.md
Section 6, "never commit an API key") and holds RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET
for the live L3 tests in test_executor.py, and ANTHROPIC_API_KEY for a live L1 run
if one is ever done. Never overwrites a variable already set in the environment.
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(_ENV_PATH)
