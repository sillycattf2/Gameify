#!/usr/bin/env bash
# Installs gameify by symlinking it into ~/.local/bin.
# Because it's a symlink into your clone, `git pull` is all you need to update.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin="${HOME}/.local/bin"

case "${1:-install}" in
  install)
    command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 1; }
    mkdir -p "$bin"
    chmod +x "$here/gameify.py"
    ln -sf "$here/gameify.py" "$bin/gameify"
    rm -f "$bin/gamelist"   # old name
    echo "Installed: $bin/gameify -> $here/gameify.py"
    case ":$PATH:" in
      *":$bin:"*) echo "Run it with:  gameify" ;;
      *) echo "Note: $bin is not on your PATH. Add this to your shell profile:"
         echo "  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
    esac
    ;;
  uninstall)
    rm -f "$bin/gameify" "$bin/gamelist"
    echo "Removed $bin/gameify"
    ;;
  *)
    echo "usage: $0 [install|uninstall]" >&2
    exit 1
    ;;
esac
