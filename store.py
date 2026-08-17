#!/usr/bin/env python3
"""store.py — WarpEngine catalog client for Batocera.

Reads a WarpEngine catalog API (`GET /api/software`), keeps the entries whose
platform maps to a system this Batocera box actually has, downloads the matching
release asset into that system's ROM folder and writes EmulationStation
metadata (title, description, author, box art) into its gamelist.xml.

The engine knows nothing about any particular site: which host to talk to, what
the store is called and where its games land all come from `config.json`. A
store repository is therefore just that config plus an installer — see README.

Standard library only — Batocera ships python3 but no pip packages.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

VERSION = "2.0.0"
STATE_VERSION = 2

# Every store keeps its code, config, state and log in one directory, so a box
# can carry several stores side by side without them treading on each other.
# The installer exports BATOCERA_STORE_HOME; run straight from a checkout the
# script's own directory is the store home.
DEFAULT_HOME = os.environ.get("BATOCERA_STORE_HOME") or os.path.dirname(os.path.abspath(__file__))

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


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

VERBOSE = False
# Log prefix and temp-file prefix; replaced with the store id once configured.
TAG = "batocera-store"

HOME = DEFAULT_HOME
CONFIG_PATH = os.path.join(HOME, "config.json")
STATE_PATH = os.path.join(HOME, "state.json")
CATALOG_CACHE = os.path.join(HOME, "catalog.json")


def set_home(config_path):
    """Anchor state and cache next to the config file that selected this store."""
    global HOME, CONFIG_PATH, STATE_PATH, CATALOG_CACHE
    CONFIG_PATH = os.path.abspath(config_path)
    HOME = os.path.dirname(CONFIG_PATH)
    STATE_PATH = os.path.join(HOME, "state.json")
    CATALOG_CACHE = os.path.join(HOME, "catalog.json")


def log(msg):
    print(f"[{TAG}] {msg}", flush=True)


def debug(msg):
    if VERBOSE:
        log(msg)


def die(msg, code=1):
    print(f"[{TAG}] error: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log(f"warning: cannot read {path}: {exc}")
        return default


def write_json(path, data):
    write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))


def write_atomic(path, blob):
    """Write bytes to `path` via a temp file in the same dir, then rename."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{TAG}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def deep_merge(base, overlay):
    """Recursively merge `overlay` into a copy of `base`; dicts merge, rest wins."""
    merged = dict(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path, overrides=None):
    global TAG

    user = load_json(path, default=None)
    if user is None:
        log(f"warning: no config at {path} — falling back to the built-in defaults")
    cfg = deep_merge(json.loads(json.dumps(DEFAULT_CONFIG)), user or {})

    for key, value in (overrides or {}).items():
        if value is None:
            continue
        section, _, leaf = key.partition(".")
        cfg[section][leaf] = value

    cfg["store"]["base_url"] = str(cfg["store"]["base_url"]).rstrip("/")
    if not cfg["store"].get("id"):
        die("config: store.id is required")
    if not cfg["paths"].get("subfolder"):
        die("config: paths.subfolder is required — it is what keeps stores apart")
    TAG = f"{cfg['store']['id']}-store"
    return cfg


# Shorthands for the values the code below reaches for constantly.
def base_url(cfg):
    return cfg["store"]["base_url"]


def subfolder(cfg):
    return cfg["paths"]["subfolder"]


def roms_root(cfg):
    return cfg["paths"]["roms_root"]


def api_url(cfg, endpoint, params=None):
    path = cfg["store"]["api"][endpoint]
    url = base_url(cfg) + "/" + path.lstrip("/")
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def user_agent(cfg):
    return f"batocera-store/{VERSION} ({cfg['store']['id']})"


def _opener(cfg):
    if cfg["behavior"].get("insecure"):
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener()


