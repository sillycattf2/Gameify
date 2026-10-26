#!/usr/bin/env python3
"""
gameify - a terminal UI that shows every game installed on your Linux system,
how much disk space each one takes up, how long you've played it, and when you
last played it.

Detects games from:
  * Steam            (native + Flatpak installs, all library folders,
                      plus games you added with "Add a Non-Steam Game")
  * Lutris           (pga.db, native + Flatpak)
  * Heroic           (Epic via legendary, GOG)  -- native + Flatpak
  * .desktop files   (category "Game": native, Flatpak, Snap)
  * /usr/games       (classic Debian/Ubuntu terminal games)

The list updates by itself when games are installed or removed.

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
  Tab, Left/Right  switch source tab       s             cycle sort
  Enter          launch game (gameify closes while you play, then reopens)
  q              quit

Only the Python 3 standard library is required.
"""

import argparse
import curses
import json
import locale
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
import zlib
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
    workshop_size: int = 0       # bytes used by Steam Workshop items for this game
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
        if game.source not in ("native", "flatpak", "snap"):
            return 0   # launcher-run game with no known folder: don't measure the launcher itself
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


def parse_binary_vdf(data):
    """Parse Valve's binary KeyValues format (used by shortcuts.vdf) into nested dicts."""
    pos = 0

    def read_cstr():
        nonlocal pos
        end = data.index(b"\x00", pos)
        text = data[pos:end].decode("utf-8", "replace")
        pos = end + 1
        return text

    def read_int(size):
        nonlocal pos
        value = int.from_bytes(data[pos:pos + size], "little")
        pos += size
        return value

    def read_dict():
        nonlocal pos
        result = {}
        while pos < len(data):
            kind = data[pos]
            pos += 1
            if kind == 0x08:                 # end of this section
                return result
            key = read_cstr()
            if kind == 0x00:
                result[key] = read_dict()
            elif kind == 0x01:
                result[key] = read_cstr()
            elif kind in (0x02, 0x03, 0x04, 0x06):
                result[key] = read_int(4)
            elif kind in (0x07, 0x0A):
                result[key] = read_int(8)
            else:
                raise ValueError(f"unknown VDF type {kind}")
        return result

    return read_dict()


# folders that hold lots of unrelated files: never measure these as "the game's folder"
SHARED_FOLDERS = {
    Path("/"), HOME, HOME / "Downloads", HOME / "Desktop", HOME / "Documents", HOME / "Games",
    HOME / "bin", HOME / ".local/bin", HOME / "Applications", HOME / "AppImages", Path("/opt"),
}
SYSTEM_FOLDERS = [Path(p) for p in (
    "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/var", "/snap", "/proc", "/sys",
    "/dev", "/boot", "/run")]


def shortcut_folder(exe):
    """The game's own folder (the program's parent), or '' if it's a shared place."""
    parent = Path(os.path.realpath(exe)).parent
    if parent in SHARED_FOLDERS:
        return ""
    if any(parent == p or p in parent.parents for p in SYSTEM_FOLDERS):
        return ""
    return str(parent)


def scan_steam_shortcuts(stats, launch_base):
    """Games added to Steam with "Add a Non-Steam Game" (stored in shortcuts.vdf)."""
    games, seen = [], set()
    for root in sorted({r.resolve() for r in STEAM_ROOTS if r.is_dir()}):
        for cfg in sorted(root.glob("userdata/*/config/shortcuts.vdf")):
            try:
                tree = parse_binary_vdf(cfg.read_bytes())
            except (OSError, ValueError, IndexError):
                continue
            shortcuts = ci_get(tree, "shortcuts")
            if not isinstance(shortcuts, dict):
                continue
            for entry in shortcuts.values():
                name, raw_exe = ci_get(entry, "AppName"), ci_get(entry, "Exe")
                if not isinstance(name, str) or not isinstance(raw_exe, str):
                    continue
                exe = raw_exe.strip().strip("\"'")
                if not name or not exe or ci_get(entry, "IsHidden") == 1:
                    continue
                appid = ci_get(entry, "appid")
                if not isinstance(appid, int):   # older Steam: derived from program + name
                    appid = zlib.crc32((raw_exe + name).encode()) | 0x80000000
                appid &= 0xFFFFFFFF
                if appid in seen:
                    continue
                path = exe if os.path.isabs(exe) else shutil.which(exe)
                if not path or not os.path.exists(path):
                    continue   # the program has been removed
                seen.add(appid)
                minutes, last = stats.get(str(appid), (0, 0))
                last_play = ci_get(entry, "LastPlayTime")
                games.append(Game(
                    name=name, source="non-steam", app_id=str(appid), path=path,
                    install_dir=shortcut_folder(path), playtime_seconds=minutes * 60,
                    last_played=max(last, last_play if isinstance(last_play, int) else 0),
                    launch=launch_base + [f"steam://rungameid/{(appid << 32) | 0x02000000}"],
                ))
    return games


