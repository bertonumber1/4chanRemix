#!/usr/bin/env python3
"""Fix jumbled/duplicate tracks in Bit Music's owned multi-CD releases.

Standalone CLI, not wired into web_ui.py. Some releases in the owned archive
are a merge of two rips that a previous manual sort pass only half-finished:
CD1's folder holds the same tracks under two or three different naming
schemes, with only a track or two actually split out into CD2/CD3 where they
belong (see memory project_music_organiser_multicdi_jumbled_2026-09-14).

This never trusts the files' own numbering to decide where a track belongs —
that numbering is the actual root cause. A release is only auto-fixed when
its CDn split can be verified against a .cue sheet already in the folder, or
(the more common case, since most of these releases have no cue at all)
against Discogs' own per-disc tracklist for the release the Labels tab has
already matched. Anything that can't be verified either way — including a
release that turns out to be two genuinely different alternate rips, not a
sort mistake — is left alone and reported, never guessed at.

Bit Music only. Deliberately hardcoded to this one archive, not parameterized
by --root, so a typo or a copy-paste can't point it at another label.

Reuses label_ref.py (title matching, Discogs client, disc-folder detection)
and label_panel.py (the move/undo log the Labels tab already writes to, so
every move this script makes is undoable through the running web app's
existing /api/label/undo).
"""
import argparse
import os
import re
import shutil
import sys

# config.py (holds the Discogs token) lives under zzzzScriptstuff/, which
# only ever lands on sys.path because web_ui.py's own bootstrap puts it
# there. Standalone CLI use (--scan/--plan/--apply without the web server
# running) needs the same bootstrap, or _discogs_client() silently falls
# back to no token (its `except Exception` swallows the ModuleNotFoundError).
_HERE = os.path.dirname(os.path.abspath(__file__))
for _sub in ("zzzzScriptstuff", "scriptstuff"):
    _d = os.path.join(_HERE, _sub)
    if os.path.isdir(_d) and _d not in sys.path:
        sys.path.insert(0, _d)
        break

import label_ref as L
import label_panel as P
import fingerprint_kit as FK

# EAC-era filenames in this archive carry accented Spanish/Catalan titles;
# the default Windows console codepage mangles them on print otherwise.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

BIT_MUSIC_LABEL_ID = 10663
DEFAULT_ROOT = r"D:\Bit Music [FLAC-WAV]"

# Manually sorted by the owner already — never scan or touch these.
EXCLUDED_RELEASES = {
    "(10-076) Bit Music Greatest Hits Vol. 1 (2012)",
    "(10-077) Bit Music Greatest Hits Vol. 2 (2012)",
    "(10-078) Bit Music Greatest Hits Vol. 3 (2012)",
    "(10-079) Bit Music Greatest Hits Vol. 4 (2012)",
    "(10-080) Bit Music Greatest Hits Vol. 5 (2012)",
}

_TRACK_RE = re.compile(r'^\s*TRACK\s+(\d+)\s+AUDIO', re.I)
_TITLE_RE = re.compile(r'^\s*TITLE\s+"([^"]*)"', re.I)
_PERFORMER_RE = re.compile(r'^\s*PERFORMER\s+"([^"]*)"', re.I)
_DISC_NUM_RE = re.compile(
    r'\b(?:cd|disc|disco|disk|dvd|vol|volume|part|parte)\D{0,3}(\d+)\b', re.I)
_LEADING_NUM_RE = re.compile(r'^\d{1,3}\s*[-.]?\s*')


# ─── .cue parsing (nothing in the repo parses past a cue's header today) ──────
def _read_text_flex(path: str) -> str:
    """EAC-era cues from this archive are not reliably UTF-8 (accented
    Spanish/Catalan titles show up mojibake'd when force-read as UTF-8) —
    try UTF-8 first, fall back to cp1252 (what EAC on Windows actually wrote)."""
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with open(path, encoding=enc) as fh:
                return fh.read()
        except UnicodeDecodeError:
            continue
    with open(path, encoding="latin-1") as fh:
        return fh.read()


