# gameify - install with:  sudo make install
#
#   make                      show this help
#   sudo make install         install to /usr/local/bin
#   sudo make uninstall       remove it again
#
# Options:
#   PREFIX=/usr               install somewhere else (default: /usr/local)
#   DESTDIR=/tmp/pkg          stage the files for packaging

PREFIX  ?= /usr/local
BINDIR  ?= $(PREFIX)/bin
DOCDIR  ?= $(PREFIX)/share/doc/gameify
INSTALL ?= install

.PHONY: help install uninstall

help:
	@echo "gameify - available targets:"
	@echo "  sudo make install     install to $(BINDIR)"
	@echo "  sudo make uninstall   remove it"
	@echo ""
	@echo "Options: PREFIX=/usr  DESTDIR=/some/staging/dir"

install:
	@python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null || \
		{ echo "error: Python 3.8 or newer is required" >&2; exit 1; }
	$(INSTALL) -Dm755 gameify.py $(DESTDIR)$(BINDIR)/gameify
	$(INSTALL) -Dm644 README.md $(DESTDIR)$(DOCDIR)/README.md
	@echo ""
	@echo "Installed. Run it with:  gameify"

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/gameify
	rm -f $(DESTDIR)$(DOCDIR)/README.md
	-rmdir $(DESTDIR)$(DOCDIR) 2>/dev/null
	@echo "Removed gameify."
