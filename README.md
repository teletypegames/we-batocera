# warp-engine-batocera-store — a Batocera store engine for WarpEngine sites

Pulls games from a [WarpEngine](https://git.teletypegames.org/tools/warp_engine)
catalog straight into a Batocera box's ROM folders, with EmulationStation
metadata and box art. Triggered from the **Ports** menu on the device, or from
the CLI over SSH.

This repository is the engine only. It knows the WarpEngine API but nothing
about any particular site: the host, the store's name and where its games land
all come from a `config.json` that lives in a separate **store repository**.
The part that is not about Batocera — catalog, releases, host matching, state,
HTTP — is [`warpstore`](https://git.teletypegames.org/engines/warpstore), shared
with the [RetroArch engine](https://git.teletypegames.org/tools/warp-engine-retroarch-store).

```
warpstore                           the shared core — warpstore.py
        ▲
warp-engine-batocera-store          this engine — store.py, install.sh
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
[`ttg-batocera-store`](https://git.teletypegames.org/tools/ttg-batocera-store).

```
Ports ▸ "<store name>"
        │
        ├─ GET /api/software                        the whole catalog
        ├─ keep platforms this box can run          c64 → c64, tic80 → tic80
        ├─ pick the newest non-dev release          that carries the right asset
        ├─ GET /api/download?path=<asset>           .prg / .tic into the ROM folder
        ├─ GET <software.imageUrl>                  box art
        └─ merge gamelist.xml                       title, desc, author, image
```

Python 3 standard library only — Batocera ships python3 and no pip.

## Installing a store

The installer takes the store as a parameter, so it works with any config:

```sh
STORE_CONFIG=https://git.example.org/tools/my-store/raw/branch/master/config.json \
  bash <(curl -fsSL https://git.teletypegames.org/tools/warp-engine-batocera-store/raw/branch/master/install.sh)
```

From a checkout, with a local config:

```sh
scp -r warp-engine-batocera-store root@batocera:/tmp/
ssh root@batocera 'STORE_CONFIG=/tmp/my-config.json /tmp/warp-engine-batocera-store/install.sh'
```

It reads `store.id` from the config, installs the engine and the config into
`/userdata/system/batocera-store/<id>/`, writes a `<id>-store` launcher and a
Ports entry, and runs the first sync. Restart EmulationStation
(`batocera-es-swissknife --restart`) to see the games.

It installs two files: `store.py` from this repository and `warpstore.py` from
the [shared core](https://git.teletypegames.org/engines/warpstore). They have to
sit next to each other — the engine imports the core from its own directory.

Installer knobs, all environment variables: `STORE_CONFIG` (required),
`BATOCERA_STORE_ROOT`, `BATOCERA_PORTS_DIR`, `BATOCERA_PORT_NAME`,
`ENGINE_RAW_BASE`, `WARPSTORE_RAW_BASE`, and `WARPSTORE_SRC` to install a local
`warpstore.py` instead of downloading it.

## Writing a store repository

Three files:

```
my-batocera-store/
├── config.json     the store: URL, name, subfolder, platform mapping
├── install.sh      a wrapper that hands that config to the engine installer
└── README.md
```

`install.sh` is small enough to quote in full:

```sh
#!/bin/bash
set -euo pipefail
ENGINE_RAW_BASE="${ENGINE_RAW_BASE:-https://git.teletypegames.org/tools/warp-engine-batocera-store/raw/branch/master}"
export STORE_CONFIG="${STORE_CONFIG:-https://git.example.org/tools/my-batocera-store/raw/branch/master/config.json}"
export BATOCERA_PORT_NAME="My Store"
curl -fsSL "$ENGINE_RAW_BASE/install.sh" | bash
```

Pick a `store.id` and a `paths.subfolder` nobody else uses — they are what keeps
two stores on the same box from deleting each other's games.

## Use

**On the device.** *Ports ▸ "\<store name\>"*. The script downloads anything
new, then restarts EmulationStation so the games show up. Ports scripts get no
console in Batocera, so the output goes to `store.log` in the store home.

**Over SSH**, through the launcher the installer wrote:

```sh
S=/userdata/system/batocera-store/example-store

$S list                 # compatible catalog entries; * installed, ^ update available
$S sync                 # download everything new, refresh gamelists
$S sync blessingofra    # just one title (never prunes)
$S -n sync              # dry run
$S remove c64:demo      # uninstall (bare name works too)
$S config               # effective configuration
```

Global flags go **before** the subcommand: `$S --roms-root /tmp/roms sync`.

## Where things land

```
/userdata/system/batocera-store/
├── example-store                       launcher for the store below
└── example/
    ├── store.py, warpstore.py          the engine and its shared core
    ├── config.json                     the store that selected it
    └── state.json, catalog.json, store.log

/userdata/roms/c64/example/             blessingofra-2.0.0.prg, rabbit-1.0.0.prg, …
/userdata/roms/c64/example/images/      blessingofra.png, …
/userdata/roms/c64/gamelist.xml         our entries merged in
/userdata/roms/ports/Example Store.sh
```

Everything a store installs lives under its own `paths.subfolder`, so a prune
can never reach ROMs you put there yourself — or another store's games. The
first time a `gamelist.xml` is touched it is copied to
`gamelist.xml.<store id>-backup`; on every merge only `<game>`/`<folder>` nodes
under that store's subfolder are rewritten, so your own entries keep their play
counts, favourites and scraped media, and two stores can share one gamelist.

## Configuration

`config.json` in the store home, template in `config.example.json`:

| Key | Default | Meaning |
| --- | --- | --- |
| `store.id` | `warp` | slug: names the store home, log prefix and gamelist backup |
| `store.name` | `WarpEngine Store` | display name for the Ports entry |
| `store.base_url` | — | WarpEngine host |
| `store.api.catalog` | `/api/software` | catalog endpoint, if the engine is mounted elsewhere |
| `store.api.download` | `/api/download` | download endpoint |
| `paths.roms_root` | `/userdata/roms` | where systems live |
| `paths.subfolder` | `warp` | this store's subfolder inside each system |
| `emulationstation.folder_name` | `WarpEngine Store` | display name of that folder in ES |
| `emulationstation.restart` | `true` | restart EmulationStation after a change |
| `catalog.statuses` | `["released", "archived"]` | catalog `status` values to install |
| `catalog.owner_id` | `null` | restrict to one publisher (`/api/software?owner_id=`) |
| `catalog.only` / `catalog.exclude` | `[]` | software-name allow / deny lists |
| `platforms` | c64, tic80 | platform → system, asset kind, extension, enabled; `install` picks ROM or Port, and the kind may be per-architecture |
| `behavior.prune` | `true` | remove games that left the catalog or the filters |
| `behavior.timeout` | `30` | HTTP timeout, seconds |
| `behavior.insecure` | `false` | skip TLS verification (self-hosted test instances) |

### Platform mapping

A catalog platform is installable when its release asset is a file the Batocera
system can boot directly:

| Catalog platform | Asset kind | Batocera system | Extension |
| --- | --- | --- | --- |
| `c64` | `cartridge` | `c64` (VICE) | `.prg` |
| `tic80` | `cartridge` | `tic80` | `.tic` |

The other WarpEngine platforms (`ebitengine`, `love`, `godot`, `bevy`,
`phaser`) ship `html` and per-OS zip archives, not a ROM a Batocera system
launches, so they are not mapped by default. Adding one is a config edit:

```json
"platforms": { "godot": { "system": "godot", "kind": "linux_x64", "ext": ".zip", "enabled": true } }
```

### Native games as Ports

A cartridge is booted by an emulator, so it goes into that system's ROM folder.
A native build is a program: Batocera runs Linux, and its **Ports** system takes
`.sh` launchers. Set `install` to `port` and point `system` at `ports`:

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
/userdata/roms/ports/<subfolder>/<name>.sh          the launcher ES lists
/userdata/roms/ports/<subfolder>/images/<name>.png  box art
/userdata/roms/ports/.data/<subfolder>/<name>/      the unpacked program
```

The payload sits under `.data`, whose leading dot is what hides it from
EmulationStation — the launchers themselves are in a normal subfolder, scanned
like any other system's. Both paths carry the store's subfolder, so an uninstall
or a prune can only ever reach this store's own files.

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

A platform is also skipped when the box has no ROM folder for its system —
`list` reports that as `skipped <name>: no '<system>' ROM folder`.

## How a sync stays safe next to EmulationStation

EmulationStation keeps gamelists in memory and writes them back when it exits,
so a merge done while it runs can be clobbered. Launched from the Ports menu
(`sync-from-es`), the engine therefore downloads first, then hands the gamelist
merge to a detached `apply-gamelists --wait-pid <es-pid>` child that waits for
the old ES process to die before writing. From SSH, with no ES running, the
merge happens inline.

Re-running a sync is idempotent: files already present are left alone, and a
release bump (`1.1` → `2.0.0`) deletes the old asset before fetching the new
one. `GET /api/download` is used rather than `/file/`, so downloads count
towards the catalog's statistics.

## State

`state.json` records what this store installed, keyed `<system>:<name>` so the
same software name on two systems stays two entries. A `version: 1` state file
written by the pre-split `ttg-store` is migrated to that key format on first
run.
