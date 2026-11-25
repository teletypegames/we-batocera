#!/usr/bin/env python3
"""store.py — the Batocera adapter of the WarpEngine store engine.

Reads a WarpEngine catalog (`GET /api/software`), keeps the entries whose
platform maps to a system this Batocera box can run, downloads the matching
release asset and writes EmulationStation metadata (title, description, author,
box art) into a gamelist.xml.

Where the games land is the store's choice, `emulationstation.menu.mode`:

  system   the store gets its own EmulationStation menu entry. Its games live in
           `<roms_root>/<subfolder>/<system>/`, and an `es_systems_<id>.cfg`
           declares one ES system per platform, all grouped under the store's
           name — so the box shows one entry holding a folder per platform.
  merge    the games go into the box's own systems, under `<system>/<subfolder>/`,
           and are merged into the box's gamelists. What the engine did before
           the menu entry existed.

The engine knows nothing about any particular site: which host to talk to, what
the store is called and where its games land all come from `config.json`. A
WarpEngine catalog serves that config and an installer — see README.

Everything that is not specific to Batocera — the catalog, release selection,
architecture matching, state, HTTP — lives in `warpstore.py`, the module this
engine shares with the other store engines. The installer puts the two files
side by side.

Standard library only — Batocera ships python3 but no pip packages.
"""

import argparse
import errno
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
             "(https://git.teletypegames.org/engines/we-core)")
from warpstore import debug, die, log

VERSION = "4.5.1"

# EmulationStation's own files. The user config dir is where our es_systems
# overlay goes; the shipped es_systems.cfg is what we read a system's launch
# command out of, so that nothing about emulators is hardcoded here.
ES_CONFIG_DIR = "/userdata/system/configs/emulationstation"
ES_SYSTEMS_CFG = "/usr/share/emulationstation/es_systems.cfg"

