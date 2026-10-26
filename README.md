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
| Steam | Native and Flatpak installs, every library folder, plus games you added with "Add a Non-Steam Game" (shown as `non-steam`) |
| Lutris | Installed games (native and Flatpak) |
| Heroic | Epic and GOG games |
| Desktop entries | Anything in the "Game" category: native, Flatpak, Snap |
| `/usr/games` | Classic terminal games |

Uninstalled and half-installed games are hidden. A game is skipped when:

- its launcher marks it as not installed (including Steam games still downloading),
- its install folder is missing or empty (for example, you deleted it or the drive is unplugged),
- it's a Lutris game whose config file is missing or whose executable no longer exists, which is what a cancelled or failed install leaves behind,
- it's a menu entry whose program no longer exists.

Steam games are only listed under Steam. Lutris also imports your Steam library and marks even half-downloaded games as installed, so gameify ignores those Lutris copies and goes by what Steam says is really installed. (If Steam isn't found on your system, they're kept.) Games are matched by name, ignoring case, punctuation and symbols like ® and ™.

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
| `Tab` / `←` `→` | Switch source tab |
| `s` | Cycle sort: name / source / size / playtime / recent |
| `Enter` | Launch. gameify closes while you play and reopens when you quit the game |
| `q` | Quit |

## How sizes are measured

- **Steam:** the size Steam reports in its manifest.
- **Steam Workshop:** the games that have Workshop items get a WORKSHOP column with the space those items use (the total Steam reports, or the folder size if Steam has no record). It isn't included in the SIZE column. The total at the top and the details line at the bottom add the two together.
- **Lutris, Heroic, GOG:** disk usage of the game's install folder.
- **Flatpak:** disk usage of the app itself (shared runtimes are not counted).
- **Snap:** size of the installed `.snap` image.
- **Native (apt, pacman, dnf):** installed size of the package that owns the game's executable, plus its `-data` package on Debian/Ubuntu.

Sizes are measured in the background, so the list appears instantly and the numbers fill in.
Wine prefixes, save files and shared runtimes are not included.

## Playtime and last played

| Source | Where the numbers come from |
|---|---|
| Steam | Steam's local files (`localconfig.vdf` and the game's manifest). Steam saves these on its own schedule, so the numbers can lag behind a game you just played. |
| Lutris | Lutris's own database |
| Heroic (Epic, GOG) | Heroic's playtime records (best effort, depends on your Heroic version) |
| Native, Flatpak, Snap | Nothing tracks these, so gameify times the sessions you start **from inside gameify** (press `Enter`) and saves them to `~/.local/share/gameify/playtime.json` |

Notes on the last row:
- Games you launch some other way (app menu, command line) aren't counted, so these start at `-` and only grow when you launch through gameify.
- Playtime is the time the launched process stays running. If a game starts a separate launcher and exits straight away, the time will be too short.
- If you quit gameify while a game is running, the time so far is saved.

On narrow terminals the right-hand columns are hidden to keep game names readable (last played goes first).

## What happens when you launch a game

Press `Enter` and gameify clears the screen, starts the game, and waits. When you close the game, gameify comes back on its own with the list refreshed, so playtime and last played are up to date. Press `Ctrl+C` while waiting to come back right away (the game keeps running).

How it knows the game has closed:

| Source | How |
|---|---|
| Native, Flatpak, Snap | It waits for the program it started. A game that starts a separate launcher and exits straight away will make gameify come back early. |
| Steam, non-Steam | It watches for Steam's game process (`SteamLaunch AppId=...`). |
| Lutris | It watches for Lutris's `lutris-wrapper` process. |
| Heroic (Epic, GOG) | Best effort: it watches for Heroic's `launch <game>` process. |

If gameify can't see a launcher's game start within 3 minutes, it comes back and says so. If it has no way to watch a game at all, it asks you to press `Enter` when you're done.

## Stays up to date

gameify updates itself while it's open. Install a game (or remove one) and the list changes within a few seconds, with a note like `New: Hades` at the bottom. Your selection stays where it was. The green `● live` in the corner shows it's watching.

It watches Steam's library and manifests, your non-Steam shortcuts, Lutris and Heroic's records, and the app menu folders (native, Flatpak and Snap). A Steam game appears once its download finishes, not while it's still downloading.

Games you delete by hand outside any launcher (for example, removing a folder) are noticed the next time something else changes, or when you restart gameify.

## Colors

gameify uses your terminal's own color palette, so it follows whatever theme you use (Dracula, Gruvbox, Solarized, Nord, light or dark). It never sets fixed colors of its own. Change your terminal theme and gameify changes with it.

On terminals with no color support it falls back to bold, dim and reversed text, and in a plain ASCII locale it uses simple characters instead of box-drawing symbols.
