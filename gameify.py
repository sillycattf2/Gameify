#!/usr/bin/env python3
"""
gameify - a terminal UI that shows every game installed on your Linux system,
how much disk space each one takes up, how long you've played it, and when you
last played it.

Detects games from:
  * Steam            (native + Flatpak installs, all library folders)
  * Lutris           (pga.db, native + Flatpak)
  * Heroic           (Epic via legendary, GOG)  -- native + Flatpak
  * .desktop files   (category "Game": native, Flatpak, Snap)
  * /usr/games       (classic Debian/Ubuntu terminal games)

Playtime / last played comes from Steam, Lutris and Heroic's own records. For
games that don't track it (native, Flatpak, Snap), gameify times the sessions
you start from inside gameify.

Usage:
  gameify                       interactive TUI (default when run in a terminal)
  gameify --list                plain text list
  gameify --json                JSON output
  gameify --sort recent         name | source | size | playtime | recent

Keys (TUI):
  Up/Down, j/k   move          PgUp/PgDn   page          g/G   top/bottom
  /              search        Esc         clear search
  Tab or f       cycle source filter       s             cycle sort
  Enter          launch game   q           quit

Only the Python 3 standard library is required.
"""

import argparse
import curses
import json
import os
import queue
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

HOME = Path.home()