# Batocera's root is a RAM overlay, so a file written under /usr is gone after a
# reboot. /boot/postshare.sh is the one boot hook that runs before
# EmulationStation — every other user hook starts after it has loaded its themes.
VOLATILE_ROOT = "/usr/"
POSTSHARE = "/boot/postshare.sh"

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
        # Everything we install lives under this subfolder. In "system" mode it
        # is the store's own ROM folder, `<roms_root>/<subfolder>/<system>`; in
        # "merge" mode it is a subfolder of the box's own system folders,
        # `<roms_root>/<system>/<subfolder>`. Either way nothing outside it is
        # ever touched, so a prune cannot reach the user's own ROMs — nor
        # another store's games.
        "subfolder": "warp",
    },
    "emulationstation": {
        "menu": {
            # "system": the store gets its own ES menu entry (see the module
            # docstring). "merge": install into the box's own systems.
            "mode": "system",
            # Label of that menu entry; null means store.name.
            "name": None,
            # Theme folder the entry takes its logo from. Null means: the
            # store's own name when it ships a logo (see `logo`), and "ports"
            # otherwise — a folder every Batocera theme has, so the entry has
            # *some* icon rather than none.
            "theme": None,
            # The store's own carousel logo: a path or URL, or a list of them
            # (one per format — a theme may look for .png, another for .svg).
            # EmulationStation has no place of its own for this, so the file is
            # installed into the logo folder of every theme on the box; a theme
            # we cannot reach shows the store's name as text instead.
            "logo": None,
            # An "Update <store>" entry inside the menu, so the sync can be
            # started from the same place the games are.
            "updater": True,
            # Per-system folder labels inside the entry; a system that is not
            # listed keeps the fullname the box's own es_systems.cfg gives it.
            "labels": {},
        },
        # A Ports entry that runs the sync. Redundant once the store has its own
        # menu entry with an updater in it, so "system" mode defaults to false.
        "ports_entry": None,
        # Display name of our subfolder in the box's gamelists ("merge" mode
        # only); null hides the folder node.
        "folder_name": "WarpEngine Store",
        # Restart EmulationStation after a sync that changed something.
        "restart": True,
        # Where ES keeps its user config; null means the Batocera default.
        "config_dir": None,
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
        "c64": {"system": "c64", "kind": "cartridge", "ext": "", "enabled": True},
        "plus4": {"system": "cplus4", "kind": "cartridge", "ext": "", "enabled": True},
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


def menu(cfg):
    return cfg["emulationstation"]["menu"]


def es_config_dir(cfg):
    return (os.environ.get("BATOCERA_ES_CONFIG_DIR")
            or cfg["emulationstation"].get("config_dir")
            or ES_CONFIG_DIR)


def es_systems_cfg(cfg):
    return os.environ.get("BATOCERA_ES_SYSTEMS_CFG") or ES_SYSTEMS_CFG


def menu_theme(cfg):
    """The theme folder the menu entry uses — and the name of its logo file.

    With a logo of our own it has to be a name of ours, or installing it would
    overwrite the theme's own artwork for whatever folder we borrowed.
    """
    explicit = menu(cfg).get("theme")
    if explicit:
        return explicit
    return cfg["store"]["id"] if logo_sources(cfg) else "ports"


def logo_sources(cfg):
    value = menu(cfg).get("logo")
    if not value:
        return []
    return [value] if isinstance(value, str) else list(value)


def wants_ports_entry(cfg):
    """A Ports entry for the sync — off by default once the store has a menu."""
    want = cfg["emulationstation"].get("ports_entry")
    if want is None:
        return isinstance(layout(cfg), MergeLayout)
    return bool(want)


# --------------------------------------------------------------------------
# where the files go
# --------------------------------------------------------------------------


class Layout:
    """Where this store's files live, and what it is allowed to delete.

    Two answers, one per `emulationstation.menu.mode`, and every path in the
    engine goes through one of them: the install, the removal, the gamelist and
    the empty-directory sweep all ask the layout rather than build paths of
    their own.
    """

    def __init__(self, cfg):
        self.cfg = cfg

    def base(self, system):
        """The directory this system's content lives in."""
        raise NotImplementedError

    def rel_rom(self, game):
        return game["asset"]

    def rel_image(self, game, ext):
        return f"images/{game['name']}{ext}"

    def rel_launcher(self, game):
        return f"{game['name']}.sh"

    def rel_data(self, game):
        # The leading dot is what hides the unpacked program from ES; only the
        # `.sh` launcher next to it is meant to be listed as a game.
        return f".data/{game['name']}"

    def gamelist(self, system):
        return os.path.join(self.base(system), "gamelist.xml")

    def owns(self, rel):
        """May we delete this path (relative to `base`)? The prune's safety net.

        Everything under the store's own folder is ours; what is refused is a
        path that would climb out of it, however it is spelled.
        """
        if os.path.isabs(rel):
            return False
        normalized = os.path.normpath(rel)
        return normalized != ".." and not normalized.startswith(".." + os.sep)

    def claims(self, rel, wanted, base):
        """Is this existing gamelist node ours to rewrite? (see merge_gamelist)"""
        raise NotImplementedError

    def purge_dirs(self, system):
        """Directories to try removing once the games are gone, deepest first."""
        return []

    # Only the box's own gamelists are worth backing up, and only ours is worth
    # deleting when it ends up empty.
    backup_gamelists = True
    folder_node = False


class MergeLayout(Layout):
    """`<roms_root>/<system>/<subfolder>` — inside the box's own systems.

    Shared ground: the ROMs sit next to the user's, the gamelist is the box's,
    and another store may keep its own games in the same folders. Hence the
    subfolder prefix on everything and the `owns` check before every delete.
    """

    backup_gamelists = True
    folder_node = True

    def base(self, system):
        return os.path.join(roms_root(self.cfg), system)

    def rel_rom(self, game):
        return f"{subfolder(self.cfg)}/{game['asset']}"

    def rel_image(self, game, ext):
        return f"{subfolder(self.cfg)}/images/{game['name']}{ext}"

    def rel_launcher(self, game):
        return f"{subfolder(self.cfg)}/{game['name']}.sh"

    def rel_data(self, game):
        return f".data/{subfolder(self.cfg)}/{game['name']}"

    def owns(self, rel):
        prefix = subfolder(self.cfg) + "/"
        return rel.startswith(prefix) or rel.startswith(f".data/{prefix}")

    def claims(self, rel, wanted, base):
        return rel.startswith(subfolder(self.cfg) + "/") or rel == subfolder(self.cfg)

    def purge_dirs(self, system):
        base = self.base(system)
        return [os.path.join(base, subfolder(self.cfg), "images"),
                os.path.join(base, subfolder(self.cfg)),
                # `.data` itself stays: it is shared ground, another store may
                # still be keeping a payload there.
                os.path.join(base, ".data", subfolder(self.cfg))]


class SystemLayout(Layout):
    """`<roms_root>/<subfolder>/<system>` — the store's own ROM folder.

    Every file under it is ours, including the gamelists, so there is nothing to
    back up and nothing to merge around. What keeps the user's own files safe
    here is that we only ever delete what `state.json` says we installed.
    """

    backup_gamelists = False
    folder_node = False

    def store_root(self):
        return os.path.join(roms_root(self.cfg), subfolder(self.cfg))

    def base(self, system):
        return os.path.join(self.store_root(), system)

    def claims(self, rel, wanted, base):
        if not rel or rel in wanted:
            return True
        # A leftover of ours points at a file that is not there any more; a ROM
        # the user added themselves does exist, and keeps its entry.
        return not os.path.exists(os.path.join(base, rel))

    def purge_dirs(self, system):
        base = self.base(system)
        return [os.path.join(base, "images"), os.path.join(base, ".data"), base]


def layout_name(lay):
    return "merge" if isinstance(lay, MergeLayout) else "system"


def layout_named(cfg, name):
    return MergeLayout(cfg) if name == "merge" else SystemLayout(cfg)


_LAYOUT = None


def layout(cfg):
    global _LAYOUT
    if _LAYOUT is None or _LAYOUT.cfg is not cfg:
        mode = (menu(cfg).get("mode") or "system").lower()
        _LAYOUT = MergeLayout(cfg) if mode == "merge" else SystemLayout(cfg)
    return _LAYOUT


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
            raise ws.Skip(f"this box has no '{system}' system")
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
    return layout(cfg).base(system)


def box_systems(cfg):
    """The box's own system definitions, by name — or None if unreadable.

    This one file answers both questions the engine has about the machine: which
    systems it can run, and how it launches them. A system is in there because
    an emulator for it was built into this particular Batocera image, which is a
    truer compatibility test than the presence of a ROM folder.
    """
    path = es_systems_cfg(cfg)
    if not os.path.isfile(path):
        debug(f"no {path} — falling back to the ROM folders")
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        log(f"warning: {path} is not valid XML ({exc}) — falling back to the ROM folders")
        return None
    nodes = {}
    for node in root.findall("system"):
        name = (node.findtext("name") or "").strip()
        if name:
            nodes[name] = node
    return nodes or None


def detect_systems(cfg):
    """Systems this box can run."""
    nodes = box_systems(cfg)
    if nodes:
        return set(nodes)
    root = roms_root(cfg)
    if not os.path.isdir(root):
        return set()
    return {d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))}


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
    lay = layout(cfg)
    base = lay.base(game["system"])
    launcher_rel = lay.rel_launcher(game)
    data_rel = lay.rel_data(game)
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
    lay = layout(cfg)
    base = lay.base(game["system"])
    changed = False
    data_rel = None

    if game.get("install") == "port":
        # A port ket dologbol all: egy indito .sh es a kicsomagolt payload.
        rom_rel = lay.rel_launcher(game)
        data_rel = lay.rel_data(game)
        installed = (os.path.isfile(os.path.join(base, rom_rel))
                     and os.path.isdir(os.path.join(base, data_rel))
                     and state_entry and state_entry.get("asset") == game["asset"])
        if installed:
            debug(f"{game['name']}: port already installed")
        else:
            rom_rel, data_rel = install_port(cfg, game, dry_run=dry_run)
            changed = True
    else:
        rom_rel = lay.rel_rom(game)
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
                image_rel = lay.rel_image(game, ".png")
            else:
                image_rel = fetch_image(cfg, game, base)
            changed = changed or bool(image_rel)
    else:
        # Box art disappeared from the catalog: drop the file we cached.
        if image_rel and not dry_run:
            stale = os.path.join(base, image_rel)
            if lay.owns(image_rel) and os.path.isfile(stale):
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
    rel = layout(cfg).rel_image(game, ext)
    ws.write_atomic(os.path.join(base, rel), body)
    debug(f"{game['name']}: box art -> {rel}")
    return rel