def http_get(cfg, url):
    """Return (body_bytes, content_type)."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(cfg)})
    with _opener(cfg).open(req, timeout=cfg["behavior"]["timeout"]) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def http_download(cfg, url, dest):
    """Stream `url` into `dest` atomically. Returns bytes written."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(cfg)})
    directory = os.path.dirname(dest) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{TAG}-", suffix=".part")
    written = 0
    try:
        with _opener(cfg).open(req, timeout=cfg["behavior"]["timeout"]) as resp, os.fdopen(fd, "wb") as out:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
        if written == 0:
            raise IOError("empty response")
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return written


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------


def fetch_catalog(cfg, use_cache=False):
    owner = cfg["catalog"].get("owner_id")
    url = api_url(cfg, "catalog", {"owner_id": owner} if owner is not None else None)
    try:
        body, _ = http_get(cfg, url)
        data = json.loads(body.decode("utf-8"))
        write_json(CATALOG_CACHE, data)
        return data
    except (urllib.error.URLError, OSError, ValueError) as exc:
        cached = load_json(CATALOG_CACHE) if use_cache else None
        if cached is not None:
            log(f"warning: catalog fetch failed ({exc}) — using cached copy")
            return cached
        die(f"cannot fetch catalog from {url}: {exc}")


def machine():
    """A gep architekturaja, `uname -m` szerint, normalizalt neven.

    A gazdaplatform ugyanaz (Batocera), a gep viszont nem: x86_64 mini-PC,
    Raspberry Pi es kezikonzol egyaránt. Egy x86_64 binaris ARM-on feltelepul,
    de nem indul el, ezért a kiadasvalasztasnak tudnia kell, min fut.
    """
    m = (platform.machine() or "").lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    if m.startswith("armv") or m == "arm":
        return "armhf"
    if m in ("i386", "i486", "i586", "i686"):
        return "x86"
    return m or "unknown"


def resolve_for_arch(value, arch):
    """Egy config-erteket architekturara oldunk fel.

    A `kind` es az `ext` lehet egyszeru string (minden gepen ugyanaz — igy
    mukodik a cartridge), vagy architektura -> ertek leképezes. Az utóbbinal
    a `"*"` kulcs szolgal tartaleknak.
    """
    if not isinstance(value, dict):
        return value
    return value.get(arch, value.get("*"))


def asset_basename(path):
    """`/file/blessingofra-2.0.0.prg` -> `blessingofra-2.0.0.prg`."""
    return os.path.basename(path.rstrip("/"))


def pick_release(entry, kind, ext):
    """Newest non-dev release that carries the asset kind we need.

    `releases` comes back from the API already sorted newest-first, and
    `latestRelease` is the newest stable one — check that first, then walk the
    rest so a game whose newest build is missing the asset still installs.
    """
    candidates = []
    latest = entry.get("latestRelease")
    if latest:
        candidates.append(latest)
    candidates.extend(entry.get("releases") or [])

    seen = set()
    for release in candidates:
        rid = release.get("id")
        if rid in seen:
            continue
        seen.add(rid)
        if str(release.get("version", "")).startswith("dev-"):
            continue
        for asset in release.get("assets") or []:
            if asset.get("kind") != kind:
                continue
            name = asset_basename(asset.get("path", ""))
            if not name or (ext and not name.lower().endswith(ext.lower())):
                continue
            return release, name
    return None, None


def select_games(cfg, catalog, available_systems, arch=None):
    """Catalog -> list of installable game dicts, plus a list of skip reasons."""
    arch = arch or machine()
    statuses = {s.lower() for s in cfg["catalog"].get("statuses") or []}
    only = {n.lower() for n in cfg["catalog"].get("only") or []}
    exclude = {n.lower() for n in cfg["catalog"].get("exclude") or []}

    games, skipped = [], []
    for entry in catalog.get("softwares") or []:
        sw = entry.get("software") or {}
        name = sw.get("name")
        if not name:
            continue

        spec = cfg["platforms"].get(sw.get("platform"))
        if not spec or not spec.get("enabled", True):
            continue
        system = spec["system"]
        if system not in available_systems:
            skipped.append(f"{name}: no '{system}' ROM folder on this box")
            continue
        if statuses and str(sw.get("status", "")).lower() not in statuses:
            continue
        if only and name.lower() not in only:
            continue
        if name.lower() in exclude:
            continue

        kind = resolve_for_arch(spec["kind"], arch)
        if not kind:
            skipped.append(f"{name}: {sw.get('platform')} has no asset kind for {arch}")
            continue
        ext = resolve_for_arch(spec.get("ext", ""), arch) or ""

        release, file_name = pick_release(entry, kind, ext)
        if not release:
            skipped.append(f"{name}: no '{kind}' asset in any release")
            continue

        games.append(
            {
                "name": name,
                "system": system,
                "platform": sw.get("platform"),
                "version": str(release.get("version", "")),
                "asset": file_name,
                "title": sw.get("title") or name,
                "desc": (sw.get("desc") or "").strip(),
                "author": (sw.get("author") or "").strip(),
                "image_url": sw.get("imageUrl"),
                "releasedate": es_date(release.get("createdAt")),
            }
        )
    return games, skipped


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


