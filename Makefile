.PHONY: install deps start stop restart status logs health open enable disable uninstall release

# ── install ───────────────────────────────────────────────────────────────────
install:
	bash install.sh

deps:
	bash install.sh --deps

# ── service control ───────────────────────────────────────────────────────────
start:
	music-organiser start

stop:
	music-organiser stop

restart:
	music-organiser restart

status:
	music-organiser status

logs:
	music-organiser logs

health:
	music-organiser health

open:
	music-organiser open

enable:
	music-organiser enable

disable:
	music-organiser disable

# ── uninstall ─────────────────────────────────────────────────────────────────
uninstall:
	@echo "Stopping and disabling service..."
	systemctl --user stop  music-organiser 2>/dev/null || true
	systemctl --user disable music-organiser 2>/dev/null || true
	systemctl --user daemon-reload
	rm -f ~/.config/systemd/user/music-organiser.service
	rm -f ~/.local/bin/music-organiser
	@echo "Done. Your music files and databases have NOT been touched."

# ── release tarball ───────────────────────────────────────────────────────────
release:
	bash make-release.sh
