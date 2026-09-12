#!/usr/bin/env python3
"""
web_ui.py — Browser frontend for music-organiser.
Run:  python3 web_ui.py
Open: http://127.0.0.1:8082 on the machine running it, or
      http://<its-LAN-IP>:8082 from another device on the network
      (printed on startup below — it's different on every machine, not
      a fixed address).
"""
from __future__ import annotations

import asyncio, json, logging, os, re, shutil, signal, socket, sqlite3, subprocess, sys, tempfile, threading, time
import urllib.parse
from pathlib import Path
from queue import Empty, Queue
from typing import Any

_START_TIME = time.time()

# ─── path bootstrap ────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
for _sub in ("zzzzScriptstuff", "scriptstuff"):
    _d = _HERE / _sub
    if _d.is_dir():
        sys.path.insert(0, str(_d))
        break

try:
    from fastapi import FastAPI, Query, Request
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError:
    sys.exit("pip install fastapi uvicorn")

# ─── constants ─────────────────────────────────────────────────────────────────
_SESSION_DB = Path("~/.local/share/music-organiser/web_session.db").expanduser()
_LIBRARY_DB = Path("~/.local/share/music-organiser/library.db").expanduser()
# Throwaway DB for the Fake-FLAC tab's "check this folder directly" mode —
# wiped and re-indexed on every scan, same disposable pattern as _SESSION_DB.
_FOLDER_DB  = Path("~/.local/share/music-organiser/web_folder_scan.db").expanduser()
_CFG_PATH   = Path("~/.config/music-organiser/config.toml").expanduser()
_LOG_FILE   = Path("~/.local/share/music-organiser/web_ui.log").expanduser()
_VERSION    = "1.10.0"

try:
    import telegram_panel as tgp
except Exception as _tg_err:            # a broken panel must not kill the app
    tgp = None
    _TG_ERR = repr(_tg_err)

try:
    import label_ref
    import label_panel as lbl
except Exception as _lbl_err:           # likewise: no tab is worth the whole app
    lbl = None
    _LBL_ERR = repr(_lbl_err)
else:
    _LBL_ERR = ""

