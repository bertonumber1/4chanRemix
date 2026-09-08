#!/usr/bin/env python3
"""
web_ui.py — Browser frontend for music-organiser.
Run:  python3 web_ui.py
Open: http://192.168.0.65:8082
"""
from __future__ import annotations

import asyncio, json, logging, os, re, shutil, signal, socket, sqlite3, subprocess, sys, threading, time
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
    import uvicorn
except ImportError:
    sys.exit("pip install fastapi uvicorn")

# ─── constants ─────────────────────────────────────────────────────────────────
_SESSION_DB = Path("~/.local/share/music-organiser/web_session.db").expanduser()
_LIBRARY_DB = Path("~/.local/share/music-organiser/library.db").expanduser()
_CFG_PATH   = Path("~/.config/music-organiser/config.toml").expanduser()
_LOG_FILE   = Path("~/.local/share/music-organiser/web_ui.log").expanduser()
_VERSION    = "1.4.2"

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
def _load_cfg() -> dict:
    try:
        from config import load_config
        return load_config()
    except Exception:
        return {}

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


def _do_direct(ui, sources, dest, provider_ids, cfg, dry_run):
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

    db = Database(":memory:")
    try:
        import_sources(sources, cfg=override, db=db, ui=ui, dry_run=dry_run)

        provs = []
        for pid in provider_ids:
            pr = make_provider(pid)
            if pr is None: continue
            try: pr.configure(cfg, lambda **kw: None)
            except Exception: pass
            provs.append(pr)
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

        organise_in_place(db, cfg=override, ui=ui, dry_run=dry_run)
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


# ─── job runner ───────────────────────────────────────────────────────────────
def _run_job(kind, sources, dest, provider_ids, cfg, dry_run):
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
            elif kind == "rebuild":
                _do_rebuild(ui, dest, cfg)
            elif kind == "vacuum":
                _do_vacuum(ui)
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


@app.get("/", response_class=HTMLResponse)
def root(): return HTMLResponse(_HTML)


@app.get("/wallpaper.jpg")
def wallpaper():
    from fastapi.responses import FileResponse
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "wallpaper-pirate.jpg")
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


def _pick_windows(start_dir: Path | None) -> tuple[bool, str]:
    """WinForms FolderBrowserDialog via PowerShell (Windows).

    powershell.exe needs -STA for WinForms; pwsh (7+) dropped that switch and
    is already STA, hence the two spellings. The chosen path goes to stdout on
    its own — anything PowerShell writes as a warning stays on stderr.
    """
    if not _windows_has_desktop():
        return False, "no interactive desktop (running as a service)"
    exe = shutil.which("powershell") or shutil.which("powershell.exe")
    args = ["-NoProfile", "-STA", "-NonInteractive", "-Command", "-"]
    if not exe:
        exe = shutil.which("pwsh")
        args = ["-NoProfile", "-NonInteractive", "-Command", "-"]
    if not exe:
        return False, "powershell not found"
    initial = str(start_dir) if start_dir is not None else ""
    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null\n"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog\n"
        "$d.Description = 'music-organiser - choose a folder'\n"
        "$d.ShowNewFolderButton = $true\n"
        f"$d.SelectedPath = '{initial.replace(chr(39), chr(39) * 2)}'\n"
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::Out.Write($d.SelectedPath) }\n"
    )
    proc = subprocess.run([exe] + args, input=script,
                          capture_output=True, text=True, timeout=300)
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
             "rebuild","vacuum","stop"}
    if kind not in valid:
        return JSONResponse({"error": f"unknown job: {kind}"}, status_code=400)
    if kind == "stop":
        _stop_flag.set()
        return JSONResponse({"ok": True})
    if _job_thread and _job_thread.is_alive():
        return JSONResponse({"error": "job already running"}, status_code=409)
    body = await request.json()
    while not _msg_q.empty():
        try: _msg_q.get_nowait()
        except Empty: break
    _stop_flag.clear()
    cfg = _load_cfg()
    _job_thread = threading.Thread(
        target=_run_job,
        args=(kind, body.get("sources",[]), body.get("dest",""),
              body.get("providers",["discogs","musicbrainz"]),
              cfg, bool(body.get("dry_run"))),
        daemon=True,
    )
    _job_thread.start()
    return JSONResponse({"ok": True, "kind": kind})


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
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>music-organiser</title>
<style>
:root{
  --bg:#0a0908;--panel:#14110dee;--card:#1a150e88;--border:#3a2f22;
  --acc:#d63a2f;--acc2:#e0a53a;--text:#e8ddc6;--dim:#847660;
  --ok:#4ecb71;--warn:#f0c040;--err:#ff5566;--info:#60b8ff;--dup:#c060f0;
}
*{box-sizing:border-box;margin:0;padding:0}
body{color:var(--text);background:var(--bg);
     font-family:'JetBrains Mono','Fira Code','Cascadia Code',monospace;
     font-size:13px;height:100vh;display:flex;flex-direction:column;overflow:hidden}
/* fixed pirate-flag backdrop, heavily darkened so the dense UI stays readable */
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
     background:
       radial-gradient(ellipse at top,rgba(30,22,14,.30) 0%,transparent 60%),
       linear-gradient(rgba(8,7,6,.80),rgba(6,5,4,.90)),
       url("/wallpaper.jpg") center/cover no-repeat fixed}

/* HEADER */
header{background:var(--panel);border-bottom:1px solid var(--border);
       padding:7px 16px;display:flex;align-items:center;gap:14px;flex-shrink:0}
/* street-graffiti stencil wordmark: bone fill, hard black stencil edge, red
   overspray, roughened by the #graffiti-rough turbulence filter */