def parse_cue_tracks(cue_path: str) -> list:
    """[{'track_no', 'title', 'performer'}] in file order. Album-level
    TITLE/PERFORMER lines (before the first TRACK) are ignored on purpose —
    only per-track ones are wanted here."""
    tracks, cur = [], None
    for line in _read_text_flex(cue_path).splitlines():
        m = _TRACK_RE.match(line)
        if m:
            if cur:
                tracks.append(cur)
            cur = {"track_no": int(m.group(1)), "title": "", "performer": ""}
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
    if cur:
        tracks.append(cur)
    return tracks


def _find_cue_files(release_root: str) -> list:
    out = []
    for dp, _dirs, fs in os.walk(release_root):
        for f in fs:
            if f.lower().endswith(".cue"):
                out.append(os.path.join(dp, f))
    return out


def _cue_disc_number(cue_path: str, release_root: str):
    """Which CDn this cue is for, from the folder it sits in or its own
    filename. None when neither says — an ambiguous top-level cue, common
    in this archive (a bare "Album Title.cue" sitting beside CD1/CD2/CD3)."""
    rel_dir = os.path.dirname(os.path.relpath(cue_path, release_root))
    for part in rel_dir.split(os.sep):
        if L.is_disc_dir(part):
            m = re.search(r"\d+", part)
            if m:
                return int(m.group())
    m = _DISC_NUM_RE.search(os.path.basename(cue_path))
    return int(m.group(1)) if m else None


def cue_tracks_by_disc(release_root: str, discs: list) -> tuple:
    """(dict[disc] -> [{"title", "duration"}, ...], notes). Two cues resolving to the same
    disc are trusted only if their tracklists roughly agree — the Pont Aeri
    case in this archive has two FULLY DIFFERENT cues (a 2-track continuous
    DJ-session split vs. a 14-track individual-song listing) sitting in one
    folder, and that disc's cue must be treated as untrustworthy rather than
    picking one arbitrarily."""
    by_disc, unresolved, notes = {}, [], []
    for c in _find_cue_files(release_root):
        tracks = parse_cue_tracks(c)
        if not tracks:
            continue
        d = _cue_disc_number(c, release_root)
        if d is None:
            unresolved.append((c, tracks))
        else:
            by_disc.setdefault(d, []).append((c, tracks))

    if len(unresolved) == 1:
        # A single cue with no disc marker of its own, sitting loose next to
        # CD1/CD2/CD3, is conventionally the first disc's cue (subsequent
        # discs in this archive are the ones that get an explicit CD2/CD3
        # marker) — but only default to that when disc 1 isn't already
        # covered by another, disc-marked cue, and otherwise only when
        # exactly one disc remains uncovered so there's no real ambiguity.
        missing = [d for d in discs if d not in by_disc]
        target = 1 if 1 in missing else (missing[0] if len(missing) == 1 else None)
        if target is not None:
            by_disc.setdefault(target, []).append(unresolved.pop())

    if unresolved:
        names = ", ".join(os.path.basename(c) for c, _ in unresolved)
        notes.append(f"{len(unresolved)} .cue file(s) with no disc marker and no single "
                     f"missing disc to assign them to — ignoring: {names}")

    # duration is always None for a cue-sourced track: parse_cue_tracks()
    # only reads TITLE/PERFORMER, never the INDEX timestamps a duration
    # would have to come from. A caller wanting duration-verified recovery
    # across discs falls back to comparing against the sibling occurrence's
    # own winner instead (see build_fix_plan) — that still works, it just
    # cannot come from the cue itself.
    resolved = {}
    for d, entries in by_disc.items():
        if len(entries) == 1:
            resolved[d] = [{"title": t["title"], "duration": None}
                           for t in entries[0][1] if t["title"]]
            continue
        title_sets = [set(L.norm(t["title"]) for t in tracks if t["title"])
                      for _, tracks in entries]
        base = title_sets[0]
        agree = all(len(base & s) / max(1, len(base | s)) >= 0.5
                    for s in title_sets[1:])
        if agree:
            resolved[d] = [{"title": t["title"], "duration": None}
                           for t in entries[0][1] if t["title"]]
        else:
            notes.append(f"CD{d}: {len(entries)} conflicting .cue files "
                          f"in this release — ignoring cue for this disc")
    return resolved, notes