# ─── logging ──────────────────────────────────────────────────────────────────
def _setup_logging(verbose: bool = False) -> None:
    fmt = "%(asctime)s  %(levelname)-7s  %(message)s"
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(_LOG_FILE, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    # silence uvicorn access spam — keep warnings+
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

log = logging.getLogger("music-organiser")

# ─── job state ─────────────────────────────────────────────────────────────────
_msg_q: Queue[dict | None] = Queue()
_stop_flag = threading.Event()
_job_thread: threading.Thread | None = None


class _StopRequested(Exception):
    pass


# ─── UI adapter ───────────────────────────────────────────────────────────────
class _WebUI:
    def __init__(self):
        self._total = self._done = self._imported = self._duplicate = self._broken = 0
        self._lock = threading.Lock()
        self.started_at = time.time()

    def __enter__(self): return self

    def __exit__(self, *_):
        elapsed = round(time.time() - self.started_at)
        if not _stop_flag.is_set():
            _msg_q.put({"type": "log", "level": "info",
                        "text": f"finished in {elapsed}s — "
                                f"imported={self._imported}  "
                                f"duplicate={self._duplicate}  "
                                f"broken={self._broken}"})

    def log(self, kind: str, message: str) -> None:
        if _stop_flag.is_set():
            raise _StopRequested()
        _msg_q.put({"type": "log", "level": kind, "text": message})

    def update(self, **_: Any) -> None: pass
    def set_grabbing(self, _: Any) -> None: pass
    def set_unit(self, _: str) -> None: pass

    def set_total(self, total: int) -> None:
        with self._lock: self._total = total
        self._push_progress()

    def advance(self, *, imported=False, duplicate=False, broken=False, size_bytes=0):
        if _stop_flag.is_set():
            raise _StopRequested()
        with self._lock:
            self._done += 1
            if imported:  self._imported += 1
            if duplicate: self._duplicate += 1
            if broken:    self._broken += 1
        self._push_progress()

    def _push_progress(self):
        with self._lock:
            d, t = self._done, self._total
        _msg_q.put({"type": "progress", "done": d, "total": t,
                    "imported": self._imported,
                    "duplicate": self._duplicate,
                    "broken": self._broken})

    def is_stopped(self) -> bool: return _stop_flag.is_set()


# ─── config ───────────────────────────────────────────────────────────────────
_CFG_ERROR = ""          # last config.toml parse failure, for the doctor


def _load_cfg() -> dict:
    """Load config.toml, remembering WHY it failed.

    This used to swallow the exception and return {}. A malformed TOML then
    looked exactly like "the UI ignores my paths": every setting silently fell
    back to a default and nothing anywhere said so. The error is now kept and
    surfaced by /api/doctor.
    """
    global _CFG_ERROR
    try:
        from config import load_config
        cfg = load_config()
        _CFG_ERROR = ""
        return cfg
    except Exception as exc:
        _CFG_ERROR = "%s: %s" % (type(exc).__name__, exc)
        return {}


def _doctor() -> dict:
    """Check the things that fail SILENTLY, and say so out loud.

    Every entry is something that has actually gone wrong here before: a
    source folder that no longer exists scans nothing and reports success; a
    destination on an unmounted drive gets created as an empty directory; a
    read-only database makes every write vanish.
    """
    import os as _os
    problems, checks = [], []
    cfg = _load_cfg()
    paths = (cfg or {}).get("paths") or {}

    def add(ok, label, detail="", fatal=True):
        checks.append({"ok": bool(ok), "label": label, "detail": detail})
        if not ok and fatal:
            problems.append("%s — %s" % (label, detail) if detail else label)

    add(not _CFG_ERROR, "config.toml parses",
        _CFG_ERROR or str(_CFG_PATH))
    if _CFG_ERROR:
        return {"ok": False, "problems": problems, "checks": checks}

    # Config values come straight from the TOML file, so a "~/..." default
    # (config.default.toml's database path, or anything a user types by hand)
    # is still a literal tilde here. os.path.isdir("~/...") never expands it
    # on any platform, and on Windows "~" isn't a shell convention at all — it
    # just fails to exist, turning a correct default into a false "broken
    # configuration" warning. Expand before every filesystem check.
    def _exp(p):
        return _os.path.expanduser(p) if p else p

    srcs = [_exp(s) for s in (paths.get("sources") or [])]
    if not srcs:
        add(False, "a source folder is configured",
            "nothing to import from", fatal=False)
    for s in srcs:
        ok = _os.path.isdir(s)
        add(ok, "source exists", s if ok else "%s — does not exist, so a "
            "scan finds nothing and reports no error" % s)
        if ok and not _os.access(s, _os.R_OK):
            add(False, "source readable", s)

    dest = _exp(paths.get("destination_root") or "")
    if dest:
        ok = _os.path.isdir(dest)
        add(ok, "destination exists", dest if ok else
            "%s — missing; on a bind/mount this gets recreated empty and "
            "owned by root" % dest)
        if ok and not _os.access(dest, _os.W_OK):
            add(False, "destination writable", dest)
    else:
        add(False, "a destination is configured", "", fatal=False)

    db = _exp(paths.get("database")) or str(_LIBRARY_DB)
    dbdir = _os.path.dirname(db) or "."
    if _os.path.exists(db):
        add(_os.access(db, _os.W_OK), "library.db writable", db)
    else:
        add(_os.path.isdir(dbdir) and _os.access(dbdir, _os.W_OK),
            "library.db can be created", dbdir)

    try:
        import telegram_panel as _t
        up = _exp((_t.settings(cfg) or {}).get("upload_dir") or "")
        if up:
            add(_os.path.isdir(up), "telegram upload dir exists", up)
    except Exception:
        pass

    return {"ok": not problems, "problems": problems, "checks": checks}


def _save_cfg(paths: dict | None = None, providers: dict | None = None) -> None:
    txt = _CFG_PATH.read_text()
    if paths:
        if "sources" in paths:
            items = "\n".join(f'    "{s}",' for s in paths["sources"])
            new_arr = f'[\n{items}\n]' if paths["sources"] else '[]'
            txt = re.sub(r'sources\s*=\s*\[[^\]]*\]', f'sources = {new_arr}',
                         txt, flags=re.DOTALL)
        if "destination_root" in paths:
            v = paths["destination_root"]
            txt = re.sub(r'destination_root\s*=\s*"[^"]*"',
                         f'destination_root = "{v}"', txt)
    if providers:
        for prov_id, fields in providers.items():
            section = f"providers.{prov_id}"
            for k, v in fields.items():
                if not str(v).strip():
                    continue
                pattern = rf'(\[{re.escape(section)}\][^\[]*?){re.escape(k)}\s*=\s*"[^"]*"'
                new_txt = re.sub(pattern, rf'\g<1>{k} = "{v}"', txt, flags=re.DOTALL)
                if new_txt == txt:
                    m = re.search(rf'\[{re.escape(section)}\]', txt)
                    if m:
                        txt = txt[:m.end()] + f'\n{k} = "{v}"' + txt[m.end():]
                    else:
                        txt += f'\n[{section}]\n{k} = "{v}"\nenabled = true\n'
                else:
                    txt = new_txt
    _CFG_PATH.write_text(txt)


# ─── db helpers ───────────────────────────────────────────────────────────────
def _db_rows(db_path: Path, sql: str, params=()) -> list[dict]:
    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

def _db_one(db_path: Path, sql: str, params=()) -> Any:
    if not db_path.exists():
        return None
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None


# ─── core job functions ───────────────────────────────────────────────────────
def _do_import(ui, sources, dest, cfg, dry_run):
    from database import Database
    from importer import import_sources
    db_path = str(_SESSION_DB)
    override = dict(cfg)
    p = dict(override.get("paths", {}))
    if sources: p["sources"] = sources
    if dest:    p["destination_root"] = dest
    p["database"] = db_path
    override["paths"] = p
    if dry_run:
        ui.log("info", "DRY RUN — nothing will be moved or written")
    else:
        if _SESSION_DB.exists():
            _SESSION_DB.unlink()
        ui.log("info", f"session DB: {db_path}")
    db = Database(db_path)
    import_sources(sources or p.get("sources", []),
                   cfg=override, db=db, ui=ui, dry_run=dry_run)


def _do_fetch(ui, provider_ids, cfg, dry_run, only_missing=True):
    from database import Database
    from metadata_lookup import fill_missing_metadata
    from metadata_providers import make_provider
    db_path = str(_SESSION_DB)
    if not _SESSION_DB.exists():
        ui.log("warning", "no session data — run Import first")
        return
    provs = []
    for pid in provider_ids:
        p = make_provider(pid)
        if p is None: continue
        try: p.configure(cfg, lambda **kw: None)
        except Exception: pass
        provs.append(p)
    if not provs:
        ui.log("broken", "no providers — add API keys in Config and save")
        return
    ui.log("info", f"providers: {[p.id for p in provs]}")
    if dry_run: ui.log("info", "DRY RUN — tags will not be written to files")
    db = Database(db_path)
    fill_missing_metadata(
        db, providers=provs,
        target_columns=["year","label","catalog_number","genre","country",
                        "mb_release_id","discogs_release_id","release_type","barcode"],
        only_missing=only_missing, write_to_files=not dry_run,
        db_path=db_path, ui=ui,
    )


def _do_organise(ui, dest, cfg, dry_run):
    from database import Database
    from importer import organise_in_place
    db_path = str(_SESSION_DB)
    if not _SESSION_DB.exists():
        ui.log("warning", "no session data — run Import first")
        return
    override = dict(cfg)
    p = dict(override.get("paths", {}))
    if dest: p["destination_root"] = dest
    p["database"] = db_path
    override["paths"] = p
    if dry_run: ui.log("info", "DRY RUN — no files will be moved")
    db = Database(db_path)
    organise_in_place(db, cfg=override, ui=ui, dry_run=dry_run)


def _do_direct(ui, sources, dest, provider_ids, cfg, dry_run, steps=("import", "fetch", "organise")):
    """Direct mode: import → fetch tags → organise, chained on a single
    throwaway in-memory DB (Database(":memory:") — see database.py). Nothing
    persists to a database file; sources+dest are the only inputs, output is
    just organised files on disk with Beatport-style "(catno) Artist - Album
    (Year)/NN - Title.ext" naming (see organise.artist_led_folder in
    organiser_core.build_destination_path)."""
    from database import Database
    from importer import import_sources, organise_in_place
    from metadata_lookup import fill_missing_metadata
    from metadata_providers import make_provider

    if not sources:
        ui.log("broken", "no source folder set"); return
    if not dest:
        ui.log("broken", "no output folder set"); return

    override = dict(cfg)
    p = dict(override.get("paths", {}))
    p["sources"] = sources
    p["destination_root"] = dest
    p["database"] = ":memory:"
    override["paths"] = p
    organise_cfg = dict(override.get("organise", {}))
    organise_cfg["artist_led_folder"] = True
    override["organise"] = organise_cfg

    ui.log("info", "DIRECT MODE — no database will be written, files only")
    if dry_run: ui.log("info", "DRY RUN — nothing will be moved or written")

    if set(steps) != {"import", "fetch", "organise"}:
        ui.log("info", "step(s): " + ", ".join(steps))

    db = Database(":memory:")
    try:
        # Every step needs the files indexed first — the in-memory DB is the
        # only place they exist. Indexing is read-only, so it is never skipped.
        import_sources(sources, cfg=override, db=db, ui=ui,
                       dry_run=dry_run or "import" not in steps)

        provs = []
        for pid in provider_ids:
            pr = make_provider(pid)
            if pr is None: continue
            try: pr.configure(cfg, lambda **kw: None)
            except Exception: pass
            provs.append(pr)
        if provs and "fetch" not in steps:
            ui.log("info", "skipping the tag lookup — this run is organise-only")
            provs = []
        if provs:
            ui.log("info", f"providers: {[pr.id for pr in provs]}")
            fill_missing_metadata(
                db, providers=provs,
                target_columns=["year","label","catalog_number","genre","country",
                                "mb_release_id","discogs_release_id","release_type","barcode"],
                only_missing=True, write_to_files=not dry_run,
                db_path=":memory:", resume=False, ui=ui,
            )
        else:
            ui.log("warning", "no providers configured — organising from existing tags only")

        if "organise" in steps:
            organise_in_place(db, cfg=override, ui=ui, dry_run=dry_run)
        else:
            ui.log("info", "not organising — tags only, files stay where they are")
    finally:
        db.close()


def _do_rebuild(ui, dest, cfg):
    from database import Database
    from indexer import index_tree
    dest_path = Path(dest or cfg.get("paths", {}).get("destination_root", "")).expanduser()
    if not dest_path.exists():
        ui.log("broken", f"destination not found: {dest_path}")
        return
    _LIBRARY_DB.parent.mkdir(parents=True, exist_ok=True)
    db = Database(str(_LIBRARY_DB))
    ui.log("info", f"rebuilding library index from {dest_path} …")
    index_tree(dest_path, cfg=cfg, db=db, ui=ui)
    count = _db_one(_LIBRARY_DB, "SELECT COUNT(*) FROM files") or 0
    ui.log("info", f"library DB now has {count:,} files")


def _do_vacuum(ui):
    if not _LIBRARY_DB.exists():
        ui.log("warning", "library.db not found — nothing to vacuum")
        return
    size_before = _LIBRARY_DB.stat().st_size
    ui.log("info", f"vacuuming library.db ({size_before//1024:,} KB) …")
    try:
        conn = sqlite3.connect(str(_LIBRARY_DB))
        conn.execute("VACUUM")
        conn.execute("ANALYZE")
        conn.close()
        size_after = _LIBRARY_DB.stat().st_size
        saved = (size_before - size_after) // 1024
        ui.log("info", f"done — {size_after//1024:,} KB (saved {saved:,} KB)")
    except Exception as e:
        ui.log("broken", f"vacuum failed: {e}")


def _do_fake_flac_scan(ui, db_path: Path, force: bool):
    from database import Database
    from fake_flac import verify_lossless_in_db, dependencies_available, missing_dependencies
    if not dependencies_available():
        ui.log("broken", f"fake-flac scan needs: {', '.join(missing_dependencies())} "
                          f"— pip install -r requirements.txt")
        return
    if not db_path.exists():
        ui.log("warning", f"{db_path.name} not found — run Rebuild index (or Import) first")
        return
    db = Database(str(db_path))
    try:
        stats = verify_lossless_in_db(db, ui=ui, force=force)
        ui.log("info", stats.summary().replace("\n", "  |  "))
    finally:
        db.close()


def _do_fake_flac_scan_folder(ui, folder: Path, cfg: dict, force: bool):
    """Check an arbitrary folder directly — no import/catalog step first.
    Indexes it into the disposable _FOLDER_DB (read-only walk, same as
    Rebuild index) then runs the same Stage-1 scan against that."""
    from database import Database
    from indexer import index_tree
    from fake_flac import verify_lossless_in_db, dependencies_available, missing_dependencies
    if not dependencies_available():
        ui.log("broken", f"fake-flac scan needs: {', '.join(missing_dependencies())} "
                          f"— pip install -r requirements.txt")
        return
    if not folder.is_dir():
        ui.log("broken", f"folder not found: {folder}")
        return
    if _FOLDER_DB.exists():
        _FOLDER_DB.unlink()
    _FOLDER_DB.parent.mkdir(parents=True, exist_ok=True)
    db = Database(str(_FOLDER_DB))
    try:
        ui.log("info", f"indexing {folder} …")
        index_tree(folder, cfg=cfg, db=db, ui=ui, force=force)
        stats = verify_lossless_in_db(db, ui=ui, force=True)
        ui.log("info", stats.summary().replace("\n", "  |  "))
    finally:
        db.close()


def _do_fake_flac_vamp(ui, db_path: Path):
    from database import Database
    from rip_audio import run_on_suspects, find_sonic_annotator
    if not find_sonic_annotator():
        ui.log("warning", "sonic-annotator not installed — Vamp confirm skipped")
        return
    if not db_path.exists():
        ui.log("warning", f"{db_path.name} not found — run a scan first")
        return
    db = Database(str(db_path))
    try:
        stats = run_on_suspects(db, quick=True, log_cb=ui.log)
        ui.log("info", f"vamp confirm — checked={stats['checked']}  "
                        f"lossy={stats['confirmed_lossy']}  "
                        f"lossless={stats['confirmed_lossless']}  "
                        f"errors={stats['errors']}")
    finally:
        db.close()


def _do_tags_from_names(ui, db_path: Path, dry_run: bool):
    """Fill artist / title / album from the FILE NAME where tags are absent.

    "Broken" in this app has only ever meant "artist, album or title is
    missing" -- never damaged audio. A file called
    `01. Baby's Gang - Challenger.flac` is carrying exactly the fields the
    tags are missing, so this reads them off the path and writes them in,
    which is what actually empties the Broken pile.

    Only STRONG recovery is used: a real "Artist - Title" filename or an
    "Artist - Album" folder. The bare folder name is refused -- treating it
    as an album is what once stamped two unrelated tracks with one
    MusicBrainz release.
    """
    from database import Database
    from detection import recover_from_path, is_unknown_tag
    from tag_writer import write_tags_to_file

    if not db_path.exists():
        ui.log("warning", f"{db_path.name} not found — run Import or Rebuild index first")
        return

    db = Database(str(db_path))
    fixed = weak = skipped = errors = 0
    try:
        for row in db.iter_all():
            path = row.get("path") or ""
            have = {}
            for f in ("artist", "album", "title"):
                v = (row.get(f) or "").strip()
                have[f] = bool(v) and not is_unknown_tag(v)
            if all(have.values()):
                continue
            rec = recover_from_path(path, have_artist=have["artist"],
                                    have_album=have["album"],
                                    have_title=have["title"])
            if rec.get("confidence") != "strong":
                weak += 1
                continue
            tags = {f: rec[f].strip() for f in ("artist", "album", "title")
                    if not have[f] and rec.get(f, "").strip()
                    and not is_unknown_tag(rec[f])}
            if not tags:
                weak += 1
                continue
            res = write_tags_to_file(path, tags, only_missing=True, dry_run=dry_run)
            if getattr(res, "error", None):
                errors += 1
                ui.log("warning", f"{Path(path).name}: {res.error}")
                continue
            if getattr(res, "skipped_entirely", False):
                skipped += 1
                continue
            fixed += 1
            ui.log("info", "%s  →  %s" % (
                Path(path).name[:56],
                "  ".join("%s='%s'" % (k, v) for k, v in tags.items())))
            if not dry_run:
                new_row = dict(row)
                new_row.update(tags)
                if all((new_row.get(f) or "").strip() for f in ("artist", "album", "title")) \
                        and (new_row.get("status") or "") == "broken":
                    new_row["status"] = "imported"
                    new_row["comment"] = "tags recovered from filename"
                db.upsert_file(new_row)
    finally:
        db.close()
    ui.log("info", "tags from filenames%s — fixed=%d  nothing-usable=%d  "
                    "skipped(verified rip)=%d  errors=%d"
                    % (" (dry run)" if dry_run else "", fixed, weak, skipped, errors))
    if weak:
        ui.log("info", "the 'nothing usable' ones have neither tags nor an "
                        "'Artist - Title' filename — their audio is fine, there "
                        "is simply nothing to read the names from")


# ─── job runner ───────────────────────────────────────────────────────────────
def _do_label(ui, kind, cfg, dest="", force=False):
    """Labels tab work, on the shared job thread.

    Everything here is slow for an honest reason — a catalogue is hundreds of
    paged API calls, and reading tags off 1,200 folders on a USB bridge that
    manages 5-8 MB/s takes minutes — so it streams progress into the same log
    the rest of the app uses instead of blocking a request.
    """
    if lbl is None:
        ui.log("broken", "label panel unavailable: %s" % _LBL_ERR)
        return
    def log(m):
        # ui.log() only feeds the SSE stream, so a job's progress exists solely
        # in a browser that happens to be watching. A label scan can run for
        # forty minutes over a network mount; mirror it to the log file too, so
        # `music-organiser logs` shows what it is doing and a finished run
        # leaves evidence behind. Only the label jobs do this — an import logs
        # once per FILE, which would bury the log.
        logging.info(m)
        ui.log("info", m)
    try:
        if kind == "label_catalogue":
            lbl.fetch_catalogue(cfg, log, force=bool(force))
        elif kind == "label_tracklists":
            lbl.fetch_tracklists(cfg, log, limit=int(dest) if dest else 2000,
                                 should_stop=_stop_flag.is_set)
        elif kind == "label_prices":
            lbl.fetch_prices(cfg, log, limit=int(dest) if dest else 2000,
                             should_stop=_stop_flag.is_set)
        else:
            # Stop must reach the folder loop itself. ui.log() only raises
            # _StopRequested on the NEXT log line, and this job logs once every
            # hundred folders — so without this, Stop could sit for minutes.
            lbl.scan(cfg, log, should_stop=_stop_flag.is_set)
    except Exception as exc:
        ui.log("broken", "label job failed: %s" % exc)


def _do_label_onboard(ui, cfg, path, role):
    """"+ Owned folder" / "+ Incoming": add the root, then run the full
    onboarding pass (index -> match -> completeness -> authenticity ->
    cross-check) on it. See label_panel.onboard_root() for the real work.
    """
    if lbl is None:
        ui.log("broken", "label panel unavailable: %s" % _LBL_ERR)
        return
    def log(m):
        logging.info(m)
        ui.log("info", m)
    try:
        res = lbl.onboard_root(cfg, path, role, log, should_stop=_stop_flag.is_set)
        if not res.get("ok", True):
            ui.log("warning", res.get("reason", "onboarding did not complete"))
    except Exception as exc:
        ui.log("broken", "onboarding failed: %s" % exc)


def _do_label_authenticity(ui, cfg):
    """Opt-in backfill: authenticity-check every already-held release the
    last scan has never sampled. Potentially long — see
    label_panel.authenticity_backfill().
    """
    if lbl is None:
        ui.log("broken", "label panel unavailable: %s" % _LBL_ERR)
        return
    def log(m):
        logging.info(m)
        ui.log("info", m)
    try:
        lbl.authenticity_backfill(cfg, log, should_stop=_stop_flag.is_set)
    except Exception as exc:
        ui.log("broken", "authenticity backfill failed: %s" % exc)


def _run_job(kind, sources, dest, provider_ids, cfg, dry_run, db_target="library",
            force=False, role="owned"):
    ui = _WebUI()
    try:
        with ui:
            if kind == "pipeline":
                for name, fn in [
                    ("import",   lambda: _do_import(ui, sources, dest, cfg, dry_run)),
                    ("fetch",    lambda: _do_fetch(ui, provider_ids, cfg, dry_run)),
                    ("organise", lambda: _do_organise(ui, dest, cfg, dry_run)),
                ]:
                    if _stop_flag.is_set(): break
                    _msg_q.put({"type": "phase", "phase": name, "status": "running"})
                    fn()
                    status = "stopped" if _stop_flag.is_set() else "done"
                    _msg_q.put({"type": "phase", "phase": name, "status": status})
            elif kind == "import":
                _do_import(ui, sources, dest, cfg, dry_run)
            elif kind == "fetch":
                _do_fetch(ui, provider_ids, cfg, dry_run)
            elif kind == "fetch_broken":
                ui.log("info", "re-fetching — retrying ALL tags (only_missing=False)")
                _do_fetch(ui, provider_ids, cfg, dry_run, only_missing=False)
            elif kind == "organise":
                _do_organise(ui, dest, cfg, dry_run)
            elif kind == "direct":
                _do_direct(ui, sources, dest, provider_ids, cfg, dry_run)
            elif kind == "direct_scan":
                _do_direct(ui, sources, dest, provider_ids, cfg, True,
                           steps=("import",))
            elif kind == "direct_tags":
                _do_direct(ui, sources, dest, provider_ids, cfg, dry_run,
                           steps=("import", "fetch"))
            elif kind == "direct_organise":
                _do_direct(ui, sources, dest, provider_ids, cfg, dry_run,
                           steps=("import", "organise"))
            elif kind in ("label_scan", "label_catalogue", "label_tracklists",
                         "label_prices"):
                _do_label(ui, kind, cfg, dest, force)
            elif kind == "label_onboard":
                _do_label_onboard(ui, cfg, dest, role)
            elif kind == "label_authenticity":
                _do_label_authenticity(ui, cfg)
            elif kind == "rebuild":
                _do_rebuild(ui, dest, cfg)
            elif kind == "vacuum":
                _do_vacuum(ui)
            elif kind == "tags_from_names":
                db_p = _FOLDER_DB if db_target == "folder" else \
                       (_SESSION_DB if db_target == "session" else _LIBRARY_DB)
                _do_tags_from_names(ui, db_p, dry_run)
            elif kind == "fake_flac_scan":
                if db_target == "folder":
                    _do_fake_flac_scan_folder(ui, Path(dest), cfg, force)
                else:
                    db_path = _SESSION_DB if db_target == "session" else _LIBRARY_DB
                    _do_fake_flac_scan(ui, db_path, force)
            elif kind == "fake_flac_vamp":
                db_path = _FOLDER_DB if db_target == "folder" else \
                          (_SESSION_DB if db_target == "session" else _LIBRARY_DB)
                _do_fake_flac_vamp(ui, db_path)
    except _StopRequested:
        _msg_q.put({"type": "log", "level": "warning", "text": "stopped by user"})
    except Exception as exc:
        import traceback
        _msg_q.put({"type": "log", "level": "broken",
                    "text": f"{kind} error: {exc}\n{traceback.format_exc()}"})
    finally:
        _msg_q.put(None)


# ─── app ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="music-organiser")

# The page lives in templates/index.html, static/app.css and static/app.js
# rather than inline in this file. StaticFiles handles ETag/If-None-Match, so
# an unchanged asset is a 304 rather than a re-download.
_TEMPLATES = _HERE / "templates"
_STATIC    = _HERE / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

_index_cache = {"stamp": None, "html": ""}


def _asset_version():
    """Cache-buster: the newest mtime across the page's three files.

    Without it a browser keeps last week's app.js after an upgrade and the
    bug report is 'the button does nothing'.
    """
    newest = 0.0
    # Every file under static/, not a hard-coded three. The list was written
    # when there were exactly three; i18n.js and i18n-lang.js were then added
    # and would have been cached across an upgrade while the page that uses
    # them was not — the language menu would work for new visitors and appear
    # broken for everyone who had loaded the page before.
    paths = [_TEMPLATES / "index.html"]
    try:
        paths += [f for f in _STATIC.iterdir() if f.is_file()]
    except OSError:
        pass
    for f in paths:
        try:
            newest = max(newest, f.stat().st_mtime)
        except OSError:
            pass
    return str(int(newest))


def _index_html():
    """index.html with the cache-buster filled in, re-read when it changes."""
    stamp = _asset_version()
    if _index_cache["stamp"] != stamp:
        raw = (_TEMPLATES / "index.html").read_text(encoding="utf-8")
        _index_cache["html"] = raw.replace("__ASSETV__", stamp)
        _index_cache["stamp"] = stamp
    return _index_cache["html"]


@app.get("/", response_class=HTMLResponse)
def root(): return HTMLResponse(_index_html())


@app.get("/wallpaper.jpg")
def wallpaper():
    """The faceted backdrop. Plain texture, no wordmark baked into it.

    wallpaper-pirate.jpg (the original theme) stays in assets/, just unused by
    default — swap the filename below to bring it back.

    There is deliberately no /logo.png or /logo-icon.png any more: the header
    wordmark is CSS + inline SVG in templates/index.html and the favicon is
    static/favicon.svg, so the page carries no logo image at all.
    """
    from fastapi.responses import FileResponse
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "wallpaper-ice.jpg")
    return FileResponse(p, media_type="image/jpeg")


