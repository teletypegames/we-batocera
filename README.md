# ttg-store — Teletype Games catalog client for Batocera

Pulls games from the WarpEngine catalog on <https://teletypegames.org> straight
into a Batocera box's ROM folders, with EmulationStation metadata and box art.
Triggered from the **Ports** menu on the device, or from the CLI over SSH.

```
Ports ▸ "Teletype Games Store"
        │
        ├─ GET /api/software                        the whole catalog
        ├─ keep platforms this box can run          c64 → c64, tic80 → tic80
        ├─ pick the newest non-dev release          that carries the right asset
        ├─ GET /api/download?path=<asset>           .prg / .tic into the ROM folder
        ├─ GET /api/image/<id>                      box art
        └─ merge gamelist.xml                       title, desc, author, image
```

Python 3 standard library only — Batocera ships python3 and no pip.

Repository: `https://git.teletypegames.org/tools/batocera-store`.
The catalog side lives in `teletypegames` (`libs/ruby/warp_engine`).

## Install

From a checkout of this repo:

```sh
scp -r batocera-store root@batocera:/tmp/
ssh root@batocera /tmp/batocera-store/install.sh
```

Or straight onto the device:

```sh
curl -fsSL https://git.teletypegames.org/tools/batocera-store/raw/branch/master/install.sh | bash
```

The installer drops `ttg-store` in `/userdata/system/ttg-store/`, writes a
default config, creates the Ports entry and runs the first sync. Restart
EmulationStation (`batocera-es-swissknife --restart`) to see the games.

## Use

**On the device.** *Ports ▸ Teletype Games Store*. The script downloads
anything new, then restarts EmulationStation so the games show up. Ports
scripts get no console in Batocera, so the output goes to
`/userdata/system/ttg-store/ttg-store.log`.

**Over SSH.**

```sh
ttg-store list                 # compatible catalog entries; * installed, ^ update available
ttg-store sync                 # download everything new, refresh gamelists
ttg-store sync blessingofra    # just one title (never prunes)
ttg-store -n sync              # dry run
ttg-store remove c64demo       # uninstall
ttg-store config               # effective configuration
```

Global flags go **before** the subcommand: `ttg-store --roms-root /tmp/roms sync`.

## Where things land

```
/userdata/system/ttg-store/     ttg-store, config.json, state.json, catalog.json, ttg-store.log
/userdata/roms/c64/teletypegames/        blessingofra-2.0.0.prg, rabbit-1.0.0.prg, …
/userdata/roms/c64/teletypegames/images/ blessingofra.png, …
/userdata/roms/c64/gamelist.xml          our entries merged in
/userdata/roms/ports/Teletype Games Store.sh
```

Everything installed lives under the `teletypegames/` subfolder of a system, so
a prune can never reach ROMs you put there yourself. The first time a
`gamelist.xml` is touched it is copied to `gamelist.xml.ttg-backup`; on every
merge only `<game>`/`<folder>` nodes under `teletypegames/` are rewritten —
your own entries keep their play counts, favourites and scraped media.

## Configuration

`/userdata/system/ttg-store/config.json`, written by `ttg-store config --write`:

| Key | Default | Meaning |
| --- | --- | --- |
| `base_url` | `https://teletypegames.org` | WarpEngine host |
| `roms_root` | `/userdata/roms` | where systems live |
| `subfolder` | `teletypegames` | our subfolder inside each system |
| `folder_name` | `Teletype Games` | display name of that folder in ES |
| `statuses` | `["released", "archived"]` | catalog `status` values to install |
| `owner_id` | `null` | restrict to one publisher (`/api/software?owner_id=`) |
| `only` / `exclude` | `[]` | software-name allow / deny lists |
| `platforms` | c64, tic80 | platform → system, asset kind, extension, enabled |
| `prune` | `true` | remove games that left the catalog or the filters |
| `restart_es` | `true` | restart EmulationStation after a change |
| `timeout` | `30` | HTTP timeout, seconds |
| `insecure` | `false` | skip TLS verification (self-hosted test instances) |

### Platform mapping

A catalog platform is installable when its release asset is a file the Batocera
system can boot directly:

| Catalog platform | Asset kind | Batocera system | Extension |
| --- | --- | --- | --- |
| `c64` | `cartridge` | `c64` (VICE) | `.prg` |
| `tic80` | `cartridge` | `tic80` | `.tic` |

The other WarpEngine platforms (`ebitengine`, `love`, `godot`, `bevy`,
`phaser`) ship `html` and per-OS zip archives, not a ROM a Batocera system
launches, so they are not mapped. Adding one is a config edit:

```json
"platforms": { "godot": { "system": "godot", "kind": "linux_x64", "ext": ".zip", "enabled": true } }
```

A platform is also skipped when the box has no ROM folder for its system —
`ttg-store list` reports that as `skipped <name>: no '<system>' ROM folder`.

## How a sync stays safe next to EmulationStation

EmulationStation keeps gamelists in memory and writes them back when it exits,
so a merge done while it runs can be clobbered. Launched from the Ports menu
(`sync-from-es`), the tool therefore downloads first, then hands the gamelist
merge to a detached `apply-gamelists --wait-pid <es-pid>` child that waits for
the old ES process to die before writing. From SSH, with no ES running, the
merge happens inline.

Re-running a sync is idempotent: files already present are left alone, and a
release bump (`1.1` → `2.0.0`) deletes the old asset before fetching the new
one. `GET /api/download` is used rather than `/file/`, so downloads count
towards the catalog's statistics.