def remove_game(cfg, record, dry_run=False, lay=None):
    """Delete one installed game's files. `lay` is for the migration, which has
    to remove what a *previous* layout installed."""
    lay = lay or layout(cfg)
    base = lay.base(record["system"])
    for rel in (record.get("rom"), record.get("image")):
        if not rel:
            continue
        path = os.path.join(base, rel)
        # Never step outside what this layout says is ours — in merge mode the
        # folder is shared with the user and possibly with another store.
        if not lay.owns(rel):
            log(f"warning: refusing to delete {path} (not ours)")
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
        if not lay.owns(data_rel):
            log(f"warning: refusing to delete {data_abs} (not ours)")
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
    return layout(cfg).gamelist(system)


def normalize_path(text):
    text = (text or "").strip()
    return text[2:] if text.startswith("./") else text


# The tags we write ourselves, from the catalog. Everything else in a <game>
# node was put there by EmulationStation — play counts, favourites, the last
# time it was launched — and has to survive a rewrite.
OUR_TAGS = ("path", "name", "desc", "image", "thumbnail", "developer", "publisher",
            "releasedate")


def merge_gamelist(cfg, system, records, dry_run=False, backup=True, lay=None):
    """Rewrite our <game>/<folder> nodes, and only ours.

    In merge mode "ours" means everything under the store's subfolder: the rest
    of the file is the box's — the user's own scraped ROMs, their play counts and
    favourites, and whatever another store installed under its own subfolder.

    In system mode the file is in the store's own folder, so the rule is
    different: a node is ours if we are writing that game now, or if the file it
    points at is gone (a leftover of ours). A ROM the user dropped into the
    folder themselves keeps its entry.

    Either way, the tags EmulationStation owns are carried over onto the new
    node — a favourite stays a favourite across a sync.

    `backup=False` is for the uninstall: by then any copy worth having was made
    on the first sync, and backing up again would only preserve our own entries.
    """
    lay = lay or layout(cfg)
    path = lay.gamelist(system)
    base = lay.base(system)
    wanted = {record["rom"]: record for record in records}
    if not records and not os.path.isfile(path):
        return

    if os.path.isfile(path):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            log(f"warning: {path} is not valid XML ({exc}) — starting a fresh gamelist")
            root = ET.Element("gameList")
        else:
            backup_path = f"{path}.{cfg['store']['id']}-backup"
            if backup and lay.backup_gamelists and not os.path.exists(backup_path) and not dry_run:
                shutil.copy2(path, backup_path)
    else:
        root = ET.Element("gameList")

    # What ES added to the nodes we are about to rewrite, kept aside by path.
    carried = {}
    for node in list(root):
        target = normalize_path(node.findtext("path"))
        if not lay.claims(target, wanted, base):
            continue
        if target in wanted:
            carried[target] = [child for child in node if child.tag not in OUR_TAGS]
        root.remove(node)

    folder_name = cfg["emulationstation"].get("folder_name")
    if records and lay.folder_node and folder_name:
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
        for child in carried.get(record["rom"], []):
            game.append(child)

    indent(root)
    blob = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            if fh.read() == blob:
                debug(f"{path} is up to date")
                return
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
# the store's own menu entry
# --------------------------------------------------------------------------

# The folder that holds the updater, and — because a menu entry has to be a
# system, and a system with no games of its own is dropped by ES — the folder
# the menu entry itself points at. Not a catalog platform, so it can never
# collide with one.
STORE_SYSTEM = "store"


def es_parent_name(cfg, box=None):
    """The ES system name of the menu entry itself.

    The store id — unless the box already has a system by that name. An overlay
    whose `<name>` matches an existing system *modifies that system* instead of
    adding one, and a store that called itself `nes` must not rewrite the box's
    NES.
    """
    name = cfg["store"]["id"]
    if box and name in box:
        name = f"{name}-store"
        log(f"'{cfg['store']['id']}' is already a system on this box — "
            f"the menu entry is called '{name}'")
    return name


def source_system(system):
    """The box system one of our systems borrows its launcher from."""
    return "ports" if system == STORE_SYSTEM else system


def system_label(cfg, system, src):
    labels = menu(cfg).get("labels") or {}
    if system in labels:
        return labels[system]
    fullname = (src.findtext("fullname") or "").strip() if src is not None else ""
    return fullname or system


def configured_exts(cfg, system):
    """The extensions our own config expects for a system — the last resort when
    the box's system definition has none to copy."""
    exts = set()
    for spec in (cfg.get("platforms") or {}).values():
        if spec.get("system") != system:
            continue
        ext = spec.get("ext")
        for value in (ext.values() if isinstance(ext, dict) else [ext]):
            if value:
                exts.add(value)
    return " ".join(sorted(exts))


def text_node(parent, tag, value):
    node = ET.SubElement(parent, tag)
    node.text = value
    return node


def derive_child(cfg, parent, system, src):
    """One `<system>` of ours, borrowing everything about emulators from the box.

    `%SYSTEM%` is resolved here rather than left to EmulationStation: ES replaces
    it with the name of the system being launched (`FileData.cpp`), which for us
    would be `ttg-c64` — a name configgen has never heard of. The box's own
    system name goes in instead, so a game of ours is launched exactly as the
    box would launch it, with the emulator the user configured for that system.
    """
    src_name = source_system(system)
    node = ET.Element("system")
    text_node(node, "name", f"{parent}-{system}")
    text_node(node, "fullname", system_label(cfg, system, src))
    text_node(node, "path", layout(cfg).base(system))
    text_node(node, "extension",
              (src.findtext("extension") or "").strip() or configured_exts(cfg, system))
    text_node(node, "command", (src.findtext("command") or "").replace("%SYSTEM%", src_name))
    for tag in ("platform", "theme", "manufacturer", "release", "hardware"):
        value = (src.findtext(tag) or "").strip()
        if value:
            text_node(node, tag, value)
    # This is what puts the system inside the store's menu entry instead of
    # giving it one of its own.
    text_node(node, "group", parent)
    emulators = src.find("emulators")
    if emulators is not None:
        node.append(emulators)
    return node