@app.get("/api/config")
def get_config():
    cfg = _load_cfg()
    paths = cfg.get("paths", {})
    prov_cfg = cfg.get("providers") or {}
    try:
        from metadata_providers import ALL_PROVIDERS
        plist = []
        for cls in ALL_PROVIDERS:
            p = cls()
            pc = prov_cfg.get(p.id) or {}
            req = getattr(p, "requires_auth", False)
            key_field = next((k for k in ("token","api_key","client_id") if pc.get(k)), None)
            raw = pc.get(key_field, "") if key_field else ""
            if not key_field and req:
                key_field = "token"
            plist.append({
                "id": p.id, "name": getattr(p, "name", p.id),
                "requires_auth": req, "key_field": key_field,
                "has_key": bool(raw),
                "key_hint": ("•"*max(0,len(raw)-4)+raw[-4:]) if len(raw)>4 else "•"*len(raw),
                "enabled": pc.get("enabled", True),
            })
    except Exception as exc:
        plist = [{"id":"error","name":str(exc),"requires_auth":False,
                  "key_field":None,"has_key":False,"key_hint":"","enabled":False}]
    # Where the tree browser opens. "/" is the drive list on Windows.
    br = paths.get("destination_root", "") or ("" if os.name == "nt" else "/mnt")
    if not br or not Path(br).exists(): br = "/"
    return JSONResponse({
        "sources": paths.get("sources", []),
        "destination_root": paths.get("destination_root",""),
        "browse_root": br,
        "providers": plist,
    })


@app.post("/api/config/save")
async def save_config(request: Request):
    body = await request.json()
    try:
        _save_cfg(paths=body.get("paths"), providers=body.get("providers"))
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# "/" is the tree browser's root. On Linux that is a real directory; on
# Windows there is no single root, so "/" is a VIRTUAL root listing the drives
# (C:\\, D:\\, …). Path("/") is a real, valid path on Windows too — it means
# "root of the current drive" — so the virtual root has to be intercepted
# before any Path() work, or D: would be unreachable from the browser.
_VROOT = "/"


def _windows_drives() -> list[str]:
    """Drive roots that currently exist, e.g. ['C:\\', 'D:\\']."""
    import ctypes, string
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return []
    return [f"{letter}:\\" for i, letter in enumerate(string.ascii_uppercase)
            if mask >> i & 1]


def _is_vroot(path: str) -> bool:
    return os.name == "nt" and path.strip() in ("", "/", "\\", _VROOT)


@app.get("/api/browse")
def browse(path: str = "/"):
    if _is_vroot(path):
        entries = [{"name": d, "path": d, "is_dir": True} for d in _windows_drives()]
        return JSONResponse({"path": _VROOT, "parent": None, "entries": entries})
    p = Path(path)
    if not p.is_dir(): p = p.parent
    try:
        raw = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        entries = [{"name": e.name, "path": str(e), "is_dir": e.is_dir()}
                   for e in raw if not e.name.startswith(".")]
    except PermissionError:
        entries = []
    parent = str(p.parent) if str(p) != str(p.parent) else None
    # At a drive root (C:\) the parent is itself, which would dead-end the
    # browser on that one drive — send it back to the drive list instead.
    if parent is None and os.name == "nt":
        parent = _VROOT
    return JSONResponse({"path": str(p), "parent": parent, "entries": entries})


_PICKER_LOCK = threading.Lock()