def install_game(cfg, game, state_entry, dry_run=False):
    """Download the asset + box art if missing. Returns (record, changed)."""
    base = system_dir(cfg, game["system"])
    rom_rel = rel_rom_path(cfg, game)
    rom_abs = os.path.join(base, rom_rel)
    changed = False

    if os.path.isfile(rom_abs) and os.path.getsize(rom_abs) > 0:
        debug(f"{game['name']}: {game['asset']} already present")
    else:
        url = api_url(cfg, "download", {"path": game["asset"]})
        if dry_run:
            log(f"would download {game['name']} {game['version']} -> {rom_abs}")
        else:
            size = http_download(cfg, url, rom_abs)
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
    return record, changed


def image_url(cfg, game):
    """The catalog gives a server-relative `imageUrl`; absolute ones pass through."""
    url = game["image_url"]
    return url if url.startswith(("http://", "https://")) else base_url(cfg) + url


def fetch_image(cfg, game, base):
    try:
        body, ctype = http_get(cfg, image_url(cfg, game))
    except (urllib.error.URLError, OSError) as exc:
        log(f"warning: box art for {game['name']} failed: {exc}")
        return None
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(
        ctype.split(";")[0].strip().lower(), ".png"
    )
    rel = rel_image_path(cfg, game, ext)
    write_atomic(os.path.join(base, rel), body)
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
        if record.get("releasedate"):
            ET.SubElement(game, "releasedate").text = record["releasedate"]

    indent(root)
    blob = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
    if dry_run:
        log(f"would write {path} ({len(records)} entries)")
        return
    write_atomic(path, blob)
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
# state
# --------------------------------------------------------------------------


def game_key(record):
    """Two systems may both carry a game called `foo` — key on both."""
    return f"{record['system']}:{record['name']}"


def load_state():
    state = load_json(STATE_PATH, default=None) or {}
    version = state.get("version")
    if version == 1:
        state = migrate_state_v1(state)
    elif version != STATE_VERSION:
        state = {"version": STATE_VERSION, "installed": {}}
    state.setdefault("installed", {})
    return state


def migrate_state_v1(state):
    """v1 keyed `installed` by bare software name; v2 keys by `system:name`."""
    installed = {}
    for record in (state.get("installed") or {}).values():
        if record.get("system") and record.get("name"):
            installed[game_key(record)] = record
    log(f"migrated {len(installed)} state entries to the per-system key format")
    return {"version": STATE_VERSION, "installed": installed}


def save_state(state):
    state["version"] = STATE_VERSION
    write_json(STATE_PATH, state)


def records_by_system(installed):
    grouped = {}
    for record in installed.values():
        grouped.setdefault(record["system"], []).append(record)
    return grouped


