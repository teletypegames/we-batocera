#!/bin/bash
# Install ttg-store on a Batocera box.
#
#   scp -r batocera-store root@batocera:/tmp/ && ssh root@batocera /tmp/batocera-store/install.sh
#
# or, straight from the forge:
#
#   curl -fsSL https://git.teletypegames.org/tools/batocera-store/raw/branch/master/install.sh | bash
#
set -euo pipefail

HOME_DIR="${TTG_STORE_HOME:-/userdata/system/ttg-store}"
PORTS_DIR="${TTG_PORTS_DIR:-/userdata/roms/ports}"
PORT_NAME="${TTG_PORT_NAME:-Teletype Games Store}"
RAW_BASE="${TTG_RAW_BASE:-https://git.teletypegames.org/tools/batocera-store/raw/branch/master}"
SRC_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

say() { echo "[install] $*"; }

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
[ -d /userdata ] || say "warning: /userdata not found — this does not look like a Batocera box"

mkdir -p "$HOME_DIR" "$PORTS_DIR"

if [ -f "$SRC_DIR/ttg-store" ]; then
  say "installing ttg-store from $SRC_DIR"
  install -m 0755 "$SRC_DIR/ttg-store" "$HOME_DIR/ttg-store"
else
  say "downloading ttg-store from $RAW_BASE"
  curl -fsSL "$RAW_BASE/ttg-store" -o "$HOME_DIR/ttg-store"
  chmod 0755 "$HOME_DIR/ttg-store"
fi

# ttg-store reads TTG_STORE_HOME for its config/state/cache locations.
export TTG_STORE_HOME="$HOME_DIR"

if [ -f "$HOME_DIR/config.json" ]; then
  say "keeping existing $HOME_DIR/config.json"
else
  "$HOME_DIR/ttg-store" config --write
fi

# The Ports entry: EmulationStation gives launched scripts no console, so send
# everything to a log file the user can read over SSH.
cat > "$PORTS_DIR/$PORT_NAME.sh" <<EOF
#!/bin/bash
# Teletype Games Store — syncs the catalog into this box's ROM folders.
export TTG_STORE_HOME="$HOME_DIR"
exec >>"$HOME_DIR/ttg-store.log" 2>&1
echo "=== \$(date -Iseconds) sync started ==="
"$HOME_DIR/ttg-store" sync-from-es
echo "=== \$(date -Iseconds) sync finished (exit \$?) ==="
EOF
chmod 0755 "$PORTS_DIR/$PORT_NAME.sh"
say "ports entry: $PORTS_DIR/$PORT_NAME.sh"

say "running the first sync"
"$HOME_DIR/ttg-store" --verbose sync --no-restart

cat <<EOF

[install] done.

  Menu:     Ports -> "$PORT_NAME"   (re-run any time to pull new releases)
  CLI:      $HOME_DIR/ttg-store list|sync|remove
  Config:   $HOME_DIR/config.json
  Log:      $HOME_DIR/ttg-store.log

Restart EmulationStation to see the games:  batocera-es-swissknife --restart
EOF