def _picker_start_dir(start: str) -> Path | None:
    """Normalise the 'open here' hint: an existing dir, else its parent."""
    if not start.strip():
        return None
    d = Path(start).expanduser()
    if d.is_dir():
        return d
    return d.parent if d.parent.is_dir() else None


def _pick_zenity(start_dir: Path | None) -> tuple[bool, str]:
    """GTK folder chooser on this machine's X display (Linux)."""
    if not shutil.which("zenity"):
        return False, "zenity not installed"
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return False, "no display"
    cmd = ["zenity", "--file-selection", "--directory",
           "--title=music-organiser — choose a folder"]
    if start_dir is not None:
        # trailing slash: zenity opens INSIDE the dir rather than beside it
        cmd.append(f"--filename={start_dir}/")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        return False, "cancelled"
    return True, (proc.stdout or "").strip()


def _windows_has_desktop() -> bool:
    """False when we're running as a session-0 service, which has no desktop
    to draw a dialog on — the dialog would hang invisibly until the timeout."""
    import ctypes
    try:
        k32 = ctypes.windll.kernel32
        sid = ctypes.c_ulong()
        if not k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(sid)):
            return True          # couldn't tell — let it try
        return sid.value != 0
    except Exception:
        return True


# The Vista-era IFileOpenDialog with FOS_PICKFOLDER is the dialog Explorer
# itself uses: address bar, sidebar, search, network locations, the lot.
# FolderBrowserDialog (the .NET default) is the ancient tree-only popup with
# none of that, so we declare the COM interfaces by hand and fall back to
# FolderBrowserDialog only if that fails.
_WIN_PICKER_PS = r"""
$ErrorActionPreference = 'Stop'
$initial = @'
__INITIAL__
'@
$initial = $initial.Trim()

function Use-ModernPicker($start) {
  Add-Type -Namespace MO -Name Dlg -MemberDefinition @"
[ComImport, Guid("42f85136-db7e-439c-85f1-e4075d135fc8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
private interface IFileOpenDialog {
  [PreserveSig] int Show(IntPtr parent);
  void SetFileTypes(uint c, IntPtr f); void SetFileTypeIndex(uint i); void GetFileTypeIndex(out uint i);
  void Advise(IntPtr p, out uint c); void Unadvise(uint c);
  void SetOptions(uint opts); void GetOptions(out uint opts);
  void SetDefaultFolder(IShellItem si); void SetFolder(IShellItem si);
  void GetFolder(out IShellItem si); void GetCurrentSelection(out IShellItem si);
  void SetFileName(string n); void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string n);
  void SetTitle(string t); void SetOkButtonLabel(string t); void SetFileNameLabel(string t);
  void GetResult(out IShellItem si);
}
[ComImport, Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
private interface IShellItem {
  void BindToHandler(IntPtr bc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
  void GetParent(out IShellItem p);
  void GetDisplayName(uint sigdn, [MarshalAs(UnmanagedType.LPWStr)] out string name);
  void GetAttributes(uint mask, out uint attrs);
  void Compare(IShellItem psi, uint hint, out int order);
}
[ComImport, Guid("dc1c5a9c-e88a-4dde-a5a1-60f82a20aef7")] private class FileOpenDialogRCW { }
[DllImport("shell32.dll", CharSet=CharSet.Unicode, PreserveSig=false)]
private static extern void SHCreateItemFromParsingName(string path, IntPtr bc,
  [MarshalAs(UnmanagedType.LPStruct)] Guid riid, [MarshalAs(UnmanagedType.Interface)] out object item);

public static string Pick(string start) {
  var dlg = (IFileOpenDialog)(new FileOpenDialogRCW());
  uint opts; dlg.GetOptions(out opts);
  // FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST
  dlg.SetOptions(opts | 0x20 | 0x40 | 0x800);
  dlg.SetTitle("music-organiser - choose a folder");
  if (!string.IsNullOrEmpty(start)) {
    try {
      object si;
      SHCreateItemFromParsingName(start, IntPtr.Zero,
        new Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"), out si);
      dlg.SetFolder((IShellItem)si);
    } catch { }
  }
  if (dlg.Show(IntPtr.Zero) != 0) return "";   // cancelled
  IShellItem res; dlg.GetResult(out res);
  string path; res.GetDisplayName(0x80058000, out path);   // SIGDN_FILESYSPATH
  return path;
}
"@ -ErrorAction Stop | Out-Null
  return [MO.Dlg]::Pick($start)
}

function Use-LegacyPicker($start) {
  Add-Type -AssemblyName System.Windows.Forms | Out-Null
  $d = New-Object System.Windows.Forms.FolderBrowserDialog
  $d.Description = 'music-organiser - choose a folder'
  $d.ShowNewFolderButton = $true
  if ($start) { $d.SelectedPath = $start }
  if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return $d.SelectedPath }
  return ""
}

try   { $out = Use-ModernPicker $initial }
catch { $out = Use-LegacyPicker $initial }
[Console]::Out.Write($out)
"""


def _pick_windows(start_dir: Path | None) -> tuple[bool, str]:
    """Explorer's own folder chooser via PowerShell (Windows).

    Uses IFileOpenDialog + FOS_PICKFOLDERS — the same dialog Explorer draws,
    with the address bar, Quick-access sidebar and search. Falls back to the
    old FolderBrowserDialog only if declaring the COM interfaces fails.

    powershell.exe needs -STA for the shell dialog; pwsh (7+) dropped that
    switch and is already STA, hence the two spellings. The chosen path goes
    to stdout on its own — PowerShell warnings stay on stderr.

    The script goes to a temp .ps1 file run with -File, NOT piped via stdin
    with -Command -. Piping it in (subprocess.run(..., input=script)) looked
    fine — exit 0, no stderr — but produced zero stdout and never showed the
    dialog at all when the parent process's stdin is itself redirected/piped
    (exactly how uvicorn's worker launches this). -File with a real script
    file opens the dialog reliably in the same situation; confirmed by
    reproducing both paths directly outside the app.
    """
    if not _windows_has_desktop():
        return False, "no interactive desktop (running as a service)"
    exe = shutil.which("powershell") or shutil.which("powershell.exe")
    args = ["-NoProfile", "-STA", "-NonInteractive"]
    if not exe:
        exe = shutil.which("pwsh")
        args = ["-NoProfile", "-NonInteractive"]
    if not exe:
        return False, "powershell not found"
    initial = str(start_dir) if start_dir is not None else ""
    script = _WIN_PICKER_PS.replace("__INITIAL__", initial)
    fd, tmp_path = tempfile.mkstemp(suffix=".ps1", prefix="mo_pick_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        proc = subprocess.run([exe] + args + ["-File", tmp_path],
                              capture_output=True, text=True, timeout=300)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    chosen = (proc.stdout or "").strip()
    if not chosen:
        return False, "cancelled"
    return True, chosen


def _pick_macos(start_dir: Path | None) -> tuple[bool, str]:
    """Finder's own chooser via osascript (macOS)."""
    if not shutil.which("osascript"):
        return False, "osascript not found"
    default = f' default location POSIX file "{start_dir}"' if start_dir else ""
    script = f'POSIX path of (choose folder with prompt "Choose a folder"{default})'
    proc = subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        return False, "cancelled"
    return True, (proc.stdout or "").strip()


@app.post("/api/session/edit")
async def session_edit(request: Request):
    """Edit one file's tags — and optionally its NAME — from the Session tab.

    Writes the tags into the file itself (not just the database row), because
    a database that disagrees with the file is how this library got into
    trouble in the first place. `only_missing=False`: an edit is an
    instruction, not a suggestion.
    """
    body = await request.json()
    path = (body.get("path") or "").strip()
    fields = body.get("fields") or {}
    rename = (body.get("rename") or "").strip()
    write_tags = bool(body.get("write_tags", True))
    which = body.get("db", "session")
    db_path = _SESSION_DB if which == "session" else _LIBRARY_DB

    p = Path(path)
    if not p.is_file():
        return JSONResponse({"ok": False, "reason": "file not found"}, status_code=404)

    allowed = ("artist", "albumartist", "album", "title", "year", "label",
               "catalog_number", "genre", "track_number", "disc_number")
    clean = {k: v for k, v in fields.items() if k in allowed}
    notes = []

    if write_tags and clean:
        try:
            from tag_writer import write_tags_to_file
            res = write_tags_to_file(p, clean, only_missing=False,
                                     touch_verified_rips=False, dry_run=False)
            if getattr(res, "error", None):
                return JSONResponse({"ok": False, "reason": "tag write failed: %s"
                                     % res.error}, status_code=500)
            if getattr(res, "skipped_entirely", False):
                notes.append("tags NOT written: %s" % res.skip_reason)
        except Exception as exc:
            return JSONResponse({"ok": False, "reason": str(exc)}, status_code=500)

    final = p
    if rename and rename != p.name:
        bad = set('<>:"/\\|?*')
        if any(c in bad for c in rename):
            return JSONResponse({"ok": False,
                                 "reason": "a filename cannot contain < > : \" / \\ | ? *"},
                                status_code=400)
        if not Path(rename).suffix:
            rename += p.suffix
        target = p.with_name(rename)
        if target.exists():
            return JSONResponse({"ok": False, "reason": "%s already exists" % rename},
                                status_code=409)
        try:
            p.rename(target)
            final = target
        except Exception as exc:
            return JSONResponse({"ok": False, "reason": "rename failed: %s" % exc},
                                status_code=500)

    if db_path.exists():
        try:
            with sqlite3.connect(str(db_path)) as conn:
                sets = ["%s = ?" % k for k in clean]
                vals = list(clean.values())
                if final != p:
                    sets.append("path = ?")
                    vals.append(str(final))
                    sets.append("filename = ?")
                    vals.append(final.name)
                if sets:
                    vals.append(str(p))
                    conn.execute("UPDATE files SET %s WHERE path = ?"
                                 % ", ".join(sets), vals)
                conn.commit()
        except Exception as exc:
            notes.append("file updated but the database was not: %s" % exc)

    return JSONResponse({"ok": True, "path": str(final), "filename": final.name,
                         "reason": "; ".join(notes) or "saved to the file and the database"})