def match_keys(installed, names):
    """Resolve user-typed `name` or `system:name` arguments to state keys."""
    wanted = {n.lower() for n in names}
    return [
        key for key, record in installed.items()
        if key.lower() in wanted or record["name"].lower() in wanted
    ]


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

    catalog = fetch_catalog(cfg, use_cache=True)
    games, skipped = select_games(cfg, catalog, systems)
    for reason in skipped:
        log(f"skipped {reason}")

    if names:
        wanted = {n.lower() for n in names}
        games = [g for g in games if g["name"].lower() in wanted or game_key(g).lower() in wanted]

    state = load_state()
    installed = state["installed"]
    touched, changed = set(), False

    for game in games:
        key = game_key(game)
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
        keep = {game_key(g) for g in games}
        for key in list(installed):
            if key in keep:
                continue
            log(f"pruning {key} (no longer in the catalog or filtered out)")
            remove_game(cfg, installed[key], dry_run=dry_run)
            touched.add(installed[key]["system"])
            del installed[key]
            changed = True

    if not dry_run:
        save_state(state)
    return state, changed, touched


def cmd_sync(cfg, args):
    state, changed, touched = perform_sync(cfg, args.name, dry_run=args.dry_run)
    if args.dry_run:
        log("dry run — nothing written")
        return 0
    if not changed and not args.force:
        log("already up to date")
        return 0

    grouped = records_by_system(state["installed"])
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
            for system, records in records_by_system(state["installed"]).items():
                merge_gamelist(cfg, system, records)
        return 0

    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__),
         "--config", CONFIG_PATH, "apply-gamelists", "--wait-pid", str(pid)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    if cfg["emulationstation"].get("restart") and not args.no_restart:
        restart_es()
    return 0


def cmd_apply_gamelists(cfg, args):
    if args.wait_pid:
        if not wait_for_pid(args.wait_pid, timeout=90):
            log(f"warning: EmulationStation (pid {args.wait_pid}) still running — writing anyway")
    state = load_state()
    grouped = records_by_system(state["installed"])
    if not grouped:
        log("nothing installed")
        return 0
    for system, records in grouped.items():
        merge_gamelist(cfg, system, records, dry_run=args.dry_run)
    return 0


def cmd_list(cfg, args):
    systems = detect_systems(cfg)
    catalog = fetch_catalog(cfg, use_cache=True)
    arch = machine()
    log(f"architecture: {arch}")
    games, skipped = select_games(cfg, catalog, systems, arch)
    installed = load_state()["installed"]

    if not games:
        log("no compatible games in the catalog")
    for game in sorted(games, key=lambda g: (g["system"], g["title"].lower())):
        record = installed.get(game_key(game))
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
    state = load_state()
    installed = state["installed"]
    touched = set()
    keys = match_keys(installed, args.name)
    if not keys:
        log(f"not installed: {', '.join(args.name)}")
        return 0
    for key in keys:
        record = installed[key]
        remove_game(cfg, record, dry_run=args.dry_run)
        touched.add(record["system"])
        if not args.dry_run:
            del installed[key]
    if args.dry_run:
        return 0
    save_state(state)
    grouped = records_by_system(installed)
    for system in touched:
        merge_gamelist(cfg, system, grouped.get(system, []))
    return 0


def cmd_config(cfg, args):
    if args.write:
        if os.path.exists(args.config) and not args.force:
            die(f"{args.config} already exists (use --force to overwrite)")
        write_json(args.config, DEFAULT_CONFIG)
        log(f"wrote default config to {args.config}")
        return 0
    # A felismert architektura nem a configbol jon, de az asset-valasztast
    # eldonti, ezert itt is lathatonak kell lennie.
    print(json.dumps({"detected_arch": machine(), **cfg}, indent=2, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------


def main(argv=None):
    global VERBOSE

    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.argv[0]) or "store.py",
        description="Install WarpEngine catalog releases into Batocera.",
    )
    parser.add_argument("--config", default=CONFIG_PATH,
                        help=f"store config file (default: {CONFIG_PATH})")
    parser.add_argument("--base-url", help="override the catalog base URL")
    parser.add_argument("--roms-root", help="override the ROMs root (default: /userdata/roms)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="report actions, change nothing")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=VERSION)
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

    VERBOSE = args.verbose
    set_home(args.config)
    cfg = load_config(args.config, {"store.base_url": args.base_url, "paths.roms_root": args.roms_root})
    os.makedirs(HOME, exist_ok=True)
    return args.func(cfg, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