def build_es_systems(cfg, systems, box):
    """The `<systemList>` for the store: the menu entry, and a system under it
    for every folder that has something in it."""
    lay = layout(cfg)
    parent = es_parent_name(cfg, box)
    root = ET.Element("systemList")

    # The menu entry is a system of its own rather than a group ES invents by
    # itself, for two reasons: it is the only way to give it a `<theme>` (a
    # group ES creates has no system behind it, so it looks for a theme folder
    # named after the group, which no theme has), and with `HideUniqueGroups`
    # on — the default — a group with a single system under it is dissolved
    # unless a system by that name exists.
    #
    # Which means it has to be a system ES will keep, and ES keeps a system
    # only if it holds at least one game (`loadSystem`: `has no games! Ignoring
    # it.`). So the entry is the store folder — the updater in it is that game,
    # and it is where the "Update <store>" item comes from.
    #
    # Its path must also *not* be the directory above the systems'. ES marks a
    # directory it has enumerated with a `<dir>/*` cache entry, and adds the
    # mark **before** reading the entries; from then on any path under it that
    # is not itself cached answers "does not exist" (`FileSystemUtil.cpp`,
    # `getCacheEntry`). Systems load in a thread pool, so an entry sitting above
    # the systems loses that race for a random couple of them, and ES drops
    # those with `System "..." path does not exist !`. `store/` is their
    # sibling, so there is no race to lose.
    src_store = box.get(source_system(STORE_SYSTEM))
    entry = ET.SubElement(root, "system")
    text_node(entry, "name", parent)
    text_node(entry, "fullname", menu(cfg).get("name") or cfg["store"]["name"])
    text_node(entry, "path", lay.base(STORE_SYSTEM))
    if src_store is not None:
        text_node(entry, "extension", (src_store.findtext("extension") or ".sh").strip())
        text_node(entry, "command", (src_store.findtext("command") or "").replace(
            "%SYSTEM%", source_system(STORE_SYSTEM)))
    else:
        # No `ports` system on this box: the entry still appears, but nothing in
        # it can be launched — including the updater.
        log(f"warning: no '{source_system(STORE_SYSTEM)}' system on this box — "
            "the updater cannot be launched from the menu")
        text_node(entry, "extension", ".sh")
        text_node(entry, "command", "true")
    text_node(entry, "platform", "pc")
    text_node(entry, "theme", menu_theme(cfg))
    if src_store is not None:
        emulators = src_store.find("emulators")
        if emulators is not None:
            entry.append(emulators)

    for system in sorted(systems):
        if system == STORE_SYSTEM:
            continue
        src = box.get(source_system(system))
        if src is None:
            log(f"warning: no '{source_system(system)}' system on this box — "
                f"{system} is not added to the menu")
            continue
        root.append(derive_child(cfg, parent, system, src))
    return root


def es_systems_path(cfg):
    return os.path.join(es_config_dir(cfg), f"es_systems_{cfg['store']['id']}.cfg")


def write_es_systems(cfg, systems, dry_run=False):
    """Declare our systems to EmulationStation. True if the file changed.

    ES writes this file never, so unlike a gamelist it can be written while ES
    is running. What it cannot do is take effect without a restart.
    """
    box = box_systems(cfg)
    if not box:
        log(f"warning: cannot read {es_systems_cfg(cfg)} — the store's menu entry was "
            "not written. The games are installed; ES will list them once it can.")
        return False

    root = build_es_systems(cfg, systems, box)
    indent(root)
    blob = b'<?xml version="1.0"?>\n' + ET.tostring(root, encoding="utf-8")
    path = es_systems_path(cfg)
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            if fh.read() == blob:
                debug(f"{path} is up to date")
                return False
    if dry_run:
        log(f"would write {path} ({len(root) - 1} systems)")
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ws.write_atomic(path, blob)
    log(f"EmulationStation systems: {path} ({len(root) - 1} systems)")
    return True


def remove_es_systems(cfg, dry_run=False):
    path = es_systems_path(cfg)
    if not os.path.isfile(path):
        return
    if dry_run:
        log(f"would remove {path}")
        return
    os.unlink(path)
    log(f"removed {path}")


def updater_script(cfg):
    """The updater, as the ES menu launches it.

    A `.sh` in a folder of ours, so it is a game as far as ES is concerned. It
    calls this same engine — not the installer's launcher — so it keeps working
    whatever the store home is called.
    """
    return (
        "#!/bin/bash\n"
        f"# {cfg['store']['name']} — pulls new releases from the catalog.\n"
        f"exec >>{shlex.quote(os.path.join(ws.HOME, 'store.log'))} 2>&1\n"
        'echo "=== $(date -Iseconds) sync started ==="\n'
        f"BATOCERA_STORE_HOME={shlex.quote(ws.HOME)} "
        f"{shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))} "
        f"--config {shlex.quote(ws.CONFIG_PATH)} sync-from-es\n"
        'echo "=== $(date -Iseconds) sync finished (exit $?) ==="\n'
    ).encode("utf-8")


def updater_records(cfg):
    label = menu(cfg).get("name") or cfg["store"]["name"]
    return [{
        "system": STORE_SYSTEM,
        "rom": "update.sh",
        "title": f"Update {label}",
        "desc": "Downloads everything new from the catalog and refreshes this menu.",
    }]


def write_updater(cfg, dry_run=False):
    path = os.path.join(layout(cfg).base(STORE_SYSTEM), "update.sh")
    blob = updater_script(cfg)
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            if fh.read() == blob:
                return
    if dry_run:
        log(f"would write {path}")
        return
    ws.write_atomic(path, blob)
    os.chmod(path, 0o755)
    log(f"updater: {path}")


# --------------------------------------------------------------------------
# the store's logo in the themes
# --------------------------------------------------------------------------

