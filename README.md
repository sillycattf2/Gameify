# gameify

A terminal app for Linux that shows every game installed on your system, how much disk space each one uses, how long you've played it, and when you last played it.

## Get it

This project is installed by cloning the repo, then installing it:

```bash
git clone https://github.com/YOUR-USERNAME/gameify.git
cd gameify
sudo make install
```

Then run it from anywhere with:

```bash
gameify
```

To remove it: `sudo make uninstall`. Use `sudo make install PREFIX=/usr` to install somewhere else.

### Updating

```bash
cd gameify
git pull
sudo make install
```

### Without sudo

```bash
./install.sh            # symlinks gameify into ~/.local/bin (updates with just `git pull`)
gameify                 # run it from anywhere
./install.sh uninstall  # remove the symlink
```

If `gameify` isn't found afterwards, add `~/.local/bin` to your PATH. The installer tells you how.

### Just trying it out

You can also run it straight from the clone without installing anything: `./gameify.py`

Requires Python 3.8+ and nothing else.

## What it finds

| Source | Where |
|---|---|
| Steam | Native and Flatpak installs, every library folder |
| Lutris | Installed games (native and Flatpak) |
| Heroic | Epic and GOG games |
| Desktop entries | Anything in the "Game" category: native, Flatpak, Snap |
| `/usr/games` | Classic terminal games |

Uninstalled games are hidden. Anything a launcher marks as not installed is skipped, and so is leftover clutter: a game whose install folder has been deleted (or its drive unplugged), or a menu entry whose program no longer exists.

If a game is installed on both Steam and Lutris, only the Steam entry is shown. Games are matched by name, ignoring case, punctuation and symbols like ® and ™.

## Usage

```bash
gameify                # interactive TUI
gameify --list         # plain text
gameify --json         # JSON
gameify --sort recent  # name | source | size | playtime | recent
```

| Key | Action |
|---|---|
| `↑` `↓` / `j` `k` | Move |
| `PgUp` `PgDn`, `g` `G` | Page, top / bottom |
| `/` | Search (`Esc` clears) |
| `Tab` | Cycle source filter |
| `s` | Cycle sort: name / source / size / playtime / recent |
| `Enter` | Launch |
| `q` | Quit |

## How sizes are measured

- **Steam:** the size Steam reports in its manifest.
- **Lutris, Heroic, GOG:** disk usage of the game's install folder.
- **Flatpak:** disk usage of the app itself (shared runtimes are not counted).
- **Snap:** size of the installed `.snap` image.
- **Native (apt, pacman, dnf):** installed size of the package that owns the game's executable, plus its `-data` package on Debian/Ubuntu.

Sizes are measured in the background, so the list appears instantly and the numbers fill in.
Wine prefixes, save files and shared runtimes are not included.

## Playtime and last played

| Source | Where the numbers come from |
|---|---|
| Steam | Steam's local files (`localconfig.vdf` and the game's manifest). Steam updates these when a game closes, so a game you're playing right now won't have counted yet. |
| Lutris | Lutris's own database |
| Heroic (Epic, GOG) | Heroic's playtime records (best effort, depends on your Heroic version) |
| Native, Flatpak, Snap | Nothing tracks these, so gameify times the sessions you start **from inside gameify** (press `Enter`) and saves them to `~/.local/share/gameify/playtime.json` |

Notes on the last row:
- Games you launch some other way (app menu, command line) aren't counted, so these start at `-` and only grow when you launch through gameify.
- Playtime is the time the launched process stays running. If a game starts a separate launcher and exits straight away, the time will be too short.
- If you quit gameify while a game is running, the time so far is saved.

On narrow terminals the right-hand columns are hidden to keep game names readable (last played goes first).
