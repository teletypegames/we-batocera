#!/bin/sh
# Remove a WarpEngine store from a Batocera box.
#
# The engine does the removing, not this script: it is the only thing that knows
# which ROMs, box art, Ports payloads, gamelist entries and ES systems were the
# store's — so it runs `purge` first, and only then do the launcher, the Ports
# entry and the store home go.
#
#   ./uninstall.sh                  # the only store installed, or say which one
#   STORE_ID=example ./uninstall.sh
#   DRY_RUN=1 ./uninstall.sh        # show what would go, remove nothing
#   ssh root@batocera 'curl -fsSL https://git.teletypegames.org/stores/we-batocera/raw/branch/master/uninstall.sh | sh'
#
set -eu

STORE_ROOT="${BATOCERA_STORE_ROOT:-/userdata/system/batocera-store}"
PORTS_DIR="${BATOCERA_PORTS_DIR:-/userdata/roms/ports}"
STORE_ID="${STORE_ID:-}"
STORE_CONFIG="${STORE_CONFIG:-}"
DRY_RUN="${DRY_RUN:-0}"
# Keep the store home — its state.json, catalog cache and log — and take only the
# games, the launcher and the Ports entry. Useful before a reinstall.
KEEP_HOME="${KEEP_HOME:-0}"

say() { echo "[uninstall] $*"; }
die() { echo "[uninstall] error: $*" >&2; exit 1; }
run() {
  if [ "$DRY_RUN" = "1" ]; then echo "[uninstall] would: $*"; else "$@"; fi
}

command -v python3 >/dev/null 2>&1 || die "python3 is required"

# Which store? An explicit id wins, then the id in a config, then — if exactly
# one store is installed — that one. Guessing between several would be worse than
# asking.
if [ -z "$STORE_ID" ] && [ -n "$STORE_CONFIG" ]; then
  TMP_CFG="$(mktemp)"
  trap 'rm -f "$TMP_CFG"' EXIT
  case "$STORE_CONFIG" in
    http://*|https://*) curl -fsSL "$STORE_CONFIG" -o "$TMP_CFG" ;;
    *) [ -f "$STORE_CONFIG" ] || die "no such file: $STORE_CONFIG"; cp "$STORE_CONFIG" "$TMP_CFG" ;;
  esac
  STORE_ID="$(python3 -c 'import json,sys; print((json.load(open(sys.argv[1])).get("store") or {}).get("id") or "")' "$TMP_CFG")"
  [ -n "$STORE_ID" ] || die "no store.id in $STORE_CONFIG"
fi

if [ -z "$STORE_ID" ]; then
  [ -d "$STORE_ROOT" ] || die "nothing is installed under $STORE_ROOT"
  FOUND=""
  COUNT=0
  for d in "$STORE_ROOT"/*/; do
    if [ -f "$d/config.json" ]; then
      FOUND="$FOUND $(basename "$d")"
      COUNT=$((COUNT + 1))
    fi
  done
  case "$COUNT" in
    0) die "nothing is installed under $STORE_ROOT" ;;
    1) STORE_ID="${FOUND# }"; say "the only store installed is '$STORE_ID'" ;;
    *) die "several stores are installed ($FOUND) — pick one: STORE_ID=<id> $0" ;;
  esac
fi

STORE_HOME="$STORE_ROOT/$STORE_ID"
LAUNCHER="$STORE_ROOT/$STORE_ID-store"
[ -d "$STORE_HOME" ] || die "no store '$STORE_ID' in $STORE_ROOT"

# The Ports entry cannot be derived from the config: the installer may be given
# BATOCERA_PORT_NAME to something else entirely (ours is "Teletype Games Store",
# while store.name is "Teletype Games"). What is certain is that the entry runs
# this store's launcher, so the entry is found by looking inside the scripts —
# which also works for stores installed before this uninstaller existed.
PORTS_ENTRIES=""
if [ -n "${BATOCERA_PORT_NAME:-}" ]; then
  [ -f "$PORTS_DIR/$BATOCERA_PORT_NAME.sh" ] && PORTS_ENTRIES="$PORTS_DIR/$BATOCERA_PORT_NAME.sh"
elif [ -d "$PORTS_DIR" ]; then
  for entry in "$PORTS_DIR"/*.sh; do
    [ -f "$entry" ] || continue
    if grep -q -F "$STORE_HOME" "$entry" 2>/dev/null; then
      PORTS_ENTRIES="$PORTS_ENTRIES
$entry"
    fi
  done
fi

say "removing the store '$STORE_ID' from $STORE_HOME"

# The games first, through the engine. If it cannot finish — an unmounted ROMs
# root, say — stop here rather than delete the engine that knows what it
# installed and leave the files orphaned.
if [ -f "$STORE_HOME/store.py" ] && [ -f "$STORE_HOME/config.json" ]; then
  DRY=""
  [ "$DRY_RUN" = "1" ] && DRY="--dry-run"
  # shellcheck disable=SC2086 # the flag is ours and deliberately word-split
  BATOCERA_STORE_HOME="$STORE_HOME" python3 "$STORE_HOME/store.py" \
    --config "$STORE_HOME/config.json" $DRY purge \
    || die "the engine could not remove the games (see above) — nothing else was touched."
else
  say "warning: no engine in $STORE_HOME — the ROMs and gamelist entries have to go by hand"
fi

# Newline-separated so a Ports entry with spaces in its name survives the loop.
if [ -n "$PORTS_ENTRIES" ]; then
  printf '%s\n' "$PORTS_ENTRIES" | while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    if [ "$DRY_RUN" = "1" ]; then
      echo "[uninstall] would: rm -f $entry"
    else
      rm -f "$entry"
      say "removed the Ports entry $entry"
    fi
  done
else
  say "no Ports entry found in $PORTS_DIR"
fi
run rm -f "$LAUNCHER"

if [ "$KEEP_HOME" = "1" ]; then
  say "keeping $STORE_HOME (KEEP_HOME=1)"
else
  run rm -rf "$STORE_HOME"
  # Only if this was the last store on the box.
  if rmdir "$STORE_ROOT" 2>/dev/null; then say "removed the now-empty $STORE_ROOT"; fi
fi

if [ "$DRY_RUN" = "1" ]; then
  say "dry run — nothing was removed."
else
  say "done. EmulationStation was restarted if it was running; if not, the games are"
  say "already gone from the gamelists, and so is the store's own menu entry."
fi