# A theme keeps its system logos in a folder of its own choosing — `art/logos`,
# `_inc/systems/logos`, `_art/Colorlogos`, whatever the author picked. Rather
# than knowing them all, we look for the folder that holds the logos of systems
# every theme has: the one with the most of these in it is the logo folder.
LOGO_PROBES = frozenset((
    "ports", "snes", "nes", "megadrive", "psx", "gba", "n64", "c64", "atari2600",
    "dreamcast", "amiga500", "gb", "gbc", "mame", "pcengine", "saturn", "segacd",
    "tic80", "wii", "3do", "gamegear", "mastersystem",
))
LOGO_EXTS = (".png", ".svg", ".jpg", ".webp")


def theme_dirs(cfg):
    """Every theme installed on the box, from the paths ES looks in."""
    roots = [os.path.join(roms_root(cfg), "..", "themes"),
             os.path.join(es_config_dir(cfg), "themes"),
             "/usr/share/emulationstation/themes"]
    if os.environ.get("BATOCERA_THEMES_DIRS"):
        roots = os.environ["BATOCERA_THEMES_DIRS"].split(os.pathsep)
    found = []
    for root in roots:
        root = os.path.normpath(root)
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if not name.startswith(".") and os.path.isdir(path):
                found.append(path)
    return found


def theme_logo_dir(root):
    """The folder this theme keeps its system logos in, or None."""
    best, best_score = None, 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        names = {os.path.splitext(f)[0].lower() for f in files
                 if os.path.splitext(f)[1].lower() in LOGO_EXTS}
        score = len(names & LOGO_PROBES)
        if score > best_score or (score and score == best_score and len(base) < len(best)):
            best, best_score = base, score
    # A handful of known systems is proof enough; fewer means we found a folder
    # of something else, and a wrong guess would litter the user's theme.
    return best if best_score >= 4 else None


def cached_logos(cfg, dry_run=False):
    """The logo files, in the store home. `{extension: path}`.

    `menu.logo` may be a URL or a path; either way it is copied in once and used
    from there, so a theme installed later does not need the network.
    """
    out = {}
    for source in logo_sources(cfg):
        ext = os.path.splitext(source)[1].lower()
        if ext not in LOGO_EXTS:
            log(f"warning: {source} is not a logo we can install ({', '.join(LOGO_EXTS)})")
            continue
        local = os.path.join(ws.HOME, f"logo{ext}")
        if not os.path.isfile(local):
            if dry_run:
                log(f"would fetch the menu logo {source}")
                continue
            try:
                if source.startswith(("http://", "https://")):
                    ws.http_download(cfg, source, local)
                else:
                    path = source if os.path.isabs(source) else os.path.join(ws.HOME, source)
                    shutil.copyfile(path, local)
                log(f"menu logo: {local}")
            except (urllib.error.URLError, OSError) as exc:
                log(f"warning: cannot read the menu logo {source}: {exc}")
                continue
        out[ext] = local
    return out


def install_logos(cfg, state, dry_run=False):
    """Put the store's logo where each theme looks for a system logo.

    EmulationStation resolves a system's carousel logo through the theme
    (`SystemData::getProperty("image")` asks the theme for `system/logo`), and it
    has no user-level override — so the only way for a custom system to have its
    own artwork is to install the file into the themes. Which is why every path
    written is recorded in `state.json`: an uninstall takes them all back out,
    and a file we did not write is never touched.
    """
    sources = cached_logos(cfg, dry_run=dry_run)
    if not sources:
        return
    name = menu_theme(cfg)
    known = state.setdefault("theme_logo_dirs", {})
    written = state.setdefault("logos", {})
    volatile = []

    for root in theme_dirs(cfg):
        target_dir = known.get(root)
        if not target_dir or not os.path.isdir(target_dir):
            target_dir = theme_logo_dir(root)
            if target_dir and not dry_run:
                known[root] = target_dir
        if not target_dir:
            debug(f"{root}: no system-logo folder found — this theme will show "
                  "the store's name as text")
            continue

        for ext, source in sources.items():
            target = os.path.join(target_dir, name + ext)
            if os.path.exists(target) and target not in written:
                log(f"warning: {target} already exists and is not ours — left alone")
                continue
            size = os.path.getsize(source)
            if os.path.isfile(target) and os.path.getsize(target) == size:
                continue
            if dry_run:
                log(f"would install the logo into {target}")
                continue
            try:
                shutil.copyfile(source, target)
                # The rest of a theme is world-readable; ours should be too.
                os.chmod(target, 0o644)
            except OSError as exc:
                log(f"warning: cannot write {target}: {exc}")
                continue
            written[target] = size
            log(f"logo installed: {target}")
            # Batocera's root is a RAM overlay: a theme that ships with the
            # system image takes the file, but loses it on the next boot.
            if is_volatile(target):
                volatile.append(target)

    if volatile:
        log("note: these are on the read-only system image, so the copy lives in "
            f"Batocera's RAM overlay and is gone after a reboot — {postshare_path()} "
            "puts it back before EmulationStation starts:")
        for path in volatile:
            log(f"  {path}")
    write_postshare(cfg, state, written, dry_run=dry_run)


def remove_logos(cfg, state, dry_run=False):
    written = state.get("logos") or {}
    for target in sorted(written):
        if not os.path.isfile(target):
            continue
        if dry_run:
            log(f"would remove the logo {target}")
        else:
            os.unlink(target)
            log(f"removed the logo {target}")
    write_postshare(cfg, state, {}, dry_run=dry_run)
    if not dry_run:
        state.pop("logos", None)
        state.pop("theme_logo_dirs", None)


def is_volatile(path):
    return path.startswith(os.environ.get("BATOCERA_VOLATILE_ROOT") or VOLATILE_ROOT)


def postshare_path():
    return os.environ.get("BATOCERA_POSTSHARE") or POSTSHARE


def postshare_markers(cfg):
    tag = f"{cfg['store']['id']}-store: theme logos"
    return f"# >>> {tag}", f"# <<< {tag}"


