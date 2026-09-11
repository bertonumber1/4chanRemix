#!/usr/bin/env python3
"""Label sorter panel — state, jobs, moves and exports for the Labels tab.

The engine (matching, Discogs, disk reading) is `label_ref`.  This module owns
everything with a side effect: which label is active, which folders have been
added as roots, the cached result of the last scan, the exports, and the move
log that makes a move reversible.

State lives in `label_state.json` beside the databases rather than in
config.toml.  The config writer is deliberately a regex over the raw TOML text
(so the comments in that file survive), and a list-of-tables like `roots` is
exactly the shape that kind of surgery gets wrong.
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sys
import time
from datetime import datetime

import label_ref as L

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_HERE, "label_state.json")
CACHE_DIR = os.path.join(_HERE, "label-cache")
MOVELOG = os.path.join(_HERE, "label_moves.tsv")


def _scan_path(label_id) -> str:
    """Where one label's last_scan.json lives — a sibling of that label's own
    catalogue.json/tracklists.json inside L.LabelCache's directory, not a
    second path scheme. Per-label so switching the active label (Batch 2's
    multi-label tracking) never discards another label's scan results — a
    single shared last_scan.json, which is what this was before, meant
    switching labels silently threw away whatever you had just scanned."""
    return os.path.join(CACHE_DIR, str(int(label_id)), "last_scan.json")

# Seeded so the tool is useful the moment it is opened, on the box where the
# Bit Music work already lives.  It is only a STARTING LABEL, not a root: no
# folder is ever assumed — the owner's rule is that paths are added in the UI.
DEFAULT_STATE = {
    "label_id": 10663,
    "label_name": "Bit Music",
    "roots": [],          # [{"path": ..., "role": "owned"|"incoming"}]
    "archive": "",        # where Move files things; blank until chosen
    "overrides": [],      # catnos the owner has declared finished
    "tracked_labels": [], # [{"id": ..., "name": ...}] — labels seen via
                          # "Change label...", for the overview strip and
                          # cross-label incoming matching. The ACTIVE label
                          # (label_id/label_name above) is not duplicated in
                          # here; it's implicitly tracked too.
}


# Windows is the primary target and Linux is still supported, so paths must be
# compared the way the PLATFORM compares them. os.path.normcase lower-cases and
# turns "/" into "\\" on Windows and does nothing at all on POSIX, which is
# exactly the distinction wanted: on Windows "D:\\Music" and "d:/music" are one
# folder, on Linux "Music" and "music" are two. Comparing raw strings meant the
# move guard would refuse a legitimate folder on Windows because its drive
# letter was typed in the other case, and the same folder could be added as a
# root twice.
def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(path or "")))


# ─── state ────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    st = dict(DEFAULT_STATE)
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            st.update(json.load(fh) or {})
    except Exception:
        pass
    st["roots"] = [r for r in (st.get("roots") or []) if r.get("path")]
    # dict(DEFAULT_STATE) is a SHALLOW copy — decouple the list so appending
    # to a fresh state's tracked_labels can never mutate DEFAULT_STATE itself.
    st["tracked_labels"] = list(st.get("tracked_labels") or [])
    return st


def save_state(st: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def cache(st: dict | None = None) -> L.LabelCache:
    st = st or load_state()
    return L.LabelCache(CACHE_DIR, st["label_id"])


def _add_tracked(st: dict, label_id: int, label_name: str) -> None:
    label_id = int(label_id)
    tl = st.setdefault("tracked_labels", [])
    for t in tl:
        if int(t["id"]) == label_id:
            t["name"] = label_name or t["name"]
            return
    tl.append({"id": label_id, "name": label_name or ""})


def track_label(label_id: int, label_name: str = "") -> dict:
    st = load_state()
    _add_tracked(st, label_id, label_name)
    save_state(st)
    return st


def untrack_label(label_id: int) -> dict:
    st = load_state()
    label_id = int(label_id)
    st["tracked_labels"] = [t for t in st.get("tracked_labels") or []
                            if int(t["id"]) != label_id]
    save_state(st)
    return st


def set_label(label_id: int, label_name: str) -> dict:
    st = load_state()
    old_id, old_name = st["label_id"], st["label_name"]
    label_id = int(label_id)
    if old_id != label_id:
        # Tracking builds up automatically as you switch labels — no separate
        # "add to tracked" step for the basic case. The label being left
        # behind is the one that would otherwise be forgotten.
        _add_tracked(st, old_id, old_name)
    st["label_id"] = label_id
    st["label_name"] = label_name or st["label_name"]
    save_state(st)
    return st


def overview() -> list:
    """One line per tracked label plus the currently active one, read from
    each label's own already-cached last_scan() — no scanning, no Discogs
    calls. Staleness is only meaningful for the ACTIVE label: roots are
    still global-to-the-active-label in this version (tracking is additive
    state, not a full per-label-roots redesign), so a non-active label has
    nothing to compare its old scan's roots against right now.
    """
    st = load_state()
    active_id = int(st["label_id"])
    seen = {active_id: st["label_name"]}
    for t in st.get("tracked_labels") or []:
        seen.setdefault(int(t["id"]), t.get("name", ""))
    out = []
    for label_id, label_name in seen.items():
        scan_ = last_scan(label_id)
        out.append({
            "label_id": label_id, "label_name": label_name,
            "active": label_id == active_id,
            "summary": (scan_ or {}).get("summary"),
            "stale": _stale(st, scan_) if (scan_ and label_id == active_id) else "",
        })
    out.sort(key=lambda o: (not o["active"], o["label_name"].lower()))
    return out


def cross_label_check(role: str = "incoming") -> dict:
    """For every folder under a `role` root, check it against EVERY tracked
    label's catalogue, not just the active one — an incoming share judged
    only against Bit Music never says "this isn't Bit Music, it's Makina
    Force". Reads only already-cached catalogue.json files (zero new Discogs
    calls) and the same folders.json cache a plain scan already maintains.
    Returns folders that matched a DIFFERENT label than the one active now.
    """
    st = load_state()
    roots = [r for r in st["roots"] if r.get("role") == role]
    if not roots:
        return {"rows": []}

    active_id = int(st["label_id"])
    label_ids = {active_id} | {int(t["id"]) for t in st.get("tracked_labels") or []}
    names = {active_id: st["label_name"]}
    for t in st.get("tracked_labels") or []:
        names[int(t["id"])] = t.get("name", "")
    catalogues = {}
    for lid in label_ids:
        cat = L.LabelCache(CACHE_DIR, lid).catalogue()
        if cat:
            catalogues[lid] = cat

    folders = L.index_roots(roots, lambda m: None,
                            cache_path=os.path.join(CACHE_DIR, "folders.json"))
    rows = []
    for f in folders:
        for lid, m in L.match_against_catalogues(f, catalogues).items():
            if lid == active_id:
                continue
            rows.append({
                "path": f["path"], "name": f["name"], "root": f["root"],
                "label_id": lid, "label_name": names.get(lid, ""),
                "catno": m["row"].get("catno", ""),
                "title": m["row"].get("title", ""),
                "matched_by": m["matched_by"],
            })
    return {"rows": rows}


def add_root(path: str, role: str) -> tuple[bool, str]:
    path = os.path.abspath(os.path.expanduser((path or "").strip()))
    if not path:
        return False, "no path given"
    if not os.path.isdir(path):
        return False, "not a folder: " + path
    st = load_state()
    for r in st["roots"]:
        if _norm(r["path"]) == _norm(path):
            r["role"] = role
            save_state(st)
            return True, "role updated"
    st["roots"].append({"path": path, "role": role if role in
                        ("owned", "incoming") else "owned"})
    save_state(st)
    return True, "added"


def remove_root(path: str) -> tuple[bool, str]:
    st = load_state()
    before = len(st["roots"])
    st["roots"] = [r for r in st["roots"]
                   if _norm(r["path"]) != _norm(path)]
    save_state(st)
    return (len(st["roots"]) < before), "removed" if len(st["roots"]) < before else "not found"


def set_archive(path: str) -> tuple[bool, str]:
    path = (path or "").strip()
    if path and not os.path.isdir(path):
        return False, "not a folder: " + path
    st = load_state()
    st["archive"] = os.path.abspath(path) if path else ""
    save_state(st)
    return True, "saved"


# ─── Discogs token, borrowed from the app's own provider config ───────────────
def _token(cfg: dict) -> str:
    tok = (((cfg or {}).get("providers") or {}).get("discogs") or {}).get("token") or ""
    if tok:
        return str(tok).strip()
    # Fall back to the label2lossless token file, which is where the other tools
    # on the Linux box keep it (a snap-confined python cannot read a dotfile in
    # $HOME, hence the app-dir copy). These paths simply do not exist on Windows
    # or in Docker, which is correct: there the token comes from config.toml
    # above, and "Get catalogue" fetches from Discogs rather than seeding.
    for p in ("/home/media/music-tools/label2lossless/discogs_token.txt",
              os.path.expanduser("~/.discogs_token")):
        try:
            with open(p) as fh:
                t = fh.read().strip()
            if t:
                return t
        except OSError:
            continue
    return ""


# ─── catalogue jobs ───────────────────────────────────────────────────────────
def catalogue_status(st: dict | None = None) -> dict:
    st = st or load_state()
    c = cache(st)
    cat = c.catalogue()
    tls = c.tracklists()
    have_tl = 0
    for r in cat:
        if L.tracklist_for(r, tls):
            have_tl += 1
    return {"releases": len(cat), "tracklists": have_tl,
            "tracklists_cached": len(tls),
            "catalogue_file": c.cat_path, "fetched": bool(cat)}


def fetch_catalogue(cfg: dict, log=print, force: bool = False) -> dict:
    st = load_state()
    c = cache(st)
    if not force:
        seeded = c.seed(log)
        log("[label] " + seeded)
        if c.catalogue():
            return catalogue_status(st)
    d = L.Discogs(_token(cfg), log)
    log(f"[label] fetching Discogs catalogue for {st['label_name']} "
        f"(label {st['label_id']}) …")
    rows = d.label_releases(st["label_id"],
                            progress=lambda p, t, n: log(
                                f"[label]   page {p}/{t} ({n} releases)"))
    c.save_catalogue(rows)
    log(f"[label] catalogue saved: {len(rows)} releases")
    return catalogue_status(st)


def fetch_tracklists(cfg: dict, log=print, limit: int = 2000,
                     time_budget_s: float = 600.0,
                     only_missing_releases: bool = True,
                     should_stop=None) -> dict:
    """Fill in tracklists, newest gap first.

    A tracklist is one API call per release and Discogs allows 60 a minute, so
    this paces itself (label_ref.Discogs._get, backed by a shared _RateBudget)
    and stops after `time_budget_s` rather than an arbitrary release count —
    2,955 releases is the better part of an hour, and it is pointless to spend
    it on releases we already hold whole. `want` is recomputed fresh from what
    is still missing a tracklist on every call, so a run that stops partway
    (time budget, Stop, or the `limit` backstop) is resumable for free: just
    run it again.
    """
    st = load_state()
    c = cache(st)
    cat, tls = c.catalogue(), c.tracklists()
    if not cat:
        log("[label] no catalogue yet — fetch that first")
        return {"added": 0}

    want = []
    scan = last_scan()
    interesting = None
    if only_missing_releases and scan:
        interesting = {r["key"] for r in scan.get("rows", [])
                       if r["status"] in ("missing", "partial", "held")}
    for r in cat:
        if L.tracklist_for(r, tls):
            continue
        if not r.get("id"):
            continue
        if interesting is not None and L.release_key(r) not in interesting:
            continue
        want.append(r)
    if not want:
        log("[label] every release we care about already has a tracklist")
        return {"added": 0}

    d = L.Discogs(_token(cfg), log)
    log(f"[label] {len(want)} releases without a tracklist; "
        f"fetching up to {min(len(want), limit)} (time budget {time_budget_s:.0f}s)")
    added = 0
    start = time.time()
    for r in want:
        if should_stop and should_stop():
            log(f"[label] tracklists: stopped — {added}/{len(want)} done")
            break
        if time.time() - start > time_budget_s:
            log(f"[label] tracklists: time budget ({time_budget_s:.0f}s) reached, "
                f"{added}/{len(want)} done — run again to continue")
            break
        if added >= limit:
            log(f"[label] tracklists: limit ({limit}) reached, "
                f"{added}/{len(want)} done — run again to continue")
            break
        try:
            tracks = d.tracklist(r["id"])
        except L.DiscogsError as e:
            log(f"[label]   {r.get('catno', '?')}: {e}")
            continue
        entry = {"id": r["id"], "title": r.get("title", ""), "tracks": tracks}
        for k in L.catno_keys(r.get("catno")):
            tls[k] = entry
        added += 1
        if added % 25 == 0:
            c.save_tracklists(tls)
            log(f"[label]   {added}/{len(want)} …")
    c.save_tracklists(tls)
    log(f"[label] tracklists added: {added}")
    return {"added": added}


# ─── the scan ─────────────────────────────────────────────────────────────────
def last_scan(label_id=None) -> dict | None:
    """The given label's cached scan result, or the ACTIVE label's if
    `label_id` is omitted. `None` on a miss — a label that has never been
    scanned, not an error."""
    if label_id is None:
        label_id = load_state()["label_id"]
    try:
        with open(_scan_path(label_id), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def cross_check_release(row: dict, folder_info: dict | None, tls: dict) -> dict:
    """Attach row['crosscheck'], using the tracklist already cached — zero new
    Discogs calls, which is why this runs on every plain scan() rather than
    staying opt-in like authenticity (which decodes audio and is genuinely
    expensive).
    """
    expected = L.tracklist_for(row, tls)
    info = folder_info or {}
    matched = L.match_tracks(expected, info.get("ids") or []) if expected else {}
    result = L.cross_check(row, info, expected, matched)
    row["crosscheck"] = result
    return result


def fix_tags(paths: list, dry_run: bool = False) -> dict:
    """Fill in missing artist / track-number tags, per row, from the label's
    own cached scan and Discogs tracklist.

    Deliberately narrow: writes ONLY the two fields tag_check() already flags
    as missing (never a contradiction — picking a side between a file's own
    tag and the catalogue is a human call, not this button's), and reuses
    write_tags_to_file's only_missing=True so an existing tag, right or
    wrong, is never touched. Same "outside the configured roots" refusal as
    move_folders — this writes to files, so it gets the same guard.

    Reads the folder FRESH rather than through FolderCache: this runs on one
    or two folders at a time, opt-in, so the per-file read cost that the
    cache exists to avoid on a full scan is not a concern here — and a cache
    entry from before this folder's tags were fixed (or from before the
    'paths' field below existed) must never be trusted for a write.
    """
    from tag_writer import write_tags_to_file

    st = load_state()
    roots = [r["path"] for r in st.get("roots") or []]
    scan_ = last_scan()
    if not scan_:
        return {"ok": False, "results": [],
                "reason": "no scan yet — run Scan folders first"}
    by_folder = {row["folder"]: row for row in scan_["rows"] if row.get("folder")}
    tls = cache(st).tracklists()

    results = []
    for src in paths:
        src = os.path.abspath(src)
        name = os.path.basename(src.rstrip(os.sep))
        out = {"path": src, "name": name, "fixed_files": 0, "ok": False, "reason": ""}

        if not any(_under(src, r) for r in roots):
            out["reason"] = "outside every configured folder — refusing to touch it"
            results.append(out)
            continue
        row = by_folder.get(src)
        if row is None:
            out["reason"] = "not in the last scan — rescan first"
            results.append(out)
            continue

        expected = L.tracklist_for(row, tls)
        info = L.folder_tracks(src)
        matched = L.match_tracks(expected, info.get("ids") or []) if expected else {}
        file_paths = info.get("paths") or []
        file_tags = info.get("tags") or []
        row_artist = (row.get("artist") or "").strip()
        various = row_artist.lower() in ("", "various", "various artists", "va", "v/a")

        fixed = errors = 0
        for ti in range(len(expected)):
            fi = matched.get(ti)
            if fi is None or fi < 0 or fi >= len(file_paths):
                continue
            tags_on_file = file_tags[fi] if fi < len(file_tags) else {}
            new_tags = {}
            if not various and not (tags_on_file.get("artist") or "").strip():
                new_tags["artist"] = row_artist
            if not (tags_on_file.get("tracknumber") or "").strip():
                new_tags["track_number"] = str(ti + 1)
            if not new_tags:
                continue
            res = write_tags_to_file(file_paths[fi], new_tags,
                                     only_missing=True, dry_run=dry_run)
            if res.error:
                errors += 1
            elif res.written_fields:
                fixed += 1
        out["fixed_files"] = fixed
        out["ok"] = errors == 0
        if dry_run:
            out["reason"] = "would fix %d file(s)" % fixed
        elif errors:
            out["reason"] = "%d error(s) writing tags" % errors
        elif fixed:
            out["reason"] = "fixed %d file(s)" % fixed
        else:
            out["reason"] = "nothing to fix"
        results.append(out)
    return {"ok": bool(results) and all(r["ok"] for r in results),
            "results": results, "dry_run": dry_run}


def scan(cfg: dict, log=print, should_stop=None) -> dict:
    st = load_state()
    c = cache(st)
    cat = c.catalogue()
    if not cat:
        log("[label] no catalogue for this label yet — run 'Get catalogue' first")
        raise RuntimeError("no catalogue")
    if not st["roots"]:
        log("[label] no folders added yet — add at least one with + Add folder")
        raise RuntimeError("no roots")

    log(f"[label] {st['label_name']}: {len(cat)} catalogue releases, "
        f"{len(st['roots'])} folder(s) to read")
    # cache_path is NOT optional in practice: without it every scan re-reads
    # every folder's tags off the network mount, which is the entire cost of a
    # scan. It was omitted when this was first wired up and the cache — the
    # thing that makes a rescan take seconds instead of half an hour — simply
    # never existed.
    folders = L.index_roots(st["roots"], log,
                            cache_path=os.path.join(CACHE_DIR, "folders.json"),
                            should_stop=should_stop)
    log(f"[label] read {len(folders)} release folders; matching …")
    tls = c.tracklists()
    res = L.assess(cat, folders, tls, st.get("overrides"))
    # Cross-check costs nothing new (no audio decode, no Discogs calls — just
    # the tags/artwork index_roots already read), so unlike authenticity it
    # runs on every scan, not as a separate opt-in job.
    by_path = {f["path"]: f for f in folders}
    for row in res["rows"]:
        if row.get("folder"):
            cross_check_release(row, by_path.get(row["folder"]), tls)
    out = {
        "when": int(time.time()),
        "label_id": st["label_id"], "label_name": st["label_name"],
        "roots": st["roots"],
        "rows": res["rows"],
        "orphans": [{k: o[k] for k in ("path", "name", "root", "role", "catno",
                                       "title", "lossless", "lossy",
                                       "needs_convert", "files")}
                    for o in res["orphans"]],
        "summary": L.summarise(res["rows"]),
        "series": L.group_series(res["rows"]),
    }
    _write_scan(out)
    s = out["summary"]
    log(f"[label] done — {s['complete']} complete, {s['partial']} partial, "
        f"{s['missing']} missing, {s['tracks_missing']} outstanding tracks, "
        f"{len(out['orphans'])} folders not in this catalogue")
    return out


def _write_scan(out: dict) -> None:
    """Writes to the label named in out['label_id'] — falls back to the
    active label only if the caller left that field out."""
    path = _scan_path(out.get("label_id") or load_state()["label_id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _import_label_authenticity():
    """Lazy, defensive import of the ONE sanctioned crossing into
    zzzzScriptstuff (the fake-FLAC/SPEK-TRO analyser). label_authenticity.py
    does its own sys.path bootstrap to find fake_flac/spectral as siblings,
    but something still has to put zzzzScriptstuff itself on sys.path before
    `import label_authenticity` can find IT — web_ui.py does that at process
    start, but the test suite imports label_panel directly, so it must not be
    assumed here.
    """
    zz = os.path.join(_HERE, "zzzzScriptstuff")
    if zz not in sys.path:
        sys.path.insert(0, zz)
    import label_authenticity
    return label_authenticity


def authenticate_release(row: dict, log=print, should_stop=None) -> dict:
    """Attach row['authenticity'] and return it.  Never raises — a missing or
    broken fake-FLAC/spectral dependency degrades to 'unchecked', not a
    failure that could abort onboarding or a backfill run.
    """
    try:
        auth = _import_label_authenticity()
        import_error = None
    except Exception as exc:
        auth = None
        import_error = exc

    if auth is None:
        result = {"checked": False, "reason": "authenticity unavailable: %s" % import_error}
    elif not auth.available():
        result = {"checked": False, "reason": "numpy/soundfile not installed"}
    elif not row.get("folder"):
        result = {"checked": False, "reason": "no folder"}
    else:
        st = load_state()
        cache = auth.AuthenticityCache(os.path.join(CACHE_DIR, str(st["label_id"])))
        result = auth.authenticate_folder(row["folder"], cache, log, should_stop)
        cache.save()
    row["authenticity"] = result
    return result


def _apply_authenticity_status(row: dict) -> None:
    """complete + condemned -> held_fake.  Only ever moves in that one
    direction, and never fires from a bare 'suspect' verdict — condemned is
    only ever true from the real condemn set (see label_authenticity).
    """
    a = row.get("authenticity") or {}
    if row.get("status") == "complete" and a.get("checked") and a.get("condemned"):
        row["status"] = "held_fake"


def authenticity_backfill(cfg: dict, log=print, should_stop=None) -> dict:
    """Sweep every owned, held-or-better row in the last scan that has never
    been authenticity-checked. Opt-in and potentially long (real audio decode
    per sampled track, across however many hundreds of folders are still
    unchecked) — never implied by a plain Scan.
    """
    scan = last_scan()
    if not scan:
        log("[label] no scan yet — run Scan first")
        return {"checked": 0}
    rows = scan.get("rows", [])
    todo = [r for r in rows
           if r.get("folder") and r.get("status") in ("complete", "partial", "held")
           and not (r.get("authenticity") or {}).get("checked")]
    if not todo:
        log("[label] every held release has already been authenticity-checked")
        return {"checked": 0}
    log(f"[label] authenticity: {len(todo)} release(s) to check")
    checked = 0
    for row in todo:
        if should_stop and should_stop():
            log(f"[label] authenticity: stopped — {checked}/{len(todo)} done")
            break
        authenticate_release(row, log, should_stop)
        _apply_authenticity_status(row)
        checked += 1
        if checked % 10 == 0:
            scan["summary"] = L.summarise(scan["rows"])
            _write_scan(scan)
            log(f"[label]   {checked}/{len(todo)} …")
    scan["summary"] = L.summarise(scan["rows"])
    _write_scan(scan)
    log(f"[label] authenticity backfill done: {checked} checked")
    return {"checked": checked}


def onboard_root(cfg: dict, path: str, role: str, log=print, should_stop=None) -> dict:
    """Add one folder as a root and run the onboarding pass on it: index ->
    match -> completeness -> authenticity -> cross-check.

    "+ Owned folder" / "+ Incoming" adds one ROOT — a directory holding many
    release folders, not a single release — so steps (a)-(c) and cross-check
    are just scan() (cache-accelerated across EVERY configured root, so this
    costs real time only for the folders under the root just added — every
    other folder is a FolderCache hit). Authenticity runs ONLY for folders
    newly discovered under `path`, never for the label's other already-scanned
    folders — that backlog is swept up separately, at the user's own pace, by
    authenticity_backfill(), not implied by every "+ Owned folder" click.

    Owned folders only: an incoming folder is not something we hold yet, so
    "held but fake" and canonical naming are meaningless before it is adopted.
    """
    ok, msg = add_root(path, role)
    if not ok:
        log("[label] " + msg)
        return {"ok": False, "reason": msg}

    out = scan(cfg, log, should_stop=should_stop)

    if role != "owned":
        log("[label] incoming folder — no authenticity check until it's owned")
        return {"ok": True, "onboarded": 0, "scan": out}

    target_rows = [r for r in out["rows"]
                  if r.get("folder") and _under(r["folder"], path)
                  and r["status"] in ("complete", "partial", "held")]
    onboarded = 0
    for row in target_rows:
        if should_stop and should_stop():
            log(f"[label] onboarding stopped — {onboarded}/{len(target_rows)} done")
            break
        authenticate_release(row, log, should_stop)
        _apply_authenticity_status(row)
        onboarded += 1
    if onboarded:
        out["summary"] = L.summarise(out["rows"])
        _write_scan(out)
    log(f"[label] onboarding done — {onboarded} release(s) authenticity-checked "
        f"under this folder")
    return {"ok": True, "onboarded": onboarded, "scan": out}


# Catalogue "artists" that name nobody.  Matching these is what stops a search
# list being full of lines like "Various Virtuosity".
_VARIOUS = {"various", "various artists", "va", "v/a", "verschiedene",
            "compilation", "diverse", "varios", "varios artistas"}


# ─── exports ──────────────────────────────────────────────────────────────────
def export(kind: str) -> tuple[str, str]:
    """Build one of the collector lists.  Returns (filename, text)."""
    scan_ = last_scan()
    if not scan_:
        raise RuntimeError("nothing scanned yet")
    rows = scan_["rows"]
    label = scan_.get("label_name", "label").replace("/", "-")
    stamp = datetime.now().strftime("%Y-%m-%d")

    if kind == "missing_releases":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["catno", "artist", "title", "year", "discogs_id",
                    "tracks_expected"])
        for r in rows:
            if r["status"] == "missing":
                w.writerow([r["catno"], r["artist"], r["title"], r["year"],
                            r["id"] or "", r["expected"]])
        return f"{label} - missing releases - {stamp}.csv", buf.getvalue()

    if kind == "outstanding_tracks":
        out = []
        for r in rows:
            if not r["missing_tracks"]:
                continue
            head = f"({r['catno']}) {r['title']}"
            if r["year"]:
                head += f" ({r['year']})"
            state = "MISSING RELEASE" if r["status"] == "missing" else \
                    f"have {r['have']} of {r['expected']}"
            out.append(f"{head}  [{state}]")
            for t in r["missing_tracks"]:
                out.append(f"    - {t}")
            out.append("")
        header = [f"{scan_.get('label_name')} — outstanding tracks — {stamp}",
                  "=" * 60, ""]
        return f"{label} - outstanding tracks - {stamp}.txt", \
            "\n".join(header + out)

    if kind == "partial":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["catno", "title", "have", "expected", "folder", "missing"])
        for r in rows:
            if r["status"] == "partial":
                w.writerow([r["catno"], r["title"], r["have"], r["expected"],
                            r["folder"], " | ".join(r["missing_tracks"])])
        return f"{label} - incomplete releases - {stamp}.csv", buf.getvalue()

    if kind == "search_terms":
        # One line per thing to look for, in the form a search actually takes:
        # artist + title.  A bare title matches the wrong release.
        #
        # Except on a compilation, where the catalogue's "artist" is the
        # placeholder "Various" and "Various Virtuosity" is not a search anyone
        # can use.  For those the track title stands alone and the release name
        # is carried as a trailing comment, so the line is still one you can
        # paste but you can see which record it came from.
        out = []
        for r in rows:
            a = (r["artist"] or "").strip()
            va = a.lower().rstrip(".") in _VARIOUS
            if r["status"] == "missing":
                out.append(r["title"] if va else f"{a} {r['title']}".strip())
            if r["status"] == "partial":
                for track in r["missing_tracks"]:
                    if va:
                        out.append(f"{track}    # from {r['title']}")
                    else:
                        out.append(f"{a} {track}".strip() if a else track)
        seen, uniq = set(), []
        for line in out:
            key = line.split("    #")[0].strip().lower()
            if key and key not in seen:
                seen.add(key)
                uniq.append(line)
        header = [f"# {scan_.get('label_name')} — search terms — {stamp}",
                  "# artist + title, one per line. A line with no artist is a",
                  "# compilation track: the catalogue does not name its artist,",
                  "# so the release it came from is noted after the #.", ""]
        return f"{label} - search terms - {stamp}.txt", "\n".join(header + uniq)

    if kind == "have":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["catno", "artist", "title", "year", "status", "have",
                    "expected", "needs_convert", "folder"])
        for r in rows:
            if r["status"] in ("complete", "partial", "held"):
                w.writerow([r["catno"], r["artist"], r["title"], r["year"],
                            r["status"], r["have"], r["expected"],
                            r["needs_convert"], r["folder"]])
        return f"{label} - what we have - {stamp}.csv", buf.getvalue()

    if kind == "orphans":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["folder", "catno", "title", "lossless", "lossy", "root"])
        for o in scan_.get("orphans", []):
            w.writerow([o["path"], o["catno"], o["title"], o["lossless"],
                        o["lossy"], o["root"]])
        return f"{label} - not in catalogue - {stamp}.csv", buf.getvalue()

    if kind == "re_hunt_fake":
        # A release we hold in FULL, but the copy is a confident transcode —
        # complete track count means nothing if the source was lossy. This is
        # the whole point of "genuine complete" vs "complete": these rows are
        # the gap between the two.
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["catno", "artist", "title", "year", "verdict",
                    "confidence", "folder"])
        for r in rows:
            if r["status"] == "held_fake":
                a = r.get("authenticity") or {}
                w.writerow([r["catno"], r["artist"], r["title"], r["year"],
                            a.get("worst_verdict", ""), a.get("confidence", ""),
                            r["folder"]])
        return f"{label} - re-hunt (fake) - {stamp}.csv", buf.getvalue()

    raise RuntimeError("unknown export: " + kind)


# ─── moves, with an undo log ──────────────────────────────────────────────────
def _log_move(action: str, src: str, dest: str, ok: bool, note: str = "") -> None:
    new = not os.path.exists(MOVELOG)
    with open(MOVELOG, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        if new:
            w.writerow(["when", "action", "src", "dest", "ok", "note"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), action,
                    src, dest, "1" if ok else "0", note])


def move_log(limit: int = 200) -> list:
    """The move history, newest first, with whether each one can still be undone."""
    if not os.path.exists(MOVELOG):
        return []
    out = []
    with open(MOVELOG, encoding="utf-8", newline="") as fh:
        for i, row in enumerate(csv.reader(fh, delimiter="\t")):
            if i == 0 or len(row) < 5:
                continue
            when, action, src, dest, ok = row[0], row[1], row[2], row[3], row[4]
            note = row[5] if len(row) > 5 else ""
            out.append({"when": when, "action": action, "src": src, "dest": dest,
                        "ok": ok == "1", "note": note,
                        # undoable only while the destination is still there and
                        # the original place is still free
                        "undoable": ok == "1" and action == "move"
                        and os.path.isdir(dest) and not os.path.exists(src)})
    out.reverse()
    return out[:limit]


def _unique_dest(dest_dir: str, name: str) -> str:
    target = os.path.join(dest_dir, name)
    if not os.path.exists(target):
        return target
    for n in range(2, 100):
        cand = os.path.join(dest_dir, f"{name} ({n})")
        if not os.path.exists(cand):
            return cand
    raise RuntimeError("cannot find a free name for " + name)


def _under(path: str, parent: str) -> bool:
    """Is `path` inside `parent` (or equal to it)?  Symlinks resolved on both."""
    try:
        p = os.path.normcase(os.path.realpath(path))
        q = os.path.normcase(os.path.realpath(parent))
        return p == q or p.startswith(q.rstrip(os.sep) + os.sep)
    except OSError:
        return False


def _audio_count(path: str) -> int:
    return len(L.audio_files(path))


def _free_bytes(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _dir_bytes(path: str) -> int:
    total = 0
    for dp, _dirs, fs in os.walk(path):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def move_folders(paths: list, dest_dir: str = "", dry_run: bool = False) -> dict:
    """Move release folders into the archive.  Reports PER FOLDER, never one ok.

    On a move of forty you need to know which three failed and why.

    This is the only part of the tab that relocates audio, so every refusal
    below is deliberate and none of them are advisory:

    * the source must lie under one of the CONFIGURED ROOTS.  Without that,
      `/api/label/move` would relocate any directory on the machine — and it is
      an unauthenticated endpoint bound to the LAN, reachable by a stray request
      or a wrong path in the UI.
    * the destination must not be inside the source, or a folder is moved into
      itself and the tree is destroyed.
    * a symlinked source is refused: moving it either moves the link and loses
      the tie to the real data, or follows it out of the roots entirely.
    * a cross-filesystem move is a copy-then-delete, so the space has to exist
      BEFORE it starts; running out midway leaves a half-written destination.
    * after the move, the audio files are counted again.  A move that arrives
      short is reported as such, with the folder left where it landed rather
      than silently called a success.
    """
    st = load_state()
    dest_dir = (dest_dir or st.get("archive") or "").strip()
    results = []
    if not dest_dir:
        return {"ok": False, "reason": "no archive folder chosen", "results": []}
    dest_dir = os.path.abspath(dest_dir)
    if not os.path.isdir(dest_dir):
        return {"ok": False, "reason": "archive folder does not exist: " + dest_dir,
                "results": []}

    roots = [r["path"] for r in st.get("roots") or []]
    if not roots:
        return {"ok": False, "results": [],
                "reason": "no folders are configured, so there is nothing this "
                          "tab is allowed to move"}

    for src in paths:
        src = os.path.abspath(src)
        name = os.path.basename(src.rstrip(os.sep))
        row = {"src": src, "name": name, "dest": "", "ok": False, "reason": ""}

        def fail(reason):
            row["reason"] = reason
            results.append(row)

        if not os.path.isdir(src):
            fail("source folder is gone")
            continue
        # os.path.islink misses Windows JUNCTIONS, which are reparse points
        # rather than symlinks and are common on a drive that has been
        # reorganised. Comparing the resolved path against the literal one
        # catches both, on both platforms.
        if os.path.islink(src.rstrip(os.sep)) or \
                _norm(os.path.realpath(src)) != _norm(src):
            fail("source is a link or junction — refusing to move it")
            continue
        if not any(_under(src, r) for r in roots):
            fail("outside every configured folder — refusing to move it")
            continue
        if _under(dest_dir, src):
            fail("the archive is inside this folder — that would destroy it")
            continue
        if _norm(os.path.dirname(src)) == _norm(dest_dir):
            fail("already in the archive")
            continue

        before = _audio_count(src)
        try:
            target = _unique_dest(dest_dir, name)
            row["dest"] = target
            same_fs = (os.stat(src).st_dev == os.stat(dest_dir).st_dev)
            if not same_fs:
                need = _dir_bytes(src)
                free = _free_bytes(dest_dir)
                row["bytes"] = need
                if need > free:
                    fail("not enough room: needs %.1f GB, %.1f GB free"
                         % (need / 1e9, free / 1e9))
                    continue
            if dry_run:
                row["ok"] = True
                row["reason"] = ("would move %d audio file(s)%s"
                                 % (before, "" if same_fs else " (across drives — a copy)"))
                results.append(row)
                continue
            # shutil.move is a rename on the same filesystem (instant) and a
            # copy+delete across one.  Both are correct here; the undo log
            # records where it went either way.
            shutil.move(src, target)
            after = _audio_count(target)
            row["files"] = after
            if after != before:
                row["reason"] = ("ARRIVED SHORT: %d audio file(s) went in, %d came "
                                 "out — left at %s" % (before, after, target))
                _log_move("move", src, target, False, row["reason"])
            else:
                row["ok"] = True
                row["reason"] = "moved %d audio file(s)" % after
                _log_move("move", src, target, True)
        except Exception as exc:
            row["reason"] = str(exc)
            if not dry_run:
                _log_move("move", src, row["dest"], False, str(exc))
            results.append(row)
            continue
        results.append(row)
    return {"ok": bool(results) and all(r["ok"] for r in results),
            "results": results, "dest": dest_dir, "dry_run": dry_run}


def undo_move(dest: str) -> tuple[bool, str]:
    """Put one moved folder back where it came from."""
    for row in move_log(limit=5000):
        if row["action"] != "move" or _norm(row["dest"]) != _norm(dest):
            continue
        if not os.path.isdir(row["dest"]):
            return False, "the moved folder is no longer there"
        if os.path.exists(row["src"]):
            return False, "something is already back at the original path"
        try:
            os.makedirs(os.path.dirname(row["src"]), exist_ok=True)
            shutil.move(row["dest"], row["src"])
            _log_move("undo", row["dest"], row["src"], True)
            return True, "put back"
        except Exception as exc:
            _log_move("undo", row["dest"], row["src"], False, str(exc))
            return False, str(exc)
    return False, "no move logged for that folder"


def _stale(st: dict, scan_: dict) -> str:
    """Why the shown results no longer describe the current setup, or "".

    Every row in the table and every export is read from the LAST SCAN. Change
    the label or the folders and the numbers keep being displayed as though
    nothing happened — and Move acts on paths from that stale picture. Saying so
    is the difference between a stale answer and a wrong one.
    """
    if int(scan_.get("label_id") or 0) != int(st["label_id"]):
        return "these results are for a different label — rescan"
    now = {(_norm(r["path"]), r.get("role")) for r in st.get("roots") or []}
    then = {(_norm(r["path"]), r.get("role"))
            for r in scan_.get("roots") or []}
    if now != then:
        return "the folders have changed since this scan — rescan"
    gone = [r["path"] for r in st.get("roots") or [] if not os.path.isdir(r["path"])]
    if gone:
        return "a folder is not readable right now: " + gone[0]
    return ""


# ─── what the tab asks for on every poll ──────────────────────────────────────
def status(cfg: dict) -> dict:
    st = load_state()
    scan_ = last_scan()
    roots = []
    for r in st["roots"]:
        roots.append({**r, "exists": os.path.isdir(r["path"])})
    return {
        "label_id": st["label_id"], "label_name": st["label_name"],
        "roots": roots,
        "archive": st.get("archive", ""),
        "archive_exists": bool(st.get("archive")) and os.path.isdir(st["archive"]),
        "catalogue": catalogue_status(st),
        "has_token": bool(_token(cfg)),
        "scan": ({"when": scan_["when"], "summary": scan_["summary"],
                  "rows": len(scan_["rows"]), "orphans": len(scan_["orphans"]),
                  "label_id": scan_.get("label_id"),
                  "stale": _stale(st, scan_)} if scan_ else None),
        "moves": len(move_log(limit=5000)),
    }
