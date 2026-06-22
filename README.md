# music-organiser

Browser-based organiser for FLAC music collections.  
Imports from Nicotine+ (Soulseek), fetches metadata from Discogs / MusicBrainz / Deezer, and organises files into a clean folder layout.

---

## Install (one command)

```bash
bash install.sh
```

That's it. The script checks all requirements, installs the service, and prints the URL.

If you're starting fresh and don't have the Python dependencies yet:

```bash
bash install.sh --deps
```

---

## Open the UI

After install, open your browser to the URL shown in the terminal — something like:

```
http://192.168.1.x:8082
```

The service runs in the background and auto-starts whenever you log in.

---

## Requirements

- **Linux** with systemd (Ubuntu 20.04+, Debian 11+, etc.)
- **Python 3.10+** — check with `python3 --version`
- **fpcalc** — for AcoustID fingerprinting: `sudo apt install libchromaprint-tools`
- Internet access for metadata lookups (Discogs, MusicBrainz, etc.)

All Python packages are installed automatically when you run `bash install.sh --deps`.

---

## Control the service

```
music-organiser start      start
music-organiser stop       stop
music-organiser restart    restart (run this after any update)
music-organiser status     show URL + live stats
music-organiser logs       follow live log output
music-organiser health     JSON health check
music-organiser open       open browser (desktop only)
music-organiser enable     auto-start on login
music-organiser disable    remove auto-start
```

Or use make:

```
make start
make stop
make restart
make logs
make status
```

---

## First-time setup

1. Run `bash install.sh`
2. Open the browser URL
3. Go to the **Pipeline** tab
4. Set your **Source** folder (where Nicotine+ downloads to)
5. Set your **Output** folder (where organised files will go)
6. Click **Save config**
7. Click **▶ Run All** to import, fetch tags, and organise in one go

---

## Pipeline

| Step | What it does |
|---|---|
| **Import** | Scans source folder, moves FLACs into output, detects duplicates |
| **Fetch Tags** | Looks up metadata from Discogs, MusicBrainz, Deezer, Bandcamp, AcoustID |
| **Organise** | Renames and moves files into `<year> - <artist>/album\|single\|mix/` layout |

Run them individually or all at once with **Run All**.

---

## Tabs

- **Pipeline** — run jobs, watch live log, configure paths and API keys
- **Session** — browse the current batch, re-fetch broken files, commit to library
- **Library** — search and browse the full permanent library
- **Tools** — rebuild index, compact DB, SQL query, audit checks

---

## API keys

Discogs and AcoustID keys go in `~/.config/music-organiser/config.toml`.  
You can also paste them in the **Pipeline** tab → providers panel → **Save config**.

- **Discogs**: get a token at https://www.discogs.com/settings/developers
- **AcoustID**: get a key at https://acoustid.org/login

---

## Update

```bash
bash install.sh
# or:
make install
```

---

## Uninstall

```bash
make uninstall
```

Removes the service and control script. Your music files and databases are untouched.

---

## Folder layout

```
<output>/
  2024 - Artist Name/
    single/
      01 - Track Title.flac
  2023 - VA - Album Name/
    mix/
      01 - Track.flac
      cover.jpg
```
