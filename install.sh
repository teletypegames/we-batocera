#!/bin/sh
# Install a WarpEngine store on a Batocera box.
#
# This installer belongs to the engine, so it takes the store as a parameter:
# point STORE_CONFIG at a config.json — a local path or a URL — and it sets up
# that store. A store repository's own install.sh is a three-line wrapper that
# supplies its config; see README.
#
#   STORE_CONFIG=./config.json ./install.sh
#   STORE_CONFIG=https://git.example.org/tools/my-store/raw/branch/master/config.json \
#     curl -fsSL https://git.teletypegames.org/stores/warp-engine-batocera-store/raw/branch/master/install.sh | sh
#
set -eu

STORE_ROOT="${BATOCERA_STORE_ROOT:-/userdata/system/batocera-store}"
PORTS_DIR="${BATOCERA_PORTS_DIR:-/userdata/roms/ports}"
ENGINE_RAW_BASE="${ENGINE_RAW_BASE:-https://git.teletypegames.org/stores/warp-engine-batocera-store/raw/branch/master}"
# The shared core lives in its own repository, because both store engines use it.
WARPSTORE_RAW_BASE="${WARPSTORE_RAW_BASE:-https://git.teletypegames.org/engines/warpstore/raw/branch/master}"
STORE_CONFIG="${STORE_CONFIG:-}"
# Only set when run as a file; piped from curl "$0" is `bash`, and then the
# engine has to come from the forge rather than from whatever the cwd holds.
SRC_DIR=""
if [ -f "$0" ]; then
  SRC_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
fi

say() { echo "[install] $*"; }
die() { echo "[install] error: $*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 is required"
[ -n "$STORE_CONFIG" ] || die "STORE_CONFIG is required (path or URL of a store config.json)"
[ -d /userdata ] || say "warning: /userdata not found — this does not look like a Batocera box"

fetch() {  # fetch <source> <dest>; source may be a local path or a URL
  case "$1" in
    http://*|https://*) curl -fsSL "$1" -o "$2" ;;
    *) [ -f "$1" ] || die "no such file: $1"; cp "$1" "$2" ;;
  esac
}

# Stage the config first: the store id in it decides where everything lands.
TMP_CFG="$(mktemp)"
trap 'rm -f "$TMP_CFG"' EXIT
say "reading store config from $STORE_CONFIG"
fetch "$STORE_CONFIG" "$TMP_CFG"

# The store id names its home; the mode decides whether a Ports entry is wanted
# at all, since a store with its own ES menu entry has an updater inside it.
STORE_META="$(python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
store = cfg.get("store") or {}
sid = store.get("id") or ""
if not sid or "/" in sid:
    sys.exit("store.id is required and must not contain a slash")
es = cfg.get("emulationstation") or {}
mode = ((es.get("menu") or {}).get("mode") or "system").lower()
port = es.get("ports_entry")
port = (mode == "merge") if port is None else bool(port)
print(sid, "1" if port else "0", store.get("name") or sid)
' "$TMP_CFG")" || die "cannot read $STORE_CONFIG"

STORE_ID="${STORE_META%% *}"
STORE_REST="${STORE_META#* }"
WANT_PORTS_ENTRY="${STORE_REST%% *}"
STORE_NAME="${STORE_REST#* }"

STORE_HOME="$STORE_ROOT/$STORE_ID"
PORT_NAME="${BATOCERA_PORT_NAME:-$STORE_NAME}"
LAUNCHER="$STORE_ROOT/$STORE_ID-store"

mkdir -p "$STORE_HOME" "$PORTS_DIR"

install_file() {  # install_file <name> <raw base> [local override]
  if [ -n "${3:-}" ] && [ -f "${3:-}" ]; then
    say "installing $1 from $3"
    install -m 0755 "$3" "$STORE_HOME/$1"
  elif [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/$1" ]; then
    say "installing $1 from $SRC_DIR"
    install -m 0755 "$SRC_DIR/$1" "$STORE_HOME/$1"
  else
    say "downloading $1 from $2"
    curl -fsSL "$2/$1" -o "$STORE_HOME/$1"
    chmod 0755 "$STORE_HOME/$1"
  fi
}

install_file store.py "$ENGINE_RAW_BASE"
# The shared core: one file next to the engine, fetched from its own repository.
install_file warpstore.py "$WARPSTORE_RAW_BASE" "${WARPSTORE_SRC:-}"

# The config is the store's identity, so it is always refreshed; state.json,
# catalog.json and the log stay put, which makes re-running the upgrade path.
if [ -f "$STORE_HOME/config.json" ]; then
  say "updating $STORE_HOME/config.json (previous copy kept as config.json.bak)"
  cp "$STORE_HOME/config.json" "$STORE_HOME/config.json.bak"
fi
install -m 0644 "$TMP_CFG" "$STORE_HOME/config.json"

# A short launcher so the CLI is one path, not a python invocation.
cat > "$LAUNCHER" <<EOF
#!/bin/bash
# $STORE_NAME — CLI wrapper around the shared batocera-store engine.
export BATOCERA_STORE_HOME="$STORE_HOME"
exec "$STORE_HOME/store.py" --config "$STORE_HOME/config.json" "\$@"
EOF
chmod 0755 "$LAUNCHER"

# The Ports entry: EmulationStation gives launched scripts no console, so send
# everything to a log file the user can read over SSH. A store that has its own
# ES menu entry keeps the same script inside it instead — the engine writes that
# one — so there is nothing to put in Ports.
if [ "$WANT_PORTS_ENTRY" = "1" ]; then
  cat > "$PORTS_DIR/$PORT_NAME.sh" <<EOF
#!/bin/bash
# $STORE_NAME — syncs the catalog into this box's ROM folders.
exec >>"$STORE_HOME/store.log" 2>&1
echo "=== \$(date -Iseconds) sync started ==="
"$LAUNCHER" sync-from-es
echo "=== \$(date -Iseconds) sync finished (exit \$?) ==="
EOF
  chmod 0755 "$PORTS_DIR/$PORT_NAME.sh"
  say "ports entry: $PORTS_DIR/$PORT_NAME.sh"
fi

say "running the first sync"
"$LAUNCHER" --verbose sync --no-restart

if [ "$WANT_PORTS_ENTRY" = "1" ]; then
  MENU_LINE="  Menu:     Ports -> \"$PORT_NAME\"   (re-run any time to pull new releases)"
else
  MENU_LINE="  Menu:     \"$STORE_NAME\" -> Store -> \"Update $STORE_NAME\"   (re-run any time to pull new releases)"
fi

cat <<EOF

[install] done.

$MENU_LINE
  CLI:      $LAUNCHER list|sync|remove
  Config:   $STORE_HOME/config.json
  Log:      $STORE_HOME/store.log

Restart EmulationStation to see the games:  batocera-es-swissknife --restart
EOF