def postshare_block(cfg, targets):
    """The lines that copy the cached logos back into the volatile themes.

    Plain `cp` from the store home — no python, no network, nothing that can hold
    up the boot — and only where the file is missing, so a theme that later ships
    a logo of that name keeps its own.
    """
    begin, end = postshare_markers(cfg)
    lines = [begin,
             "# Written by the store engine; its uninstall takes this block out.",
             'if [ "$1" = "start" ]; then']
    for target in targets:
        source = os.path.join(ws.HOME, "logo" + os.path.splitext(target)[1].lower())
        lines.append(f"  [ -e {shlex.quote(target)} ] || "
                     f"cp {shlex.quote(source)} {shlex.quote(target)} 2>/dev/null || :")
    lines += ["fi", end]
    return "\n".join(lines) + "\n"


def strip_postshare_block(cfg, text):
    begin, end = postshare_markers(cfg)
    out, inside = [], False
    for line in text.splitlines(keepends=True):
        if line.rstrip("\n") == begin:
            inside = True
        elif inside and line.rstrip("\n") == end:
            inside = False
        elif not inside:
            out.append(line)
    return "".join(out)


def write_postshare(cfg, state, logos, dry_run=False):
    """Keep our block in /boot/postshare.sh in step with the volatile logos.

    The file is the user's as much as ours, so only the marked block is touched,
    and it goes right after the shebang: an `exit` in the user's own part must not
    keep it from running. A file we created and left empty goes away again.
    """
    path = postshare_path()
    if not os.path.isdir(os.path.dirname(path)):
        debug(f"no {os.path.dirname(path)} — the logos cannot be restored at boot")
        return
    try:
        with open(path, "r", errors="replace") as fh:
            current = fh.read()
    except FileNotFoundError:
        current = None
    except OSError as exc:
        log(f"warning: cannot read {path}: {exc}")
        return

    targets = sorted(t for t in logos if is_volatile(t))
    rest = strip_postshare_block(cfg, current or "")
    created = bool(state.get("postshare_created"))
    if targets:
        if not rest:
            rest, created = "#!/bin/bash\n", current is None or created
        head, sep, tail = rest.partition("\n")
        if head.startswith("#!"):
            new = head + sep + postshare_block(cfg, targets) + tail
        else:
            new = postshare_block(cfg, targets) + rest
    else:
        new = None if created and rest.strip() in ("", "#!/bin/bash") else rest

    if new == current or (new is None and current is None):
        return
    if dry_run:
        log(f"would {'remove' if new is None else 'update'} the logo hook in {path}")
        return
    if write_boot_file(path, new):
        log(f"{'removed' if new is None else 'updated'} the logo hook in {path}")
        if new is None or not targets:
            state.pop("postshare_created", None)
        elif created:
            state["postshare_created"] = True


def write_boot_file(path, content):
    """Write (or with None, delete) a file on /boot, which Batocera mounts
    read-only — remounted read-write only for as long as the write takes."""
    def apply():
        if content is None:
            os.unlink(path)
        else:
            with open(path, "w") as fh:
                fh.write(content)

    try:
        apply()
        return True
    except OSError as exc:
        if exc.errno != errno.EROFS:
            log(f"warning: cannot write {path}: {exc}")
            return False
    mount = os.path.dirname(path)
    if subprocess.run(["mount", "-o", "remount,rw", mount], check=False).returncode != 0:
        log(f"warning: cannot remount {mount} read-write — the logos will not be "
            "restored at boot")
        return False
    try:
        apply()
        return True
    except OSError as exc:
        log(f"warning: cannot write {path}: {exc}")
        return False
    finally:
        subprocess.run(["sync"], check=False)
        subprocess.run(["mount", "-o", "remount,ro", mount], check=False)


def apply_menu(cfg, systems, dry_run=False, gamelists=True):
    """Write everything the store's menu entry is made of.

    Returns True if EmulationStation has to restart to notice. `gamelists=False`
    is for a sync launched from inside ES, which defers every gamelist write to
    the child that waits for ES to exit.
    """
    lay = layout(cfg)
    if not isinstance(lay, SystemLayout):
        return False

    wanted = set(systems) - {STORE_SYSTEM}
    updater = bool(menu(cfg).get("updater"))
    # ES refuses a system whose path does not exist, so the folders go first —
    # the menu entry's own one (`store/`) included, whether or not the updater
    # will put anything in it.
    if not dry_run:
        os.makedirs(lay.base(STORE_SYSTEM), exist_ok=True)
        for system in sorted(wanted):
            os.makedirs(lay.base(system), exist_ok=True)

    changed = write_es_systems(cfg, wanted, dry_run=dry_run)
    if updater:
        write_updater(cfg, dry_run=dry_run)
        if gamelists:
            merge_gamelist(cfg, STORE_SYSTEM, updater_records(cfg), dry_run=dry_run)

    state = ws.load_state()
    before = dict(state.get("logos") or {})
    install_logos(cfg, state, dry_run=dry_run)
    if not dry_run and (state.get("logos") or {}) != before:
        ws.save_state(state)
        changed = True
    return changed


# --------------------------------------------------------------------------
# migration from the merge layout
# --------------------------------------------------------------------------


def ports_entries(cfg):
    """The Ports scripts that run this store, found by looking inside them.

    The name cannot be derived from the config — the installer may be given
    `BATOCERA_PORT_NAME` to anything — but whatever it is called, the script
    names this store's home. Same trick as `uninstall.sh`.
    """
    ports_dir = os.path.join(roms_root(cfg), "ports")
    if not os.path.isdir(ports_dir):
        return []
    found = []
    for name in sorted(os.listdir(ports_dir)):
        if not name.endswith(".sh"):
            continue
        path = os.path.join(ports_dir, name)
        try:
            with open(path, "r", errors="replace") as fh:
                if ws.HOME and ws.HOME in fh.read():
                    found.append(path)
        except OSError:
            continue
    return found


def drop_ports_gamelist_entry(cfg, script, dry_run=False):
    """Take the Ports entry's own node out of the box's ports gamelist.

    The node is not under our subfolder — the script sat in the Ports root — so
    the gamelist merge leaves it alone, and ES then complains about a game whose
    file is gone on every start.
    """
    path = os.path.join(roms_root(cfg), "ports", "gamelist.xml")
    if not os.path.isfile(path):
        return
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return
    name = os.path.basename(script)
    dropped = [node for node in list(root)
               if normalize_path(node.findtext("path")) == name]
    if not dropped:
        return
    if dry_run:
        log(f"would remove the '{name}' entry from {path}")
        return
    for node in dropped:
        root.remove(node)
    indent(root)
    ws.write_atomic(path, b'<?xml version="1.0" encoding="UTF-8"?>\n'
                    + ET.tostring(root, encoding="utf-8"))
    log(f"removed the '{name}' entry from {path}")