@dataclass
class Game:
    name: str
    source: str
    launch: list
    path: str = ""
    install_dir: str = ""        # folder to measure, if known
    app_id: str = ""             # flatpak / snap id, if any
    size: Optional[int] = None   # bytes. None = still calculating, 0 = unknown
    playtime_seconds: int = 0    # 0 = none / unknown
    last_played: int = 0         # unix timestamp, 0 = never / unknown
    terminal: bool = False       # needs to run inside the terminal


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def read_text(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def human_size(n):
    if n is None:
        return "..."
    if not n:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def fmt_playtime(seconds):
    if not seconds:
        return "-"
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min"
    hours = seconds / 3600
    return f"{hours:.0f} h" if hours >= 100 else f"{hours:.1f} h"


def fmt_last_played(ts):
    if not ts:
        return "-"
    when = datetime.fromtimestamp(ts)
    days = (datetime.now().date() - when.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    return when.strftime("%Y-%m-%d")


def run(cmd):
    """Run a command, return stdout (or '' on any failure)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                           env=dict(os.environ, LC_ALL="C"))
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


KV = re.compile(r'"([^"]+)"\s+"([^"]*)"')
VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text):
    """Parse Valve's KeyValues format into nested dicts."""
    root = {}
    stack, key = [root], None
    for m in VDF_TOKEN.finditer(text):
        string, brace = m.groups()
        if brace == "{":
            child = {}
            stack[-1][key or ""] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = string
        else:
            stack[-1][key] = string
            key = None
    return root


def ci_get(d, name):
    """Case-insensitive dict lookup (Steam isn't consistent about key casing)."""
    if not isinstance(d, dict):
        return None
    for k, v in d.items():
        if k.lower() == name.lower():
            return v
    return None


def iso_to_ts(value):
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return 0


# --------------------------------------------------------------------------- #
# size calculation
# --------------------------------------------------------------------------- #

def dir_size(path):
    """Disk usage of a folder (like `du`): real blocks used, hardlinks counted once."""
    total, seen, stack = 0, set(), [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        continue
                    if st.st_nlink > 1:
                        key = (st.st_dev, st.st_ino)
                        if key in seen:
                            continue
                        seen.add(key)
                    total += st.st_blocks * 512
        except OSError:
            continue
    return total


FLATPAK_BASES = [Path("/var/lib/flatpak"), HOME / ".local/share/flatpak"]


def find_exe(argv):
    """First real executable in a launch command (skips `env VAR=x` prefixes)."""
    for arg in argv:
        if arg == "env" or "=" in arg:
            continue
        found = shutil.which(arg)
        if found:
            return found
    return None


def package_size(argv):
    """Installed size of the system package that owns the game's executable."""
    exe = find_exe(argv)
    if not exe:
        return 0
    paths = [exe, os.path.realpath(exe)]

    if shutil.which("dpkg-query"):
        for p in paths:
            out = run(["dpkg", "-S", p])
            if out:
                pkg = out.splitlines()[0].split(": ")[0].split(", ")[0]
                total = 0
                # games usually keep their assets in a separate "<name>-data" package
                for name in (pkg, pkg.split(":")[0] + "-data"):
                    kb = run(["dpkg-query", "-W", "-f=${Installed-Size}", name])
                    total += int(kb) * 1024 if kb.isdigit() else 0
                return total

    if shutil.which("pacman"):
        for p in paths:
            pkg = run(["pacman", "-Qoq", p])
            if pkg:
                info = run(["pacman", "-Qi", pkg.splitlines()[0]])
                m = re.search(r"Installed Size\s*:\s*([\d.]+)\s*(\w+)", info)
                if m:
                    mult = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}
                    return int(float(m.group(1)) * mult.get(m.group(2), 1))

    if shutil.which("rpm"):
        for p in paths:
            pkg = run(["rpm", "-qf", "--qf", "%{NAME}\n", p])
            if pkg:
                size = run(["rpm", "-q", "--qf", "%{SIZE}", pkg.splitlines()[0]])
                return int(size) if size.isdigit() else 0
    return 0


def snap_size(name):
    files = list(Path("/var/lib/snapd/snaps").glob(f"{name}_*.snap"))
    if not files:
        return 0
    return max(files, key=lambda f: f.stat().st_mtime).stat().st_size


def compute_size(game):
    try:
        if game.install_dir:
            return dir_size(game.install_dir)
        if game.source == "flatpak" and game.app_id:
            return sum(dir_size(b / "app" / game.app_id) for b in FLATPAK_BASES)
        if game.source == "snap" and game.app_id:
            return snap_size(game.app_id.split("_")[0])
        return package_size(game.launch)
    except Exception:
        return 0


def start_size_workers(games, workers=4):
    """Measure every game with an unknown size in background daemon threads."""
    todo = queue.Queue()
    for g in games:
        if g.size is None:
            todo.put(g)

    def work():
        while True:
            try:
                g = todo.get_nowait()
            except queue.Empty:
                return
            g.size = compute_size(g)

    threads = [threading.Thread(target=work, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    return threads


# --------------------------------------------------------------------------- #
# playtime tracking for games whose launcher doesn't track it
# --------------------------------------------------------------------------- #

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "gameify"
TRACK_FILE = DATA_DIR / "playtime.json"
LEGACY_TRACK_FILE = DATA_DIR.parent / "gamelist" / "playtime.json"   # from before the rename
TRACKED_SOURCES = {"native", "flatpak", "snap"}   # Steam/Lutris/Heroic track their own


class Tracker:
    """Times the sessions started from inside gameify and remembers them."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = {}   # token -> (game, start_time)
        self.next_token = 0
        self.data = {}
        for f in (TRACK_FILE, LEGACY_TRACK_FILE):
            try:
                self.data = json.loads(f.read_text())
                break
            except (OSError, ValueError):
                continue

    @staticmethod
    def key(game):
        return f"{game.source}:{game.name}"

    @staticmethod
    def applies_to(game):
        return game.source in TRACKED_SOURCES

    def apply(self, games):
        for g in games:
            rec = self.data.get(self.key(g)) if self.applies_to(g) else None
            if isinstance(rec, dict):
                g.playtime_seconds = int(rec.get("seconds", 0))
                g.last_played = int(rec.get("last", 0))

    def _save(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = TRACK_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            os.replace(tmp, TRACK_FILE)
        except OSError:
            pass

    def begin(self, game):
        now = time.time()
        with self.lock:
            token = self.next_token
            self.next_token += 1
            self.running[token] = (game, now)
            rec = self.data.setdefault(self.key(game), {"seconds": 0, "last": 0})
            rec["last"] = int(now)
            game.last_played = int(now)
            self._save()
        return token

    def end(self, token):
        with self.lock:
            item = self.running.pop(token, None)
            if not item:
                return
            game, start = item
            rec = self.data.setdefault(self.key(game), {"seconds": 0, "last": 0})
            rec["seconds"] = int(rec.get("seconds", 0)) + int(time.time() - start)
            game.playtime_seconds = rec["seconds"]
            self._save()

    def flush(self):
        """Called on quit: count time so far for games that are still running."""
        for token in list(self.running):
            self.end(token)


# --------------------------------------------------------------------------- #
# scanners
# --------------------------------------------------------------------------- #

STEAM_ROOTS = [
    HOME / ".steam/steam",
    HOME / ".local/share/Steam",
    HOME / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
]
STEAM_SKIP = re.compile(
    r"^(Proton\b|Steam Linux Runtime|Steamworks Common Redistributables|"
    r"Steam Controller Configs)", re.I)


def steam_stats():
    """{appid: (playtime_minutes, last_played_ts)} from Steam's localconfig.vdf."""
    stats = {}
    roots = {r.resolve() for r in STEAM_ROOTS if r.is_dir()}
    for root in roots:
        for cfg in root.glob("userdata/*/config/localconfig.vdf"):
            node = parse_vdf(read_text(cfg))
            for part in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps"):
                node = ci_get(node, part)
                if not isinstance(node, dict):
                    break
            else:
                for appid, info in node.items():
                    try:
                        minutes = int(ci_get(info, "Playtime") or 0)
                        last = int(ci_get(info, "LastPlayed") or 0)
                    except ValueError:
                        continue
                    old = stats.get(appid, (0, 0))
                    stats[appid] = (max(old[0], minutes), max(old[1], last))
    return stats


def scan_steam():
    libraries = set()
    for root in STEAM_ROOTS:
        steamapps = root / "steamapps"
        if not steamapps.is_dir():
            continue
        libraries.add(steamapps.resolve())
        for key, value in KV.findall(read_text(steamapps / "libraryfolders.vdf")):
            if key == "path":
                libraries.add(Path(value) / "steamapps")

    base = ["steam"] if shutil.which("steam") else ["xdg-open"]
    stats = steam_stats()

    games, seen = [], set()
    for lib in sorted(libraries):
        for manifest in lib.glob("appmanifest_*.acf"):
            kv = {}
            for k, v in KV.findall(read_text(manifest)):
                kv.setdefault(k, v)
            appid, name = kv.get("appid"), kv.get("name")
            if not appid or not name or appid in seen:
                continue
            try:  # StateFlags bit 4 == fully installed
                if not int(kv.get("StateFlags", "0")) & 4:
                    continue
            except ValueError:
                continue
            if STEAM_SKIP.match(name):
                continue
            seen.add(appid)
            try:
                size = int(kv.get("SizeOnDisk", "0")) or None
            except ValueError:
                size = None
            try:
                manifest_last = int(kv.get("LastPlayed", "0"))
            except ValueError:
                manifest_last = 0
            minutes, last = stats.get(appid, (0, 0))
            folder = str(lib / "common" / kv.get("installdir", ""))
            games.append(Game(
                name=name, source="steam", size=size,
                path=folder, install_dir=folder,
                playtime_seconds=minutes * 60, last_played=max(last, manifest_last),
                launch=base + [f"steam://rungameid/{appid}"],
            ))
    return games


def scan_lutris():
    db_paths = [
        HOME / ".local/share/lutris/pga.db",
        HOME / ".var/app/net.lutris.Lutris/data/lutris/pga.db",
    ]
    games = []
    for db in db_paths:
        if not db.is_file():
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT name, slug, runner, directory, playtime, lastplayed "
                    "FROM games WHERE installed = 1").fetchall()
            except sqlite3.Error:   # older Lutris without playtime columns
                rows = [r + (None, None) for r in con.execute(
                    "SELECT name, slug, runner, directory FROM games WHERE installed = 1")]
            con.close()
        except sqlite3.Error:
            continue
        flatpak = "net.lutris.Lutris" in str(db)
        for name, slug, runner, directory, playtime, lastplayed in rows:
            if not name or not slug:
                continue
            uri = f"lutris:rungame/{slug}"
            cmd = ["flatpak", "run", "net.lutris.Lutris", uri] if flatpak else ["lutris", uri]
            try:   # Lutris stores playtime as (fractional) hours
                seconds = int(float(playtime or 0) * 3600)
            except (TypeError, ValueError):
                seconds = 0
            games.append(Game(name=name, source="lutris", launch=cmd,
                              path=directory or "", install_dir=directory or "",
                              playtime_seconds=seconds, last_played=int(lastplayed or 0)))
    return games


def heroic_playtime(root, app_name):
    """(seconds, last_played_ts) from Heroic's timestamp store (best effort)."""
    try:
        data = json.loads(read_text(root / "store/timestamp.json") or "{}")
        rec = data.get(app_name) or (data.get("games") or {}).get(app_name) or {}
        return int(float(rec.get("totalPlayed") or 0) * 60), iso_to_ts(rec.get("lastPlayed"))
    except (ValueError, AttributeError, TypeError):
        return 0, 0


def scan_heroic():
    roots = [
        HOME / ".config/heroic",
        HOME / ".var/app/com.heroicgameslauncher.hgl/config/heroic",
    ]
    games = []
    for root in roots:
        if not root.is_dir():
            continue

        # Epic Games via legendary
        try:
            data = json.loads(read_text(root / "legendaryConfig/legendary/installed.json") or "{}")
            for app_name, info in data.items():
                path = info.get("install_path", "")
                seconds, last = heroic_playtime(root, app_name)
                games.append(Game(
                    name=info.get("title") or app_name, source="epic",
                    path=path, install_dir=path,
                    size=int(info.get("install_size") or 0) or None,
                    playtime_seconds=seconds, last_played=last,
                    launch=["xdg-open", f"heroic://launch/legendary/{app_name}"],
                ))
        except (ValueError, AttributeError):
            pass

        # GOG
        try:
            installed = json.loads(read_text(root / "gog_store/installed.json") or "{}")
            library = json.loads(read_text(root / "store_cache/gog_library.json") or "{}")
            titles = {g.get("app_name"): g.get("title") for g in library.get("games", [])}
            for item in installed.get("installed", []):
                app_name = item.get("appName", "")
                path = item.get("install_path", "")
                name = titles.get(app_name) or Path(path).name or app_name
                seconds, last = heroic_playtime(root, app_name)
                games.append(Game(
                    name=name, source="gog", path=path, install_dir=path,
                    playtime_seconds=seconds, last_played=last,
                    launch=["xdg-open", f"heroic://launch/gog/{app_name}"],
                ))
        except (ValueError, AttributeError):
            pass
    return games


DESKTOP_DIRS = [
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    HOME / ".local/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),
    HOME / ".local/share/flatpak/exports/share/applications",
    Path("/var/lib/snapd/desktop/applications"),
]
# game stores / launchers that carry the "Game" category but aren't games
LAUNCHER_IDS = {
    "steam", "lutris", "heroic", "bottles", "net.lutris.lutris",
    "com.valvesoftware.steam", "com.heroicgameslauncher.hgl",
    "com.usebottles.bottles", "itch", "io.itch.itch",
}


def parse_desktop(path):
    entry, in_entry = {}, False
    for line in read_text(path).splitlines():
        line = line.strip()
        if line.startswith("["):
            in_entry = line == "[Desktop Entry]"
        elif in_entry and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            entry.setdefault(k.strip(), v.strip())
    return entry


def scan_desktop_files():
    games, exec_names, seen_ids = [], set(), set()
    for d in DESKTOP_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.desktop")):
            if f.stem.lower() in LAUNCHER_IDS:
                continue
            e = parse_desktop(f)
            if e.get("Type", "Application") != "Application":
                continue
            if e.get("NoDisplay", "").lower() == "true" or e.get("Hidden", "").lower() == "true":
                continue
            if "Game" not in e.get("Categories", "").split(";"):
                continue
            exec_line = e.get("Exec", "")
            # Launchers' own shortcuts for games we already list via their scanners
            if any(s in exec_line for s in ("steam://", "lutris:", "heroic://")):
                continue
            exec_line = exec_line.replace("%%", "\0")
            exec_line = re.sub(r"%[a-zA-Z]", "", exec_line).replace("\0", "%")
            try:
                argv = shlex.split(exec_line)
            except ValueError:
                continue
            if not argv or not e.get("Name"):
                continue

            sp = str(f)
            source = "flatpak" if "flatpak" in sp else "snap" if "snapd" in sp else "native"
            key = (source, e["Name"].lower())
            if key in seen_ids:
                continue
            seen_ids.add(key)
            exec_names.add(Path(argv[0]).name)
            games.append(Game(
                name=e["Name"], source=source, launch=argv, path=sp, app_id=f.stem,
                terminal=e.get("Terminal", "").lower() == "true",
            ))
    return games, exec_names


def scan_usr_games(already):
    games = []
    for d in (Path("/usr/games"), Path("/usr/local/games")):
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.is_file() and os.access(f, os.X_OK) and f.name not in already:
                games.append(Game(name=f.name, source="native", launch=[str(f)],
                                  path=str(f), terminal=True))
    return games


def is_missing(game):
    """True if the game's files or launcher are gone (a leftover entry from an uninstall)."""
    try:
        if game.install_dir:
            return not Path(game.install_dir).is_dir()
        if game.source != "native":   # launcher-run games (Lutris, Heroic...), Flatpak, Snap
            return False
        for arg in game.launch:       # native: is the program still there?
            if arg == "env" or "=" in arg:
                continue
            if arg.startswith("/"):
                return not os.path.exists(arg)
            if "/" in arg:
                return False          # relative path, can't tell - keep it
            return shutil.which(arg) is None
    except OSError:
        pass
    return False


def normalize_name(name):
    """Loose form of a game name so 'Half-Life®' and 'half life' compare equal."""
    name = name.casefold().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", name)


def hide_lutris_duplicates(games):
    """If a game is on Steam, hide the copy that Lutris also lists."""
    steam_names = {normalize_name(g.name) for g in games if g.source == "steam"}
    return [g for g in games
            if not (g.source == "lutris" and normalize_name(g.name) in steam_names)]


def find_games():
    games = []
    for scanner in (scan_steam, scan_lutris, scan_heroic):
        try:
            games += scanner()
        except Exception:  # never let one broken launcher kill the app
            pass
    try:
        desktop_games, exec_names = scan_desktop_files()
        games += desktop_games
        games += scan_usr_games(exec_names)
    except Exception:
        pass
    games = [g for g in games if not is_missing(g)]
    games = hide_lutris_duplicates(games)
    games.sort(key=lambda g: g.name.lower())
    return games


# --------------------------------------------------------------------------- #
# TUI
# --------------------------------------------------------------------------- #

SORTS = ("name", "source", "size", "playtime", "recent")
SOURCE_COLORS = {"steam": 4, "lutris": 5, "epic": 6, "gog": 6,
                 "flatpak": 7, "snap": 7, "native": 8}

# (header, width, alignment, value getter) - dropped right-to-left on narrow terminals
COLUMNS = [
    ("SOURCE", 9, "<", lambda g: g.source),
    ("SIZE", 9, ">", lambda g: human_size(g.size)),
    ("PLAYED", 9, ">", lambda g: fmt_playtime(g.playtime_seconds)),
    ("LAST PLAYED", 12, "<", lambda g: fmt_last_played(g.last_played)),
]


def sort_games(games, mode):
    name = lambda g: g.name.lower()
    if mode == "source":
        return sorted(games, key=lambda g: (g.source, name(g)))
    if mode == "size":
        return sorted(games, key=lambda g: (-(g.size or 0), name(g)))
    if mode == "playtime":
        return sorted(games, key=lambda g: (-g.playtime_seconds, name(g)))
    if mode == "recent":
        return sorted(games, key=lambda g: (-g.last_played, name(g)))
    return sorted(games, key=name)


def tui(stdscr, all_games, tracker, start_sort=0):
    curses.curs_set(0)
    colors = curses.has_colors()
    if colors:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)   # selection
        curses.init_pair(2, curses.COLOR_CYAN, -1)                   # title
        curses.init_pair(3, curses.COLOR_YELLOW, -1)                 # status
        curses.init_pair(4, curses.COLOR_BLUE, -1)
        curses.init_pair(5, curses.COLOR_MAGENTA, -1)
        curses.init_pair(6, curses.COLOR_GREEN, -1)
        curses.init_pair(7, curses.COLOR_YELLOW, -1)
        curses.init_pair(8, curses.COLOR_WHITE, -1)

    def put(y, x, text, attr=0):
        h, w = stdscr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                stdscr.addstr(y, x, text[: max(0, w - x - 1)], attr)
            except curses.error:
                pass

    sources = ["all"] + sorted({g.source for g in all_games})
    src_i = sel = top = 0
    sort_i = start_sort
    query, searching, message = "", False, ""

    while True:
        # ---- build the current view ----
        view = all_games
        if sources[src_i] != "all":
            view = [g for g in view if g.source == sources[src_i]]
        if query:
            q = query.lower()
            view = [g for g in view if q in g.name.lower()]
        view = sort_games(view, SORTS[sort_i])
        pending = any(g.size is None for g in all_games)
        total_size = sum(g.size or 0 for g in view)
        total_play = sum(g.playtime_seconds for g in view)

        h, w = stdscr.getmaxyx()
        list_h = h - 6
        sel = max(0, min(sel, len(view) - 1))
        if list_h > 0:
            if sel < top:
                top = sel
            elif sel >= top + list_h:
                top = sel - list_h + 1
            top = max(0, min(top, max(0, len(view) - list_h)))

        # ---- layout: drop right-hand columns until the name column has room ----
        cols = list(COLUMNS)
        while cols and (w - 2) - sum(1 + c[1] for c in cols) < 18:
            cols.pop()
        name_w = max(10, (w - 2) - sum(1 + c[1] for c in cols))

        def render(name, cell_of):
            line = f"{name[:name_w]:<{name_w}}"
            for header, width, align, getter in cols:
                line += f" {cell_of(getter, header)[:width]:{align}{width}}"
            return line

        # ---- draw ----
        stdscr.erase()
        if h < 8 or w < 36:
            put(0, 0, "Terminal too small")
        else:
            title = (f" GAMEIFY  -  {len(view)} shown / {len(all_games)} installed"
                     f"  -  {human_size(total_size) if total_size else '-'} used")
            if total_play:
                title += f"  -  {fmt_playtime(total_play)} played"
            if pending:
                title += "  (measuring...)"
            put(0, 0, title.ljust(w - 1),
                curses.color_pair(2) | curses.A_BOLD if colors else curses.A_BOLD)
            cursor = "_" if searching else ""
            put(1, 1, f"Source: {sources[src_i]}   Sort: {SORTS[sort_i]}   Search: {query}{cursor}",
                curses.A_BOLD if searching else curses.A_DIM)

            put(2, 1, render("NAME", lambda getter, header: header), curses.A_UNDERLINE)

            if not view:
                put(4, 2, "No games found." if not all_games else "No matches.")
            for row in range(list_h):
                idx = top + row
                if idx >= len(view):
                    break
                g = view[idx]
                line = render(g.name, lambda getter, header, g=g: getter(g))
                if idx == sel:
                    put(3 + row, 1, line.ljust(w - 2),
                        curses.color_pair(1) if colors else curses.A_REVERSE)
                else:
                    put(3 + row, 1, line)
                    if colors and cols and cols[0][0] == "SOURCE":
                        put(3 + row, 2 + name_w, f"{g.source:<{cols[0][1]}}",
                            curses.color_pair(SOURCE_COLORS.get(g.source, 8)))

            if view:
                g = view[sel]
                detail = g.path or " ".join(g.launch)
                if g.last_played:
                    stamp = datetime.fromtimestamp(g.last_played).strftime("%Y-%m-%d %H:%M")
                    detail += f"   |   last played {stamp}"
                put(h - 3, 1, detail, curses.A_DIM)
            put(h - 2, 1, message, curses.color_pair(3) if colors else 0)
            help_txt = ("Type to search | Enter accept | Esc clear" if searching else
                        "j/k move  / search  Tab source  s sort  Enter launch  q quit")
            put(h - 1, 1, help_txt, curses.A_DIM)
        stdscr.refresh()

        # ---- input (wake up regularly while sizes are still being measured) ----
        stdscr.timeout(300 if pending else -1)
        try:
            key = stdscr.get_wch()
        except curses.error:
            continue
        message = ""

        if key == curses.KEY_RESIZE:
            continue

        if searching:
            if key in ("\n", "\r", curses.KEY_ENTER):
                searching = False
            elif key == "\x1b":
                query, searching = "", False
            elif key in ("\x7f", "\b", curses.KEY_BACKSPACE):
                query = query[:-1]
                sel = top = 0
            elif key == curses.KEY_UP:
                sel -= 1
            elif key == curses.KEY_DOWN:
                sel += 1
            elif isinstance(key, str) and key.isprintable():
                query += key
                sel = top = 0
            continue

        if key in ("q", "Q"):
            return
        elif key in (curses.KEY_UP, "k"):
            sel -= 1
        elif key in (curses.KEY_DOWN, "j"):
            sel += 1
        elif key == curses.KEY_PPAGE:
            sel -= max(1, list_h)
        elif key == curses.KEY_NPAGE:
            sel += max(1, list_h)
        elif key in (curses.KEY_HOME, "g"):
            sel = 0
        elif key in (curses.KEY_END, "G"):
            sel = len(view) - 1
        elif key == "/":
            searching = True
        elif key == "\x1b":
            query, sel, top = "", 0, 0
        elif key in ("\t", "f"):
            src_i = (src_i + 1) % len(sources)
            sel = top = 0
        elif key == "s":
            sort_i = (sort_i + 1) % len(SORTS)
        elif key in ("\n", "\r", curses.KEY_ENTER) and view:
            g = view[sel]
            track = tracker.applies_to(g)
            try:
                if g.terminal:
                    token = tracker.begin(g) if track else None
                    curses.endwin()
                    try:
                        subprocess.call(g.launch)
                    finally:
                        if token is not None:
                            tracker.end(token)
                        stdscr.refresh()
                else:
                    proc = subprocess.Popen(
                        g.launch, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True)
                    message = f"Launched {g.name}"
                    if track:
                        token = tracker.begin(g)
                        threading.Thread(
                            target=lambda p=proc, t=token: (p.wait(), tracker.end(t)),
                            daemon=True).start()
            except (OSError, subprocess.SubprocessError) as exc:
                message = f"Could not launch {g.name}: {exc}"


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        prog="gameify",
        description="List all games installed on this system, with disk usage and playtime.")
    ap.add_argument("--list", action="store_true", help="print a plain text list and exit")
    ap.add_argument("--json", action="store_true", help="print JSON and exit")
    ap.add_argument("--sort", choices=SORTS, default="name", help="sort order (default: name)")
    args = ap.parse_args()

    games = find_games()
    tracker = Tracker()
    tracker.apply(games)
    workers = start_size_workers(games)
    interactive = sys.stdout.isatty() and sys.stdin.isatty()

    if args.json or args.list or not interactive:
        for t in workers:   # batch modes: wait until every size is known
            t.join()
        games = sort_games(games, args.sort)
        if args.json:
            print(json.dumps([asdict(g) for g in games], indent=2))
            return
        print(f"{'NAME':<40} {'SOURCE':<8} {'SIZE':>9} {'PLAYED':>9}  LAST PLAYED")
        for g in games:
            print(f"{g.name[:40]:<40} {g.source:<8} {human_size(g.size):>9} "
                  f"{fmt_playtime(g.playtime_seconds):>9}  {fmt_last_played(g.last_played)}")
        print(f"\n{len(games)} games found, "
              f"{human_size(sum(g.size or 0 for g in games))} used, "
              f"{fmt_playtime(sum(g.playtime_seconds for g in games))} played")
        return

    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(tui, games, tracker, SORTS.index(args.sort))
    finally:
        tracker.flush()


if __name__ == "__main__":
    main()
