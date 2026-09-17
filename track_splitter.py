"""Split ONE continuous audio file (a whole vinyl side or CD rip captured as
a single track) into its individual tracks.

Two ways to know where each track starts, tried by the caller in whatever
order it prefers — this module doesn't pick for you:

  CUE      A cue sheet with a single FILE entry and multiple TRACK/INDEX 01
           lines is the authoritative, sample-accurate source: it names the
           real cut points someone (usually the ripper) already marked.
  DISCOGS  When there's no such cue, Discogs' own per-track durations can
           be summed into cumulative start times instead — track 2 starts
           where track 1's stated length ends, and so on. This is an
           ESTIMATE, not a measurement: Discogs' stated lengths are rounded
           to the second and don't account for whatever silence or crossfade
           actually sits at the real boundary, so cuts land close but rarely
           frame-exact. Every function that uses this path says so in its
           result, never claims cue-sheet-grade precision.

Needs `ffmpeg` on PATH to actually cut anything; AVAILABLE is False and
`split_file()` refuses cleanly otherwise, same self-disabling pattern as
`fingerprint_kit.py` for fpcalc/libchromaprint.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

AVAILABLE = bool(shutil.which("ffmpeg"))

_FILE_RE = re.compile(r'^\s*FILE\s+"([^"]+)"', re.I)
_TRACK_RE = re.compile(r"^\s*TRACK\s+(\d+)\s+AUDIO", re.I)
_TITLE_RE = re.compile(r'^\s*TITLE\s+"([^"]*)"', re.I)
_PERFORMER_RE = re.compile(r'^\s*PERFORMER\s+"([^"]*)"', re.I)
_INDEX01_RE = re.compile(r"^\s*INDEX\s+01\s+(\d+):(\d+):(\d+)", re.I)


def _read_text_flex(path: str) -> str:
    """Same EAC-era-cue encoding fallback as multicd_dedupe.py's own
    _read_text_flex — kept separate rather than imported, this module has
    no other reason to depend on multicd_dedupe (Bit Music only) at all."""
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with open(path, encoding=enc) as fh:
                return fh.read()
        except UnicodeDecodeError:
            continue
    with open(path, encoding="latin-1") as fh:
        return fh.read()


def cue_to_seconds(mm: str, ss: str, ff: str) -> float:
    """Cue timestamps are MM:SS:FF — FF is CD frames, 75 per second, not
    a fraction of 100 like it looks."""
    return int(mm) * 60 + int(ss) + int(ff) / 75.0


def parse_continuous_cue(cue_path: str) -> dict:
    """{'ok', 'reason', 'source_file', 'tracks': [{'track_no','title',
    'performer','start'}]}.

    Only handles a cue describing ONE physical file split into many tracks
    — a cue with more than one FILE line means the tracks are already
    separate files (this archive's own cues are almost all this shape; see
    multicd_dedupe.py's parse_cue_tracks(), which only reads titles because
    that's the only cue shape it has ever needed to handle). That is a
    naming/matching job, not a splitting one — refuse it here rather than
    silently doing nothing useful with it.
    """
    files_seen = []
    tracks = []
    cur = None
    for line in _read_text_flex(cue_path).splitlines():
        m = _FILE_RE.match(line)
        if m:
            files_seen.append(m.group(1))
            continue
        m = _TRACK_RE.match(line)
        if m:
            if cur:
                tracks.append(cur)
            cur = {"track_no": int(m.group(1)), "title": "", "performer": "", "start": None}
            continue
        if cur is None:
            continue
        m = _TITLE_RE.match(line)
        if m:
            cur["title"] = m.group(1).strip()
            continue
        m = _PERFORMER_RE.match(line)
        if m:
            cur["performer"] = m.group(1).strip()
            continue
        m = _INDEX01_RE.match(line)
        if m:
            cur["start"] = cue_to_seconds(*m.groups())
    if cur:
        tracks.append(cur)

    if len(files_seen) != 1:
        return {"ok": False,
                "reason": f"cue references {len(files_seen)} file(s), not one — "
                          "already split into per-track files, nothing to cut",
                "source_file": "", "tracks": []}
    missing = [t["track_no"] for t in tracks if t["start"] is None]
    if missing:
        return {"ok": False,
                "reason": f"track(s) {missing} have no INDEX 01 timestamp",
                "source_file": files_seen[0], "tracks": []}
    if not tracks:
        return {"ok": False, "reason": "no TRACK entries in this cue",
                "source_file": files_seen[0], "tracks": []}
    return {"ok": True, "reason": "", "source_file": files_seen[0], "tracks": tracks}


def split_points(tracks: list, total_duration: float) -> list:
    """[{'track_no','title','performer','start','end'}] — pairs each
    track's own start with the NEXT track's start (or total_duration for
    the last one). `tracks` must already be in file order with a 'start'
    each — parse_continuous_cue()'s or durations_to_tracks()'s output."""
    points = []
    for i, t in enumerate(tracks):
        end = tracks[i + 1]["start"] if i + 1 < len(tracks) else total_duration
        points.append({**t, "end": end})
    return points


def durations_to_tracks(discogs_tracks: list) -> dict:
    """{'ok','reason','tracks':[{'track_no','title','performer','start'}]}
    from Discogs' own per-track durations (the same {"title","duration",
    "artist"} shape label_ref.Discogs.tracklist_by_disc() already
    produces), by summing them into cumulative start times.

    Refuses outright if ANY track is missing a duration rather than
    computing what it can: offsets are cumulative, so a gap at track 3
    makes every start time from there on a guess piled on a guess, not an
    estimate — exactly the kind of silent-guess this app avoids
    everywhere else (see multicd_dedupe.py's own title_match()/duration
    checks, which refuse rather than guess under the same principle)."""
    missing = [i + 1 for i, t in enumerate(discogs_tracks)
              if not (t.get("duration") if isinstance(t, dict) else None)]
    if missing:
        return {"ok": False,
                "reason": f"Discogs has no duration for track(s) {missing} — "
                          "cannot compute reliable start times past a gap",
                "tracks": []}
    out, start = [], 0.0
    for i, t in enumerate(discogs_tracks):
        title = t["title"] if isinstance(t, dict) else t
        artist = t.get("artist") if isinstance(t, dict) else None
        out.append({"track_no": i + 1, "title": title, "performer": artist or "",
                    "start": start})
        start += float(t["duration"])
    return {"ok": True, "reason": "", "tracks": out}


# ─── cutting ────────────────────────────────────────────────────────────────
_ILLEGAL_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00]')


def _safe_track_filename(track_no: int, title: str, ext: str) -> str:
    name = f"{track_no:02d} - {title}".strip(" -")
    return _ILLEGAL_CHARS_RE.sub("_", name) + ext


def split_file(source: str, points: list, out_dir: str, dry_run: bool = True) -> dict:
    """Cut `source` at each point in `points` (as returned by
    split_points()) into `out_dir`, named "NN - Title.<ext>". Re-encodes
    through ffmpeg's own codec for the source's container (flac for FLAC,
    pcm for WAV) rather than stream-copying — both are lossless, and
    stream-copy cuts on some containers land on the nearest keyframe
    instead of the exact requested sample. Never touches or deletes the
    source file.

    dry_run=True (the default, matching every other CD Tools action in
    this app) only reports what WOULD be written."""
    if not AVAILABLE:
        return {"ok": False, "reason": "ffmpeg not found on PATH", "written": []}
    if not os.path.isfile(source):
        return {"ok": False, "reason": f"source file not found: {source}", "written": []}
    ext = os.path.splitext(source)[1].lower()
    codec = {".flac": "flac", ".wav": "pcm_s16le", ".aiff": "pcm_s16le", ".aif": "pcm_s16le"}.get(ext)
    if codec is None:
        return {"ok": False,
                "reason": f"don't know a lossless codec for {ext} — only .flac/.wav/.aiff are handled",
                "written": []}

    planned = []
    for p in points:
        out_name = _safe_track_filename(p["track_no"], p["title"] or f"Track {p['track_no']}", ext)
        planned.append({"track_no": p["track_no"], "title": p["title"],
                        "start": p["start"], "end": p["end"],
                        "dest": os.path.join(out_dir, out_name)})

    if dry_run:
        return {"ok": True, "reason": "", "written": [], "planned": planned}

    os.makedirs(out_dir, exist_ok=True)
    written, failed = [], []
    for p in planned:
        duration = p["end"] - p["start"]
        cmd = ["ffmpeg", "-y", "-nostdin", "-ss", f"{p['start']:.3f}",
              "-i", source, "-t", f"{duration:.3f}", "-c:a", codec, p["dest"]]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (subprocess.TimeoutExpired, OSError) as exc:
            failed.append({"dest": p["dest"], "reason": str(exc)})
            continue
        if r.returncode != 0 or not os.path.isfile(p["dest"]):
            failed.append({"dest": p["dest"], "reason": r.stderr.strip()[:300]})
            continue
        written.append(p["dest"])

    ok = not failed
    reason = "" if ok else f"{len(failed)} track(s) failed to cut"
    return {"ok": ok, "reason": reason, "written": written, "failed": failed, "planned": planned}