def steam_libraries():
    """Every Steam library's steamapps folder (including ones on other drives)."""
    libraries = set()
    for root in STEAM_ROOTS:
        steamapps = root / "steamapps"
        if not steamapps.is_dir():
            continue
        libraries.add(steamapps.resolve())
        for key, value in KV.findall(read_text(steamapps / "libraryfolders.vdf")):
            if key == "path":
                libraries.add(Path(value) / "steamapps")
    return libraries


def workshop_size_for(libraries, appid):
    """Disk space used by a game's Steam Workshop items (0 if it has none)."""
    total = 0
    for lib in libraries:
        kv = {}
        for k, v in KV.findall(read_text(lib / "workshop" / f"appworkshop_{appid}.acf")):
            kv.setdefault(k, v)
        try:
            size = int(kv.get("SizeOnDisk", "0"))   # the total Steam itself reports
        except ValueError:
            size = 0
        content = lib / "workshop" / "content" / appid
        if not size and content.is_dir():            # no usable manifest: measure the folder
            size = dir_size(content)
        total += size
    return total


def scan_steam():
    libraries = steam_libraries()

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
                name=name, source="steam", size=size, app_id=appid,
                workshop_size=workshop_size_for(libraries, appid),
                path=folder, install_dir=folder,
                playtime_seconds=minutes * 60, last_played=max(last, manifest_last),
                launch=base + [f"steam://rungameid/{appid}"],
            ))
    try:
        games += scan_steam_shortcuts(stats, base)
    except Exception:
        pass
    return games


LUTRIS_EXE_KEY = re.compile(r"^  (?:exe|main_file):\s*(.+?)\s*$", re.M)


def lutris_install_incomplete(games_dir, configpath):
    """True if Lutris couldn't launch this game: its config is gone or points at a missing file."""
    if not configpath or not games_dir.is_dir():
        return False
    config = games_dir / f"{configpath}.yml"
    if not config.is_file():
        return True
    for value in LUTRIS_EXE_KEY.findall(read_text(config)):
        value = value.strip("'\"")
        if value.startswith("/") and not os.path.exists(value):
            return True
    return False


XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
LUTRIS_SOURCES = [   # (database, folder holding the per-game config files)
    (HOME / ".local/share/lutris/pga.db", XDG_CONFIG / "lutris/games"),
    (HOME / ".var/app/net.lutris.Lutris/data/lutris/pga.db",
     HOME / ".var/app/net.lutris.Lutris/config/lutris/games"),
]


def scan_lutris():
    sources = LUTRIS_SOURCES
    steam_here = any((root / "steamapps").is_dir() for root in STEAM_ROOTS)
    games = []
    for db, games_dir in sources:
        if not db.is_file():
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            have = {r[1] for r in con.execute("PRAGMA table_info(games)")}
            col = lambda c: c if c in have else "NULL"   # older Lutris lacks some columns
            rows = con.execute(
                f"SELECT name, slug, runner, directory, {col('playtime')}, "
                f"{col('lastplayed')}, {col('configpath')}, {col('service')} "
                f"FROM games WHERE installed = 1"
            ).fetchall()
            con.close()
        except sqlite3.Error:
            continue
        flatpak = "net.lutris.Lutris" in str(db)
        for name, slug, runner, directory, playtime, lastplayed, configpath, service in rows:
            if not name or not slug:
                continue
            # Games Lutris merely imported from Steam: Lutris marks the whole Steam library
            # (even half-downloaded games) as installed. Steam knows what is really installed
            # and those games are listed under "steam", so skip Lutris's copy.
            if steam_here and "steam" in (runner, service):
                continue
            if lutris_install_incomplete(games_dir, configpath):
                continue   # half-installed or broken: Lutris can't launch it
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


HEROIC_ROOTS = [
    HOME / ".config/heroic",
    HOME / ".var/app/com.heroicgameslauncher.hgl/config/heroic",
]


def scan_heroic():
    roots = HEROIC_ROOTS
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
            folder = Path(game.install_dir)
            return not folder.is_dir() or not any(folder.iterdir())
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
# auto-update: notice installs and uninstalls while the TUI is open
# --------------------------------------------------------------------------- #

POLL_SECONDS = 2   # how often the cheap "did anything change?" check runs
RESCAN_GAP = 5     # never rescan more often than this (Steam rewrites files while downloading)