# ─── Discogs fallback ──────────────────────────────────────────────────────────
def _discogs_client():
    try:
        from config import load_config
        cfg = load_config()
    except Exception:
        cfg = {}
    token = P._token(cfg)
    return L.Discogs(token) if token else None


def discogs_tracks_by_disc(folder_name: str, tls: dict, discogs) -> tuple:
    """(dict[disc] -> [{"title", "duration"}, ...], note). Looks up the release the Labels
    tab has ALREADY matched for this folder (by catalogue number embedded in
    the folder name) rather than doing a fresh fuzzy search — a release this
    script can't already identify confidently has no business being
    auto-fixed off a guessed Discogs match."""
    catno = L.folder_catno(folder_name)
    if not catno:
        return {}, "no catalogue number in the folder name"
    release_id = None
    for k in sorted(L.catno_keys(catno)):
        got = tls.get(k)
        if got and got.get("id"):
            release_id = got["id"]
            break
    if release_id is None:
        return {}, "release not matched in the Bit Music catalogue cache"
    if discogs is None:
        return {}, "no Discogs token configured"
    try:
        return discogs.tracklist_by_disc(int(release_id)), ""
    except L.DiscogsError as exc:
        return {}, f"Discogs lookup failed: {exc}"


# ─── source-of-truth selection, per disc ───────────────────────────────────────
def disc_numbers(release_root: str) -> list:
    out = []
    try:
        entries = os.listdir(release_root)
    except OSError:
        return out
    for name in entries:
        full = os.path.join(release_root, name)
        if os.path.isdir(full) and L.is_disc_dir(name):
            m = re.search(r"\d+", name)
            if m:
                out.append(int(m.group()))
    return sorted(set(out))


def disc_dir_path(release_root: str, disc: int):
    for name in os.listdir(release_root):
        full = os.path.join(release_root, name)
        if os.path.isdir(full) and L.is_disc_dir(name):
            m = re.search(r"\d+", name)
            if m and int(m.group()) == disc:
                return full
    return None


def build_disc_sources(release_root: str, folder_name: str, discs: list,
                       tls: dict, discogs) -> tuple:
    cue_by_disc, reasons = cue_tracks_by_disc(release_root, discs)
    need_discogs = [d for d in discs if d not in cue_by_disc]
    dg_by_disc, dg_note = ({}, "")
    if need_discogs:
        dg_by_disc, dg_note = discogs_tracks_by_disc(folder_name, tls, discogs)

    sources = {}
    for d in discs:
        if d in cue_by_disc:
            sources[d] = {"kind": "cue", "tracks": cue_by_disc[d]}
        elif d in dg_by_disc:
            sources[d] = {"kind": "discogs", "tracks": dg_by_disc[d]}
        else:
            sources[d] = {"kind": "none", "tracks": []}
            reasons.append(f"CD{d}: no usable cue or Discogs tracklist"
                           + (f" ({dg_note})" if dg_note else ""))
    return sources, reasons


# ─── matching a source tracklist against the files actually on disk ───────────
def guess_titles_from_filename(path: str) -> set:
    base = os.path.splitext(os.path.basename(path))[0]
    base = _LEADING_NUM_RE.sub("", base).strip(" -.")
    out = {base}
    if " - " in base:
        out.add(base.split(" - ", 1)[1].strip())
    return out


def audio_pref_key(path: str) -> int:
    """0 = keep this format over anything scoring 1 (FLAC/APE/WV/ALAC over
    the WAV/AIFF the archive is meant to be converted away from)."""
    return 1 if path.lower().endswith(L.NEEDS_CONVERT_EXT) else 0


# Lossless rips of the SAME source rarely differ by more than a second or
# two of container/encoder padding — anything within this window is treated
# as "the same recording" for duplicate recovery. Anything past it is
# treated as a different edit/length and left alone (see the real (32-717)
# case that motivated this: two "Chumitraxx" duplicates floating around
# were 388s against CD1's genuine 161s copy — same title, not the same
# song, and duration is the only thing here that catches that).
_DURATION_TOLERANCE_S = 3.0

# Loaded once per build_fix_plan() call (not per-comparison) and saved back
# at the end — a release only ever has a handful of candidates that reach
# this check, so this is cheap, but reusing the on-disk cache across calls
# still avoids re-running fpcalc on the same files every time a release is
# re-planned.
_fp_cache = None