def remove_ports_entry(cfg, dry_run=False):
    for path in ports_entries(cfg):
        if dry_run:
            log(f"would remove the Ports entry {path}")
        else:
            os.unlink(path)
            log(f"removed the Ports entry {path}")
        drop_ports_gamelist_entry(cfg, path, dry_run=dry_run)


def migrate_layout(cfg, dry_run=False):
    """Take the store off the box when `menu.mode` changed under it.

    The games move by being removed and downloaded again, not by being carried
    across: the catalog is small, and a half-moved install would be worse than a
    slightly longer sync. What this does is exactly what an uninstall does —
    ROMs, box art, port payloads, gamelist nodes, the empty folders — so the old
    layout is gone completely, and then the sync fills the new one.

    A state file with no `layout` in it was written by an engine that only had
    the merge layout, so that is what it is assumed to be.
    """
    want = layout_name(layout(cfg))
    state = ws.load_state()
    have = state.get("layout") or "merge"
    if have == want:
        return False

    installed = state["installed"]
    if not installed:
        if not dry_run:
            state["layout"] = want
            ws.save_state(state)
        return False

    log(f"the store's layout changed ({have} -> {want}) — removing the "
        f"{len(installed)} installed games from the old one, they will be "
        "downloaded again")
    old = layout_named(cfg, have)
    systems = set()
    for key, record in list(installed.items()):
        remove_game(cfg, record, dry_run=dry_run, lay=old)
        systems.add(ws.record_scope(record))
        if not dry_run:
            del installed[key]

    if have == "system":
        # The updater and the menu entry belong to the layout we are leaving.
        systems.add(STORE_SYSTEM)
        updater = os.path.join(old.base(STORE_SYSTEM), "update.sh")
        if os.path.isfile(updater):
            if dry_run:
                log(f"would remove {updater}")
            else:
                os.unlink(updater)
        remove_es_systems(cfg, dry_run=dry_run)
        remove_logos(cfg, state, dry_run=dry_run)

    for system in sorted(systems):
        merge_gamelist(cfg, system, [], dry_run=dry_run, backup=False, lay=old)
        drop_empty_gamelist(cfg, system, dry_run=dry_run, lay=old)

    dirs = []
    for system in sorted(systems):
        dirs += old.purge_dirs(system)
    if have == "system":
        dirs.append(old.store_root())
    ws.prune_empty_dirs(dirs, root=roms_root(cfg), dry_run=dry_run)

    if not wants_ports_entry(cfg):
        remove_ports_entry(cfg, dry_run=dry_run)

    leftovers = gamelist_backups(cfg, systems, lay=old)
    if leftovers:
        log("left in place — copies of your gamelists as we first found them:")
        for path in leftovers:
            log(f"  {path}")

    if not dry_run:
        state["layout"] = want
        ws.save_state(state)
    return True


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
    migrate_layout(cfg, dry_run=dry_run)

    systems = detect_systems(cfg)
    if not systems:
        # An unreadable es_systems.cfg with an unmounted or wrong ROMs root
        # behind it would otherwise look like "no system is compatible any more"
        # and prune everything we ever installed.
        die(f"cannot tell which systems this box has: no {es_systems_cfg(cfg)}, "
            f"and no system folders under {roms_root(cfg)} — is it mounted?")

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
    grouped = ws.records_by_scope(state["installed"])
    # The menu entry is written even when nothing was downloaded: a config that
    # renamed it, or a first sync that found nothing to install, still has to
    # leave the box with a way in.
    menu_changed = apply_menu(cfg, grouped, dry_run=args.dry_run)

    if args.dry_run:
        log("dry run — nothing written")
        return 0
    if not changed and not args.force:
        log("already up to date")
    else:
        for system in sorted(touched | set(grouped)):
            merge_gamelist(cfg, system, grouped.get(system, []))

    if (cfg["emulationstation"].get("restart") and not args.no_restart
            and (changed or menu_changed) and es_pid()):
        restart_es()
    return 0


def cmd_sync_from_es(cfg, args):
    """Sync as launched from the Ports menu — no console, so ES has to reload.

    EmulationStation holds gamelists in memory and writes them back on exit,
    so downloading happens now and the gamelist merge is handed to a detached
    child that waits for the old ES process to die first.
    """
    state, changed, _ = perform_sync(cfg)
    grouped = ws.records_by_scope(state["installed"])
    pid = es_pid()
    # The es_systems overlay and the updater script are ours alone — ES never
    # writes them back — so they can be written now. Only the gamelists wait.
    menu_changed = apply_menu(cfg, grouped, gamelists=not pid)
    if not changed:
        log("already up to date")

    if not pid:
        # Nothing racing us — write the gamelists right here. A file whose
        # content has not changed is not rewritten, so this is cheap.
        for system, records in grouped.items():
            merge_gamelist(cfg, system, records)
        return 0

    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__),
         "--config", ws.CONFIG_PATH, "apply-gamelists", "--wait-pid", str(pid)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    if (cfg["emulationstation"].get("restart") and not args.no_restart
            and (changed or menu_changed)):
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
    for system, records in grouped.items():
        merge_gamelist(cfg, system, records, dry_run=args.dry_run)
    # The updater's own gamelist waited for ES to exit along with the rest.
    if isinstance(layout(cfg), SystemLayout) and menu(cfg).get("updater"):
        merge_gamelist(cfg, STORE_SYSTEM, updater_records(cfg), dry_run=args.dry_run)
    return 0


def cmd_list(cfg, args):
    systems = detect_systems(cfg)
    catalog = ws.fetch_catalog(cfg, use_cache=True)
    host = ws.host()
    log(f"architecture: {host['arch']}")
    state = ws.load_state()
    if state["installed"] and (state.get("layout") or "merge") != layout_name(layout(cfg)):
        log(f"the next sync will move these games to the '{layout_name(layout(cfg))}' "
            f"layout — they are removed from the old one and downloaded again")
    games, skipped = select_games(cfg, catalog, systems, host)
    installed = state["installed"]

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