def watch_paths():
    """The files and folders whose changes mean a game was installed or removed."""
    paths = []
    for lib in steam_libraries():
        paths += [lib, lib / "libraryfolders.vdf"] + sorted(lib.glob("appmanifest_*.acf"))
        paths += [lib / "workshop"] + sorted(lib.glob("workshop/appworkshop_*.acf"))
    for root in STEAM_ROOTS:
        paths += sorted(root.glob("userdata/*/config/shortcuts.vdf"))
    for db, _games_dir in LUTRIS_SOURCES:
        paths += [db, Path(f"{db}-wal")]
    for root in HEROIC_ROOTS:
        paths += [root / "legendaryConfig/legendary/installed.json",
                  root / "gog_store/installed.json"]
    paths += DESKTOP_DIRS + [Path("/usr/games"), Path("/usr/local/games")]
    return paths


def watch_fingerprint():
    """A snapshot that changes whenever something gameify reads has changed."""
    snapshot = []
    for path in watch_paths():
        try:
            st = os.stat(path)
            snapshot.append((str(path), st.st_mtime_ns, st.st_size))
        except OSError:
            snapshot.append((str(path), None, None))   # missing now; noticed if it appears
    return tuple(snapshot)


def describe_changes(before, after):
    """'New: Hades   Removed: Portal 2' from two sets of (source, name)."""
    def names(keys):
        listed = sorted({name for _source, name in keys}, key=str.lower)
        if len(listed) > 3:
            return ", ".join(listed[:3]) + f" and {len(listed) - 3} more"
        return ", ".join(listed)

    parts = []
    if after - before:
        parts.append("New: " + names(after - before))
    if before - after:
        parts.append("Removed: " + names(before - after))
    return "   ".join(parts)


# --------------------------------------------------------------------------- #
# TUI look & feel
# --------------------------------------------------------------------------- #

SORTS = ("name", "source", "size", "playtime", "recent")
SORT_LABELS = {"name": "Name", "source": "Source", "size": "Size",
               "playtime": "Playtime", "recent": "Last played"}
SORT_COLUMN = {"name": "name", "source": "source", "size": "size",
               "playtime": "played", "recent": "last"}
SORT_DESCENDING = {"size", "playtime", "recent"}   # biggest / most recent first

SOURCE_ORDER = ["steam", "non-steam", "lutris", "epic", "gog", "flatpak", "snap", "native"]
SOURCE_NAMES = {"steam": "Steam", "non-steam": "Non-Steam", "lutris": "Lutris", "epic": "Epic",
                "gog": "GOG", "flatpak": "Flatpak", "snap": "Snap", "native": "Native"}

MIN_W, MIN_H = 50, 14
BAR_W = 8          # width of the little size bars
CHROME_ROWS = 11   # screen rows used by everything except the game rows