def _get_fp_cache() -> dict:
    global _fp_cache
    if _fp_cache is None:
        _fp_cache = FK.load_cache()
    return _fp_cache


def build_fix_plan(release_root: str, discs: list, sources: dict,
                   extra_reasons: list = None) -> dict:
    """Per expected OCCURRENCE (a (disc, title) pair — the same title can
    legitimately appear on two different discs of a VA compilation), decide
    which physical file answers for it.

    Processes every occurrence across every disc as one pool rather than
    disc-by-disc-in-isolation: the old per-disc-in-order version marked
    EVERY file matching a title as "consumed" the moment the FIRST disc
    asked for it, including the spare copies it was about to file as
    duplicates — so a title genuinely repeated on a second disc always
    reported "no file found" even when a spare was sitting right there,
    about to be discarded as a "duplicate" of the first disc's copy. Found
    2026-09-16 on a real release ((32-717) Limite The Sound), where it
    turned out NOT to be recoverable (the spare was a different-length
    edit) — but the failure mode is real and archive-wide, not specific to
    that one release.

    A later occurrence that finds no fresh match may still be recoverable
    from an EARLIER occurrence's own spare copies — but only when a
    candidate's actual audio duration matches closely enough to be
    confident it is the same recording, never on title alone. The target
    duration is Discogs' own stated duration for this occurrence when
    Discogs bothered to fill it in (often it did not), else the sibling
    occurrence's own winning file's real duration — still a genuine signal:
    if disc A's accepted copy and the spare are the same length, they are
    almost certainly the same rip. A recovered entry is marked
    "recovered_from_duplicate": the caller (apply_fix/apply_plan) uses that
    to know the file must be MOVED into the new disc's folder as a proper
    resident, not left in place — it is answering for a real, distinct
    tracklist entry now, not sitting there as a spare.
    """
    all_files = L.audio_files(release_root)
    file_titles = {f: guess_titles_from_filename(f) for f in all_files}
    disc_dirs = {d: disc_dir_path(release_root, d) for d in discs}
    reasons = list(extra_reasons or [])

    occurrences = []
    for d in discs:
        for t in sources[d]["tracks"]:
            title = t["title"] if isinstance(t, dict) else t
            duration = t.get("duration") if isinstance(t, dict) else None
            artist = t.get("artist") if isinstance(t, dict) else None
            occurrences.append({"disc": d, "title": title, "duration": duration, "artist": artist})

    consumed = set()
    resolved = []    # occurrences with a winner (+ provisional losers) assigned
    unresolved = []  # occurrences that found no fresh candidate at all

    for occ in occurrences:
        d, title = occ["disc"], occ["title"]
        candidates = [f for f in all_files if f not in consumed
                     and any(L.title_match(title, c) for c in file_titles[f])]
        if not candidates:
            unresolved.append(occ)
            continue
        candidates.sort(key=audio_pref_key)
        best = audio_pref_key(candidates[0])
        winners = [c for c in candidates if audio_pref_key(c) == best]
        if len(winners) > 1:
            # A tie on format alone (two FLACs, say) is not really
            # ambiguous when exactly one of them already sits in THIS
            # disc's own folder — that one is the authentic copy, the
            # other is the duplicate that leaked into another disc,
            # which is exactly the failure mode this script exists to
            # fix. Only genuinely flag it when that doesn't settle it.
            home = disc_dirs.get(d)
            in_home = [w for w in winners
                      if home and P._norm(os.path.dirname(w)) == P._norm(home)]
            if len(in_home) == 1:
                winners = in_home
        if len(winners) > 1:
            reasons.append(
                f'CD{d}: {len(winners)} equally good files for "{title}" — '
                + ", ".join(os.path.basename(w) for w in winners))
            # Too many candidates, not zero — a human pick, not a "go find
            # this track" case. Keep it out of the generic "no file found"
            # reason and the missing-tracks list below (nothing to search
            # Soulseek for here), but still let the duration-recovery pass
            # have a look — a sibling disc's duration can sometimes settle
            # a tie that format/location alone could not.
            occ["ambiguous"] = True
            unresolved.append(occ)
            continue
        winner = winners[0]
        occ["winner"] = winner
        # ALL matches are consumed here, not just the winner — a later
        # occurrence of the SAME title must NOT be able to grab one of
        # these directly in this same pass with no duration check at all.
        # It can still reclaim one through the duration-verified recovery
        # pass below, which is the only path allowed to promote a
        # provisional loser into someone else's winner.
        occ["losers"] = [c for c in candidates if c != winner]
        consumed.update(candidates)
        resolved.append(occ)

    fp_cache_dirty = False
    for occ in list(unresolved):
        title, target_duration = occ["title"], occ["duration"]
        sibling = next((r for r in resolved if r["title"] == title), None)
        if sibling is None:
            continue
        if target_duration is None:
            target_duration = L.audio_duration(sibling["winner"])
        if target_duration is None:
            continue
        for spare in list(sibling["losers"]):
            dur = L.audio_duration(spare)
            if dur is None or abs(dur - target_duration) > _DURATION_TOLERANCE_S:
                continue
            # Duration agreement alone is the historical bar (see the note
            # above _DURATION_TOLERANCE_S) but two DIFFERENT songs can
            # coincidentally land within 3s of each other. Where a real
            # fingerprint comparison is available on this machine AND
            # actually produces a verdict, require it to agree too, never
            # on its own (see fingerprint_kit.py's own docstring on why
            # audio similarity alone is never enough — an Original Mix and
            # its Extended Mix can score above SAME over a long window
            # despite being different tracks; duration is what tells them
            # apart). compare_recordings() returning None (not False) —
            # fpcalc couldn't get a usable fingerprint from one of the
            # files at all, e.g. it's silent or corrupt — means this check
            # has nothing to add; duration already vouched for the match,
            # so that stays enough, exactly as it was before fingerprinting
            # existed. Only a real disagreement (False) blocks it.
            if FK.AVAILABLE:
                cache = _get_fp_cache()
                verdict = FK.compare_recordings(sibling["winner"], spare, cache)
                if verdict is False:
                    continue
                if verdict is True:
                    fp_cache_dirty = True
            sibling["losers"].remove(spare)
            consumed.add(spare)
            occ["winner"] = spare
            occ["losers"] = []
            occ["recovered_from_duplicate"] = True
            resolved.append(occ)
            unresolved.remove(occ)
            break
    if fp_cache_dirty:
        FK.save_cache(_fp_cache)

    missing = []
    for occ in unresolved:
        if occ.get("ambiguous"):
            continue   # already has its own "equally good files" reason above
        reasons.append(f'CD{occ["disc"]}: no file found for "{occ["title"]}"')
        missing.append({"disc": occ["disc"], "title": occ["title"], "artist": occ.get("artist") or ""})

    disc_plan = {d: [] for d in discs}
    for occ in resolved:
        entry = {"expected_title": occ["title"], "winner": occ["winner"],
                 "losers": occ["losers"]}
        if occ.get("recovered_from_duplicate"):
            entry["recovered_from_duplicate"] = True
        disc_plan[occ["disc"]].append(entry)
    for d in discs:
        order = {(t["title"] if isinstance(t, dict) else t): i
                for i, t in enumerate(sources[d]["tracks"])}
        disc_plan[d].sort(key=lambda e: order.get(e["expected_title"], 0))

    all_losers = {f for r in resolved for f in r["losers"]}
    leftover = [f for f in all_files if f not in consumed and f not in all_losers]
    if leftover:
        shown = ", ".join(os.path.basename(f) for f in leftover[:5])
        more = " ..." if len(leftover) > 5 else ""
        reasons.append(f"{len(leftover)} file(s) match no expected track: {shown}{more}")

    status = "auto_fixable" if not reasons else "needs_review"
    return {"status": status, "reasons": reasons, "disc_plan": disc_plan,
            "sources": {d: sources[d]["kind"] for d in discs}, "missing": missing}