@app.post("/api/session/act")
async def session_act(request: Request):
    """Manual work on the session: move, copy, delete, re-read tags, forget.

    Reports what happened to EACH file rather than one ok/failed — on a move
    of forty files you need to know which three did not make it and why.
    Nothing here touches library.db; the session is the scratch pad.
    """
    body = await request.json()
    action = (body.get("action") or "").strip()
    paths = [p for p in (body.get("paths") or []) if p]
    dest = (body.get("dest") or "").strip()
    which = body.get("db", "session")
    db_path = _SESSION_DB if which == "session" else _LIBRARY_DB
    if action not in ("move", "copy", "delete", "forget", "retag"):
        return JSONResponse({"error": f"unknown action: {action}"}, status_code=400)
    if not paths:
        return JSONResponse({"error": "nothing selected"}, status_code=400)
    if action in ("move", "copy"):
        if not dest:
            return JSONResponse({"error": "pick a destination folder first"},
                                status_code=400)
        try:
            Path(dest).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return JSONResponse({"error": f"cannot use {dest}: {exc}"}, status_code=400)

    done, failed = [], []
    conn = sqlite3.connect(str(db_path)) if db_path.exists() else None
    try:
        for raw in paths:
            src = Path(raw)
            try:
                if action == "forget":
                    if conn:
                        conn.execute("DELETE FROM files WHERE path = ?", (str(src),))
                elif action == "delete":
                    if src.is_file():
                        src.unlink()
                    if conn:
                        conn.execute("DELETE FROM files WHERE path = ?", (str(src),))
                elif action == "retag":
                    if not src.is_file():
                        raise FileNotFoundError("file is gone")
                    from metadata import extract_metadata
                    meta = extract_metadata(str(src)) or {}
                    if conn and meta:
                        sets, vals = [], []
                        for col in ("artist", "albumartist", "album", "title", "year",
                                    "label", "catalog_number", "genre", "track_number",
                                    "disc_number"):
                            if col in meta:
                                sets.append(f"{col} = ?")
                                vals.append(meta[col])
                        if sets:
                            vals.append(str(src))
                            conn.execute("UPDATE files SET %s WHERE path = ?"
                                         % ", ".join(sets), vals)
                else:                                   # move / copy
                    if not src.is_file():
                        raise FileNotFoundError("file is gone")
                    target = Path(dest) / src.name
                    n = 1
                    while target.exists():
                        target = Path(dest) / f"{src.stem} ({n}){src.suffix}"
                        n += 1
                    if action == "move":
                        shutil.move(str(src), str(target))
                        if conn:
                            conn.execute("UPDATE files SET path = ? WHERE path = ?",
                                         (str(target), str(src)))
                    else:
                        shutil.copy2(str(src), str(target))
                done.append(str(src))
            except Exception as exc:
                failed.append({"path": str(src), "error": str(exc)})
        if conn:
            conn.commit()
    finally:
        if conn:
            conn.close()
    return JSONResponse({"ok": not failed, "action": action,
                         "done": len(done), "failed": failed})


@app.get("/api/fs")
def fs_list(path: str = "", audio: bool = True):
    """List one directory for the file browser.

    Returns folders first, each with a count of the audio files directly
    inside it — when you are choosing a source folder, "how much is in here"
    is the only question you actually have, and the old tree could not answer
    it. Never raises: an unreadable directory comes back with an `error` and
    an empty list so the browser can say so instead of going blank.
    """
    import os as _os
    exts = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".aif",
            ".ape", ".wv", ".alac", ".dsf", ".dff", ".wave"}
    raw = (path or "").strip() or str(Path.home())
    try:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path.home() / p
        p = Path(_os.path.normpath(str(p)))
    except Exception:
        p = Path.home()

    out = {"path": str(p), "parent": str(p.parent) if p.parent != p else "",
           "dirs": [], "files": 0, "audio": 0, "error": ""}
    if not p.is_dir():
        out["error"] = "not a folder: %s" % p
        return JSONResponse(out)
    try:
        entries = sorted(_os.scandir(p), key=lambda e: e.name.lower())
    except PermissionError:
        out["error"] = "no permission to read %s" % p
        return JSONResponse(out)
    except OSError as exc:
        out["error"] = str(exc)
        return JSONResponse(out)

    for e in entries:
        try:
            if e.name.startswith("."):
                continue
            if e.is_dir(follow_symlinks=False):
                n = 0
                if audio:
                    try:
                        with _os.scandir(e.path) as it:
                            for c in it:
                                if (c.is_file(follow_symlinks=False)
                                        and _os.path.splitext(c.name)[1].lower() in exts):
                                    n += 1
                    except OSError:
                        n = -1                      # unreadable, say so
                out["dirs"].append({"name": e.name, "path": e.path, "audio": n})
            elif e.is_file(follow_symlinks=False):
                out["files"] += 1
                if _os.path.splitext(e.name)[1].lower() in exts:
                    out["audio"] += 1
        except OSError:
            continue
    return JSONResponse(out)


@app.get("/api/fs/places")
def fs_places():
    """Shortcuts worth having, and only the ones that exist on this machine."""
    import os as _os
    out = []
    home = Path.home()
    out.append({"label": "Home", "path": str(home)})
    for base in ("/mnt", "/media", "/media/" + _os.environ.get("USER", ""),
                 "C:\\", "D:\\"):
        b = Path(base)
        try:
            if b.is_dir():
                out.append({"label": base, "path": str(b)})
                for child in sorted(b.iterdir())[:12]:
                    if child.is_dir():
                        out.append({"label": "  " + child.name, "path": str(child)})
        except OSError:
            continue
    seen, uniq = set(), []
    for r in out:
        if r["path"] not in seen:
            seen.add(r["path"])
            uniq.append(r)
    return JSONResponse({"places": uniq[:24]})


@app.get("/api/pick-folder")
def pick_folder(start: str = ""):
    """Open the platform's own folder chooser and return the chosen directory.

    zenity on Linux, a WinForms FolderBrowserDialog on Windows, osascript on
    macOS. The dialog is drawn on THIS machine's desktop, so it only helps when
    the UI is being driven from the box the server runs on — a browser on
    another machine has no desktop here to draw on. In that case (or with no
    picker binary available) we return ok=False and the front-end silently
    falls back to the in-page tree browser, which works from anywhere.
    """
    # One dialog at a time — a second would fight for focus and the user would
    # have no idea which field they were answering.
    if not _PICKER_LOCK.acquire(blocking=False):
        return JSONResponse({"ok": False, "reason": "a picker is already open"})
    try:
        start_dir = _picker_start_dir(start)
        if os.name == "nt":
            ok, result = _pick_windows(start_dir)
        elif sys.platform == "darwin":
            ok, result = _pick_macos(start_dir)
        else:
            ok, result = _pick_zenity(start_dir)
        if not ok:
            return JSONResponse({"ok": False, "reason": result})
        if not result or not Path(result).is_dir():
            return JSONResponse({"ok": False, "reason": "cancelled"})
        return JSONResponse({"ok": True, "path": result})
    except subprocess.TimeoutExpired:
        return JSONResponse({"ok": False, "reason": "timed out"})
    except Exception as exc:
        return JSONResponse({"ok": False, "reason": str(exc)})
    finally:
        _PICKER_LOCK.release()