header h1{font-family:"Impact","Haettenschweiler","Franklin Gothic Bold","Arial Black",sans-serif;
          font-size:25px;font-weight:900;letter-spacing:1px;text-transform:uppercase;line-height:1;
          color:#ece3d0;white-space:nowrap;-webkit-text-stroke:1.4px #0a0a0a;
          text-shadow:2px 2px 0 #0a0a0a,0 0 5px rgba(0,0,0,.9),
                      0 0 16px rgba(214,58,47,.55),0 0 32px rgba(214,58,47,.25);
          filter:url(#graffiti-rough)}
.tabs{display:flex;gap:2px;margin-left:6px}
.tab-btn{background:none;border:1px solid transparent;color:var(--dim);
         padding:3px 11px;border-radius:4px;cursor:pointer;font:inherit;font-size:11px;
         transition:all .15s}
.tab-btn:hover{color:var(--text)}
.tab-btn.active{background:#1e1e30;border-color:var(--acc);color:var(--acc)}
#hdr-status{font-size:11px;color:var(--dim);flex:1;white-space:nowrap}
.hdr-btn{background:none;border:1px solid var(--border);color:var(--text);
         padding:3px 10px;border-radius:4px;cursor:pointer;font:inherit;font-size:11px}
.hdr-btn:hover{border-color:var(--acc);color:var(--acc)}
.hdr-btn.ok{border-color:var(--ok);color:var(--ok)}
.hdr-btn.err{border-color:var(--err);color:var(--err)}

/* TAB CONTENT */
.tab-content{display:none;flex:1;overflow:hidden}
.tab-content.active{display:flex}

/* ═══ PIPELINE TAB ═══ */
#tab-pipeline{flex-direction:row}
#tab-direct{flex-direction:row}
aside{width:310px;min-width:240px;border-right:1px solid var(--border);
      display:flex;flex-direction:column;overflow:hidden;flex-shrink:0}
.card{border-bottom:1px solid var(--border);padding:10px 12px;flex-shrink:0}
.card h3{font-size:9px;text-transform:uppercase;letter-spacing:.12em;
         color:var(--dim);margin-bottom:8px}
.path-row{display:flex;gap:6px;margin-bottom:8px;align-items:center}
.path-label{font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.09em;
            min-width:54px;padding:6px 6px;border-radius:4px;text-align:center;color:#120c04}
.path-label.src{background:var(--acc2)}
.path-label.out{background:var(--acc);color:#fff}
.path-input{flex:1;background:#1c1710;border:1px solid var(--border);
            color:var(--text);padding:7px 9px;border-radius:4px;
            font:inherit;font-size:12px;min-width:0;transition:border-color .15s,box-shadow .15s}
.path-input:focus{outline:none;border-color:var(--acc2);box-shadow:0 0 0 2px rgba(224,165,58,.18)}
.path-input.active-target{border-color:var(--acc2)!important;box-shadow:0 0 0 2px rgba(224,165,58,.22)}
.btn-xs{background:var(--acc);border:none;color:#fff;padding:6px 11px;
        border-radius:4px;cursor:pointer;font:inherit;font-size:10px;font-weight:700;white-space:nowrap}
.btn-xs:hover{opacity:.82}
.btn-xs.ghost{background:#1e1e30;color:var(--dim)}
.btn-xs.ghost:hover{color:var(--text)}
.btn-xs.ghost.active{background:#2a2a48;color:var(--acc)}
.browser-target{display:flex;gap:5px;margin-bottom:6px}
.bpath{font-size:10px;color:var(--dim);padding:0 0 5px;word-break:break-all}
.browser-wrap{flex:1;overflow-y:auto}
.be{display:flex;align-items:center;gap:5px;padding:3px 12px;cursor:pointer;user-select:none}
.be:hover{background:#1a1a28}
.be .ico{font-size:10px;color:var(--acc);width:14px;text-align:center}
.be .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
.be .bbtns{display:none;gap:3px}
.be:hover .bbtns{display:flex}
.bbtn{font-size:9px;border:1px solid var(--border);background:none;color:var(--dim);
      border-radius:2px;padding:1px 5px;cursor:pointer;white-space:nowrap}
.bbtn:hover{border-color:var(--acc);color:var(--acc)}
.bbtn.dest:hover{border-color:var(--acc2);color:var(--acc2)}
.be-file .ico{color:var(--dim)}
.be-up{color:var(--dim);font-size:11px}
.be-up:hover{color:var(--text)}
.prov-row{display:flex;align-items:center;gap:6px;margin-bottom:6px}
.prov-row label{cursor:pointer;flex:1;font-size:12px}
.prov-row input[type=checkbox]{accent-color:var(--acc);cursor:pointer}
.prov-key{flex:2;background:#181824;border:1px solid var(--border);color:var(--text);
          padding:3px 7px;border-radius:3px;font:inherit;font-size:11px}
.prov-key:focus{outline:none;border-color:var(--acc)}
.prov-key::placeholder{color:var(--dim)}
.key-eye{background:none;border:none;color:var(--dim);cursor:pointer;padding:0 3px;font-size:12px}
.key-eye:hover{color:var(--text)}
.badge{font-size:9px;padding:1px 5px;border-radius:10px}
.badge.set{background:#004020;color:var(--ok)}
.badge.unset{background:#302000;color:var(--warn)}
.log-area{flex:1;display:flex;flex-direction:column;overflow:hidden}
.pipeline{padding:10px 14px 0;display:flex;align-items:center;gap:6px;flex-shrink:0;flex-wrap:wrap}
.phase{display:flex;align-items:center;gap:6px;padding:5px 12px;
       border:1px solid var(--border);border-radius:5px;font-size:11px;
       color:var(--dim);transition:all .2s}
.phase .dot{width:7px;height:7px;border-radius:50%;background:var(--dim)}
.phase.running{border-color:var(--acc);color:var(--acc)}
.phase.running .dot{background:var(--acc);animation:pulse 1s infinite}
.phase.done{border-color:var(--ok);color:var(--ok)}
.phase.done .dot{background:var(--ok)}
.phase.stopped{border-color:var(--warn);color:var(--warn)}
.phase.stopped .dot{background:var(--warn)}
.phase-arrow{color:var(--dim);font-size:10px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.action-bar{padding:8px 14px;border-bottom:1px solid var(--border);
            display:flex;align-items:center;gap:6px;flex-wrap:wrap;flex-shrink:0;margin-top:8px}
.btn{background:var(--acc);border:none;color:#fff;padding:6px 14px;border-radius:4px;
     cursor:pointer;font:inherit;font-size:12px;font-weight:600}
.btn:hover:not(:disabled){opacity:.82}
.btn:disabled{opacity:.35;cursor:not-allowed}
.btn.run-all{background:linear-gradient(90deg,var(--acc),var(--acc2))}
.btn.ghost{background:#1e1e30;color:var(--text);font-weight:400}
.btn.danger{background:var(--err)}
.btn.ok{background:#1a3a22;border:1px solid var(--ok);color:var(--ok)}
.btn.warn{background:#2a2000;border:1px solid var(--warn);color:var(--warn)}
.dry-label{display:flex;align-items:center;gap:5px;cursor:pointer;
           font-size:11px;color:var(--warn);margin-left:4px;user-select:none}
.dry-label input{accent-color:var(--warn);cursor:pointer}
.progress-bar{height:2px;background:var(--border);flex-shrink:0}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .25s}
.log{flex:1;overflow-y:auto;padding:7px 14px;font-size:12px;line-height:1.65}
.ll{display:flex;gap:8px}
.ll .ts{color:var(--dim);min-width:56px}
.ll .lv{min-width:60px;font-size:10px;text-align:right;opacity:.7}
.ll .tx{word-break:break-all;flex:1}
.ll.info .lv,.ll.info .tx{color:var(--info)}
.ll.imported .lv,.ll.imported .tx{color:var(--ok)}
.ll.warning .lv,.ll.warning .tx{color:var(--warn)}
.ll.broken .lv,.ll.broken .tx{color:var(--err)}
.ll.duplicate .lv,.ll.duplicate .tx{color:var(--dup)}
.ll.debug .lv,.ll.debug .tx{color:var(--dim)}
.ll.phase-hdr .tx{color:var(--acc2);font-weight:600;letter-spacing:.05em}
.stat-bar{padding:4px 14px;border-top:1px solid var(--border);
          font-size:11px;color:var(--dim);display:flex;gap:14px;flex-shrink:0}

/* ═══ SHARED TABLE STYLES ═══ */
.page-panel{flex:1;display:flex;flex-direction:column;overflow:hidden}
.toolbar{padding:8px 14px;border-bottom:1px solid var(--border);
         display:flex;align-items:center;gap:8px;flex-shrink:0;flex-wrap:wrap}
.search-input{background:#181824;border:1px solid var(--border);color:var(--text);
              padding:5px 10px;border-radius:3px;font:inherit;font-size:12px;width:220px}
.search-input:focus{outline:none;border-color:var(--acc)}
.filter-chips{display:flex;gap:4px}
.chip{background:none;border:1px solid var(--border);color:var(--dim);
      padding:3px 10px;border-radius:12px;cursor:pointer;font:inherit;font-size:11px}
.chip:hover{border-color:var(--text);color:var(--text)}
.chip.active{background:#1e1e30;border-color:var(--acc);color:var(--acc)}
.stats-ribbon{padding:6px 14px;background:#0e0e1a;border-bottom:1px solid var(--border);
              display:flex;gap:16px;flex-shrink:0;font-size:11px;flex-wrap:wrap}
.stat-pill{display:flex;gap:5px;align-items:center}
.stat-pill .val{color:var(--text);font-weight:600}
.stat-pill .lbl{color:var(--dim)}
.stat-pill.imp .val{color:var(--ok)}
.stat-pill.brk .val{color:var(--err)}
.stat-pill.dup .val{color:var(--dup)}
.tbl-wrap{flex:1;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th{position:sticky;top:0;background:#0e0e1a;border-bottom:1px solid var(--border);
   padding:6px 10px;text-align:left;font-size:10px;text-transform:uppercase;
   letter-spacing:.08em;color:var(--dim);white-space:nowrap;cursor:pointer;user-select:none}
th:hover{color:var(--text)}
td{padding:5px 10px;border-bottom:1px solid #1a1a26;white-space:nowrap;
   overflow:hidden;text-overflow:ellipsis;max-width:260px}
tr:hover td{background:#16162200}
tr:hover{background:#161622}
.status-badge{font-size:10px;padding:1px 7px;border-radius:10px;font-weight:600}
.status-badge.imported{background:#003818;color:var(--ok)}
.status-badge.broken{background:#2a0a0a;color:var(--err)}
.status-badge.duplicate{background:#1a0a2a;color:var(--dup)}
.status-badge.indexed{background:#0a1a2a;color:var(--info)}
.status-badge.unknown{background:#1a1a1a;color:var(--dim)}
.pagination{padding:6px 14px;border-top:1px solid var(--border);
            display:flex;align-items:center;gap:8px;flex-shrink:0;font-size:11px}
.pagination span{color:var(--dim)}

/* ═══ SESSION TAB ═══ */
#tab-session{flex-direction:column}

/* ═══ LIBRARY TAB ═══ */
#tab-library{flex-direction:column}

/* ═══ TOOLS TAB ═══ */
#tab-tools{flex-direction:row;overflow:hidden}
.tools-left{width:300px;min-width:220px;border-right:1px solid var(--border);
            display:flex;flex-direction:column;overflow-y:auto;flex-shrink:0}
.tool-card{padding:12px 14px;border-bottom:1px solid var(--border)}
.tool-card h3{font-size:9px;text-transform:uppercase;letter-spacing:.12em;
              color:var(--dim);margin-bottom:10px}
.tool-btn{width:100%;background:#1e1e30;border:1px solid var(--border);
          color:var(--text);padding:8px 12px;border-radius:4px;cursor:pointer;
          font:inherit;font-size:12px;text-align:left;margin-bottom:6px;
          display:flex;align-items:center;gap:8px;transition:all .15s}
.tool-btn:hover{border-color:var(--acc);color:var(--acc)}
.tool-btn .tb-icon{font-size:14px;width:18px;text-align:center}
.tool-btn .tb-text{flex:1}
.tool-btn .tb-hint{font-size:10px;color:var(--dim)}
.tool-btn:hover .tb-hint{color:var(--acc)}
.tool-btn.danger:hover{border-color:var(--err);color:var(--err)}
.tool-btn.commit-btn:hover{border-color:var(--ok);color:var(--ok)}
.tools-right{flex:1;display:flex;flex-direction:column;overflow:hidden}
.sql-area{flex:1;display:flex;flex-direction:column;padding:14px;gap:10px;overflow:hidden}
.sql-area h3{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--dim)}
.sql-header{display:flex;align-items:center;gap:8px}
.sql-db-sel{background:#181824;border:1px solid var(--border);color:var(--text);
            padding:4px 8px;border-radius:3px;font:inherit;font-size:11px}
textarea.sql-input{flex:0 0 100px;background:#181824;border:1px solid var(--border);
                   color:var(--text);padding:8px;border-radius:3px;
                   font:'JetBrains Mono',monospace;font-size:12px;resize:vertical}
textarea.sql-input:focus{outline:none;border-color:var(--acc)}
.sql-results{flex:1;overflow:auto;border:1px solid var(--border);border-radius:3px}
.sql-results table td,.sql-results table th{max-width:300px}
.audit-panel{border-top:1px solid var(--border);flex-shrink:0;max-height:280px;overflow-y:auto}
.audit-row{display:flex;align-items:center;gap:8px;padding:5px 14px;
           border-bottom:1px solid #1a1a26;font-size:11px}
.audit-row .ac{min-width:36px;text-align:right;font-weight:600;color:var(--warn)}
.audit-row .ac.ok{color:var(--ok)}
.audit-row .al{flex:1;color:var(--dim)}
.audit-row.has-issues .al{color:var(--text)}

/* ═══ DETAIL POPUP ═══ */
.popup-overlay{position:fixed;inset:0;background:#00000088;z-index:100;
               display:flex;align-items:center;justify-content:center}
.popup-overlay.hidden{display:none}
.popup{background:var(--panel);border:1px solid var(--border);border-radius:6px;
       width:700px;max-width:95vw;max-height:80vh;display:flex;flex-direction:column}
.popup-hdr{padding:12px 16px;border-bottom:1px solid var(--border);
           display:flex;align-items:center;gap:8px}
.popup-hdr h2{flex:1;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.popup-close{background:none;border:none;color:var(--dim);cursor:pointer;font-size:18px;
             line-height:1;padding:0 4px}
.popup-close:hover{color:var(--text)}
.popup-body{overflow-y:auto;padding:12px 16px}
.meta-grid{display:grid;grid-template-columns:140px 1fr;gap:4px 12px;font-size:12px}
.meta-grid .mk{color:var(--dim);text-align:right;padding:2px 0}
.meta-grid .mv{color:var(--text);word-break:break-all;padding:2px 0}
.meta-grid .mv.ok{color:var(--ok)}
.meta-grid .mv.err{color:var(--err)}
.meta-grid .mv.warn{color:var(--warn)}
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <filter id="graffiti-rough">
    <feTurbulence type="fractalNoise" baseFrequency="0.02 0.03" numOctaves="2" seed="7" result="n"/>
    <feDisplacementMap in="SourceGraphic" in2="n" scale="3" xChannelSelector="R" yChannelSelector="G"/>
  </filter>
</svg>
<header>
  <h1 title="music-organiser">Smash-n-Grab</h1>
  <nav class="tabs">
    <button class="tab-btn active" onclick="switchTab('pipeline')">Pipeline</button>
    <button class="tab-btn" onclick="switchTab('direct')">Direct</button>
    <button class="tab-btn" onclick="switchTab('session')">Session</button>
    <button class="tab-btn" onclick="switchTab('library')">Library</button>
    <button class="tab-btn" onclick="switchTab('tools')">Tools</button>
  </nav>
  <span id="hdr-status">idle</span>
  <button class="hdr-btn" id="save-btn" onclick="saveConfig()">Save config</button>
  <button class="hdr-btn" id="restart-btn" onclick="restartService()">Restart service</button>
</header>

<!-- ═══════════════ PIPELINE TAB ═══════════════ -->
<div id="tab-pipeline" class="tab-content active">
<aside>
  <div class="card">
    <h3>Input / Output</h3>
    <div class="path-row">
      <span class="path-label src">Source</span>
      <input class="path-input" id="src-in" placeholder="folder to read from" oninput="setActiveTarget('src')">
      <button class="btn-xs" onclick="nativePick('src',this)">📁 Browse</button>
    </div>
    <div class="path-row">
      <span class="path-label out">Output</span>
      <input class="path-input" id="dest-in" placeholder="folder to organise into" oninput="setActiveTarget('dest')">
      <button class="btn-xs" onclick="nativePick('dest',this)">📁 Browse</button>
    </div>
    <div style="display:flex;gap:5px;margin-top:4px">
      <button class="btn-xs" style="flex:1" onclick="scanSource()">Scan source</button>
      <span id="scan-result" style="font-size:10px;color:var(--dim);line-height:22px"></span>
    </div>
  </div>
  <div class="card">
    <h3>Providers &amp; API keys</h3>
    <div id="prov-list"></div>
  </div>
  <div class="card" style="padding-bottom:5px;flex-shrink:0">
    <h3>Filesystem browser</h3>
    <div class="browser-target">
      <button class="btn-xs ghost active" id="bt-src" onclick="activateBrowser('src')">▸ Source</button>
      <button class="btn-xs ghost" id="bt-dest" onclick="activateBrowser('dest')">▸ Output</button>
    </div>
    <div class="bpath" id="b-path">/</div>
  </div>
  <div class="browser-wrap" id="browser"></div>
</aside>

<div class="log-area">
  <div class="pipeline">
    <div class="phase" id="ph-import"><span class="dot"></span>Import</div>
    <span class="phase-arrow">→</span>
    <div class="phase" id="ph-fetch"><span class="dot"></span>Fetch Tags</div>
    <span class="phase-arrow">→</span>
    <div class="phase" id="ph-organise"><span class="dot"></span>Organise</div>
  </div>
  <div class="action-bar">
    <button class="btn run-all" id="btn-all"      onclick="run('pipeline')">▶ Run All</button>
    <button class="btn ghost"   id="btn-import"   onclick="run('import')">Import</button>
    <button class="btn ghost"   id="btn-fetch"    onclick="run('fetch')">Fetch Tags</button>
    <button class="btn ghost"   id="btn-organise" onclick="run('organise')">Organise</button>
    <button class="btn ghost"   onclick="clearLog()">Clear log</button>
    <button class="btn danger"  id="btn-stop" onclick="stopJob()" hidden>■ Stop</button>
    <label class="dry-label"><input type="checkbox" id="dry-run"> dry run</label>
  </div>
  <div class="progress-bar"><div class="progress-fill" id="prog" style="width:0%"></div></div>
  <div class="log" id="log"></div>
  <div class="stat-bar">
    <span id="s-imp">imported: —</span>
    <span id="s-dup">duplicate: —</span>
    <span id="s-bad">broken: —</span>
    <span id="s-prog">0 / 0</span>
    <span id="s-el">elapsed: —</span>
  </div>
</div>
</div><!-- /pipeline -->

<!-- ═══════════════ DIRECT TAB ═══════════════ -->
<div id="tab-direct" class="tab-content">
<aside>
  <div class="card">
    <h3>Direct mode — input / output, no database</h3>
    <div class="path-row">
      <span class="path-label src">Source</span>
      <input class="path-input" id="direct-src-in" placeholder="folder to read from" oninput="setActiveTargetDirect('src')">
      <button class="btn-xs" onclick="nativePickDirect('src',this)">📁 Browse</button>
    </div>
    <div class="path-row">
      <span class="path-label out">Output</span>
      <input class="path-input" id="direct-dest-in" placeholder="folder to organise into" oninput="setActiveTargetDirect('dest')">
      <button class="btn-xs" onclick="nativePickDirect('dest',this)">📁 Browse</button>
    </div>
    <div style="display:flex;gap:5px;margin-top:4px">
      <button class="btn-xs" style="flex:1" onclick="scanSourceDirect()">Scan source</button>
      <span id="direct-scan-result" style="font-size:10px;color:var(--dim);line-height:22px"></span>
    </div>
    <p style="font-size:10px;color:var(--dim);margin-top:8px;line-height:1.4">
      Fix + organise straight to disk — no session/library DB involved.
      One shot: reads tags, looks up Discogs/MusicBrainz/etc for whatever's
      missing, writes tags back to the files, then moves everything into
      <code>(catalog#) Artist - Album (Year)/NN - Title.ext</code>.
    </p>
  </div>
  <div class="card">
    <h3>Providers &amp; API keys</h3>
    <div id="direct-prov-list"></div>
  </div>
  <div class="card" style="padding-bottom:5px;flex-shrink:0">
    <h3>Filesystem browser</h3>
    <div class="browser-target">
      <button class="btn-xs ghost active" id="direct-bt-src" onclick="activateBrowserDirect('src')">▸ Source</button>
      <button class="btn-xs ghost" id="direct-bt-dest" onclick="activateBrowserDirect('dest')">▸ Output</button>
    </div>
    <div class="bpath" id="direct-b-path">/</div>
  </div>
  <div class="browser-wrap" id="direct-browser"></div>
</aside>

<div class="log-area">
  <div class="action-bar">
    <button class="btn run-all" id="direct-btn-all" onclick="runDirect()">▶ Run All</button>
    <button class="btn ghost"   onclick="clearLogDirect()">Clear log</button>
    <button class="btn danger"  id="direct-btn-stop" onclick="stopJob()" hidden>■ Stop</button>
    <label class="dry-label"><input type="checkbox" id="direct-dry-run"> dry run</label>
  </div>
  <div class="progress-bar"><div class="progress-fill" id="direct-prog" style="width:0%"></div></div>
  <div class="log" id="direct-log"></div>
  <div class="stat-bar">
    <span id="direct-s-imp">imported: —</span>
    <span id="direct-s-dup">duplicate: —</span>
    <span id="direct-s-bad">broken: —</span>
    <span id="direct-s-prog">0 / 0</span>
    <span id="direct-s-el">elapsed: —</span>
  </div>
</div>
</div><!-- /direct -->

<!-- ═══════════════ SESSION TAB ═══════════════ -->
<div id="tab-session" class="tab-content">
<div class="page-panel">
  <div class="stats-ribbon" id="sess-ribbon">
    <span class="stat-pill"><span class="val" id="ss-total">—</span><span class="lbl">total</span></span>
    <span class="stat-pill imp"><span class="val" id="ss-imp">—</span><span class="lbl">imported</span></span>
    <span class="stat-pill brk"><span class="val" id="ss-brk">—</span><span class="lbl">broken</span></span>
    <span class="stat-pill dup"><span class="val" id="ss-dup">—</span><span class="lbl">duplicate</span></span>
  </div>
  <div class="toolbar">
    <input class="search-input" id="sess-search" placeholder="filter artist / album / title…" oninput="filterSession()">
    <div class="filter-chips">
      <button class="chip active" onclick="setSessFilter('all',this)">All</button>
      <button class="chip" onclick="setSessFilter('imported',this)">Imported</button>
      <button class="chip" onclick="setSessFilter('broken',this)">Broken</button>
      <button class="chip" onclick="setSessFilter('duplicate',this)">Duplicate</button>
    </div>
    <button class="btn ghost" onclick="loadSession()" style="margin-left:auto">↺ Refresh</button>
    <button class="btn warn"  id="btn-refetch" onclick="refetchBroken()" hidden>↺ Re-fetch broken</button>
    <button class="btn ok"    id="btn-commit"  onclick="commitSession()">↑ Commit to library</button>
  </div>
  <div class="tbl-wrap">
    <table id="sess-table">
      <thead><tr>
        <th onclick="sortSess('filename')">File</th>
        <th onclick="sortSess('artist')">Artist</th>
        <th onclick="sortSess('album')">Album</th>
        <th onclick="sortSess('year')">Year</th>
        <th onclick="sortSess('label')">Label</th>
        <th onclick="sortSess('catalog_number')">Cat#</th>
        <th onclick="sortSess('dur')">Dur</th>
        <th onclick="sortSess('status')">Status</th>
      </tr></thead>
      <tbody id="sess-tbody"></tbody>
    </table>
  </div>
</div>
</div><!-- /session -->

<!-- ═══════════════ LIBRARY TAB ═══════════════ -->
<div id="tab-library" class="tab-content">
<div class="page-panel">
  <div class="stats-ribbon" id="lib-ribbon">
    <span class="stat-pill"><span class="val" id="ls-total">—</span><span class="lbl">files</span></span>
    <span class="stat-pill"><span class="val" id="ls-artists">—</span><span class="lbl">artists</span></span>
    <span class="stat-pill"><span class="val" id="ls-labels">—</span><span class="lbl">labels</span></span>
    <span class="stat-pill"><span class="val" id="ls-size">—</span><span class="lbl">GB</span></span>
  </div>
  <div class="toolbar">
    <input class="search-input" id="lib-search" placeholder="search artist / album / title / label…"
           oninput="debounceLibSearch()" style="width:300px">
    <div class="filter-chips">
      <button class="chip active" onclick="setLibFilter('',this)">All</button>
      <button class="chip" onclick="setLibFilter('imported',this)">Imported</button>
      <button class="chip" onclick="setLibFilter('indexed',this)">Indexed</button>
      <button class="chip" onclick="setLibFilter('broken',this)">Broken</button>
    </div>
    <button class="btn ghost" onclick="loadLibrary(0)" style="margin-left:auto">↺ Refresh</button>
  </div>
  <div class="tbl-wrap">
    <table id="lib-table">
      <thead><tr>
        <th>Artist</th>
        <th>Album</th>
        <th>Title</th>
        <th>Year</th>
        <th>Label</th>
        <th>Cat#</th>
        <th>Status</th>
      </tr></thead>
      <tbody id="lib-tbody"></tbody>
    </table>
  </div>
  <div class="pagination">
    <button class="btn-xs ghost" onclick="libPage(-1)">← Prev</button>
    <span id="lib-page-info">page 1 of 1</span>
    <button class="btn-xs ghost" onclick="libPage(1)">Next →</button>
    <span id="lib-count" style="margin-left:auto;color:var(--dim)"></span>
  </div>
</div>
</div><!-- /library -->

<!-- ═══════════════ TOOLS TAB ═══════════════ -->
<div id="tab-tools" class="tab-content">
<div class="tools-left">

  <div class="tool-card">
    <h3>Database</h3>
    <button class="tool-btn" onclick="runTool('rebuild')">
      <span class="tb-icon">⟳</span>
      <span class="tb-text">Rebuild index<br><span class="tb-hint">re-scan output → library.db</span></span>
    </button>
    <button class="tool-btn" onclick="runTool('vacuum')">
      <span class="tb-icon">◎</span>
      <span class="tb-text">Compact DB<br><span class="tb-hint">VACUUM + ANALYZE library.db</span></span>
    </button>
    <button class="tool-btn commit-btn" onclick="commitSession()">
      <span class="tb-icon">↑</span>
      <span class="tb-text">Commit session → library<br><span class="tb-hint">merge session DB into library.db</span></span>
    </button>
  </div>

  <div class="tool-card">
    <h3>Fix &amp; Rescue</h3>
    <button class="tool-btn" onclick="runTool('fetch_broken')">
      <span class="tb-icon">↺</span>
      <span class="tb-text">Re-fetch broken<br><span class="tb-hint">retry ALL tags for session files</span></span>
    </button>
    <button class="tool-btn" onclick="run('organise')">
      <span class="tb-icon">⇄</span>
      <span class="tb-text">Re-organise session<br><span class="tb-hint">move files to updated paths</span></span>
    </button>
  </div>

  <div class="tool-card">
    <h3>Audit library.db</h3>
    <button class="tool-btn" onclick="runAudits()">
      <span class="tb-icon">✓</span>
      <span class="tb-text">Run all checks<br><span class="tb-hint">13 quality checks on library.db</span></span>
    </button>
    <div id="audit-results" style="margin-top:4px"></div>
  </div>

</div><!-- /tools-left -->

<div class="tools-right">
  <div class="sql-area">
    <div class="sql-header">
      <h3>SQL Query</h3>
      <select class="sql-db-sel" id="sql-db">
        <option value="library">library.db</option>
        <option value="session">session.db</option>
      </select>
      <button class="btn-xs" onclick="runSQL()">▶ Run</button>
      <button class="btn-xs ghost" onclick="document.getElementById('sql-input').value='SELECT artist, album, year, label, status FROM files ORDER BY artist LIMIT 50'">example</button>
      <span id="sql-status" style="font-size:11px;color:var(--dim);margin-left:auto"></span>
    </div>
    <textarea class="sql-input" id="sql-input" rows="5"
      placeholder="SELECT artist, album, year, label, status FROM files ORDER BY artist LIMIT 50"></textarea>
    <div class="sql-results tbl-wrap" id="sql-results">
      <div style="padding:20px;color:var(--dim);font-size:12px">Run a SELECT query above to see results here.</div>
    </div>
  </div>
</div><!-- /tools-right -->

</div><!-- /tools -->

<!-- ═══════════════ DETAIL POPUP ═══════════════ -->
<div class="popup-overlay hidden" id="detail-overlay" onclick="closeDetail(event)">
  <div class="popup">
    <div class="popup-hdr">
      <h2 id="detail-title">File detail</h2>
      <button class="popup-close" onclick="closeDetailBtn()">✕</button>
    </div>
    <div class="popup-body" id="detail-body"></div>
  </div>
</div>

<!-- ═══════════════ TOOL LOG OVERLAY ═══════════════ -->
<div class="popup-overlay hidden" id="tool-log-overlay" onclick="closeToolLog(event)">
  <div class="popup" style="width:800px;max-height:70vh">
    <div class="popup-hdr">
      <h2 id="tool-log-title">Running…</h2>
      <button class="popup-close" onclick="closeToolLogBtn()">✕</button>
    </div>
    <div class="popup-body" style="padding:0">
      <div class="log" id="tool-log" style="padding:10px 14px;min-height:200px;max-height:55vh;overflow-y:auto"></div>
    </div>
  </div>
</div>

<script>
"use strict";

// ── state ─────────────────────────────────────────────────────────────────────
let provs=[], es=null, jobStart=0;
let sessData=[], sessFilt='all', sessSort='status', sessSortAsc=true;
let libPage_=0, libStatus_='', libQ_='', libDebounce=null;
let toolLogES=null;

// ── tab switching ─────────────────────────────────────────────────────────────
function switchTab(name){
  document.querySelectorAll('.tab-content').forEach(el=>el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el=>el.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  const btns=[...document.querySelectorAll('.tab-btn')];
  const labels={pipeline:'Pipeline',direct:'Direct',session:'Session',library:'Library',tools:'Tools'};
  btns.forEach(b=>{ if(b.textContent===labels[name]) b.classList.add('active'); });
  if(name==='session') loadSession();
  if(name==='library'){ loadLibraryStats(); loadLibrary(0); }
  if(name==='tools'){ loadLibraryStats(); }
}

// ── init ─────────────────────────────────────────────────────────────────────
async function init(){
  const r=await fetch('/api/config'); const c=await r.json();
  document.getElementById('src-in').value  = (c.sources||[])[0]||'';
  document.getElementById('dest-in').value = c.destination_root||'';
  document.getElementById('direct-src-in').value  = (c.sources||[])[0]||'';
  document.getElementById('direct-dest-in').value = c.destination_root||'';
  provs = c.providers||[];
  renderProviders();
  renderProvidersDirect();
  browse(c.browse_root||'/');
  browseDirect(c.browse_root||'/');
}

// ── providers ─────────────────────────────────────────────────────────────────
function renderProviders(){
  document.getElementById('prov-list').innerHTML = provs.map(p=>{
    const badge = p.requires_auth
      ? (p.has_key ? `<span class="badge set" title="${p.key_hint}">key ✓</span>`
                   : `<span class="badge unset">no key</span>`) : '';
    const keyInput = p.key_field ? `
      <input class="prov-key" id="key-${p.id}" type="password"
             placeholder="${p.has_key ? '(keep existing)' : 'paste key…'}">
      <button class="key-eye" onclick="toggleKey('${p.id}')">👁</button>` : '';
    return `<div class="prov-row">
      <input type="checkbox" id="p-${p.id}" ${p.enabled?'checked':''}
             onchange="setProv('${p.id}',this.checked)">
      <label for="p-${p.id}">${p.name||p.id}</label>
      ${badge}${keyInput}
    </div>`;
  }).join('');
}
function toggleKey(id){ const el=document.getElementById('key-'+id); if(el) el.type=el.type==='password'?'text':'password'; }
function setProv(id,on){
  const p=provs.find(x=>x.id===id); if(p) p.enabled=on;
  const dp=document.getElementById('dp-'+id); if(dp) dp.checked=on;
  const pp=document.getElementById('p-'+id); if(pp) pp.checked=on;
}
function renderProvidersDirect(){
  document.getElementById('direct-prov-list').innerHTML = provs.map(p=>`
    <div class="prov-row">
      <input type="checkbox" id="dp-${p.id}" ${p.enabled?'checked':''}
             onchange="setProv('${p.id}',this.checked)">
      <label for="dp-${p.id}">${p.name||p.id}</label>
      ${p.requires_auth ? (p.has_key ? `<span class="badge set">key ✓</span>` : `<span class="badge unset">no key</span>`) : ''}
    </div>`).join('');
}

// ── save config ───────────────────────────────────────────────────────────────
async function saveConfig(){
  const src=document.getElementById('src-in').value.trim();
  const dest=document.getElementById('dest-in').value.trim();
  const providerUpdates={};
  for(const p of provs){
    if(!p.key_field) continue;
    const val=(document.getElementById('key-'+p.id)?.value||'').trim();
    if(val) providerUpdates[p.id]={[p.key_field]:val};
  }
  const btn=document.getElementById('save-btn');
  btn.textContent='Saving…';
  const r=await fetch('/api/config/save',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({paths:{sources:src?[src]:[],destination_root:dest},providers:providerUpdates})});
  const j=await r.json();
  if(j.ok){
    btn.textContent='Saved ✓'; btn.className='hdr-btn ok';
    setTimeout(()=>{btn.textContent='Save config';btn.className='hdr-btn';},2500);
    const cr=await fetch('/api/config'); const cc=await cr.json();
    provs=cc.providers||[]; renderProviders();
    provs.forEach(p=>{ const el=document.getElementById('key-'+p.id); if(el) el.value=''; });
  } else {
    btn.textContent='Error'; btn.className='hdr-btn err';
    setTimeout(()=>{btn.textContent='Save config';btn.className='hdr-btn';},3000);
    appendLog('broken','config save: '+(j.error||'unknown'));
  }
}

async function restartService(force){
  const btn=document.getElementById('restart-btn');
  if(!force && !confirm('Restart the music-organiser service now?'+
      ' This drops your connection for a few seconds while it comes back up.')) return;
  btn.textContent='Restarting…'; btn.disabled=true;
  const r=await fetch('/api/restart',{method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({force:!!force})});
  if(r.status===409){
    btn.textContent='Restart service'; btn.disabled=false;
    if(confirm('A job is currently running — restarting will interrupt it. Restart anyway?'))
      return restartService(true);
    return;
  }
  const j=await r.json().catch(()=>({}));
  if(!r.ok || !j.ok){
    btn.textContent='Error'; btn.className='hdr-btn err';
    appendLog('broken','restart failed: '+(j.error||r.status));
    setTimeout(()=>{btn.textContent='Restart service';btn.className='hdr-btn';btn.disabled=false;},3000);
    return;
  }
  // Service is about to go down — poll /api/health until it's back, then reload the page.
  let tries=0;
  const poll=setInterval(async ()=>{
    tries++;
    try{
      const hr=await fetch('/api/health',{cache:'no-store'});
      if(hr.ok){ clearInterval(poll); location.reload(); return; }
    }catch(e){ /* still down, keep polling */ }
    btn.textContent='Restarting… ('+tries+'s)';
    if(tries>60){ clearInterval(poll); btn.textContent='Still down?'; btn.className='hdr-btn err'; }
  },1000);
}

// ── filesystem browser ────────────────────────────────────────────────────────
let browserTarget_='src';
function activateBrowser(t){
  browserTarget_=t;
  document.getElementById('src-in').classList.toggle('active-target',t==='src');
  document.getElementById('dest-in').classList.toggle('active-target',t==='dest');
  document.getElementById('bt-src').classList.toggle('active',t==='src');
  document.getElementById('bt-dest').classList.toggle('active',t==='dest');
}
function setActiveTarget(t){ browserTarget_=t; activateBrowser(t); }
async function browse(path){
  document.getElementById('b-path').textContent=path;
  const r=await fetch('/api/browse?path='+encodeURIComponent(path));
  const d=await r.json();
  let html='';
  if(d.parent)
    html+=`<div class="be be-up" onclick="browse(${J(d.parent)})"><span class="ico">▲</span><span class="nm">..</span></div>`;
  for(const e of d.entries){
    if(e.is_dir){
      html+=`<div class="be"><span class="ico">▶</span>
        <span class="nm" onclick="browse(${J(e.path)})">${e.name}</span>
        <span class="bbtns">
          <button class="bbtn" onclick="pickFolder(${J(e.path)},'src')">src</button>
          <button class="bbtn dest" onclick="pickFolder(${J(e.path)},'dest')">dest</button>
        </span></div>`;
    } else {
      html+=`<div class="be be-file"><span class="ico">·</span><span class="nm">${e.name}</span></div>`;
    }
  }
  document.getElementById('browser').innerHTML=html||'<div style="padding:8px 12px;color:var(--dim);font-size:11px">(empty)</div>';
}
function pickFolder(path,target){
  document.getElementById(target==='src'?'src-in':'dest-in').value=path;
  activateBrowser(target);
  if(target==='src') scanSource();
}
// ── native folder picker ──────────────────────────────────────────────────────
// Opens a real GTK folder chooser on this machine's own desktop. If there's no
// display to draw on (you're on another machine), fall back to the in-page tree.
async function nativePick(target,btn){
  const inp=document.getElementById(target==='src'?'src-in':'dest-in');
  if(btn) btn.disabled=true;
  try{
    const r=await fetch('/api/pick-folder?start='+encodeURIComponent(inp.value||''));
    const d=await r.json();
    if(d.ok&&d.path){
      inp.value=d.path; activateBrowser(target);
      if(target==='src') scanSource();
    } else if(d.reason!=='cancelled'){
      activateBrowser(target); browse(inp.value||'/');
    }
  }catch(e){ activateBrowser(target); }
  finally{ if(btn) btn.disabled=false; }
}
async function nativePickDirect(target,btn){
  const inp=document.getElementById(target==='src'?'direct-src-in':'direct-dest-in');
  if(btn) btn.disabled=true;
  try{
    const r=await fetch('/api/pick-folder?start='+encodeURIComponent(inp.value||''));
    const d=await r.json();
    if(d.ok&&d.path){
      inp.value=d.path; activateBrowserDirect(target);
      if(target==='src') scanSourceDirect();
    } else if(d.reason!=='cancelled'){
      activateBrowserDirect(target); browseDirect(inp.value||'/');
    }
  }catch(e){ activateBrowserDirect(target); }
  finally{ if(btn) btn.disabled=false; }
}
async function scanSource(){
  const p=document.getElementById('src-in').value.trim(); if(!p)return;
  document.getElementById('scan-result').textContent='scanning…';
  const r=await fetch('/api/scan?path='+encodeURIComponent(p));
  const d=await r.json();
  document.getElementById('scan-result').textContent=
    d.error?d.error:d.count.toLocaleString()+' files';
}
function J(s){ return JSON.stringify(s); }

// ── pipeline jobs ─────────────────────────────────────────────────────────────
function getJobBody(){
  return {
    sources:[document.getElementById('src-in').value.trim()].filter(Boolean),
    dest:document.getElementById('dest-in').value.trim(),
    providers:provs.filter(p=>p.enabled).map(p=>p.id),
    dry_run:document.getElementById('dry-run').checked,
  };
}
async function run(kind){
  const body=getJobBody();
  if(body.dry_run) appendLog('warning','DRY RUN — '+kind+': nothing will be written');
  const r=await fetch('/api/job/'+kind,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();
  if(j.error){ appendLog('broken',j.error); return; }
  setRunning(true,kind); jobStart=Date.now();
  if(es) es.close();
  es=new EventSource('/api/job/stream');
  es.onmessage=e=>{
    const m=JSON.parse(e.data);
    if(m.type==='log')      appendLog(m.level,m.text);
    else if(m.type==='progress') onProgress(m);
    else if(m.type==='phase')    onPhase(m);
    else if(m.type==='done'){
      setRunning(false,kind); es.close(); es=null;
      if(kind==='import'||kind==='pipeline'||kind==='fetch'||kind==='organise')
        setTimeout(loadSession,500);
    }
  };
  es.onerror=()=>{ setRunning(false,kind); if(es){es.close();es=null;} };
}
async function stopJob(){ await fetch('/api/job/stop',{method:'POST'}); }

const ACTION_BTNS=['btn-all','btn-import','btn-fetch','btn-organise'];
function setRunning(on,kind){
  ACTION_BTNS.forEach(id=>{ document.getElementById(id).disabled=on; });
  document.getElementById('btn-stop').hidden=!on;
  const s=document.getElementById('hdr-status');
  s.textContent=on?'● '+kind:'idle'; s.style.color=on?'var(--ok)':'var(--dim)';
  if(!on){
    document.getElementById('prog').style.width='0%';
    ['import','fetch','organise'].forEach(p=>{
      const el=document.getElementById('ph-'+p);
      if(el&&el.classList.contains('running')) el.className='phase';
    });
  }
}
function onProgress(m){
  const pct=m.total>0?(m.done/m.total*100).toFixed(1):0;
  document.getElementById('prog').style.width=pct+'%';
  if(m.imported!==undefined) document.getElementById('s-imp').textContent='imported: '+m.imported.toLocaleString();
  if(m.duplicate!==undefined) document.getElementById('s-dup').textContent='duplicate: '+m.duplicate.toLocaleString();
  if(m.broken!==undefined) document.getElementById('s-bad').textContent='broken: '+m.broken.toLocaleString();
  document.getElementById('s-prog').textContent=m.done.toLocaleString()+' / '+m.total.toLocaleString();
  document.getElementById('s-el').textContent='elapsed: '+Math.round((Date.now()-jobStart)/1000)+'s';
}
function onPhase(m){
  const el=document.getElementById('ph-'+m.phase); if(!el) return;
  el.className='phase '+(m.status||'');
  if(m.status==='running')
    appendLog('phase-hdr','── '+m.phase.toUpperCase()+' ──────────────────────');
}
function appendLog(level,text){
  const el=document.getElementById('log');
  const ts=new Date().toTimeString().slice(0,8);
  const div=document.createElement('div');
  div.className='ll '+(level||'info');
  div.innerHTML=`<span class="ts">${ts}</span><span class="lv">${level}</span><span class="tx">${esc(text)}</span>`;
  el.appendChild(div); el.scrollTop=el.scrollHeight;
}
function clearLog(){ document.getElementById('log').innerHTML=''; }
function esc(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── direct mode (no database — folder in, folder out) ──────────────────────────
let browserTargetDirect_='src';
let esDirect=null, jobStartDirect=0;
function activateBrowserDirect(t){
  browserTargetDirect_=t;
  document.getElementById('direct-src-in').classList.toggle('active-target',t==='src');
  document.getElementById('direct-dest-in').classList.toggle('active-target',t==='dest');
  document.getElementById('direct-bt-src').classList.toggle('active',t==='src');
  document.getElementById('direct-bt-dest').classList.toggle('active',t==='dest');
}
function setActiveTargetDirect(t){ browserTargetDirect_=t; activateBrowserDirect(t); }
async function browseDirect(path){
  document.getElementById('direct-b-path').textContent=path;
  const r=await fetch('/api/browse?path='+encodeURIComponent(path));
  const d=await r.json();
  let html='';
  if(d.parent)
    html+=`<div class="be be-up" onclick="browseDirect(${J(d.parent)})"><span class="ico">▲</span><span class="nm">..</span></div>`;
  for(const e of d.entries){
    if(e.is_dir){
      html+=`<div class="be"><span class="ico">▶</span>
        <span class="nm" onclick="browseDirect(${J(e.path)})">${e.name}</span>
        <span class="bbtns">
          <button class="bbtn" onclick="pickFolderDirect(${J(e.path)},'src')">src</button>
          <button class="bbtn dest" onclick="pickFolderDirect(${J(e.path)},'dest')">dest</button>
        </span></div>`;
    } else {
      html+=`<div class="be be-file"><span class="ico">·</span><span class="nm">${e.name}</span></div>`;
    }
  }
  document.getElementById('direct-browser').innerHTML=html||'<div style="padding:8px 12px;color:var(--dim);font-size:11px">(empty)</div>';
}
function pickFolderDirect(path,target){
  document.getElementById(target==='src'?'direct-src-in':'direct-dest-in').value=path;
  activateBrowserDirect(target);
  if(target==='src') scanSourceDirect();
}
async function scanSourceDirect(){
  const p=document.getElementById('direct-src-in').value.trim(); if(!p)return;
  document.getElementById('direct-scan-result').textContent='scanning…';
  const r=await fetch('/api/scan?path='+encodeURIComponent(p));
  const d=await r.json();
  document.getElementById('direct-scan-result').textContent=
    d.error?d.error:d.count.toLocaleString()+' files';
}
async function runDirect(){
  const body={
    sources:[document.getElementById('direct-src-in').value.trim()].filter(Boolean),
    dest:document.getElementById('direct-dest-in').value.trim(),
    providers:provs.filter(p=>p.enabled).map(p=>p.id),
    dry_run:document.getElementById('direct-dry-run').checked,
  };
  if(!body.sources.length){ appendLogDirect('broken','set a source folder first'); return; }
  if(!body.dest){ appendLogDirect('broken','set an output folder first'); return; }
  if(body.dry_run) appendLogDirect('warning','DRY RUN — nothing will be written');
  const r=await fetch('/api/job/direct',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();
  if(j.error){ appendLogDirect('broken',j.error); return; }
  setRunningDirect(true); jobStartDirect=Date.now();
  if(esDirect) esDirect.close();
  esDirect=new EventSource('/api/job/stream');
  esDirect.onmessage=e=>{
    const m=JSON.parse(e.data);
    if(m.type==='log')      appendLogDirect(m.level,m.text);
    else if(m.type==='progress') onProgressDirect(m);
    else if(m.type==='done'){ setRunningDirect(false); esDirect.close(); esDirect=null; }
  };
  esDirect.onerror=()=>{ setRunningDirect(false); if(esDirect){esDirect.close();esDirect=null;} };
}
function setRunningDirect(on){
  document.getElementById('direct-btn-all').disabled=on;
  document.getElementById('direct-btn-stop').hidden=!on;
  if(!on) document.getElementById('direct-prog').style.width='0%';
}
function onProgressDirect(m){
  const pct=m.total>0?(m.done/m.total*100).toFixed(1):0;
  document.getElementById('direct-prog').style.width=pct+'%';
  if(m.imported!==undefined) document.getElementById('direct-s-imp').textContent='imported: '+m.imported.toLocaleString();
  if(m.duplicate!==undefined) document.getElementById('direct-s-dup').textContent='duplicate: '+m.duplicate.toLocaleString();
  if(m.broken!==undefined) document.getElementById('direct-s-bad').textContent='broken: '+m.broken.toLocaleString();
  document.getElementById('direct-s-prog').textContent=m.done.toLocaleString()+' / '+m.total.toLocaleString();
  document.getElementById('direct-s-el').textContent='elapsed: '+Math.round((Date.now()-jobStartDirect)/1000)+'s';
}
function appendLogDirect(level,text){
  const el=document.getElementById('direct-log');
  const ts=new Date().toTimeString().slice(0,8);
  const div=document.createElement('div');
  div.className='ll '+(level||'info');
  div.innerHTML=`<span class="ts">${ts}</span><span class="lv">${level}</span><span class="tx">${esc(text)}</span>`;
  el.appendChild(div); el.scrollTop=el.scrollHeight;
}
function clearLogDirect(){ document.getElementById('direct-log').innerHTML=''; }

// ── session tab ───────────────────────────────────────────────────────────────
async function loadSession(){
  const r=await fetch('/api/session/files');
  const d=await r.json();
  sessData=d.files||[];
  const st=d.stats||{};
  document.getElementById('ss-total').textContent=(st.total||0).toLocaleString();
  document.getElementById('ss-imp').textContent=(st.imported||0).toLocaleString();
  document.getElementById('ss-brk').textContent=(st.broken||0).toLocaleString();
  document.getElementById('ss-dup').textContent=(st.duplicate||0).toLocaleString();
  const nb=document.getElementById('btn-refetch');
  nb.hidden=!(st.broken>0);
  nb.textContent='↺ Re-fetch broken ('+st.broken+')';
  renderSession();
}
function setSessFilter(f,btn){
  sessFilt=f;
  document.querySelectorAll('#tab-session .chip').forEach(c=>c.classList.remove('active'));
  btn.classList.add('active');
  renderSession();
}
function sortSess(col){
  if(sessSort===col) sessSortAsc=!sessSortAsc;
  else { sessSort=col; sessSortAsc=true; }
  renderSession();
}
function filterSession(){ renderSession(); }
function renderSession(){
  const q=(document.getElementById('sess-search').value||'').toLowerCase();
  let rows=sessData.filter(r=>{
    if(sessFilt!=='all'&&r.status!==sessFilt) return false;
    if(!q) return true;
    return (r.artist||'').toLowerCase().includes(q)||
           (r.album||'').toLowerCase().includes(q)||
           (r.title||'').toLowerCase().includes(q)||
           (r.filename||'').toLowerCase().includes(q);
  });
  rows.sort((a,b)=>{
    let av=a[sessSort]||'', bv=b[sessSort]||'';
    return sessSortAsc?(av<bv?-1:av>bv?1:0):(av<bv?1:av>bv?-1:0);
  });
  const COLS=['filename','artist','album','year','label','catalog_number','dur','status'];
  document.getElementById('sess-tbody').innerHTML=rows.map((r,i)=>`
    <tr onclick="showDetail(sessData,${sessData.indexOf(r)})" style="cursor:pointer">
      <td title="${esc(r.path||'')}">${esc(r.filename||'')}</td>
      <td>${esc(r.artist||'')}</td>
      <td>${esc(r.album||'')}</td>
      <td>${esc(r.year||'')}</td>
      <td>${esc(r.label||'')}</td>
      <td>${esc(r.catalog_number||'')}</td>
      <td>${esc(r.dur||'')}</td>
      <td><span class="status-badge ${r.status||'unknown'}">${r.status||'?'}</span></td>
    </tr>`).join('');
}
async function refetchBroken(){
  const body={
    sources:[document.getElementById('src-in').value.trim()].filter(Boolean),
    dest:document.getElementById('dest-in').value.trim(),
    providers:provs.filter(p=>p.enabled).map(p=>p.id),
    dry_run:false,
  };
  openToolLog('Re-fetching broken files…');
  const r=await fetch('/api/job/fetch_broken',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();
  if(j.error){ appendToolLog('broken',j.error); return; }
  startToolStream(()=>{ loadSession(); });
}
async function commitSession(){
  const r=await fetch('/api/commit',{method:'POST'});
  const j=await r.json();
  if(j.error){ alert('Commit failed: '+j.error); return; }
  alert(`✓ Committed — library.db: ${j.before.toLocaleString()} → ${j.after.toLocaleString()} files (+${j.delta})`);
}

// ── library tab ───────────────────────────────────────────────────────────────
async function loadLibraryStats(){
  const r=await fetch('/api/library/stats');
  const d=await r.json();
  const lib=d.library||{};
  document.getElementById('ls-total').textContent=(lib.total||0).toLocaleString();
  document.getElementById('ls-artists').textContent=(lib.artists||0).toLocaleString();
  document.getElementById('ls-labels').textContent=(lib.labels||0).toLocaleString();
  document.getElementById('ls-size').textContent=(lib.size_gb||0).toFixed(1);
}
function setLibFilter(s,btn){
  libStatus_=s; libPage_=0;
  document.querySelectorAll('#tab-library .chip').forEach(c=>c.classList.remove('active'));
  btn.classList.add('active');
  loadLibrary(0);
}
function debounceLibSearch(){
  clearTimeout(libDebounce);
  libDebounce=setTimeout(()=>{ libQ_=document.getElementById('lib-search').value.trim(); libPage_=0; loadLibrary(0); },300);
}
function libPage(dir){
  const newPage=libPage_+dir;
  if(newPage<0) return;
  loadLibrary(newPage);
}
async function loadLibrary(page){
  libPage_=page;
  const params=new URLSearchParams({db:'library',q:libQ_,status:libStatus_,page,per_page:50});
  const r=await fetch('/api/library/files?'+params);
  const d=await r.json();
  document.getElementById('lib-page-info').textContent=`page ${(d.page||0)+1} of ${d.pages||1}`;
  document.getElementById('lib-count').textContent=`${(d.total||0).toLocaleString()} total`;
  document.getElementById('lib-tbody').innerHTML=(d.files||[]).map((r,i)=>`
    <tr onclick="showDetail(null,null,${J(r)})" style="cursor:pointer">
      <td>${esc(r.artist||'')}</td>
      <td>${esc(r.album||'')}</td>
      <td>${esc(r.title||'')}</td>
      <td>${esc(r.year||'')}</td>
      <td>${esc(r.label||'')}</td>
      <td>${esc(r.catalog_number||'')}</td>
      <td><span class="status-badge ${r.status||'unknown'}">${r.status||'?'}</span></td>
    </tr>`).join('');
}

// ── tools tab ─────────────────────────────────────────────────────────────────
async function runTool(kind){
  const body={
    sources:[document.getElementById('src-in').value.trim()].filter(Boolean),
    dest:document.getElementById('dest-in').value.trim(),
    providers:provs.filter(p=>p.enabled).map(p=>p.id),
    dry_run:false,
  };
  const labels={rebuild:'Rebuilding library index…',vacuum:'Compacting library DB…',
                fetch_broken:'Re-fetching broken files…'};
  openToolLog(labels[kind]||kind+'…');
  const r=await fetch('/api/job/'+kind,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();
  if(j.error){ appendToolLog('broken',j.error); return; }
  startToolStream(()=>{
    if(kind==='rebuild') loadLibraryStats();
    if(kind==='fetch_broken') loadSession();
  });
}

async function runAudits(){
  const resultEl=document.getElementById('audit-results');
  resultEl.innerHTML='<div style="color:var(--dim);font-size:11px;padding:4px 0">running…</div>';
  const r=await fetch('/api/library/audits');
  const d=await r.json();
  if(d.error){ resultEl.innerHTML=`<div style="color:var(--err);font-size:11px">${esc(d.error)}</div>`; return; }
  const rows=(d.audits||[]).map(a=>`
    <div class="audit-row ${a.count>0?'has-issues':''}">
      <span class="ac ${a.count===0?'ok':''}">${a.count}</span>
      <span class="al">${esc(a.label)}</span>
    </div>`).join('');
  resultEl.innerHTML=`<div style="font-size:10px;color:var(--dim);padding:4px 0">
    ${d.total_issues} total issues</div>${rows}`;
}

async function runSQL(){
  const sql=document.getElementById('sql-input').value.trim();
  const db=document.getElementById('sql-db').value;
  const stat=document.getElementById('sql-status');
  const results=document.getElementById('sql-results');
  if(!sql){ stat.textContent='empty'; return; }
  stat.textContent='running…'; results.innerHTML='';
  const r=await fetch('/api/library/sql',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({sql,db})});
  const d=await r.json();
  if(d.error){ stat.textContent='error'; results.innerHTML=`<div style="padding:12px;color:var(--err);font-size:12px">${esc(d.error)}</div>`; return; }
  stat.textContent=`${d.count} rows`;
  if(!d.rows||!d.rows.length){ results.innerHTML='<div style="padding:12px;color:var(--dim);font-size:12px">no rows</div>'; return; }
  const cols=d.cols||Object.keys(d.rows[0]);
  results.innerHTML=`<table>
    <thead><tr>${cols.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead>
    <tbody>${d.rows.map(row=>`<tr>${cols.map(c=>`<td title="${esc(String(row[c]||''))}">${esc(String(row[c]||''))}</td>`).join('')}</tr>`).join('')}</tbody>
  </table>`;
}

// ── tool log overlay ──────────────────────────────────────────────────────────
function openToolLog(title){
  document.getElementById('tool-log-title').textContent=title;
  document.getElementById('tool-log').innerHTML='';
  document.getElementById('tool-log-overlay').classList.remove('hidden');
}
function closeToolLog(e){ if(e.target.id==='tool-log-overlay') closeToolLogBtn(); }
function closeToolLogBtn(){
  document.getElementById('tool-log-overlay').classList.add('hidden');
  if(toolLogES){ toolLogES.close(); toolLogES=null; }
}
function appendToolLog(level,text){
  const el=document.getElementById('tool-log');
  const div=document.createElement('div');
  div.className='ll '+(level||'info');
  const ts=new Date().toTimeString().slice(0,8);
  div.innerHTML=`<span class="ts">${ts}</span><span class="lv">${level}</span><span class="tx">${esc(text)}</span>`;
  el.appendChild(div); el.scrollTop=el.scrollHeight;
}
function startToolStream(onDone){
  if(toolLogES) toolLogES.close();
  toolLogES=new EventSource('/api/job/stream');
  toolLogES.onmessage=e=>{
    const m=JSON.parse(e.data);
    if(m.type==='log') appendToolLog(m.level,m.text);
    else if(m.type==='done'){
      toolLogES.close(); toolLogES=null;
      document.getElementById('tool-log-title').textContent='Done';
      if(onDone) onDone();
    }
  };
  toolLogES.onerror=()=>{ if(toolLogES){toolLogES.close();toolLogES=null;} };
}

// ── detail popup ──────────────────────────────────────────────────────────────
function showDetail(arr,idx,rowObj){
  const r=rowObj||(arr&&arr[idx]);
  if(!r) return;
  document.getElementById('detail-title').textContent=r.filename||r.path||'File detail';
  const FIELDS=[
    ['Path','path'],['Status','status'],['Artist','artist'],['Album artist','albumartist'],
    ['Album','album'],['Title','title'],['Year','year'],['Label','label'],
    ['Cat#','catalog_number'],['Genre','genre'],['Duration','dur'],['Size MB','mb'],
  ];
  const cls=f=>f==='status'?(r[f]==='imported'?'ok':r[f]==='broken'?'err':r[f]==='duplicate'?'warn':''):'';
  document.getElementById('detail-body').innerHTML=
    `<div class="meta-grid">${FIELDS.map(([k,f])=>`
      <span class="mk">${k}</span>
      <span class="mv ${cls(f)}">${esc(String(r[f]||'—'))}</span>`).join('')}
    </div>`;
  document.getElementById('detail-overlay').classList.remove('hidden');
}
function closeDetail(e){ if(e.target.id==='detail-overlay') closeDetailBtn(); }
function closeDetailBtn(){ document.getElementById('detail-overlay').classList.add('hidden'); }

init();
</script>
</body>
</html>
"""

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

    url = f"http://{_lan_ip}:{args.port}"
    log.info("=" * 56)
    log.info(f"  music-organiser  v{_VERSION}")
    log.info(f"  open  →  {url}")
    log.info(f"  log   →  {_LOG_FILE}")
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