def build_plan_for(name: str, tls: dict, discogs) -> dict:
    if name in EXCLUDED_RELEASES:
        return {"status": "excluded",
                "reasons": ["manually sorted by the owner already — never touched"],
                "disc_plan": {}, "sources": {}, "missing": []}
    release_root = os.path.join(DEFAULT_ROOT, name)
    discs = disc_numbers(release_root)
    if len(discs) < 2:
        return {"status": "not_multicd", "reasons": ["fewer than 2 CDn folders"],
                "disc_plan": {}, "sources": {}, "missing": []}
    sources, reasons = build_disc_sources(release_root, name, discs, tls, discogs)
    return build_fix_plan(release_root, discs, sources, extra_reasons=reasons)


# ─── scanning for jumbled releases ─────────────────────────────────────────────
def _scan_key(filename: str) -> str:
    base = os.path.splitext(os.path.basename(filename))[0]
    return L.norm(_LEADING_NUM_RE.sub("", base))


def scan_multicd_releases(root: str = DEFAULT_ROOT) -> list:
    """Releases with 2+ CDn subfolders where some normalized filename
    (leading track number stripped) appears more than once anywhere under
    the release — the same detector shape as last session's survey."""
    flagged = []
    for name in sorted(os.listdir(root)):
        if name in EXCLUDED_RELEASES:
            continue
        full = os.path.join(root, name)
        if not os.path.isdir(full) or len(disc_numbers(full)) < 2:
            continue
        seen = {}
        for f in L.audio_files(full):
            key = _scan_key(f)
            if key:
                seen[key] = seen.get(key, 0) + 1
        if any(n > 1 for n in seen.values()):
            flagged.append(name)
    return flagged


