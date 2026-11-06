"""Desktop shortcut entry (D40): run by pythonw.exe, so there is no console window. See launcher.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))   # this source tree first, never another installed copy

from crypto_trader.strategies.ui.launcher import launch  # noqa: E402

sys.exit(launch(sys.argv[1:]))