GLYPHS_UNICODE = dict(
    tl="╭", tr="╮", bl="╰", br="╯", h="─", v="│", ml="├", mr="┤",
    full="█", empty="░", dot="●", pointer="›", up="▲", down="▼", sep="·", thumb="┃",
    cursor="▏", more_l="‹", more_r="›", ellipsis="…", logo="◆",
    enter="⏎", updown="↑↓", leftright="←→", spinner="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
GLYPHS_ASCII = dict(
    tl="+", tr="+", bl="+", br="+", h="-", v="|", ml="+", mr="+",
    full="#", empty=".", dot="*", pointer=">", up="^", down="v", sep="-", thumb="#",
    cursor="_", more_l="<", more_r=">", ellipsis="~", logo="*",
    enter="Enter", updown="j/k", leftright="h/l", spinner="|/-\\")

# Every colour role is a terminal palette colour *name* plus style flags (b=bold, d=dim,
# r=reverse), never a fixed RGB value, so the terminal's theme decides what they look like.
THEME_SPEC = {
    "base": (None, ""), "dim": (None, "d"), "border": (None, "d"),
    "accent": ("cyan", ""), "accent_b": ("cyan", "b"),
    "pill": ("cyan", "rb"), "keycap": (None, "r"),
    "sel": ("cyan", "r"), "sel_b": ("cyan", "rb"), "sel_accent": ("cyan", "rb"),
    "good": ("green", ""), "warn": ("yellow", ""), "bad": ("red", ""), "hot": ("yellow", "b"),
    "src:steam": ("blue", "b"), "src:non-steam": ("cyan", "b"), "src:lutris": ("yellow", "b"),
    "src:epic": (None, "b"), "src:gog": ("magenta", "b"), "src:flatpak": ("green", "b"),
    "src:snap": ("red", "b"), "src:native": (None, ""),
}
THEME_KEYS = list(THEME_SPEC)
COLOR_IDS = {"black": 0, "red": 1, "green": 2, "yellow": 3,
             "blue": 4, "magenta": 5, "cyan": 6, "white": 7}


def source_name(source):
    return SOURCE_NAMES.get(source, source.title())


def source_list(games):
    present = {g.source for g in games}
    return ["all"] + [s for s in SOURCE_ORDER if s in present] + sorted(present - set(SOURCE_ORDER))


def fit(text, width, align="<", ellipsis="~"):
    """Pad or cut text to exactly `width` characters."""
    if width <= 0:
        return ""
    if len(text) > width:
        text = text[:width - 1] + ellipsis if width > 1 else text[:width]
    return f"{text:{align}{width}}"


def make_theme():
    """Curses attributes for every colour role. Colours are the terminal's own palette
    (the 8 base ANSI colours and its default text / background), so they follow its theme."""
    colors = curses.has_colors()
    if colors:
        curses.start_color()
        curses.use_default_colors()
    pairs = {}

    def attr_for(color, flags):
        attr = 0
        if "b" in flags:
            attr |= curses.A_BOLD
        if "d" in flags:
            attr |= curses.A_DIM
        if "r" in flags:
            attr |= curses.A_REVERSE
        if colors and color:
            if color not in pairs:
                pairs[color] = len(pairs) + 1
                curses.init_pair(pairs[color], COLOR_IDS[color], -1)   # -1 = terminal default bg
            attr |= curses.color_pair(pairs[color])
        return attr

    return {role: attr_for(*spec) for role, spec in THEME_SPEC.items()}


def pick_columns(inner, workshop=False):
    """Choose the columns that fit: (columns, name width, show size bars)."""
    def make(bars):
        every = {"source": ("source", "SOURCE", 11, "<"),
                 "size": ("size", "SIZE", BAR_W + 10 if bars else 9, "<" if bars else ">"),
                 "workshop": ("workshop", "WORKSHOP", 9, ">"),
                 "played": ("played", "PLAYED", 8, ">"),
                 "last": ("last", "LAST PLAYED", 13, "<")}
        order = ["source", "size"] + (["workshop"] if workshop else []) + ["played", "last"]
        return {key: every[key] for key in order}

    def name_width(cols):
        return inner - 4 - sum(2 + c[2] for c in cols.values())

    for bars, need in ((True, 26), (False, 18)):
        cols = make(bars)
        if name_width(cols) >= need:
            return list(cols.values()), name_width(cols), bars
    cols = make(False)
    for key in ("last", "played", "workshop", "size", "source"):   # least important first
        if name_width(cols) >= 18:
            break
        cols.pop(key, None)
    return list(cols.values()), name_width(cols), False


def cell_parts(g, key, width, bars, max_size, T, G):
    """The (text, attr) pieces that make up one table cell."""
    if key == "source":
        label = fit(f"{G['dot']} {source_name(g.source)}", width, "<", G["ellipsis"])
        return [(label, T.get("src:" + g.source, T["accent"]))]
    if key == "size":
        text_attr = T["base"] if g.size else T["dim"]
        if not bars:
            return [(fit(human_size(g.size), width, ">", G["ellipsis"]), text_attr)]
        ratio = (g.size or 0) / max_size if max_size else 0
        filled = max(1, round(ratio * BAR_W)) if g.size and max_size else 0
        tier = (T["bad"] if ratio >= .75 else T["hot"] if ratio >= .5
                else T["warn"] if ratio >= .25 else T["good"])
        text = " " + fit(human_size(g.size), width - BAR_W - 1, ">", G["ellipsis"])
        return [(G["full"] * filled, tier), (G["empty"] * (BAR_W - filled), T["border"]),
                (text, text_attr)]
    if key == "played":
        return [(fit(fmt_playtime(g.playtime_seconds), width, ">", G["ellipsis"]),
                 T["base"] if g.playtime_seconds else T["dim"])]
    if key == "workshop":
        text = human_size(g.workshop_size) if g.workshop_size else "-"
        return [(fit(text, width, ">", G["ellipsis"]), T["base"] if g.workshop_size else T["dim"])]
    text = fmt_last_played(g.last_played)
    attr = T["dim"] if text == "-" else T["good"] if text in ("today", "yesterday") else T["base"]
    return [(fit(text, width, "<", G["ellipsis"]), attr)]


def draw_screen(put, w, h, T, G, st):
    """Paint one frame. `put(y, x, text, attr)` writes text; `st` is the UI state."""
    W = w - 1                                   # never touch the last column
    if W < MIN_W or h < MIN_H:
        put(0, 0, f"Terminal too small (need {MIN_W + 1}x{MIN_H})", T["warn"])
        return
    view, sel, top = st["view"], st["sel"], st["top"]
    N, inner = h - CHROME_ROWS, W - 2
    border = T["border"]

    def segs(y, x, parts):
        """Write (text, attr) pieces one after another; returns the column after the last."""
        for text, attr in parts:
            put(y, x, text, attr)
            x += len(text)
        return x

    # ---- row 0: title bar with live totals ----
    pill = f" {G['logo']} GAMEIFY "
    x = segs(0, 0, [(pill, T["pill"])])
    tagline = W >= 84
    if tagline:
        put(0, x + 2, "every game on this machine", T["dim"])
    sep = (f"  {G['sep']}  ", border)
    count, games = (str(len(view)), T["accent_b"]), (" games", T["dim"])
    used = [(human_size(st["total_size"]) if st["total_size"] else "-", T["accent_b"]),
            (" used", T["dim"])]
    played = [(fmt_playtime(st["total_play"]), T["accent_b"]), (" played", T["dim"])]
    spin = []
    if st["pending"]:
        frame = G["spinner"][st["frame"] % len(G["spinner"])]
        spin = [(f"{frame} measuring", T["warn"]), sep]
    options = [spin + [count, games, sep] + used + ([sep] + played if st["total_play"] else []),
               spin + [count, games, sep] + used,
               [count, games]]
    for parts in options:
        width = sum(len(t) for t, _ in parts)
        if W - width - 1 > len(pill) + 2 + (26 if tagline else 0) + 2:
            segs(0, W - width - 1, parts)
            break

    # ---- row 1: source tabs (sources with no search matches are hidden) ----
    tabs = [(s, "All" if s == "all" else source_name(s), st["counts"].get(s, 0))
            for s in st["sources"]]
    shown = [i for i, (s, _lab, n) in enumerate(tabs) if s == "all" or n or i == st["src_i"]]
    texts = {i: (f" {tabs[i][1]} {tabs[i][2]} " if tabs[i][0] == "all"
                 else f" {G['dot']} {tabs[i][1]} {tabs[i][2]} ") for i in shown}
    first = 0   # slide the window right until the active tab is visible
    while first < len(shown) - 1 and shown[first] != st["src_i"] and sum(
            len(texts[i]) for i in shown[first:shown.index(st["src_i"]) + 1]) > W - 6:
        first += 1
    x = 1
    if first:
        put(1, x, G["more_l"], T["dim"])
        x += 2
    for i in shown[first:]:
        if x + len(texts[i]) > W - 2:
            put(1, W - 2, G["more_r"], T["dim"])
            break
        s, lab, n = tabs[i]
        if i == st["src_i"]:
            put(1, x, texts[i], T["pill"])
        elif s == "all":
            put(1, x, texts[i], T["dim"])
        else:
            segs(1, x, [(f" {G['dot']}", T.get("src:" + s, T["accent"])),
                        (f" {lab} {n} ", T["dim"])])
        x += len(texts[i])

    # ---- row 2: search box and sort ----
    segs(2, 1, [(" / ", T["keycap"])])
    if st["searching"]:
        end = segs(2, 5, [("Search: ", T["dim"]), (st["query"] + G["cursor"], T["accent_b"])])
    elif st["query"]:
        end = segs(2, 5, [("Search: ", T["dim"]), (st["query"], T["accent_b"]),
                          ("   esc to clear", T["dim"])])
    else:
        end = segs(2, 5, [("press / to search", T["dim"])])
    arrow = G["down"] if st["sort"] in SORT_DESCENDING else G["up"]
    sort_parts = [("Sort: ", T["dim"]), (f"{SORT_LABELS[st['sort']]} {arrow}", T["accent_b"]),
                  ("  (s)", T["dim"])]
    sort_w = sum(len(t) for t, _ in sort_parts)
    if W - sort_w - 1 > end + 2:
        segs(2, W - sort_w - 1, sort_parts)

    # ---- the table: rounded box, header, rows, scrollbar ----
    cols, name_w, bars = pick_columns(inner, st["has_workshop"])
    y0 = 3
    put(y0, 0, G["tl"] + G["h"] * inner + G["tr"], border)
    put(y0 + 2, 0, G["ml"] + G["h"] * inner + G["mr"], border)
    put(y0 + 3 + N, 0, G["bl"] + G["h"] * inner + G["br"], border)
    put(y0 + 1, 0, G["v"], border)
    put(y0 + 1, W - 1, G["v"], border)

    active = SORT_COLUMN[st["sort"]]
    arrow_now = G["down"] if st["sort"] in SORT_DESCENDING else G["up"]

    def header(key, title, width, align):
        label = title + (f" {arrow_now}" if key == active else "")
        return fit(label, width, align, G["ellipsis"]), T["accent_b"] if key == active else T["dim"]

    text, attr = header("name", "NAME", name_w, "<")
    put(y0 + 1, 4, text, attr)
    x = 4 + name_w + 2
    for key, title, width, align in cols:
        text, attr = header(key, title, width, align)
        put(y0 + 1, x, text, attr)
        x += width + 2

    scroll = len(view) > N
    if scroll:
        thumb_len = max(1, N * N // len(view))
        thumb_at = round(top / (len(view) - N) * (N - thumb_len))
    max_size = max((g.size or 0 for g in view), default=0)

    for i in range(N):
        y = y0 + 3 + i
        put(y, 0, G["v"], border)
        if scroll and thumb_at <= i < thumb_at + thumb_len:
            put(y, W - 1, G["thumb"], T["accent"])
        else:
            put(y, W - 1, G["v"], border)
        idx = top + i
        if idx >= len(view):
            continue
        g, chosen = view[idx], idx == sel
        name = fit(g.name, name_w, "<", G["ellipsis"])
        if chosen:
            put(y, 1, " " * inner, T["sel"])
            put(y, 2, G["pointer"], T["sel_accent"])
            put(y, 4, name, T["sel_b"])
        else:
            put(y, 4, name, T["base"])
        x = 4 + name_w + 2
        for key, _title, width, _align in cols:
            parts = cell_parts(g, key, width, bars, max_size, T, G)
            if chosen:
                parts = [(t, T["sel"]) for t, _ in parts]
            segs(y, x, parts)
            x += width + 2

    if not view:
        if st["query"]:
            note = f"No games match \"{st['query']}\""
        elif st["all_count"]:
            note = "No games in this source"
        else:
            note = "No games found on this system"
        put(y0 + 3 + N // 2, max(2, (W - len(note)) // 2), note, T["dim"])

    # ---- details for the selected game ----
    if view:
        g = view[sel]
        if g.size is None:
            size_txt = "measuring size..."
        elif g.size:
            size_txt = f"{human_size(g.size)} on disk"
        else:
            size_txt = "size unknown"
        if g.workshop_size:
            size_txt += f" + {human_size(g.workshop_size)} workshop"
        play_txt = (f"{fmt_playtime(g.playtime_seconds)} played" if g.playtime_seconds
                    else "no playtime recorded")
        if g.last_played:
            stamp = datetime.fromtimestamp(g.last_played).strftime("%Y-%m-%d %H:%M")
            last_txt = f"last played {fmt_last_played(g.last_played)} ({stamp})"
        else:
            last_txt = "no play history"
        badge = f"   {G['dot']} {source_name(g.source)}"
        info = [size_txt, play_txt, last_txt]
        while len(info) > 1 and W - 3 - len(badge) - sum(len(p) + 5 for p in info) < 20:
            info.pop()                       # drop the least important facts on narrow screens
        room = max(8, W - 3 - len(badge) - sum(len(p) + 5 for p in info))
        parts = [("  " + fit(g.name, min(len(g.name), room), "<", G["ellipsis"]), T["accent_b"]),
                 (badge, T.get("src:" + g.source, T["accent"]))]
        for piece in info:
            parts += [sep, (piece, T["base"])]
        segs(h - 4, 1, parts)
        put(h - 3, 3, g.path or " ".join(g.launch), T["dim"])

    if st["message"]:
        put(h - 2, 3, st["message"], T["warn"])

    # ---- footer: key hints ----
    if st["searching"]:
        keys = [("type", "to search"), (G["enter"], "accept"), ("esc", "clear")]
    else:
        keys = [(G["updown"], "move"), (G["enter"], "launch"), ("/", "search"),
                (G["leftright"], "source"), ("s", "sort"), ("q", "quit")]
    x = 1
    for key, label in keys:
        cap = f" {key} "
        if x + len(cap) + len(label) + 3 > W:
            break
        put(h - 1, x, cap, T["keycap"])
        put(h - 1, x + len(cap) + 1, label, T["dim"])
        x += len(cap) + len(label) + 4
    live = f"{G['dot']} live"
    if W - len(live) - 1 > x:
        put(h - 1, W - len(live) - 1, live, T["good"])


def make_view(all_games, sources, src_i, query, sort_mode):
    """Games to show, plus per-source counts for the tabs (counts respect the search)."""
    base = all_games
    if query:
        q = query.lower()
        base = [g for g in base if q in g.name.lower()]
    counts = {"all": len(base)}
    for g in base:
        counts[g.source] = counts.get(g.source, 0) + 1
    view = base if sources[src_i] == "all" else [g for g in base if g.source == sources[src_i]]
    return sort_games(view, sort_mode), counts


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


# --------------------------------------------------------------------------- #
# playing: close the TUI while a game runs, reopen it when the game closes
# --------------------------------------------------------------------------- #

START_TIMEOUT = 180   # seconds to wait for a launcher (Steam, Lutris...) to actually start the game


def process_cmdlines():
    """Command line (list of arguments) of every running process."""
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            with open(f"/proc/{entry.name}/cmdline", "rb") as f:
                raw = f.read()
        except OSError:
            continue
        if raw:
            yield raw.decode(errors="replace").split("\0")


def is_running(matcher):
    return any(matcher(args) for args in process_cmdlines())


def game_matcher(game):
    """A function that recognises the game's running process, or None if we can't tell."""
    target = game.launch[-1] if game.launch else ""
    if game.source in ("steam", "non-steam"):
        # Steam wraps every game in a "reaper SteamLaunch AppId=<id> -- ..." process.
        # Accept both id forms (the launch id and the short appid) to be safe.
        ids = {game.app_id, (re.search(r"rungameid/(\d+)", target) or [None, ""])[1]} - {""}
        if ids:
            needles = {f"AppId={i}" for i in ids}
            return lambda args: "SteamLaunch" in args and any(a in needles for a in args)
    if game.source == "lutris":   # Lutris runs games through its "lutris-wrapper" process
        return lambda args: any(a.endswith("lutris-wrapper") for a in args) and game.name in args
    if game.source in ("epic", "gog"):
        m = re.search(r"launch/[^/]+/([^/]+)$", target)
        if m:   # Heroic runs legendary / gogdl with "launch <appName>" (best effort)
            app = m.group(1)
            return lambda args: app in args and "launch" in args
    return None


def wait_for_game(matcher, start_timeout=None, quiet_polls=3):
    """Wait for the game to start, then for it to close. False if it never started."""
    deadline = time.time() + (START_TIMEOUT if start_timeout is None else start_timeout)
    while not is_running(matcher):
        if time.time() > deadline:
            return False
        time.sleep(1)
    quiet = 0   # must be gone for a few polls in a row (games sometimes restart helpers)
    while quiet < quiet_polls:
        quiet = 0 if is_running(matcher) else quiet + 1
        time.sleep(1)
    return True


def play(game, tracker):
    """Launch a game with the TUI closed. Returns when the game closes (or on Ctrl+C).
    The return value is a status message to show once the TUI is back."""
    track = tracker.applies_to(game)
    token = None
    curses.endwin()   # hand the terminal back: the TUI disappears while you play
    try:
        if game.terminal:   # a terminal game uses the terminal itself
            token = tracker.begin(game) if track else None
            subprocess.call(game.launch)
            return f"Back from {game.name}"

        clear = "\033[2J\033[H" if sys.stdout.isatty() else ""
        print(f"{clear}\n  Playing {game.name}\n"
              f"  gameify will reopen when you close the game.   (Ctrl+C to come back now)\n",
              flush=True)
        proc = subprocess.Popen(
            game.launch, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        if track:
            token = tracker.begin(game)

        if game.source in ("native", "flatpak", "snap"):
            proc.wait()   # these run as the process we started
        else:
            matcher = game_matcher(game)
            if matcher is None:
                input("  gameify can't tell when this game closes. Press Enter to come back... ")
            elif not wait_for_game(matcher):
                return f"Didn't see {game.name} start"
        return ""
    except (KeyboardInterrupt, EOFError):
        return f"Back in gameify ({game.name} may still be running)"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Could not launch {game.name}: {exc}"
    finally:
        if token is not None:
            tracker.end(token)


def refresh_games(old_games, tracker):
    """Scan again (playtime and last played have changed), keeping sizes we already measured."""
    old = {(g.source, g.name): g for g in old_games}
    fresh = find_games()
    tracker.apply(fresh)
    for g in fresh:
        previous = old.get((g.source, g.name))
        if g.size is None and previous is not None and previous.size is not None:
            g.size = previous.size
    start_size_workers(fresh)
    return fresh


def tui(stdscr, all_games, tracker, start_sort=0):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    G = GLYPHS_UNICODE if "utf" in locale.getpreferredencoding(False).lower() else GLYPHS_ASCII
    T = make_theme()

    def put(y, x, text, attr=0):
        h, w = stdscr.getmaxyx()
        if 0 <= y < h and 0 <= x < w:
            try:
                stdscr.addstr(y, x, text[: max(0, w - x - 1)], attr)
            except curses.error:
                pass

    sources = source_list(all_games)
    src_i = sel = top = 0
    sort_i = start_sort
    query, searching, message = "", False, ""
    reselect = None   # (source, name) of the game to keep selected after a rescan
    selected = None   # (source, name) of the highlighted game, remembered for rescans
    fingerprint, last_poll, last_rescan, dirty = watch_fingerprint(), time.time(), 0.0, False

    while True:
        # ---- auto-update: notice installs / uninstalls without reopening ----
        now = time.time()
        if now - last_poll >= POLL_SECONDS:
            last_poll = now
            current = watch_fingerprint()
            if current != fingerprint:
                fingerprint, dirty = current, True
        if dirty and now - last_rescan >= RESCAN_GAP:
            dirty, last_rescan = False, now
            before = {(g.source, g.name) for g in all_games}
            current_source = sources[src_i]
            fresh = refresh_games(all_games, tracker)
            all_games[:] = fresh
            sources = source_list(all_games)
            src_i = sources.index(current_source) if current_source in sources else 0
            reselect = selected
            note = describe_changes(before, {(g.source, g.name) for g in fresh})
            if note:
                message = note

        h, w = stdscr.getmaxyx()
        list_h = max(1, h - CHROME_ROWS)
        view, counts = make_view(all_games, sources, src_i, query, SORTS[sort_i])
        if reselect:
            sel = next((i for i, x in enumerate(view) if (x.source, x.name) == reselect), sel)
            reselect = None
        pending = any(g.size is None for g in all_games)
        sel = max(0, min(sel, len(view) - 1))
        selected = (view[sel].source, view[sel].name) if view else None
        if sel < top:
            top = sel
        elif sel >= top + list_h:
            top = sel - list_h + 1
        top = max(0, min(top, max(0, len(view) - list_h)))

        state = dict(
            view=view, sel=sel, top=top, sources=sources, src_i=src_i, counts=counts,
            sort=SORTS[sort_i], query=query, searching=searching, message=message,
            pending=pending, frame=int(time.time() * 10), all_count=len(all_games),
            has_workshop=any(g.workshop_size for g in all_games),
            total_size=sum((g.size or 0) + g.workshop_size for g in view),
            total_play=sum(g.playtime_seconds for g in view))
        stdscr.erase()
        draw_screen(put, w, h, T, G, state)
        stdscr.refresh()

        # ---- input (wake up often while sizes are still being measured: spinner) ----
        stdscr.timeout(100 if pending else 1000)
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
            sel -= list_h
        elif key == curses.KEY_NPAGE:
            sel += list_h
        elif key in (curses.KEY_HOME, "g"):
            sel = 0
        elif key in (curses.KEY_END, "G"):
            sel = len(view) - 1
        elif key == "/":
            searching = True
        elif key == "\x1b":
            query, sel, top = "", 0, 0
        elif key in (curses.KEY_RIGHT, "l", "\t", "f"):
            src_i = (src_i + 1) % len(sources)
            sel = top = 0
        elif key in (curses.KEY_LEFT, "h", curses.KEY_BTAB):
            src_i = (src_i - 1) % len(sources)
            sel = top = 0
        elif key == "s":
            sort_i = (sort_i + 1) % len(SORTS)
        elif key in ("\n", "\r", curses.KEY_ENTER) and view:
            g = view[sel]
            message = play(g, tracker)   # the TUI is closed until the game quits
            if g.source in ("steam", "non-steam", "lutris", "epic", "gog"):
                time.sleep(2)            # let the launcher save playtime
            current_source = sources[src_i]
            all_games[:] = refresh_games(all_games, tracker)
            sources = source_list(all_games)
            src_i = sources.index(current_source) if current_source in sources else 0
            reselect = (g.source, g.name)
            fingerprint, last_rescan, dirty = watch_fingerprint(), time.time(), False
            stdscr.clear()               # redraw the TUI
            stdscr.refresh()
            curses.flushinp()            # drop keys pressed while the game was running


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
        ws = any(g.workshop_size for g in games)
        print(f"{'NAME':<40} {'SOURCE':<9} {'SIZE':>9} " + (f"{'WORKSHOP':>9} " if ws else "")
              + f"{'PLAYED':>9}  LAST PLAYED")
        for g in games:
            print(f"{g.name[:40]:<40} {g.source:<9} {human_size(g.size):>9} "
                  + (f"{human_size(g.workshop_size):>9} " if ws else "")
                  + f"{fmt_playtime(g.playtime_seconds):>9}  {fmt_last_played(g.last_played)}")
        total = sum((g.size or 0) + g.workshop_size for g in games)
        print(f"\n{len(games)} games found, {human_size(total)} used, "
              f"{fmt_playtime(sum(g.playtime_seconds for g in games))} played")
        return

    os.environ.setdefault("ESCDELAY", "25")
    try:
        locale.setlocale(locale.LC_ALL, "")   # lets curses draw unicode
    except locale.Error:
        pass
    try:
        curses.wrapper(tui, games, tracker, SORTS.index(args.sort))
    finally:
        tracker.flush()


if __name__ == "__main__":
    main()