# ─── apply + verify (move, never delete; every move undoable) ────────────────
# One shared, centralized holding root for the whole archive (2026-09-16) —
# NOT a sibling of DEFAULT_ROOT any more, and NOT inside this app's own
# git-tracked repo either (real audio duplicates there risk `git clean`,
# accidental staging, or ending up mixed into the Linux Chromebox
# deployment, which tracks this same repo via `git pull`). Same
# ~/.local/share/music-organiser/ app-data convention library.db and
# web_ui.log already use, and the SAME base cd_tools.py's own
# _review_root() writes to — apply_fix()'s existing rel_dir (relative to
# DEFAULT_ROOT, so it already starts with the release name) still nests
# each release correctly under here without any other change needed. A
# plain module attribute, not folded into the function below, so a test
# can monkeypatch it the same way label_panel_test.py already does for
# P.CACHE_DIR/P.MOVELOG, instead of ever writing into the real one.
HOLDING_ROOT = os.path.expanduser(os.path.join("~", ".local", "share", "music-organiser", "cd_review"))


def _holding_root() -> str:
    return HOLDING_ROOT


def _move_file(src: str, dest_dir: str, dry_run: bool, action: str) -> dict:
    row = {"src": src, "dest": "", "ok": False, "reason": ""}
    if not os.path.isfile(src):
        row["reason"] = "source file is gone"
        return row
    if os.path.islink(src) or P._norm(os.path.realpath(src)) != P._norm(src):
        row["reason"] = "source is a link — refusing to move it"
        return row
    if not P._under(src, DEFAULT_ROOT):
        row["reason"] = "outside the Bit Music archive — refusing to move it"
        return row
    try:
        if dry_run:
            target = P._unique_dest(dest_dir, os.path.basename(src)) \
                if os.path.isdir(dest_dir) else os.path.join(dest_dir, os.path.basename(src))
            row["dest"] = target
            row["ok"] = True
            row["reason"] = "would move"
            return row
        os.makedirs(dest_dir, exist_ok=True)
        target = P._unique_dest(dest_dir, os.path.basename(src))
        row["dest"] = target
        shutil.move(src, target)
        row["ok"] = True
        row["reason"] = "moved"
        P._log_move(action, src, target, True)
    except Exception as exc:
        row["reason"] = str(exc)
        P._log_move(action, src, row["dest"], False, str(exc))
    return row