@app.get("/api/open-folder")
def open_folder(path: str = ""):
    """Show a folder in the desktop's own file manager.

    Explorer on Windows (Win11 reuses/adds a tab rather than a new window),
    Finder on macOS, and whatever handles directories on Linux. Same desktop
    caveat as the picker: this draws on the machine the SERVER runs on, so a
    browser on another box gets ok=False and the UI just doesn't offer it.
    """
    p = Path(path) if path else None
    if p is None or not p.exists():
        return JSONResponse({"ok": False, "reason": "no such folder"})
    if not p.is_dir():
        p = p.parent
    try:
        if os.name == "nt":
            if not _windows_has_desktop():
                return JSONResponse({"ok": False, "reason": "no interactive desktop"})
            # os.startfile is the one that reuses an existing Explorer tab;
            # explorer.exe always returns 1, so its exit code means nothing.
            os.startfile(str(p))          # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            opener = shutil.which("xdg-open") or shutil.which("nemo") \
                     or shutil.which("nautilus") or shutil.which("thunar")
            if not opener:
                return JSONResponse({"ok": False, "reason": "no file manager found"})
            subprocess.Popen([opener, str(p)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return JSONResponse({"ok": True, "path": str(p)})
    except Exception as exc:
        return JSONResponse({"ok": False, "reason": str(exc)})


# ─── telegram control panel ───────────────────────────────────────────────────
def _tg_guard():
    if tgp is None:
        return JSONResponse({"ok": False, "reason": "telegram_panel failed to load: %s" % _TG_ERR})
    return None


@app.get("/api/tg/status")
def tg_status(since: int = 0):
    g = _tg_guard()
    if g:
        return g
    return JSONResponse(tgp.status(_load_cfg(), since))


@app.post("/api/tg/scraper")
async def tg_scraper(req: Request):
    g = _tg_guard()
    if g:
        return g
    body = await req.json()
    ok, msg = tgp.run_scraper(_load_cfg(), body.get("mode", ""), body.get("channel", ""),
                              body.get("opts") or {})
    return JSONResponse({"ok": ok, "reason": msg})


@app.post("/api/tg/scraper/stop")
def tg_scraper_stop():
    g = _tg_guard()
    if g:
        return g
    ok, msg = tgp.JOB.stop()
    return JSONResponse({"ok": ok, "reason": msg})


@app.post("/api/tg/uploader")
async def tg_uploader(req: Request):
    g = _tg_guard()
    if g:
        return g
    body = await req.json()
    act = body.get("action", "")
    if act in ("on", "off"):
        ok, msg = tgp.uploader_set(_load_cfg(), act == "on")
    elif act == "retry":
        ok, msg = tgp.uploader_retry_failed(_load_cfg())
    else:
        ok, msg = False, "unknown action %r" % act
    return JSONResponse({"ok": ok, "reason": msg})


@app.post("/api/tg/settings")
async def tg_settings(req: Request):
    if tgp is None:
        return JSONResponse({"ok": False, "reason": _TG_ERR or "no panel"})
    body = await req.json()
    ok, msg = tgp.uploader_config_set(_load_cfg(), body or {})
    return JSONResponse({"ok": ok, "reason": msg,
                         "settings": tgp.uploader_config(_load_cfg())})


@app.post("/api/tg/tools")
async def tg_tools(req: Request):
    if tgp is None:
        return JSONResponse({"ok": False, "reason": _TG_ERR or "no panel"})
    body = await req.json()
    ok, out = tgp.uploader_tools(_load_cfg(), (body or {}).get("what", "all"))
    return JSONResponse({"ok": ok, "output": out})


@app.post("/api/tg/action")
async def tg_action(req: Request):
    if tgp is None:
        return JSONResponse({"ok": False, "reason": _TG_ERR or "no panel"})
    body = await req.json()
    ok, out = tgp.uploader_action(_load_cfg(), (body or {}).get("name", ""))
    return JSONResponse({"ok": ok, "output": out})


# ─── labels ───────────────────────────────────────────────────────────────────
# The Labels tab answers three questions about a record label: what we have,
# what we do not, and what is still missing from the releases we half-hold.
# Heavy work (reading 1,200 folders of tags off a USB bridge) goes through the
# ordinary job runner so it streams into the same log the other tabs use.

def _lbl_guard():
    if lbl is None:
        return JSONResponse({"ok": False,
                             "reason": "label_panel failed to load: %s" % _LBL_ERR})
    return None


@app.get("/api/label/status")
def label_status():
    g = _lbl_guard()
    if g:
        return g
    return JSONResponse(lbl.status(_load_cfg()))


@app.get("/api/label/overview")
def label_overview():
    g = _lbl_guard()
    if g:
        return g
    return JSONResponse({"labels": lbl.overview()})


@app.get("/api/label/cross-label")
def label_cross_label(role: str = "incoming"):
    g = _lbl_guard()
    if g:
        return g
    return JSONResponse(lbl.cross_label_check(role))


@app.post("/api/label/untrack")
async def label_untrack(request: Request):
    g = _lbl_guard()
    if g:
        return g
    body = await request.json()
    lbl.untrack_label(body.get("id"))
    return JSONResponse({"ok": True})


@app.get("/api/label/rows")
def label_rows(status: str = "", q: str = "", verdict: str = "",
               limit: int = 400, offset: int = 0):
    """The last scan, filtered.  Paged: 2,952 rows is not a thing to send at once."""
    g = _lbl_guard()
    if g:
        return g
    scan = lbl.last_scan()
    if not scan:
        return JSONResponse({"ok": False, "reason": "nothing scanned yet",
                             "rows": [], "total": 0})
    rows = scan["rows"]
    if status:
        want = set(status.split(","))
        rows = [r for r in rows if r["status"] in want]
    if verdict:
        want = set(verdict.split(","))
        rows = [r for r in rows if r["verdict"] in want]
    if q:
        ql = q.lower()
        rows = [r for r in rows
                if ql in (r["title"] or "").lower()
                or ql in (r["catno"] or "").lower()
                or ql in (r["artist"] or "").lower()]
    total = len(rows)
    return JSONResponse({"ok": True, "total": total,
                         "when": scan["when"], "summary": scan["summary"],
                         "rows": rows[offset:offset + max(1, min(limit, 2000))]})


@app.get("/api/label/orphans")
def label_orphans(limit: int = 400):
    """Folders under the added roots that this label's catalogue does not know.

    Not a failure: it is how you find a folder filed under the wrong label, and
    how an incoming share's non-label material shows itself.
    """
    g = _lbl_guard()
    if g:
        return g
    scan = lbl.last_scan()
    if not scan:
        return JSONResponse({"ok": False, "reason": "nothing scanned yet",
                             "rows": []})
    return JSONResponse({"ok": True, "rows": scan["orphans"][:limit],
                         "total": len(scan["orphans"])})


@app.get("/api/label/series")
def label_series():
    """Numbered series/volumes grouped together, from the last scan —
    "Limite — 9 of 13, missing 3, 7, 11" instead of 13 unrelated rows."""
    g = _lbl_guard()
    if g:
        return g
    scan = lbl.last_scan()
    if not scan:
        return JSONResponse({"ok": False, "reason": "nothing scanned yet",
                             "rows": []})
    return JSONResponse({"ok": True, "rows": scan.get("series") or []})


@app.post("/api/label/set")
async def label_set(req: Request):
    g = _lbl_guard()
    if g:
        return g
    body = await req.json()
    what = body.get("what", "")
    if what == "label":
        st = lbl.set_label(int(body.get("id") or 0), body.get("name", ""))
        return JSONResponse({"ok": True, "state": st})
    if what == "add_root":
        ok, msg = lbl.add_root(body.get("path", ""), body.get("role", "owned"))
        return JSONResponse({"ok": ok, "reason": msg})
    if what == "remove_root":
        ok, msg = lbl.remove_root(body.get("path", ""))
        return JSONResponse({"ok": ok, "reason": msg})
    if what == "archive":
        ok, msg = lbl.set_archive(body.get("path", ""))
        return JSONResponse({"ok": ok, "reason": msg})
    return JSONResponse({"ok": False, "reason": "unknown: %r" % what},
                        status_code=400)


@app.get("/api/label/search")
def label_search(q: str = ""):
    """Find a label on Discogs by name, so the id never has to be typed."""
    g = _lbl_guard()
    if g:
        return g
    if not q.strip():
        return JSONResponse({"ok": False, "reason": "type a label name"})
    try:
        d = label_ref.Discogs(lbl._token(_load_cfg()))
        return JSONResponse({"ok": True, "results": d.search_label(q)})
    except Exception as exc:
        return JSONResponse({"ok": False, "reason": str(exc)})


@app.get("/api/label/export")
def label_export(kind: str = "outstanding_tracks"):
    """Hand back a list as a file.  The browser downloads it; nothing is written
    on the server, so this works the same from the Windows box."""
    g = _lbl_guard()
    if g:
        return g
    try:
        name, text = lbl.export(kind)
    except Exception as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    from fastapi.responses import PlainTextResponse
    quoted = urllib.parse.quote(name)
    return PlainTextResponse(
        text, media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition":
                 "attachment; filename*=UTF-8''%s" % quoted})


@app.post("/api/label/move")
async def label_move(req: Request):
    g = _lbl_guard()
    if g:
        return g
    body = await req.json()
    res = lbl.move_folders(body.get("paths") or [], body.get("dest", ""),
                           bool(body.get("dry_run")))
    return JSONResponse(res)


@app.post("/api/label/fix-tags")
async def label_fix_tags(req: Request):
    g = _lbl_guard()
    if g:
        return g
    body = await req.json()
    res = lbl.fix_tags(body.get("paths") or [], bool(body.get("dry_run")))
    return JSONResponse(res)


@app.post("/api/label/get-artwork")
async def label_get_artwork(req: Request):
    g = _lbl_guard()
    if g:
        return g
    body = await req.json()
    res = lbl.get_artwork(body.get("paths") or [], _load_cfg(), bool(body.get("dry_run")))
    return JSONResponse(res)


@app.post("/api/label/tag-incoming")
async def label_tag_incoming(req: Request):
    g = _lbl_guard()
    if g:
        return g
    body = await req.json()
    res = lbl.tag_incoming(body.get("paths") or [], bool(body.get("dry_run")))
    return JSONResponse(res)


@app.get("/api/label/moves")
def label_moves(limit: int = 200):
    g = _lbl_guard()
    if g:
        return g
    return JSONResponse({"ok": True, "rows": lbl.move_log(limit)})


@app.post("/api/label/undo")
async def label_undo(req: Request):
    g = _lbl_guard()
    if g:
        return g
    body = await req.json()
    ok, msg = lbl.undo_move(body.get("dest", ""))
    return JSONResponse({"ok": ok, "reason": msg})


@app.get("/api/scan")
def scan_source(path: str = ""):
    if not path:
        return JSONResponse({"count": 0, "error": "no path"})
    p = Path(path)
    if not p.is_dir():
        return JSONResponse({"count": 0, "error": "not a directory"})
    audio = {".flac",".mp3",".m4a",".ogg",".opus",".wav",".aiff",".ape",".wv"}
    count = sum(1 for f in p.rglob("*") if f.suffix.lower() in audio)
    return JSONResponse({"count": count, "path": str(p)})


# ─── session API ──────────────────────────────────────────────────────────────
@app.get("/api/session/files")
def session_files_api():
    if not _SESSION_DB.exists():
        return JSONResponse({"files": [], "stats": {}})
    rows = _db_rows(_SESSION_DB,
        "SELECT path, artist, albumartist, album, year, label, catalog_number, "
        "title, status, genre, duration_seconds, size_bytes "
        "FROM files ORDER BY status, artist, album, path")
    stats = {}
    for r in rows:
        s = r.get("status") or "unknown"
        stats[s] = stats.get(s, 0) + 1
    stats["total"] = len(rows)
    for r in rows:
        r["filename"] = Path(r["path"]).name
        r["dur"] = f"{int((r.get('duration_seconds') or 0)//60)}:{int((r.get('duration_seconds') or 0)%60):02d}"
        r["mb"] = round((r.get("size_bytes") or 0) / 1048576, 1)
    return JSONResponse({"files": rows, "stats": stats})


# ─── library API ──────────────────────────────────────────────────────────────
@app.get("/api/library/stats")
def library_stats_api():
    def _stat(db_path, label):
        if not db_path.exists():
            return {"label": label, "exists": False}
        total  = _db_one(db_path, "SELECT COUNT(*) FROM files") or 0
        by_status = _db_rows(db_path,
            "SELECT status, COUNT(*) as n FROM files GROUP BY status ORDER BY n DESC")
        artists = _db_one(db_path,
            "SELECT COUNT(DISTINCT COALESCE(NULLIF(TRIM(primary_artist),''), "
            "NULLIF(TRIM(albumartist),''), NULLIF(TRIM(artist),''), 'Unknown')) FROM files") or 0
        labels  = _db_one(db_path,
            "SELECT COUNT(DISTINCT NULLIF(TRIM(label),'')) FROM files") or 0
        size_gb = (_db_one(db_path, "SELECT SUM(size_bytes) FROM files") or 0) / 1e9
        return {"label": label, "exists": True, "total": total,
                "by_status": by_status, "artists": artists,
                "labels": labels, "size_gb": round(size_gb, 2),
                "db_kb": round(db_path.stat().st_size / 1024)}
    return JSONResponse({
        "session": _stat(_SESSION_DB, "Session"),
        "library": _stat(_LIBRARY_DB, "Library"),
    })


@app.get("/api/library/files")
def library_files_api(
    db: str = "library",
    q: str = "",
    status: str = "",
    page: int = 0,
    per_page: int = 50,
):
    db_path = _SESSION_DB if db == "session" else _LIBRARY_DB
    if not db_path.exists():
        return JSONResponse({"files": [], "total": 0, "page": 0, "pages": 0})
    where_parts = []
    params: list = []
    if q:
        where_parts.append(
            "(artist LIKE ? OR album LIKE ? OR title LIKE ? OR label LIKE ? OR catalog_number LIKE ?)")
        p = f"%{q}%"
        params.extend([p, p, p, p, p])
    if status:
        where_parts.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    total = _db_one(db_path, f"SELECT COUNT(*) FROM files {where}", params) or 0
    offset = page * per_page
    rows = _db_rows(db_path,
        f"SELECT path, artist, albumartist, album, year, label, catalog_number, "
        f"title, status, genre, duration_seconds, size_bytes "
        f"FROM files {where} ORDER BY artist, album, path "
        f"LIMIT ? OFFSET ?",
        params + [per_page, offset])
    for r in rows:
        r["filename"] = Path(r["path"]).name
        r["dur"] = f"{int((r.get('duration_seconds') or 0)//60)}:{int((r.get('duration_seconds') or 0)%60):02d}"
        r["mb"] = round((r.get("size_bytes") or 0) / 1048576, 1)
    return JSONResponse({
        "files": rows, "total": total, "page": page,
        "pages": max(1, (total + per_page - 1) // per_page),
    })


@app.post("/api/library/sql")
async def library_sql_api(request: Request):
    body = await request.json()
    sql = (body.get("sql") or "").strip()
    db_target = body.get("db", "library")
    db_path = _SESSION_DB if db_target == "session" else _LIBRARY_DB
    if not sql:
        return JSONResponse({"error": "empty query"}, status_code=400)
    sql_upper = sql.upper().lstrip()
    if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
        return JSONResponse({"error": "only SELECT / WITH queries allowed"}, status_code=400)
    if not db_path.exists():
        return JSONResponse({"error": f"{db_path.name} not found"}, status_code=404)
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [dict(r) for r in cur.fetchmany(500)]
            return JSONResponse({"cols": cols, "rows": rows, "count": len(rows)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/library/audits")
def library_audits_api():
    if not _LIBRARY_DB.exists():
        return JSONResponse({"error": "library.db not found"})
    try:
        from database import Database
        from audit import audit_all, AUDITS
        db = Database(str(_LIBRARY_DB))
        report = audit_all(db)
        results = []
        for key, (label, _) in AUDITS.items():
            issues = report.issues_by_audit.get(key, [])
            results.append({"key": key, "label": label, "count": len(issues),
                            "sample": [r.get("path","") for r in issues[:5]]})
        return JSONResponse({"audits": results,
                             "total_issues": report.total_issues()})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── fake-flac tab ──────────────────────────────────────────────────────────────
def _fakeflac_db(db_target: str) -> Path:
    if db_target == "folder":
        return _FOLDER_DB
    return _SESSION_DB if db_target == "session" else _LIBRARY_DB


@app.get("/api/fakeflac/vamp-available")
def fakeflac_vamp_available():
    """Whether Stage-2 Vamp confirm (sonic-annotator + the CNN plugin) is
    installed on THIS machine. Expected to be False on a bare Windows box;
    fine on the Chromebox if it has the plugin — the UI hides the button
    either way, it just checks first instead of assuming."""
    try:
        from rip_audio import find_sonic_annotator
        found = find_sonic_annotator()
    except Exception:
        found = None
    return JSONResponse({"available": bool(found)})


@app.get("/api/fakeflac/suspects")
def fakeflac_suspects(db: str = "library", page: int = 0, per_page: int = 50):
    db_path = _fakeflac_db(db)
    if not db_path.exists():
        return JSONResponse({"files": [], "total": 0, "page": 0, "pages": 0})
    total = _db_one(db_path,
        "SELECT COUNT(*) FROM files WHERE transcode_suspected = 1") or 0
    offset = page * per_page
    # The verdict columns are newer than some databases, so ask for them and
    # fall back rather than 500-ing on an older file.
    cols = ("path, artist, albumartist, album, title, size_bytes, "
            "transcode_cutoff_hz, transcode_confidence, transcode_notes, "
            "transcode_verdict, transcode_wall_db, transcode_above_db")
    try:
        rows = _db_rows(db_path,
            f"SELECT {cols} FROM files WHERE transcode_suspected = 1 "
            "ORDER BY transcode_confidence DESC, path LIMIT ? OFFSET ?",
            [per_page, offset])
    except Exception:
        rows = _db_rows(db_path,
            "SELECT path, artist, albumartist, album, title, size_bytes, "
            "transcode_cutoff_hz, transcode_confidence, transcode_notes "
            "FROM files WHERE transcode_suspected = 1 "
            "ORDER BY transcode_confidence DESC, path "
            "LIMIT ? OFFSET ?", [per_page, offset])
    for r in rows:
        r["filename"] = Path(r["path"]).name
        r["mb"] = round((r.get("size_bytes") or 0) / 1048576, 1)
    return JSONResponse({
        "files": rows, "total": total, "page": page,
        "pages": max(1, (total + per_page - 1) // per_page),
    })


@app.post("/api/fakeflac/isolate")
async def fakeflac_isolate(request: Request):
    """Move a suspected file into <destination_root>/<suspected_transcode_folder>/
    and update its path in the DB so the library stays consistent."""
    body = await request.json()
    path = body.get("path", "")
    db_path = _fakeflac_db(body.get("db", "library"))
    p = Path(path)
    if not p.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)
    cfg = _load_cfg()
    paths_cfg = cfg.get("paths", {})
    dest_root = paths_cfg.get("destination_root", "")
    folder_name = paths_cfg.get("suspected_transcode_folder", "Suspected Transcodes")
    if not dest_root:
        return JSONResponse({"error": "no destination_root configured"}, status_code=400)
    target_dir = Path(dest_root) / folder_name
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / p.name
        n = 1
        while target.exists():
            target = target_dir / f"{p.stem} ({n}){p.suffix}"
            n += 1
        shutil.move(str(p), str(target))
        if db_path.exists():
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("UPDATE files SET path = ? WHERE path = ?",
                            (str(target), str(p)))
                conn.commit()
        return JSONResponse({"ok": True, "path": str(target)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/fakeflac/delete")
async def fakeflac_delete(request: Request):
    body = await request.json()
    path = body.get("path", "")
    db_path = _fakeflac_db(body.get("db", "library"))
    p = Path(path)
    if not p.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)
    try:
        p.unlink()
        if db_path.exists():
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("DELETE FROM files WHERE path = ?", (str(p),))
                conn.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/fakeflac/dismiss")
async def fakeflac_dismiss(request: Request):
    """False positive — clear transcode_suspected without touching the file."""
    body = await request.json()
    path = body.get("path", "")
    db_path = _fakeflac_db(body.get("db", "library"))
    if not db_path.exists():
        return JSONResponse({"error": f"{db_path.name} not found"}, status_code=404)
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("UPDATE files SET transcode_suspected = 0 WHERE path = ?", (path,))
            conn.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/fakeflac/bulk")
async def fakeflac_bulk(request: Request):
    """Apply one action to many files at once.

    Deliberately reports per-file outcomes rather than a single ok/failed: on
    a delete of forty files you need to know WHICH four were already gone.
    """
    body = await request.json()
    action = (body.get("action") or "").strip()
    paths = [p for p in (body.get("paths") or []) if p]
    db_path = _fakeflac_db(body.get("db", "library"))
    if action not in ("delete", "isolate", "dismiss"):
        return JSONResponse({"error": f"unknown action: {action}"}, status_code=400)
    if not paths:
        return JSONResponse({"error": "nothing selected"}, status_code=400)

    cfg = _load_cfg()
    paths_cfg = cfg.get("paths", {})
    target_dir = None
    if action == "isolate":
        dest_root = paths_cfg.get("destination_root", "")
        if not dest_root:
            return JSONResponse({"error": "no destination_root configured"},
                                status_code=400)
        target_dir = Path(dest_root) / paths_cfg.get(
            "suspected_transcode_folder", "Suspected Transcodes")
        target_dir.mkdir(parents=True, exist_ok=True)

    done, failed = [], []
    conn = sqlite3.connect(str(db_path)) if db_path.exists() else None
    try:
        for raw in paths:
            p = Path(raw)
            try:
                if action == "dismiss":
                    if conn:
                        conn.execute("UPDATE files SET transcode_suspected = 0 "
                                     "WHERE path = ?", (str(p),))
                elif action == "delete":
                    if not p.is_file():
                        raise FileNotFoundError("already gone")
                    p.unlink()
                    if conn:
                        conn.execute("DELETE FROM files WHERE path = ?", (str(p),))
                else:                                   # isolate
                    if not p.is_file():
                        raise FileNotFoundError("already gone")
                    target = target_dir / p.name
                    n = 1
                    while target.exists():
                        target = target_dir / f"{p.stem} ({n}){p.suffix}"
                        n += 1
                    shutil.move(str(p), str(target))
                    if conn:
                        conn.execute("UPDATE files SET path = ? WHERE path = ?",
                                     (str(target), str(p)))
                done.append(str(p))
            except Exception as exc:
                failed.append({"path": str(p), "error": str(exc)})
        if conn:
            conn.commit()
    finally:
        if conn:
            conn.close()
    return JSONResponse({"ok": not failed, "action": action,
                         "done": len(done), "failed": failed})


@app.post("/api/spectrogram/zip")
async def spectrogram_zip(request: Request):
    """Render the selected files' spectrograms and hand back one .zip.

    Saving forty PNGs one browser download at a time is not a workflow.
    """
    import io
    import zipfile
    from spectrogram import get_or_render, dependencies_available, missing_dependencies
    if not dependencies_available():
        return JSONResponse(
            {"error": "spectrograms need: " + ", ".join(missing_dependencies())},
            status_code=503)
    body = await request.json()
    paths = [p for p in (body.get("paths") or []) if p][:200]
    if not paths:
        return JSONResponse({"error": "nothing selected"}, status_code=400)
    buf = io.BytesIO()
    made, failed = 0, []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for raw in paths:
            try:
                png = get_or_render(raw)
                if not png or not Path(png).is_file():
                    raise RuntimeError("render failed")
                name = Path(raw).stem[:110] + ".png"
                n, base = 1, name
                while name in z.namelist():
                    name = "%s (%d).png" % (base[:-4], n)
                    n += 1
                z.write(png, name)
                made += 1
            except Exception as exc:
                failed.append("%s: %s" % (Path(raw).name, exc))
        if failed:
            z.writestr("_failed.txt", "\n".join(failed))
    buf.seek(0)
    from fastapi.responses import Response
    stamp = time.strftime("%Y-%m-%d_%H%M")
    return Response(
        content=buf.getvalue(), media_type="application/zip",
        headers={"Content-Disposition":
                 'attachment; filename="spektro-%s_%d-images.zip"' % (stamp, made)})


@app.get("/api/spectrogram")
def spectrogram_api(path: str = "", force: bool = False):
    from spectrogram import get_or_render, dependencies_available, missing_dependencies
    if not dependencies_available():
        return JSONResponse(
            {"error": f"spectrogram rendering needs: {', '.join(missing_dependencies())} "
                      f"— pip install -r requirements.txt"},
            status_code=500)
    p = Path(path)
    if not p.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)
    try:
        png_path = get_or_render(p, force=force)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    from fastapi.responses import FileResponse
    return FileResponse(str(png_path), media_type="image/png")


@app.post("/api/commit")
async def commit_api():
    """Merge web_session.db into library.db (INSERT OR REPLACE)."""
    if not _SESSION_DB.exists():
        return JSONResponse({"error": "no session DB — run Import first"}, status_code=400)
    try:
        before = _db_one(_LIBRARY_DB, "SELECT COUNT(*) FROM files") or 0 \
            if _LIBRARY_DB.exists() else 0
        _LIBRARY_DB.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(_LIBRARY_DB)) as lib_conn:
            lib_conn.execute(f"ATTACH '{_SESSION_DB}' AS sess")
            # ensure library has same schema
            sess_schema = lib_conn.execute(
                "SELECT sql FROM sess.sqlite_master WHERE type='table' AND name='files'"
            ).fetchone()
            if sess_schema:
                lib_conn.execute(sess_schema[0].replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))
            lib_conn.execute("INSERT OR REPLACE INTO main.files SELECT * FROM sess.files")
            lib_conn.execute("DETACH sess")
        after = _db_one(_LIBRARY_DB, "SELECT COUNT(*) FROM files") or 0
        return JSONResponse({"ok": True, "before": before, "after": after,
                             "delta": after - before})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── job stream + control ─────────────────────────────────────────────────────
@app.get("/api/job/stream")
async def job_stream():
    async def _gen():
        while True:
            try: msg = _msg_q.get_nowait()
            except Empty:
                await asyncio.sleep(0.08)
                yield ": keepalive\n\n"
                continue
            if msg is None:
                yield f"data: {json.dumps({'type':'done'})}\n\n"
                break
            yield f"data: {json.dumps(msg)}\n\n"
    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@app.post("/api/job/{kind}")
async def start_job(kind: str, request: Request):
    global _job_thread
    valid = {"import","fetch","fetch_broken","organise","pipeline","direct",
             "direct_scan","direct_tags","direct_organise",
             "rebuild","vacuum","fake_flac_scan","fake_flac_vamp",
             "tags_from_names","label_scan","label_catalogue",
             "label_tracklists","label_prices","label_onboard",
             "label_authenticity","stop"}
    if kind not in valid:
        return JSONResponse({"error": f"unknown job: {kind}"}, status_code=400)
    if kind == "stop":
        _stop_flag.set()
        try:
            from metadata_providers import Provider as _Prov
            _Prov.CANCEL = _stop_flag
        except Exception:
            pass
        return JSONResponse({"ok": True})
    if _job_thread and _job_thread.is_alive():
        return JSONResponse({"error": "job already running"}, status_code=409)
    body = await request.json()
    while not _msg_q.empty():
        try: _msg_q.get_nowait()
        except Empty: break
    _stop_flag.clear()
    try:                       # let provider backoffs abort the moment Stop lands
        from metadata_providers import Provider as _Prov
        _Prov.CANCEL = _stop_flag
    except Exception:
        pass
    cfg = _load_cfg()
    _job_thread = threading.Thread(
        target=_run_job,
        args=(kind, body.get("sources",[]), body.get("dest",""),
              body.get("providers",["discogs","musicbrainz"]),
              cfg, bool(body.get("dry_run")),
              body.get("db","library"), bool(body.get("force")),
              body.get("role","owned")),
        daemon=True,
    )
    _job_thread.start()
    return JSONResponse({"ok": True, "kind": kind})


# A sample release, run through the REAL path builder — so the example shown
# in the UI is produced by the same code that will move the files, not by a
# hand-written string that can drift away from it.
_NAMING_SAMPLE = {
    "path": "/incoming/01 - Bjorn Akesson - Paper Dreams (Original Mix).flac",
    "artist": "Bjorn Akesson", "albumartist": "Bjorn Akesson",
    "album": "Paper Dreams", "title": "Paper Dreams (Original Mix)",
    "track_number": 1, "disc_number": 1, "year": "2015",
    "label": "Coldharbour Recordings", "catalog_number": "CLHR215",
    "genre": "Trance", "extension": ".flac",
}
_NAMING_SAMPLE_VA = {
    "path": "/incoming/04 - Chasis - Volando (Radio Edit).flac",
    "artist": "Chasis", "albumartist": "Various Artists",
    "album": "Esto es... Makina", "title": "Volando (Radio Edit)",
    "track_number": 4, "disc_number": 2, "year": "1997",
    "label": "Bit Music", "catalog_number": "12-414",
    "genre": "Makina", "extension": ".flac",
}


@app.get("/api/naming")
def naming_preview(scheme: str = ""):
    """What a file will actually be called, under each layout.

    Beatport and bpdl-web both show you the pattern with a worked example
    beside it; guessing what "artist_release_track_mix_year" produces is not
    a reasonable thing to ask of anyone.
    """
    from pathlib import Path as _P
    cfg = _load_cfg()
    current = str(((cfg or {}).get("organise") or {}).get(
        "folder_scheme") or "artist_release_track_mix_year")
    dest = ((cfg or {}).get("paths") or {}).get("destination_root") or "/Output"

    out = {"current": current, "destination": dest, "schemes": []}
    try:
        from organiser_core import build_destination_path
    except Exception as exc:
        out["error"] = "path builder unavailable: %s" % exc
        return JSONResponse(out)

    for key, label, blurb in (
        ("artist_release_track_mix_year", "One folder per TRACK",
         "Every track gets its own folder: Artist - Release - Track - Mix - Year. "
         "Empty slots are dropped, never padded. Best when you file singles and "
         "want each mix to stand on its own."),
        ("release", "One folder per RELEASE",
         "The classic album layout: (catalogue number) Title (Year), with the "
         "tracks inside it. Best when you keep albums and compilations whole."),
    ):
        override = dict(cfg or {})
        org = dict(override.get("organise") or {})
        org["folder_scheme"] = key
        override["organise"] = org
        examples = []
        for sample, note, atype in ((_NAMING_SAMPLE, "a single", "solo"),
                                    (_NAMING_SAMPLE_VA, "a compilation track", "mix")):
            try:
                pth = build_destination_path(
                    dict(sample), atype, destination_root=dest,
                    organise_cfg=org)
                shown = str(pth)
                if shown.startswith(str(dest)):
                    shown = str(_P(shown).relative_to(dest))
                examples.append({"note": note, "path": shown})
            except Exception as exc:
                examples.append({"note": note, "path": "(could not build: %s)" % exc})
        out["schemes"].append({"key": key, "label": label, "blurb": blurb,
                               "examples": examples, "active": key == current})
    return JSONResponse(out)


# The handful of settings that change what actually happens to your files.
# Deliberately small: everything here has a consequence you would notice.
_SETTABLE = {
    "folder_scheme":  ("organise", "str"),
    "import_mode":    ("import",   "str"),
    "delete_orphaned_extras": ("organise", "bool"),
}


@app.post("/api/settings")
async def settings_set(request: Request):
    """Edit config.toml in place, keeping its comments.

    A rewrite-from-parsed-values would throw away the explanations in that
    file, which are most of its value — so each key is substituted textually,
    exactly as the existing paths/providers writer does.
    """
    body = await request.json()
    key = (body.get("key") or "").strip()
    val = body.get("value")
    if key not in _SETTABLE:
        return JSONResponse({"ok": False, "reason": f"not settable: {key}"},
                            status_code=400)
    section, kind = _SETTABLE[key]
    name = "mode" if key == "import_mode" else key
    if kind == "bool":
        text = "true" if (val is True or str(val).lower() in ("1", "true", "on", "yes")) else "false"
    else:
        text = '"%s"' % str(val).replace('"', "")
    try:
        txt = _CFG_PATH.read_text(encoding="utf-8")
        pat = re.compile(r"^(\s*%s\s*=\s*).*$" % re.escape(name), re.M)
        # only inside the right [section]
        start = txt.find("[%s]" % section)
        if start == -1:
            txt = txt.rstrip() + "\n\n[%s]\n%s = %s\n" % (section, name, text)
        else:
            nxt = txt.find("\n[", start + 1)
            end = len(txt) if nxt == -1 else nxt
            block = txt[start:end]
            if pat.search(block):
                block = pat.sub(lambda m: m.group(1) + text, block, count=1)
            else:
                block = block.rstrip() + "\n%s = %s\n" % (name, text)
            txt = txt[:start] + block + txt[end:]
        tmp = str(_CFG_PATH) + ".tmp"
        Path(tmp).write_text(txt, encoding="utf-8")
        os.replace(tmp, _CFG_PATH)
    except Exception as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=500)

    cfg = _load_cfg()
    if _CFG_ERROR:                       # we just broke it — say so loudly
        return JSONResponse({"ok": False,
                             "reason": "config.toml no longer parses: " + _CFG_ERROR})
    return JSONResponse({"ok": True, "reason": "saved",
                         "value": ((cfg.get(section) or {}).get(name))})


@app.get("/api/doctor")
def doctor():
    return JSONResponse(_doctor())


# ─── health ───────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    uptime = int(time.time() - _START_TIME)
    job_running = bool(_job_thread and _job_thread.is_alive())
    sess_files = _db_one(_SESSION_DB, "SELECT COUNT(*) FROM files") if _SESSION_DB.exists() else 0
    lib_files  = _db_one(_LIBRARY_DB, "SELECT COUNT(*) FROM files") if _LIBRARY_DB.exists() else 0
    return JSONResponse({
        "status": "ok",
        "version": _VERSION,
        "uptime_seconds": uptime,
        "job_running": job_running,
        "session_files": sess_files or 0,
        "library_files": lib_files or 0,
    })


def _restart_mode() -> str:
    """How this instance can restart itself.

    "systemd" when we are actually running as the user unit (the only case
    where `systemctl --user restart` restarts US and not some other, possibly
    stopped, copy). Otherwise "respawn" — launch a fresh process and exit,
    which is what a hand-started run on Windows or macOS needs.
    """
    if os.name == "nt" or not shutil.which("systemctl"):
        return "respawn"
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "music-organiser.service"],
            capture_output=True, text=True, timeout=5,
        )
        if (r.stdout or "").strip() == "active":
            return "systemd"
    except Exception:
        pass
    return "respawn"


