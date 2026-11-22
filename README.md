# we-batocera — a Batocera store engine for WarpEngine sites

Pulls games from a [WarpEngine](https://git.teletypegames.org/tools/warp_engine)
catalog straight onto a Batocera box, with EmulationStation metadata and box art,
and gives the store **its own entry in the EmulationStation menu**. Started from
that entry on the device, or from the CLI over SSH.

This repository is the engine only. It knows the WarpEngine API but nothing
about any particular site: the host, the store's name and where its games land
all come from a `config.json` that lives in a separate **store repository**.
The part that is not about Batocera — catalog, releases, host matching, state,
HTTP — is [`we-core`](https://git.teletypegames.org/engines/we-core), shared
with the [RetroArch engine](https://git.teletypegames.org/stores/we-retroarch).

```
we-core                             the shared core — warpstore.py
        ▲
we-batocera                         this engine — store.py, install.sh
        ▲
        │ config.json
        │
  ┌─────┴─────┬───────────────┐
  │           │               │
ttg-…       my-…            other-…       store repositories
  │           │               │
  ▼           ▼               ▼
teletypegames.org   my.example   other.example
```

The reference store is
[`ttg-we-batocera`](https://git.teletypegames.org/stores/ttg-we-batocera).

```
"<store name>" ▸ "Update <store name>"
        │
        ├─ GET /api/software                        the whole catalog
        ├─ keep platforms this box can run          c64 → c64, plus4 → cplus4, tic80 → tic80
        ├─ pick the newest non-dev release          that carries the right asset
        ├─ GET /api/download?path=<asset>           .prg / .tic into the store's folder
        ├─ GET <software.imageUrl>                  box art
        ├─ write gamelist.xml                       title, desc, author, image
        ├─ write es_systems_<id>.cfg                the store's menu entry
        └─ install the logo into the themes         so the entry is not borrowing one
```

Python 3 standard library only — Batocera ships python3 and no pip.

## The store's own menu entry

Everything the store installs lives in one folder of its own,
`/userdata/roms/<subfolder>`, with a directory per platform inside it. An
`es_systems_<id>.cfg` in EmulationStation's user config declares one ES system
per platform, all carrying the same `<group>`, and ES turns a group into a single
carousel entry holding a folder per system:

```
main carousel:  …  ▸  Teletype Games  ▸  ┌ Commodore 64
                                         ├ TIC-80
                                         ├ Ports
                                         └ Update Teletype Games
```

Nothing about emulators is hardcoded here. The engine reads the box's own
`/usr/share/emulationstation/es_systems.cfg` and copies the launch command, the
extensions, the platform, the theme and the emulator list off the system it is
borrowing — a file Batocera generated for this particular image, so it lists
exactly the emulators the box has. The one thing that is rewritten is `%SYSTEM%`:
ES replaces it with the name of the system being launched, which for us would be
`ttg-c64`, a name configgen has never heard of. The box's own system name goes in
instead, and a game of ours is launched exactly as the box would launch it, with
the emulator the user configured for that system.

Five details worth knowing, all of them EmulationStation's, and the middle two
were learned the hard way:

- **The menu entry is declared, not implied.** ES would invent the group parent
  by itself, but then its theme folder would be the group's name, which no theme
  has — and `HideUniqueGroups` (on by default) dissolves a group that has a
  single system in it unless a system by that name exists. The declared parent
  fixes both: it carries a `<theme>` (`ports` by default, since every theme has
  that one) and keeps the entry in place on a box that only has c64 games.
- **A system with no games of its own is dropped**, not just hidden:
  `loadSystem` logs `System "..." has no games! Ignoring it.` and throws it away.
  So the menu entry cannot be an empty shelf — it *is* the `store/` folder, and
  the updater in it is the game that keeps it alive. Which is also why the
  *Update …* item sits directly in the entry rather than in a subfolder.
- **The entry must not be the directory above the systems.** ES marks a
  directory it has enumerated with a `<dir>/*` entry in its file cache, and adds
  that mark *before* reading the contents; from then on any path under it that
  is not itself cached answers "does not exist" (`FileSystemUtil.cpp`,
  `getCacheEntry`). Systems load in a thread pool, so an entry sitting above the
  systems loses that race for a random couple of them, and ES drops those with
  `System "..." path does not exist !` — a different couple on every start.
  `store/` is their sibling, so there is no race to lose.
- **An empty system does not show.** Only the platforms that actually have
  something installed become folders, so the entry is never full of dead ends.
- **A restart is needed for a new system.** The engine restarts ES itself after a
  sync that added one.

The store id names the entry. If the box already has a system by that name — a
store called `nes`, say — the entry is called `<id>-store` instead, and the log
says so: an overlay whose `<name>` matches an existing system would *modify that
system* rather than add one.

### Its logo

EmulationStation resolves a system's carousel logo through the **theme**:
`SystemData::getProperty("image")` asks the theme for `system/logo`, and the
theme builds the path from the system's theme folder — carbon, for instance,
tries `art/logos/${system.theme}.png`, then `.svg`. There is no user-level
override anywhere, so a custom system has exactly two options: borrow a theme
folder that already has artwork, or put a file where the themes look.

Both are supported. With no `menu.logo`, the entry borrows `ports` — every theme
has that one, so the entry has *an* icon, just not the store's. Set `menu.logo`
to an image (a path or a URL, or a list of them for several formats) and the
engine installs it instead:

- it finds each theme's logo folder by looking for the logos of systems every
  theme has (`ports`, `snes`, `nes`, …) rather than by knowing the layouts —
  themes keep them in `art/logos`, `_inc/systems/logos`, `_art/Colorlogos`, and
  worse;
- the file is written as `<menu.theme>.<ext>`, and `menu.theme` then defaults to
  the store id, so nothing of the theme's own is ever overwritten;
- **a file we did not write is never touched** — every path installed is recorded
  in `state.json`, an uninstall takes exactly those back out, and a name clash is
  reported and skipped;
- the file is fetched once into the store home and used from there, so a theme
  installed later needs no network; re-running the installer drops that copy, so
  a store that changes its logo gets the new one onto the box. A local config's
  logos are taken from beside it — a checkout installs without the forge;
- a theme with no logo folder we can find, or that resolves logos some other way,
  shows the store's **name as text** instead — EmulationStation's own fallback
  (`CarouselComponent`: no image, so a `TextComponent`).

One Batocera wrinkle: themes that ship *with the system image* live under
`/usr/share`, and Batocera's root is a RAM overlay. The copy works and shows up
immediately, but it is gone after a reboot. So the engine also keeps a marked
block in `/boot/postshare.sh` — the one Batocera boot hook that runs *before*
EmulationStation, while every service starts after the themes are loaded — that
copies the cached logo back into those themes with a plain `cp`, only where the
file is missing. The block goes right after the shebang, so an `exit` in the
user's own part of the file cannot skip it; nothing else in the file is touched,
`/boot` is remounted read-write only for the write, and `purge` takes the block
out again (and the file, if the engine created it). Themes under
`/userdata/themes` need none of this: they keep the logo for good.

For the old behaviour — games inside the box's own systems, under a subfolder,
merged into the box's gamelists — set `emulationstation.menu.mode` to `merge`.

## Installing a store

The installer takes the store as a parameter, so it works with any config:

```sh
curl -fsSL https://git.teletypegames.org/stores/we-batocera/raw/branch/master/install.sh |
  STORE_CONFIG=https://git.example.org/tools/my-store/raw/branch/master/config.json sh
```

From a checkout, with a local config:

```sh
scp -r we-batocera root@batocera:/tmp/
ssh root@batocera 'STORE_CONFIG=/tmp/my-config.json /tmp/we-batocera/install.sh'
```

It reads `store.id` from the config, installs the engine and the config into
`/userdata/system/batocera-store/<id>/`, writes a `<id>-store` launcher, and runs
the first sync — which is what creates the menu entry. Restart EmulationStation
(`batocera-es-swissknife --restart`) to see it.

It installs two files: `store.py` from this repository and `warpstore.py` from
the [shared core](https://git.teletypegames.org/engines/we-core). They have to
sit next to each other — the engine imports the core from its own directory.

Installer knobs, all environment variables: `STORE_CONFIG` (required),
`BATOCERA_STORE_ROOT`, `BATOCERA_PORTS_DIR`, `BATOCERA_PORT_NAME`,
`ENGINE_RAW_BASE`, `WARPSTORE_RAW_BASE`, and `WARPSTORE_SRC` to install a local
`warpstore.py` instead of downloading it.

## Upgrading a store that was installed before the menu entry

Engine 4.0 is the layout change, so the first sync after the upgrade moves the
store off the box's own systems. It does that by **removing the old install and
downloading again**, not by carrying files across: the ROMs, box art and port
payloads go, our nodes come out of the box's gamelists, the empty folders are
swept, the Ports entry is taken away — and then the sync fills the store's own
folder. The catalog is small enough that this is minutes, and there is no
half-moved state to end up in.

What is kept: your gamelists (only our nodes go — play counts and favourites of
your own games are untouched), and the `gamelist.xml.<id>-backup` copies, which
are listed at the end of the run for you to delete or keep.

`-n` shows the whole thing without doing any of it. To stay on the old layout,
set `emulationstation.menu.mode` to `merge` before the first sync.

## Writing a store repository

Four small files:

```
my-batocera-store/
├── config.json     the store: URL, name, subfolder, platform mapping
├── install.sh      a wrapper that hands that config to the engine installer
├── uninstall.sh    the same for the engine's uninstaller
└── README.md
```

`install.sh` is small enough to quote in full:

```sh
#!/bin/bash
set -euo pipefail
ENGINE_RAW_BASE="${ENGINE_RAW_BASE:-https://git.teletypegames.org/stores/we-batocera/raw/branch/master}"
export STORE_CONFIG="${STORE_CONFIG:-https://git.example.org/tools/my-batocera-store/raw/branch/master/config.json}"
curl -fsSL "$ENGINE_RAW_BASE/install.sh" | sh
```

`uninstall.sh` is the same shape, except that it passes `STORE_ID` — no config
fetch is needed to take a store away, and the id is what names its home:

```sh
export STORE_ID="${STORE_ID:-example}"
curl -fsSL "$ENGINE_RAW_BASE/uninstall.sh" | sh
```

Pick a `store.id` and a `paths.subfolder` nobody else uses — they are what keeps
two stores on the same box out of each other's way, and the id is what names the
menu entry and its `es_systems` file.

## Use

**On the device.** *"\<store name\>" ▸ "Update \<store name\>"*. It
downloads anything new, then restarts EmulationStation so the games show up. ES
gives a launched script no console, so the output goes to `store.log` in the
store home.

**Over SSH**, through the launcher the installer wrote:

```sh
S=/userdata/system/batocera-store/example-store

$S list                 # compatible catalog entries; * installed, ^ update available
$S sync                 # download everything new, refresh gamelists and the menu
$S sync blessingofra    # just one title (never prunes)
$S -n sync              # dry run
$S remove c64:demo      # uninstall (bare name works too)
$S purge                # uninstall everything this store installed
$S config               # effective configuration, and where the menu entry lives
```

Global flags go **before** the subcommand: `$S --roms-root /tmp/roms sync`.

## Uninstalling

```sh
ssh root@batocera 'curl -fsSL https://git.teletypegames.org/stores/we-batocera/raw/branch/master/uninstall.sh | sh'
```

or `./uninstall.sh` from a checkout on the box. It removes the games first,
through the engine — `purge` — and only then the launcher, any Ports entry and
the store home: the shell script has no way of knowing which ROMs, box art, port
payloads, gamelist entries and ES systems were the store's, and the engine's
`state.json` does. If the engine cannot finish — an unmounted ROMs root, say —
nothing else is touched, so you are never left with the files but not the engine
that knows about them.

| Environment variable | Meaning |
|---|---|
| `STORE_ID` | which store to remove; needed only when several are installed |
| `STORE_CONFIG` | alternative to `STORE_ID` — the id is read out of the config |
| `DRY_RUN` | `1` to print what would go and remove nothing |
| `KEEP_HOME` | `1` to keep the store home (state, catalog cache, log) for a reinstall |
| `BATOCERA_STORE_ROOT`, `BATOCERA_PORTS_DIR`, `BATOCERA_PORT_NAME` | as for the installer |

In the store's own layout an uninstall is total: the store's ROM folder, its
gamelists, its `es_systems_<id>.cfg` and the logos it installed into the themes
all go, and the box is left as it was.
In `merge` mode there is more to be careful about, and these stay behind:

- **ROMs you put in the store's subfolder yourself.** Directories are removed
  only when empty, so one stray file keeps its directory. `ports/.data` itself
  also stays: another store may still keep a payload there.
- **your gamelists.** They belong to the box: our `<game>` and `<folder>` nodes
  go, the rest — your own games, their play counts and favourites — is written
  back untouched. A gamelist we *created* and that is left empty is deleted,
  since it was never yours.
- **`gamelist.xml.<store id>-backup`**, listed at the end of the run: your
  gamelists as we first found them. Deleting them is your call.

A dry run cannot predict which directories will end up empty, so it lists the
files but not the directory removals.

To take **every** store off a machine — both engines, the shared core, the
launchers — there is one script for that in
[`we-core`](https://git.teletypegames.org/engines/we-core):

```sh
curl -fsSL https://git.teletypegames.org/engines/we-core/raw/branch/master/uninstall.sh | sh
```

## Where things land

```
/userdata/system/batocera-store/
├── example-store                       launcher for the store below
└── example/
    ├── store.py, warpstore.py          the engine and its shared core
    ├── config.json                     the store that selected it
    └── state.json, catalog.json, store.log

/userdata/system/configs/emulationstation/es_systems_example.cfg   the menu entry

/userdata/roms/example/                 the store's own ROM folder
├── c64/
│   ├── blessingofra-2.0.0.prg, rabbit-1.0.0.prg, …
│   ├── images/blessingofra.png, …
│   └── gamelist.xml
├── ports/
│   ├── <name>.sh                       the launchers ES lists
│   ├── images/<name>.png
│   ├── .data/<name>/                   the unpacked programs
│   └── gamelist.xml
└── store/                              the menu entry itself
    ├── update.sh                       the sync, as the menu starts it
    └── gamelist.xml
```

The box's own system folders are not touched at all, which is what makes a prune
safe: nothing outside the store's folder can be reached, and inside it only what
`state.json` says we installed is ever deleted — a ROM you drop in there
yourself keeps its file *and* its gamelist entry.

In `merge` mode the same content goes into `<roms_root>/<system>/<subfolder>/`
and our nodes are merged into the box's `gamelist.xml`. The first time one is
touched it is copied to `gamelist.xml.<store id>-backup`; on every merge only
`<game>`/`<folder>` nodes under that store's subfolder are rewritten, so your own
entries keep their play counts, favourites and scraped media, and two stores can
share one gamelist.

Either way, the tags EmulationStation owns — `favorite`, `playcount`,
`lastplayed`, `rating` — are carried over onto the node we rewrite. A favourite
stays a favourite across a sync.

## Configuration

`config.json` in the store home, template in `config.example.json`:

| Key | Default | Meaning |
| --- | --- | --- |
| `store.id` | `warp` | slug: names the store home, the log prefix, the menu entry and its `es_systems` file |
| `store.name` | `WarpEngine Store` | display name of the menu entry |
| `store.base_url` | — | WarpEngine host |
| `store.api.catalog` | `/api/software` | catalog endpoint, if the engine is mounted elsewhere |
| `store.api.download` | `/api/download` | download endpoint |
| `paths.roms_root` | `/userdata/roms` | where systems live |
| `paths.subfolder` | `warp` | the store's own folder inside `roms_root` (in `merge` mode: its subfolder inside each system) |
| `emulationstation.menu.mode` | `system` | `system`: the store gets its own menu entry. `merge`: install into the box's own systems |
| `emulationstation.menu.name` | `null` → `store.name` | label of the menu entry |
| `emulationstation.menu.theme` | `null` → the store id with a logo, `ports` without | theme folder the entry takes its logo from, and the name of the logo file |
| `emulationstation.menu.logo` | `null` | the entry's own logo: a path or URL, or a list of them (one per format). Installed into every theme's logo folder |
| `emulationstation.menu.updater` | `true` | an *Update …* entry inside the menu that runs the sync |
| `emulationstation.menu.labels` | `{}` | per-system folder labels; a system not listed keeps the box's own `<fullname>` |
| `emulationstation.ports_entry` | `null` → `false` in `system` mode, `true` in `merge` mode | also write a Ports entry that runs the sync |
| `emulationstation.folder_name` | `WarpEngine Store` | `merge` mode only: display name of our folder inside the box's gamelist; `null` hides it |
| `emulationstation.restart` | `true` | restart EmulationStation after a change |
| `emulationstation.config_dir` | `null` → `/userdata/system/configs/emulationstation` | where ES keeps its user config |
| `catalog.statuses` | `["released", "archived"]` | catalog `status` values to install |
| `catalog.owner_id` | `null` | restrict to one publisher (`/api/software?owner_id=`) |
| `catalog.only` / `catalog.exclude` | `[]` | software-name allow / deny lists |
| `platforms` | c64, plus4, tic80 | platform → system, asset kind, extension, enabled; `install` picks ROM or Port, and the kind may be per-architecture |
| `behavior.prune` | `true` | remove games that left the catalog or the filters |
| `behavior.timeout` | `30` | HTTP timeout, seconds |
| `behavior.insecure` | `false` | skip TLS verification (self-hosted test instances) |

`BATOCERA_ES_CONFIG_DIR` and `BATOCERA_ES_SYSTEMS_CFG` override the two
EmulationStation paths from the environment, which is what makes a staging root
testable off a real box.

### Platform mapping

A catalog platform is installable when its release asset is a file a system this
box has can boot directly:

| Catalog platform | Asset kind | Batocera system | Extension |
| --- | --- | --- | --- |
| `c64` | `cartridge` | `c64` (VICE) | `.prg` |
| `plus4` | `cartridge` | `cplus4` (VICE `xplus4`) | `.prg` |
| `tic80` | `cartridge` | `tic80` | `.tic` |

"Has" means declared in the box's own `es_systems.cfg` — an emulator that got
built into this image. (A ROM folder is a poorer test: it can be there with
nothing behind it. The folders are the fallback when the file cannot be read.)

The other WarpEngine platforms (`ebitengine`, `love`, `godot`, `bevy`,
`phaser`) ship `html` and per-OS zip archives, not a ROM a Batocera system
launches, so they are not mapped by default. Adding one is a config edit:

```json
"platforms": { "godot": { "system": "godot", "kind": "linux_x64", "ext": ".zip", "enabled": true } }
```

### Native games as Ports

A cartridge is booted by an emulator, so it goes to the folder of the system that
boots it. A native build is a program: Batocera runs Linux, and its **Ports**
system takes `.sh` launchers. Set `install` to `port` and point `system` at
`ports`:

```json
"platforms": {
  "bevy": {
    "system": "ports",
    "install": "port",
    "kind": { "x86_64": "linux_x64", "aarch64": "linux_arm64" },
    "ext": ".zip",
    "enabled": true
  }
}
```

The zip is unpacked and a launcher written:

```
/userdata/roms/<subfolder>/ports/<name>.sh          the launcher ES lists
/userdata/roms/<subfolder>/ports/images/<name>.png  box art
/userdata/roms/<subfolder>/ports/.data/<name>/      the unpacked program
```

The payload sits under `.data`, whose leading dot is what hides it from
EmulationStation — only the `.sh` next to it is meant to be listed. `system:
"ports"` still names where the launcher *definition* comes from: our own ports
system copies the box's, which is `sh`, so the launcher is simply run.

The launcher `cd`s into the directory holding the executable before running it:
a game loads its `assets/` relative to the working directory. Python's zip
extraction drops the executable bit, so it is restored from the archive's
recorded mode.

`install` defaults to `rom`, so a config that does not mention it behaves exactly
as before.

### Per-architecture assets

Batocera runs on x86_64 mini-PCs, on the Raspberry Pi and on the ARM handhelds
alike, and a native binary only starts on the architecture it was built for —
an x86_64 build installs on a Pi and then does nothing. A cartridge is data for
an emulator, so it is architecture-independent; a native build is not.

Where the right asset depends on the machine, `kind` (and `ext`, if it differs)
can be a map instead of a string. `*` is the fallback:

```json
"platforms": {
  "bevy": {
    "system": "bevy",
    "kind": { "x86_64": "linux_x64", "aarch64": "linux_arm64" },
    "ext": ".zip",
    "enabled": true
  }
}
```

The engine reports the architecture it detected — `x86_64`, `aarch64`, `armhf`,
`x86` — at the top of `list`, and `config` prints it as `detected_arch`. With no
entry for the running machine and no `*`, the title is skipped and says so:
`skipped bevydemo: bevy has no asset kind for aarch64`.

A platform is also skipped when the box does not have its system —
`list` reports that as `skipped <name>: this box has no '<system>' system`.

## How a sync stays safe next to EmulationStation

EmulationStation keeps gamelists in memory and writes them back when it exits,
so a merge done while it runs can be clobbered. Launched from the menu
(`sync-from-es`), the engine therefore downloads first, then hands the gamelist
merge to a detached `apply-gamelists --wait-pid <es-pid>` child that waits for
the old ES process to die before writing. From SSH, with no ES running, the
merge happens inline.

The `es_systems_<id>.cfg` needs none of that: ES reads it and never writes it,
so it is written straight away — it just does not take effect until ES restarts.

Re-running a sync is idempotent: files already present are left alone, a release
bump (`1.1` → `2.0.0`) deletes the old asset before fetching the new one, and a
gamelist or `es_systems` file whose content has not changed is not rewritten at
all. `GET /api/download` is used rather than `/file/`, so the host sees each
download, under a `warpstore/<version> (<store id>)` user agent; whether it
counts them is the host's call — teletypegames.org leaves `warpstore/*` out of
its statistics on purpose (`ignored_user_agents`).

## State

`state.json` records what this store installed, keyed `<system>:<name>` so the
same software name on two systems stays two entries. `layout` says which
directory layout those records belong to, which is how the upgrade to the store's
own folder knows it has to run once. A `version: 1` state file written by the
pre-split `ttg-store` is migrated to the current key format on first run.
