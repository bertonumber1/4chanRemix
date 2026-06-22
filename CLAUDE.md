# music-organiser — Claude Code context

## What this project is
Python music organiser for an underground D&B / bassline / makina FLAC collection.
Files come in via Nicotine+ (Soulseek). The pipeline is: Import → Fetch Tags → Organise.

## Project layout
- `organiser.py` — TUI entry point (leave it alone, don't break it)
- `zzzzScriptstuff/` — ALL modules live here (named to sort to bottom on Soulseek shares)
- `web_ui.py` — browser frontend (FastAPI, port 8082), the primary UI
- `acoustid_helper.py` — thin wrapper, stays in root
- `install.sh` — idempotent installer: systemd service + control script
- `requirements.txt` — Python deps for fresh installs

## Service management
```
music-organiser start     # start service
music-organiser stop      # stop service
music-organiser restart   # restart service
music-organiser status    # running status + live stats
music-organiser logs      # follow systemd journal
music-organiser logfile   # follow ~/.local/share/music-organiser/web_ui.log
music-organiser health    # hit /api/health
music-organiser open      # xdg-open browser
music-organiser enable    # auto-start on login
```
Service unit: `~/.config/systemd/user/music-organiser.service`
Control script: `~/.local/bin/music-organiser`

## Re-running the installer (safe to run again)
```
bash install.sh           # reinstall service, restart
bash install.sh --deps    # also pip install -r requirements.txt
```

## CRITICAL — sys.path order
Root dir has an old `detection.py` missing `is_record_metadata_broken`.
zzzzScriptstuff MUST be inserted AFTER root so it ends up at position 0:
```python
sys.path.insert(0, str(_HERE))                    # root first
sys.path.insert(0, str(_HERE / "zzzzScriptstuff")) # zzzzScriptstuff wins
```
Get this wrong and everything breaks with an ImportError on detection.py.

## Key paths
- Source (Nicotine+): `/mnt/usb-a/drive/downloads/nicotine-downloads`
- Output:            `/mnt/usb-a/drive/downloads/organiser-output`
- Config:            `~/.config/music-organiser/config.toml`
- Main library DB:   `~/.local/share/music-organiser/library.db`
- Session DB:        `~/.local/share/music-organiser/web_session.db`
- USB mount root:    `/mnt/usb-a/drive/` (NOT /mnt/usb-a/)

## Folder structure (what we changed to)
`<catno> - <year> - <artist>/album|mix|single/<filename>`
Implemented in `zzzzScriptstuff/organiser_core.py` → `build_destination_path()`

## Provider order
Discogs → MusicBrainz → Deezer → Bandcamp → iTunes → AcoustID (last resort, fingerprint only)
API keys are in config.toml already.

## web_ui.py design decisions
- **Session DB pattern**: every Import run wipes `web_session.db` and starts fresh.
  Fetch Tags and Organise ONLY read from session DB, never main library.db.
  This is the equivalent of the TUI's `rm library.db` fresh-start flow.
- **Stop button**: `_StopRequested` exception raised inside `ui.log()` and `ui.advance()`
  so the job thread dies on the next log line, not after a long network call finishes.
- **Config save**: regex-based in-place edit of config.toml (no tomli_w dependency).
- Python 3.10 on this machine — no built-in tomllib, tomli installed via pip.
- **Health endpoint**: `GET /api/health` → uptime, version, file counts.
- **File log**: `~/.local/share/music-organiser/web_ui.log` (also goes to journal).
- **Systemd service**: auto-restarts on crash (`Restart=on-failure`, `RestartSec=3`).
- **Tabs**: Pipeline | Session | Library | Tools (full TUI feature parity).