def apply_fix(name: str, plan: dict, dry_run: bool = True) -> dict:
    if plan["status"] != "auto_fixable":
        return {"ok": False, "reason": "not auto-fixable: " + "; ".join(plan["reasons"])}
    release_root = os.path.join(DEFAULT_ROOT, name)
    holding_root = _holding_root()
    results = []
    for d, entries in plan["disc_plan"].items():
        dest_dir = disc_dir_path(release_root, d)
        if dest_dir is None:
            return {"ok": False, "reason": f"CD{d} folder not found on disk"}
        for e in entries:
            winner = e["winner"]
            if P._norm(os.path.dirname(winner)) != P._norm(dest_dir):
                results.append(_move_file(winner, dest_dir, dry_run, "move"))
            for loser in e["losers"]:
                rel_dir = os.path.relpath(os.path.dirname(loser), os.path.dirname(release_root))
                results.append(_move_file(loser, os.path.join(holding_root, rel_dir),
                                          dry_run, "dedupe-holding"))
    ok = all(r["ok"] for r in results)
    verify = {}
    if ok and not dry_run:
        for d, entries in plan["disc_plan"].items():
            dest_dir = disc_dir_path(release_root, d)
            actual = len(L.audio_files(dest_dir)) if dest_dir else 0
            if actual != len(entries):
                verify[f"CD{d}"] = f"expected {len(entries)} tracks, found {actual}"
    return {"ok": ok and not verify, "results": results, "verify": verify}


# ─── CLI ────────────────────────────────────────────────────────────────────────
def _print_plan(name: str, plan: dict):
    print(f"=== {name} ===")
    print("status:", plan["status"])
    for d, entries in plan.get("disc_plan", {}).items():
        kind = plan.get("sources", {}).get(d, "?")
        print(f"\nCD{d} (source: {kind}, {len(entries)} track(s)):")
        for e in entries:
            print(f'  "{e["expected_title"]}" <- {os.path.basename(e["winner"])}')
            for loser in e["losers"]:
                print(f"      duplicate -> holding: {os.path.basename(loser)}")
    if plan["reasons"]:
        label = "notes:" if plan["status"] == "auto_fixable" else "NOT auto-fixable because:"
        print("\n" + label)
        for r in plan["reasons"]:
            print(" -", r)


def _do_apply(name: str, plan: dict, dry_run: bool):
    if plan["status"] != "auto_fixable":
        print(f"SKIPPED ({plan['status']}): " + "; ".join(plan["reasons"]))
        return
    result = apply_fix(name, plan, dry_run=dry_run)
    for r in result.get("results", []):
        mark = "OK" if r["ok"] else "FAIL"
        print(f"  [{mark}] {os.path.basename(r['src'])} -> {r['dest']} ({r['reason']})")
    if result.get("verify"):
        print("  VERIFY MISMATCH:", result["verify"])
    print("  overall:", "OK" if result["ok"] else "FAILED")


def main():
    ap = argparse.ArgumentParser(
        description="Fix jumbled multi-CD releases in the Bit Music owned archive only.")
    ap.add_argument("--scan", action="store_true", help="list flagged releases")
    ap.add_argument("--plan", metavar="RELEASE_FOLDER", help="dry-run fix plan for one release")
    ap.add_argument("--apply", metavar="RELEASE_FOLDER", help="apply the fix for one release, for real")
    ap.add_argument("--apply-all-safe", action="store_true",
                    help="apply every currently auto-fixable flagged release")
    ap.add_argument("--undo", metavar="DEST_PATH", help="undo one logged move by its destination path")
    args = ap.parse_args()

    if args.undo:
        ok, msg = P.undo_move(args.undo)
        print(("OK: " if ok else "FAILED: ") + msg)
        return

    if not any([args.scan, args.plan, args.apply, args.apply_all_safe]):
        ap.print_help()
        return

    tls = L.LabelCache(P.CACHE_DIR, BIT_MUSIC_LABEL_ID).tracklists()
    discogs = _discogs_client()

    if args.scan:
        flagged = scan_multicd_releases()
        print(f"{len(flagged)} release(s) flagged in the Bit Music archive:")
        for name in flagged:
            print(" -", name)
        return

    if args.plan:
        _print_plan(args.plan, build_plan_for(args.plan, tls, discogs))
        return

    if args.apply:
        _do_apply(args.apply, build_plan_for(args.apply, tls, discogs), dry_run=False)
        return

    if args.apply_all_safe:
        for name in scan_multicd_releases():
            plan = build_plan_for(name, tls, discogs)
            print(f"\n=== {name} ===")
            _do_apply(name, plan, dry_run=False)


if __name__ == "__main__":
    main()