def drop_empty_gamelist(cfg, system, dry_run=False, lay=None):
    """Delete a gamelist that is empty and that we created.

    In merge mode, no `.<id>-backup` next to it means we never found a gamelist
    there — it is ours alone, and with our nodes gone there is nothing in it. In
    system mode the file is in our own folder, so an empty one is always ours.
    Leaving the empty shell behind would mean the uninstall did not quite put the
    box back.
    """
    lay = lay or layout(cfg)
    path = lay.gamelist(system)
    if not os.path.isfile(path):
        return
    if lay.backup_gamelists and os.path.exists(f"{path}.{cfg['store']['id']}-backup"):
        return
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return
    if len(root):
        return
    if dry_run:
        log(f"would remove the now-empty {path}")
    else:
        os.unlink(path)
        log(f"removed the now-empty {path}")


def gamelist_backups(cfg, systems, lay=None):
    """The `gamelist.xml.<id>-backup` copies we made, if any are still around."""
    lay = lay or layout(cfg)
    paths = [f"{lay.gamelist(system)}.{cfg['store']['id']}-backup" for system in systems]
    return sorted(p for p in paths if os.path.isfile(p))


def cmd_purge(cfg, args):
    """Uninstall every game, and take the store's own folders with it.

    What `remove` does for one game, for all of them at once — then the empty
    directories, so an uninstall leaves the box looking as it did before. This is
    what `uninstall.sh` runs before deleting the store itself: the shell script
    has no way of knowing which files were ours.

    In merge mode the gamelists are merged, not deleted: they belong to the box,
    and after our nodes are gone the rest of the user's library is still in them.
    In system mode they are ours, and go with the folders — along with the
    es_systems overlay, which is what the store's menu entry is made of.
    """
    state = ws.load_state()
    installed = state["installed"]
    if not installed:
        log("nothing is installed by this store")

    # Which layout the files are actually in is what `state.json` says, not what
    # the config asks for today: an upgrade whose first sync never ran still has
    # its games in the old places, and an uninstall has to find them there.
    recorded = state.get("layout") or ("merge" if installed else layout_name(layout(cfg)))
    lay = layout_named(cfg, recorded)

    systems = set()
    for key, record in list(installed.items()):
        remove_game(cfg, record, dry_run=args.dry_run, lay=lay)
        systems.add(ws.record_scope(record))
        if not args.dry_run:
            del installed[key]
    if not args.dry_run:
        ws.save_state(state)

    # The menu entry and its updater are not catalog games, so no state record
    # points at them. Both are removed whichever layout is recorded — a leftover
    # es_systems file would leave the box with a menu entry and nothing in it.
    remove_es_systems(cfg, dry_run=args.dry_run)
    remove_logos(cfg, state, dry_run=args.dry_run)
    store_layout = SystemLayout(cfg)
    updater = os.path.join(store_layout.base(STORE_SYSTEM), "update.sh")
    if os.path.isfile(updater):
        if args.dry_run:
            log(f"would remove {updater}")
        else:
            os.unlink(updater)
    for system in sorted(systems):
        merge_gamelist(cfg, system, [], dry_run=args.dry_run, backup=False, lay=lay)
        drop_empty_gamelist(cfg, system, dry_run=args.dry_run, lay=lay)

    dirs = []
    for system in sorted(systems):
        dirs += lay.purge_dirs(system)
    # The store's own folder is the system layout's, whatever the records say.
    if os.path.isdir(store_layout.store_root()):
        merge_gamelist(cfg, STORE_SYSTEM, [], dry_run=args.dry_run, backup=False,
                       lay=store_layout)
        drop_empty_gamelist(cfg, STORE_SYSTEM, dry_run=args.dry_run, lay=store_layout)
        dirs += store_layout.purge_dirs(STORE_SYSTEM)
        dirs.append(store_layout.store_root())
    ws.prune_empty_dirs(dirs, root=roms_root(cfg), dry_run=args.dry_run)

    leftovers = gamelist_backups(cfg, systems, lay=lay)
    if leftovers:
        log("left in place — these are copies of your gamelists as we first found them:")
        for path in leftovers:
            log(f"  {path}")

    if args.dry_run:
        log("dry run — nothing removed")
        return 0
    if cfg["emulationstation"].get("restart") and not args.no_restart and es_pid():
        restart_es()
    return 0


def cmd_config(cfg, args):
    if args.write:
        if os.path.exists(args.config) and not args.force:
            die(f"{args.config} already exists (use --force to overwrite)")
        ws.write_json(args.config, DEFAULT_CONFIG)
        log(f"wrote default config to {args.config}")
        return 0
    # A felismert architektura nem a configbol jon, de az asset-valasztast
    # eldonti, ezert itt is lathatonak kell lennie. Ugyanez all a menupontra:
    # a neve es a helye a configbol szarmazik, de nem all benne.
    lay = layout(cfg)
    extra = {"detected_arch": ws.machine(), "install_dir": lay.base("<system>")}
    if isinstance(lay, SystemLayout):
        extra["es_menu"] = {
            "entry": es_parent_name(cfg, box_systems(cfg)),
            "file": es_systems_path(cfg),
            "root": lay.store_root(),
        }
    print(json.dumps({**extra, **cfg}, indent=2, ensure_ascii=False))
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

    p_purge = sub.add_parser("purge", help="uninstall everything this store installed")
    p_purge.add_argument("--no-restart", action="store_true", help="never restart EmulationStation")
    p_purge.set_defaults(func=cmd_purge)

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
    mode = (menu(cfg).get("mode") or "system").lower()
    if mode not in ("system", "merge"):
        die(f"config: emulationstation.menu.mode is '{mode}' — expected 'system' or 'merge'")
    os.makedirs(ws.HOME, exist_ok=True)
    # The store's host says which engine release this store runs; a sync brings
    # it there first, and carries on in the new engine.
    if args.command in ("sync", "sync-from-es") and not args.dry_run and hasattr(ws, "self_update"):
        ws.self_update(cfg, os.path.abspath(__file__), VERSION, sys.argv[1:] if argv is None else argv)
    return args.func(cfg, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
