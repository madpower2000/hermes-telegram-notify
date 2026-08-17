"""Console entry point for the installed plugin utilities.

Usage from the source checkout:
    python scripts/telegram_notify.py status
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_telegram_notify.cli import handle_cli, setup_cli  # noqa: E402
import argparse  # noqa: E402


parser = argparse.ArgumentParser(prog="telegram-notify")
setup_cli(parser)
args = parser.parse_args()
raise SystemExit(handle_cli(args))