def _respawn_self(delay: float = 3.0) -> None:
    """Start a replacement process, then exit this one.

    The replacement can't bind the port until we've let go of it, so it is a
    tiny launcher that sleeps first and only then starts the real server —
    detached, so it survives our exit. Assumes you started the app yourself:
    under a supervisor that restarts the process (NSSM, a systemd unit we
    failed to detect) this would leave two copies fighting over the port.
    """
    argv = [sys.executable, str(Path(__file__).resolve())] + sys.argv[1:]
    launcher = ("import subprocess,sys,time;"
                "time.sleep(float(sys.argv[1]));"
                "subprocess.Popen(sys.argv[2:])")
    kwargs: dict[str, Any] = {
        "cwd": str(Path(__file__).resolve().parent),
        "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — no console, own group,
        # so closing/killing this process doesn't take the replacement with it.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-c", launcher, str(delay)] + argv, **kwargs)

    def _bye():
        time.sleep(1.0)   # let this request's response reach the browser first
        os._exit(0)
    threading.Thread(target=_bye, daemon=True).start()


@app.post("/api/restart")
async def restart_service(request: Request):
    body = await request.json() if await request.body() else {}
    force = bool(body.get("force"))
    job_running = bool(_job_thread and _job_thread.is_alive())
    if job_running and not force:
        return JSONResponse(
            {"error": "a job is currently running — stop it first, or pass force"},
            status_code=409,
        )
    mode = _restart_mode()
    log.info("restart requested via web UI (%s)%s", mode,
             " (forced, job was running)" if force and job_running else "")
    if mode == "systemd":
        # Detached, delayed so this request's response reaches the client before
        # the service (and this process) goes down. start_new_session=True keeps
        # it alive independently of this process's own group.
        subprocess.Popen(
            ["/bin/sh", "-c", "sleep 1 && systemctl --user restart music-organiser.service"],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        _respawn_self()
    return JSONResponse({"ok": True, "mode": mode})


# ─── HTML ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="music-organiser web UI")
    ap.add_argument("--host",    default="0.0.0.0")
    ap.add_argument("--port",    type=int, default=8082)
    ap.add_argument("--dev",     action="store_true", help="hot-reload mode")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    _setup_logging(args.verbose)

    # Resolve LAN IP for display
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        _lan_ip = _s.getsockname()[0]
        _s.close()
    except Exception:
        _lan_ip = "localhost"

    local_url = f"http://127.0.0.1:{args.port}"
    lan_url = f"http://{_lan_ip}:{args.port}"
    log.info("=" * 56)
    log.info(f"  music-organiser  v{_VERSION}")
    log.info(f"  open (this machine)      -> {local_url}")
    if _lan_ip != "localhost":
        log.info(f"  open (other device, LAN) -> {lan_url}")
    log.info(f"  log -> {_LOG_FILE}")
    log.info("=" * 56)

    # Clean shutdown on SIGTERM (systemd stop)
    def _on_sigterm(*_):
        log.info("SIGTERM received — shutting down")
        _stop_flag.set()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _on_sigterm)

    uvicorn.run(
        "web_ui:app" if args.dev else app,
        host=args.host,
        port=args.port,
        log_level="warning",
        reload=args.dev,
        access_log=False,
    )
