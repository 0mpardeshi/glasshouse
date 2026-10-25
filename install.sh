#!/bin/sh
# Glasshouse installer — no root, no pip, no npm required.
#
#   ./install.sh
#
# It makes the dashboard reachable from the applications menu and starts it.
# Re-running is safe. Undo with:  glasshouse setup --remove
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Glasshouse needs Python 3.10+. Install python3 and run this again." >&2
  exit 1
fi

"$PYTHON" - "$here" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
version = sys.version_info
if version < (3, 10):
    print(f"Glasshouse needs Python 3.10+, found {version.major}.{version.minor}", file=sys.stderr)
    raise SystemExit(1)

from glasshouse import __version__
from glasshouse import home

print(f"Glasshouse {__version__} — installing for {Path.home()}")
result = home.setup(autostart=True)
for path in result["written"]:
    print(f"  wrote {path}")

if home.start_server():
    print(f"  dashboard: {home.server_url()}")
else:
    print("  server did not start; run: python3 -m glasshouse.cli serve", file=sys.stderr)
    raise SystemExit(1)
PY

echo
echo "Done. Three ways in, same dashboard:"
echo "  1. double-click the Glasshouse icon on your desktop"
echo "  2. open 'Glasshouse' from your applications menu"
echo "  3. $here/bin/glass menu"
echo
echo "Nothing on screen yet? A maximised window hides the desktop; use your"
echo "applications menu, or drag the desktop icon into your taskbar."
