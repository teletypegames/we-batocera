#!/usr/bin/env python3
"""store.py — the Batocera adapter of the WarpEngine store engine.

Reads a WarpEngine catalog (`GET /api/software`), keeps the entries whose
platform maps to a system this Batocera box actually has, downloads the matching
release asset into that system's ROM folder and writes EmulationStation
metadata (title, description, author, box art) into its gamelist.xml.

The engine knows nothing about any particular site: which host to talk to, what
the store is called and where its games land all come from `config.json`. A
store repository is therefore just that config plus an installer — see README.

Everything that is not specific to Batocera — the catalog, release selection,
architecture matching, state, HTTP — lives in `warpstore.py`, the module this
engine shares with the other store engines. The installer puts the two files
side by side.

Standard library only — Batocera ships python3 but no pip packages.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import xml.etree.ElementTree as ET
import zipfile

# The shared core sits next to this script (the installer puts it there); make
# sure that is where we look, however the script was invoked.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import warpstore as ws
except ImportError:
    sys.exit("error: warpstore.py is missing next to store.py — reinstall the store engine "
             "(https://git.teletypegames.org/engines/warpstore)")
from warpstore import debug, die, log

VERSION = "3.0.0"

# Every store keeps its code, config, state and log in one directory, so a box
# can carry several stores side by side without them treading on each other.
# The installer exports BATOCERA_STORE_HOME; run straight from a checkout the
# script's own directory is the store home.
DEFAULT_HOME = os.environ.get("BATOCERA_STORE_HOME") or os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(DEFAULT_HOME, "config.json")

DEFAULT_CONFIG = {
    "store": {
        # Short slug: names the state, the log prefix and the gamelist backup.
        "id": "warp",
        # Human-readable, used for the Ports entry and the ES folder label.
        "name": "WarpEngine Store",
        # The WarpEngine host serving the catalog.
        "base_url": "https://example.org",
        # Endpoint paths, in case the engine is mounted somewhere other than /.
        "api": {
            "catalog": "/api/software",
            "download": "/api/download",
        },
    },
    "paths": {
        "roms_root": "/userdata/roms",
        # Everything we install lives under this subfolder of the system's ROM
        # dir, so a prune can never touch ROMs the user put there themselves —
        # nor games installed by another store.
        "subfolder": "warp",
    },
    "emulationstation": {
        # Display name of our subfolder in ES; null hides the folder node.
        "folder_name": "WarpEngine Store",
        # Restart EmulationStation after a sync that changed something.
        "restart": True,
    },
    "catalog": {
        # Catalog `status` values worth installing. "development" is left out.
        "statuses": ["released", "archived"],
        # Restrict to one publisher (WarpEngine owner_id), null = whole catalog.
        "owner_id": None,
        # Software `name` allow/deny lists; empty allow list means "everything".
        "only": [],
        "exclude": [],
    },
    # WarpEngine platform -> Batocera system + which release asset to pull.
    "platforms": {
        "c64": {"system": "c64", "kind": "cartridge", "ext": ".prg", "enabled": True},
        "tic80": {"system": "tic80", "kind": "cartridge", "ext": ".tic", "enabled": True},
    },
    "behavior": {
        # Drop installed games that fell out of the catalog or the filters.
        "prune": True,
        "timeout": 30,
        "insecure": False,
    },
}


# Shorthands for the values the code below reaches for constantly.
def subfolder(cfg):
    return cfg["paths"]["subfolder"]


def roms_root(cfg):
    return cfg["paths"]["roms_root"]


# --------------------------------------------------------------------------
# catalog -> Batocera
# --------------------------------------------------------------------------


def accept_for_batocera(available_systems):
    """The adapter's veto: a title is installable if this box has its system.

    Returns the `accept` callback `warpstore.select_games` asks for. The scope a
    record belongs to is the ES system, which is also what its gamelist.xml is
    keyed by.
    """

    def accept(spec, sw):
        system = spec["system"]
        if system not in available_systems:
            raise ws.Skip(f"no '{system}' ROM folder on this box")
        return system, {"system": system, "install": spec.get("install", "rom")}

    return accept


def select_games(cfg, catalog, available_systems, host_info=None):
    return ws.select_games(cfg, catalog, accept_for_batocera(available_systems), host_info)


def es_date(iso):
    """`2026-07-27T20:49:00.118Z` -> `20260727T204900` (ES gamelist format)."""
    if not iso or len(iso) < 19:
        return ""
    return f"{iso[0:4]}{iso[5:7]}{iso[8:10]}T{iso[11:13]}{iso[14:16]}{iso[17:19]}"


# --------------------------------------------------------------------------
# installation
# --------------------------------------------------------------------------


def system_dir(cfg, system):
    return os.path.join(roms_root(cfg), system)


def detect_systems(cfg):
    """Systems this box has a ROM folder for — our compatibility check."""
    root = roms_root(cfg)
    if not os.path.isdir(root):
        return set()
    return {d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))}


def rel_rom_path(cfg, game):
    return f"{subfolder(cfg)}/{game['asset']}"


def rel_image_path(cfg, game, ext):
    return f"{subfolder(cfg)}/images/{game['name']}{ext}"


def rel_port_launcher(cfg, game):
    return f"{subfolder(cfg)}/{game['name']}.sh"


def rel_port_data(cfg, game):
    # A `.data` pont-prefixe rejti el az ES elol; a store almappaja alatta
    # tartja kulon az egyes store-ok payloadjat.
    return f".data/{subfolder(cfg)}/{game['name']}"


def find_executable(root, name):
    """A kicsomagolt fa fo binarisa.

    Az unix zip megorzi a futtathato bitet, tehat elsore azt keressuk. Ha
    tobb jelolt van, a szoftver nevevel egyezo nyer; ha egy sem futtathato,
    a nevegyezes az utolso esely.
    """
    execs, by_name = [], []
    for base, _, files in os.walk(root):
        for f in files:
            full = os.path.join(base, f)
            if os.access(full, os.X_OK) and not os.path.isdir(full):
                execs.append(full)
            if f == name:
                by_name.append(full)
    for candidate in (execs, by_name):
        if len(candidate) == 1:
            return candidate[0]
        exact = [c for c in candidate if os.path.basename(c) == name]
        if len(exact) == 1:
            return exact[0]
    return None


def install_port(cfg, game, dry_run=False):
    """A zip asset kicsomagolasa + Ports indito. Visszaad (launcher, data) relativ utakat."""
    base = system_dir(cfg, game["system"])
    launcher_rel = rel_port_launcher(cfg, game)
    data_rel = rel_port_data(cfg, game)
    launcher_abs = os.path.join(base, launcher_rel)
    data_abs = os.path.join(base, data_rel)

    if dry_run:
        log(f"would install {game['name']} {game['version']} as a port -> {launcher_rel}")
        return launcher_rel, data_rel

    url = ws.download_url(cfg, game["asset"])
    os.makedirs(os.path.dirname(launcher_abs), exist_ok=True)
    tmp_zip = os.path.join(os.path.dirname(launcher_abs), f".{ws.TAG}-{game['name']}.zip")
    size = ws.http_download(cfg, url, tmp_zip)
    try:
        shutil.rmtree(data_abs, ignore_errors=True)
        os.makedirs(data_abs, exist_ok=True)
        with zipfile.ZipFile(tmp_zip) as zf:
            zf.extractall(data_abs)
            # A zip futtathato bitjeit a Python nem allitja vissza.
            for info in zf.infolist():
                mode = info.external_attr >> 16
                if mode & 0o111:
                    target = os.path.join(data_abs, info.filename)
                    if os.path.isfile(target):
                        os.chmod(target, os.stat(target).st_mode | 0o111)
    finally:
        if os.path.exists(tmp_zip):
            os.unlink(tmp_zip)

    exe = find_executable(data_abs, game["name"])
    if not exe:
        shutil.rmtree(data_abs, ignore_errors=True)
        raise IOError(f"no executable found in {game['asset']}")

    # A jatek az assetjeit a munkakonyvtarhoz kepest tolti be, ezert a
    # binaris konyvtarabol kell inditani.
    exe_dir = os.path.dirname(os.path.relpath(exe, data_abs))
    run_dir = os.path.join(data_abs, exe_dir) if exe_dir else data_abs
    ws.write_atomic(launcher_abs, (
        "#!/bin/bash\n"
        f"# {game['title']} — {cfg['store']['name']}\n"
        f"cd {shlex.quote(run_dir)} || exit 1\n"
        f"exec ./{os.path.basename(exe)}\n"
    ).encode("utf-8"))
    os.chmod(launcher_abs, 0o755)
    log(f"installed {game['name']} {game['version']} ({size} bytes) as a port -> {launcher_rel}")
    return launcher_rel, data_rel


def install_game(cfg, game, state_entry, dry_run=False):
    """Download the asset + box art if missing. Returns (record, changed)."""
    base = system_dir(cfg, game["system"])
    changed = False
    data_rel = None

    if game.get("install") == "port":
        # A port ket dologbol all: egy indito .sh es a kicsomagolt payload.
        rom_rel = rel_port_launcher(cfg, game)
        data_rel = rel_port_data(cfg, game)
        installed = (os.path.isfile(os.path.join(base, rom_rel))
                     and os.path.isdir(os.path.join(base, data_rel))
                     and state_entry and state_entry.get("asset") == game["asset"])
        if installed:
            debug(f"{game['name']}: port already installed")
        else:
            rom_rel, data_rel = install_port(cfg, game, dry_run=dry_run)
            changed = True
    else:
        rom_rel = rel_rom_path(cfg, game)
        rom_abs = os.path.join(base, rom_rel)
        if os.path.isfile(rom_abs) and os.path.getsize(rom_abs) > 0:
            debug(f"{game['name']}: {game['asset']} already present")
        else:
            url = ws.download_url(cfg, game["asset"])
            if dry_run:
                log(f"would download {game['name']} {game['version']} -> {rom_abs}")
            else:
                size = ws.http_download(cfg, url, rom_abs)
                log(f"installed {game['name']} {game['version']} ({size} bytes) -> {rom_rel}")
            changed = True

    image_rel = state_entry.get("image") if state_entry else None
    if game.get("image_url"):
        wanted = image_rel and os.path.isfile(os.path.join(base, image_rel))
        if not wanted:
            if dry_run:
                log(f"would fetch box art for {game['name']}")
                image_rel = rel_image_path(cfg, game, ".png")
            else:
                image_rel = fetch_image(cfg, game, base)
            changed = changed or bool(image_rel)
    else:
        # Box art disappeared from the catalog: drop the file we cached.
        if image_rel and not dry_run:
            stale = os.path.join(base, image_rel)
            if image_rel.startswith(subfolder(cfg) + "/") and os.path.isfile(stale):
                os.unlink(stale)
                changed = True
        image_rel = None

    record = dict(game)
    record["rom"] = rom_rel
    record["image"] = image_rel
    record["releasedate"] = es_date(game.get("created_at"))
    if data_rel:
        record["data"] = data_rel
    return record, changed


def fetch_image(cfg, game, base):
    body, ext = ws.download_image(cfg, game)
    if body is None:
        return None
    rel = rel_image_path(cfg, game, ext)
    ws.write_atomic(os.path.join(base, rel), body)
    debug(f"{game['name']}: box art -> {rel}")
    return rel


def remove_game(cfg, record, dry_run=False):
    base = system_dir(cfg, record["system"])
    for rel in (record.get("rom"), record.get("image")):
        if not rel:
            continue
        path = os.path.join(base, rel)
        # Never step outside our own subfolder — another store may own it.
        if not rel.startswith(subfolder(cfg) + "/"):
            log(f"warning: refusing to delete {path} (outside {subfolder(cfg)}/)")
            continue
        if os.path.isfile(path):
            if dry_run:
                log(f"would remove {path}")
            else:
                os.unlink(path)
                debug(f"removed {path}")

    # A port payloadja egy konyvtar a rejtett .data alatt.
    data_rel = record.get("data")
    if data_rel:
        data_abs = os.path.join(base, data_rel)
        if not data_rel.startswith(f".data/{subfolder(cfg)}/"):
            log(f"warning: refusing to delete {data_abs} (outside .data/{subfolder(cfg)}/)")
        elif os.path.isdir(data_abs):
            if dry_run:
                log(f"would remove {data_abs}")
            else:
                shutil.rmtree(data_abs, ignore_errors=True)
                debug(f"removed {data_abs}")


# --------------------------------------------------------------------------
# gamelist.xml
# --------------------------------------------------------------------------


def gamelist_path(cfg, system):
    return os.path.join(system_dir(cfg, system), "gamelist.xml")


def normalize_path(text):
    text = (text or "").strip()
    return text[2:] if text.startswith("./") else text


def merge_gamelist(cfg, system, records, dry_run=False):
    """Rewrite only the <game>/<folder> nodes under our subfolder.

    Everything else in the file — the user's own scraped ROMs, their play
    counts, favourites, and whatever another store installed under its own
    subfolder — is parsed and written back untouched.
    """
    path = gamelist_path(cfg, system)
    prefix = subfolder(cfg) + "/"

    if os.path.isfile(path):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            log(f"warning: {path} is not valid XML ({exc}) — starting a fresh gamelist")
            root = ET.Element("gameList")
        else:
            backup = f"{path}.{cfg['store']['id']}-backup"
            if not os.path.exists(backup) and not dry_run:
                shutil.copy2(path, backup)
    else:
        root = ET.Element("gameList")

    for node in list(root):
        target = normalize_path(node.findtext("path"))
        if target.startswith(prefix) or target == subfolder(cfg):
            root.remove(node)

    folder_name = cfg["emulationstation"].get("folder_name")
    if records and folder_name:
        folder = ET.SubElement(root, "folder")
        ET.SubElement(folder, "path").text = "./" + subfolder(cfg)
        ET.SubElement(folder, "name").text = folder_name

    for record in sorted(records, key=lambda r: r["title"].lower()):
        game = ET.SubElement(root, "game")
        ET.SubElement(game, "path").text = "./" + record["rom"]
        ET.SubElement(game, "name").text = record["title"]
        if record.get("desc"):
            ET.SubElement(game, "desc").text = record["desc"]
        if record.get("image"):
            ET.SubElement(game, "image").text = "./" + record["image"]
            ET.SubElement(game, "thumbnail").text = "./" + record["image"]
        if record.get("author"):
            ET.SubElement(game, "developer").text = record["author"]
            ET.SubElement(game, "publisher").text = record["author"]
        released = record.get("releasedate") or es_date(record.get("created_at"))
        if released:
            ET.SubElement(game, "releasedate").text = released

    indent(root)
    blob = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
    if dry_run:
        log(f"would write {path} ({len(records)} entries)")
        return
    ws.write_atomic(path, blob)
    log(f"gamelist updated: {path} ({len(records)} entries)")


def indent(elem, level=0):
    pad = "\n" + "  " * level
    if len(elem):
        if not (elem.text or "").strip():
            elem.text = pad + "  "
        for child in elem:
            indent(child, level + 1)
            if not (child.tail or "").strip():
                child.tail = pad + "  "
        if not (elem[-1].tail or "").strip():
            elem[-1].tail = pad
    if level and not (elem.tail or "").strip():
        elem.tail = pad


# --------------------------------------------------------------------------
# EmulationStation
# --------------------------------------------------------------------------


def es_pid():
    """PID of a running EmulationStation, or None."""
    try:
        out = subprocess.run(
            ["batocera-es-swissknife", "--espid"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return int(out.split()[0]) if out.split() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True


def wait_for_pid(pid, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.5)
    return False


def restart_es():
    if not shutil.which("batocera-es-swissknife"):
        log("batocera-es-swissknife not found — restart EmulationStation manually")
        return False
    log("restarting EmulationStation to pick up the new games")
    subprocess.Popen(["batocera-es-swissknife", "--restart"])
    return True


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def perform_sync(cfg, names=None, dry_run=False):
    """Download every compatible game, prune what fell out of the catalog.

    Returns (state, changed, touched_systems). Gamelists are left alone — the
    caller decides when it is safe to write them.
    """
    systems = detect_systems(cfg)
    if not systems:
        # An unmounted or wrong ROMs root would otherwise look like "no system
        # is compatible any more" and prune everything we ever installed.
        die(f"no system folders under {roms_root(cfg)} — is it mounted?")

    catalog = ws.fetch_catalog(cfg, use_cache=True)
    games, skipped = select_games(cfg, catalog, systems)
    for reason in skipped:
        log(f"skipped {reason}")

    if names:
        games = ws.limit_to_names(games, names)

    state = ws.load_state()
    installed = state["installed"]
    touched, changed = set(), False

    for game in games:
        key = ws.game_key(game)
        previous = installed.get(key)
        if previous and previous.get("asset") != game["asset"]:
            log(f"{game['name']}: {previous.get('version')} -> {game['version']}, removing old files")
            remove_game(cfg, previous, dry_run=dry_run)
            previous = None
            changed = True
        try:
            record, did = install_game(cfg, game, previous, dry_run=dry_run)
        except (urllib.error.URLError, OSError) as exc:
            log(f"warning: {game['name']} failed: {exc}")
            continue
        if did or installed.get(key) != record:
            changed = True
        installed[key] = record
        touched.add(record["system"])

    # A name-limited sync is not a full picture of the catalog, so never prune.
    if cfg["behavior"].get("prune") and not names:
        keep = {ws.game_key(g) for g in games}
        for key in list(installed):
            if key in keep:
                continue
            log(f"pruning {key} (no longer in the catalog or filtered out)")
            remove_game(cfg, installed[key], dry_run=dry_run)
            touched.add(ws.record_scope(installed[key]))
            del installed[key]
            changed = True

    if not dry_run:
        ws.save_state(state)
    return state, changed, touched


def cmd_sync(cfg, args):
    state, changed, touched = perform_sync(cfg, args.name, dry_run=args.dry_run)
    if args.dry_run:
        log("dry run — nothing written")
        return 0
    if not changed and not args.force:
        log("already up to date")
        return 0

    grouped = ws.records_by_scope(state["installed"])
    for system in sorted(touched | set(grouped)):
        merge_gamelist(cfg, system, grouped.get(system, []))

    if cfg["emulationstation"].get("restart") and not args.no_restart and es_pid():
        restart_es()
    return 0


def cmd_sync_from_es(cfg, args):
    """Sync as launched from the Ports menu — no console, so ES has to reload.

    EmulationStation holds gamelists in memory and writes them back on exit,
    so downloading happens now and the gamelist merge is handed to a detached
    child that waits for the old ES process to die first.
    """
    state, changed, _ = perform_sync(cfg)
    pid = es_pid()

    if not changed:
        log("already up to date")
    if not pid or not changed:
        # Nothing racing us (or nothing new) — write the gamelists right here.
        if changed:
            for system, records in ws.records_by_scope(state["installed"]).items():
                merge_gamelist(cfg, system, records)
        return 0

    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__),
         "--config", ws.CONFIG_PATH, "apply-gamelists", "--wait-pid", str(pid)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    if cfg["emulationstation"].get("restart") and not args.no_restart:
        restart_es()
    return 0


def cmd_apply_gamelists(cfg, args):
    if args.wait_pid:
        if not wait_for_pid(args.wait_pid, timeout=90):
            log(f"warning: EmulationStation (pid {args.wait_pid}) still running — writing anyway")
    state = ws.load_state()
    grouped = ws.records_by_scope(state["installed"])
    if not grouped:
        log("nothing installed")
        return 0
    for system, records in grouped.items():
        merge_gamelist(cfg, system, records, dry_run=args.dry_run)
    return 0


def cmd_list(cfg, args):
    systems = detect_systems(cfg)
    catalog = ws.fetch_catalog(cfg, use_cache=True)
    host = ws.host()
    log(f"architecture: {host['arch']}")
    games, skipped = select_games(cfg, catalog, systems, host)
    installed = ws.load_state()["installed"]

    if not games:
        log("no compatible games in the catalog")
    for game in sorted(games, key=lambda g: (g["system"], g["title"].lower())):
        record = installed.get(ws.game_key(game))
        if not record:
            mark = "  "
        elif record.get("asset") == game["asset"]:
            mark = "* "
        else:
            mark = "^ "
        print(f"{mark}{game['system']:<8} {game['name']:<18} {game['version']:<10} {game['title']}")
    if games:
        print("\n  * installed   ^ update available")
    for reason in skipped:
        log(f"skipped {reason}")
    return 0


def cmd_remove(cfg, args):
    state = ws.load_state()
    installed = state["installed"]
    touched = set()
    keys = ws.match_keys(installed, args.name)
    if not keys:
        log(f"not installed: {', '.join(args.name)}")
        return 0
    for key in keys:
        record = installed[key]
        remove_game(cfg, record, dry_run=args.dry_run)
        touched.add(ws.record_scope(record))
        if not args.dry_run:
            del installed[key]
    if args.dry_run:
        return 0
    ws.save_state(state)
    grouped = ws.records_by_scope(installed)
    for system in touched:
        merge_gamelist(cfg, system, grouped.get(system, []))
    return 0


def cmd_config(cfg, args):
    if args.write:
        if os.path.exists(args.config) and not args.force:
            die(f"{args.config} already exists (use --force to overwrite)")
        ws.write_json(args.config, DEFAULT_CONFIG)
        log(f"wrote default config to {args.config}")
        return 0
    # A felismert architektura nem a configbol jon, de az asset-valasztast
    # eldonti, ezert itt is lathatonak kell lennie.
    print(json.dumps({"detected_arch": ws.machine(), **cfg}, indent=2, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.argv[0]) or "store.py",
        description="Install WarpEngine catalog releases into Batocera.",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help=f"store config file (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--base-url", help="override the catalog base URL")
    parser.add_argument("--roms-root", help="override the ROMs root (default: /userdata/roms)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="report actions, change nothing")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version",
                        version=f"{VERSION} (warpstore {ws.VERSION})")
    sub = parser.add_subparsers(dest="command")

    p_sync = sub.add_parser("sync", help="download new/updated games and refresh gamelists")
    p_sync.add_argument("name", nargs="*", help="limit to these software names")
    p_sync.add_argument("--no-restart", action="store_true", help="never restart EmulationStation")
    p_sync.add_argument("--force", action="store_true", help="rewrite gamelists even without changes")
    p_sync.set_defaults(func=cmd_sync)

    p_es = sub.add_parser("sync-from-es", help="sync when launched from the Ports menu")
    p_es.add_argument("--no-restart", action="store_true")
    p_es.set_defaults(func=cmd_sync_from_es)

    p_apply = sub.add_parser("apply-gamelists", help="rewrite gamelists from the local state")
    p_apply.add_argument("--wait-pid", type=int, help="wait for this pid to exit first")
    p_apply.set_defaults(func=cmd_apply_gamelists)

    p_list = sub.add_parser("list", help="show compatible catalog entries")
    p_list.set_defaults(func=cmd_list)

    p_rm = sub.add_parser("remove", help="uninstall games (name or system:name)")
    p_rm.add_argument("name", nargs="+")
    p_rm.set_defaults(func=cmd_remove)

    p_cfg = sub.add_parser("config", help="print the effective config")
    p_cfg.add_argument("--write", action="store_true", help="write a template config file")
    p_cfg.add_argument("--force", action="store_true")
    p_cfg.set_defaults(func=cmd_config)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    ws.set_verbose(args.verbose)
    ws.init(args.config)
    cfg = ws.load_config(
        args.config, DEFAULT_CONFIG,
        overrides={"store.base_url": args.base_url, "paths.roms_root": args.roms_root},
        required=("paths.subfolder",),
    )
    os.makedirs(ws.HOME, exist_ok=True)
    return args.func(cfg, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
