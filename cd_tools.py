"""Direct tab's "CD Tools" — pick any folder (not tied to a Labels-tab
tracked root or the Bit Music catalogue), check its tracks against a .cue
sheet or a Discogs release, auto-arrange a jumbled multi-disc release, tag
files, fetch artwork, and move/copy/delete — same move-never-delete safety
net the rest of the app uses.

Reuses multicd_dedupe.py's cue-parsing and disc-matching engine: every
function there that matters for this (cue_tracks_by_disc, disc_numbers,
disc_dir_path, build_fix_plan, guess_titles_from_filename, audio_pref_key)
already takes release_root as a plain parameter — it is only the CLI/
archive-scan wrapper around them (build_plan_for, scan_multicd_releases,
apply_fix, DEFAULT_ROOT) that is Bit-Music-specific. This module is the
same engine, generalised: an explicit Discogs release id from the caller
instead of the Bit Music catalogue-number cache, and a review/holding
folder next to whatever root was picked instead of a hardcoded archive path.

Also reuses label_panel.py's move/undo-log primitives (same label_moves.tsv,
same /api/label/undo) via the same cross-module reuse multicd_dedupe.py
already established (import label_panel as P; P._norm, P._unique_dest, ...).
"""
from __future__ import annotations

import os
import re
import shutil

import label_ref as L
import label_panel as P
import multicd_dedupe as mcd
import track_splitter as ts

_RELEASE_RE = re.compile(r"(?:discogs\.com/)?release/(\d+)", re.I)
_MASTER_RE = re.compile(r"(?:discogs\.com/)?master/(\d+)", re.I)


# ─── Discogs URL/ID parsing ─────────────────────────────────────────────────
def resolve_release_id(text: str, discogs) -> tuple[str, str]:
    """(release_id, error). Accepts a bare numeric id, a discogs.com
    release URL, or a master URL (resolved to its main release — same idea
    as Get-DiscogsMaster in the Dark-Decade BBCode script)."""
    text = (text or "").strip()
    if not text:
        return "", ""
    if text.isdigit():
        return text, ""
    m = _RELEASE_RE.search(text)
    if m:
        return m.group(1), ""
    m = _MASTER_RE.search(text)
    if m:
        if discogs is None:
            return "", "master URL given but no Discogs token configured"
        try:
            master = discogs._get(f"/masters/{m.group(1)}")
        except Exception as exc:
            return "", f"could not resolve master: {exc}"
        main = master.get("main_release")
        if not main:
            return "", "this master has no main release"
        return str(main), ""
    return "", "not a recognised Discogs release/master URL or id"


# ─── check tracks (cue + discogs) ───────────────────────────────────────────
def disc_numbers(root: str) -> list:
    return mcd.disc_numbers(root)


def scan_duplicates(root: str) -> dict:
    """Normalised-filename -> [paths], for every name that appears more
    than once anywhere under root. Same detector multicd_dedupe.py's
    archive-wide scan uses, just for one folder instead of a whole
    archive."""
    seen: dict = {}
    for f in L.audio_files(root):
        key = mcd._scan_key(f)
        if key:
            seen.setdefault(key, []).append(f)
    return {k: v for k, v in seen.items() if len(v) > 1}


def _disc_sources(root: str, discs: list, release_id: str, discogs) -> tuple:
    """Same shape as multicd_dedupe.build_disc_sources, but the Discogs
    half comes from an explicit release id the caller already resolved
    (resolve_release_id), not the Bit Music catalogue cache — an arbitrary
    picked folder has no such cache to look catalogue numbers up in."""
    cue_by_disc, reasons = mcd.cue_tracks_by_disc(root, discs)
    need_discogs = [d for d in discs if d not in cue_by_disc]
    dg_by_disc, dg_note = {}, ""
    if need_discogs:
        if not release_id:
            dg_note = "no Discogs release given"
        elif discogs is None:
            dg_note = "no Discogs token configured"
        else:
            try:
                dg_by_disc = discogs.tracklist_by_disc(int(release_id))
            except L.DiscogsError as exc:
                dg_note = f"Discogs lookup failed: {exc}"
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


def build_plan(root: str, release_id: str, discogs) -> dict:
    """'Check tracks': per disc, which file wins each expected track and
    which are duplicates — verified against .cue sheets first, and the
    given Discogs release for whatever a cue does not cover. Read-only,
    moves nothing.

    status is 'auto_fixable' (safe to Auto-arrange), 'needs_review' (shown
    but not offered for auto-apply — something could not be resolved
    confidently), or 'not_multidisc' (root has fewer than 2 CDn
    subfolders — Auto-arrange has nothing to sort, but Dupe check / Tag /
    Artwork below still apply)."""
    discs = mcd.disc_numbers(root)
    if len(discs) < 2:
        return {"status": "not_multidisc", "discs": discs,
                "reasons": ["fewer than 2 CDn subfolders here — nothing to sort"],
                "disc_plan": {}, "sources": {}, "missing": []}
    sources, reasons = _disc_sources(root, discs, release_id, discogs)
    plan = mcd.build_fix_plan(root, discs, sources, extra_reasons=reasons)
    plan["discs"] = discs
    return plan


# ─── auto-arrange (apply the plan) ──────────────────────────────────────────
# One shared, centralized base for every release's duplicates (2026-09-16),
# not a sibling folder next to every release you ever run this on. Two
# reasons it lives here and not next to a release root:
#   1. It stops "- CD_REVIEW" folders scattering across every drive you
#      point CD Tools at — one place to check, always.
#   2. It is NOT inside the app's own git-tracked repo either — real audio
#      duplicates there would risk `git clean`, accidental staging, or (on
#      the Linux Chromebox deployment, which tracks this same repo via
#      `git pull`) ending up mixed into a live service's code tree.
# Same ~/.local/share/music-organiser/ app-data convention library.db and
# web_ui.log already use, and the SAME base multicd_dedupe.py's own
# _holding_root() writes to. A plain module attribute (not folded into the
# function below) so tests can monkeypatch it the same way
# label_panel_test.py already does for P.CACHE_DIR/P.MOVELOG, instead of
# ever writing into the real one during a test run.
CD_REVIEW_BASE = os.path.expanduser(os.path.join("~", ".local", "share", "music-organiser", "cd_review"))


def _review_root(root: str) -> str:
    """Where this release's duplicates go — namespaced by release name
    under CD_REVIEW_BASE."""
    return os.path.join(CD_REVIEW_BASE, os.path.basename(root.rstrip(os.sep)))


def _move_file(root: str, src: str, dest_dir: str, dry_run: bool, action: str) -> dict:
    row = {"src": src, "dest": "", "ok": False, "reason": ""}
    if not os.path.isfile(src):
        row["reason"] = "source file is gone"
        return row
    if os.path.islink(src) or P._norm(os.path.realpath(src)) != P._norm(src):
        row["reason"] = "source is a link — refusing to move it"
        return row
    if not P._under(src, root):
        row["reason"] = "outside the chosen CD root — refusing to move it"
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


def apply_plan(root: str, plan: dict, dry_run: bool = True) -> dict:
    """'Auto-arrange': move each track's winning file into its correct CDn
    folder (if not already there) and every duplicate into a review folder
    next to root (never deleted). Only runs when plan['status'] is
    'auto_fixable' — build_plan() already refused to guess on anything
    ambiguous.

    Winner moves are logged with action="move" — the SAME action name
    move_log()/undo_move() key their undo eligibility on — so they show up
    in the Labels tab's Move log with a working Put-back button, no
    separate undo mechanism. Duplicate-to-review moves use a different
    action name on purpose: putting a duplicate back where it came from
    would just recreate the jumbled state this exists to fix — the correct
    next step for a reviewed duplicate is Keep it in the review folder or
    Delete it from there, not undo."""
    if plan.get("status") != "auto_fixable":
        return {"ok": False, "reason": "not auto-fixable: " + "; ".join(plan.get("reasons") or [])}
    review_root = _review_root(root)
    results = []
    for d, entries in plan["disc_plan"].items():
        dest_dir = mcd.disc_dir_path(root, d)
        if dest_dir is None:
            return {"ok": False, "reason": f"CD{d} folder not found on disk"}
        for e in entries:
            winner = e["winner"]
            if P._norm(os.path.dirname(winner)) != P._norm(dest_dir):
                results.append(_move_file(root, winner, dest_dir, dry_run, "move"))
            for loser in e["losers"]:
                # relative to root itself, not root's parent: review_root
                # already carries the release name (it is namespaced by
                # it), so a rel_dir that ALSO started with the release
                # name would double it up in the final path.
                rel_dir = os.path.relpath(os.path.dirname(loser), root)
                dest_dir_for_loser = review_root if rel_dir == "." else os.path.join(review_root, rel_dir)
                results.append(_move_file(root, loser, dest_dir_for_loser,
                                          dry_run, "cdtools-dupe-holding"))
    ok = all(r["ok"] for r in results)
    verify_ = {}
    if ok and not dry_run:
        for d, entries in plan["disc_plan"].items():
            dest_dir = mcd.disc_dir_path(root, d)
            actual = len(L.audio_files(dest_dir)) if dest_dir else 0
            if actual != len(entries):
                verify_[f"CD{d}"] = f"expected {len(entries)} tracks, found {actual}"
    return {"ok": ok and not verify_, "results": results, "verify": verify_, "review_root": review_root}


def remove_file(root: str, path: str, dry_run: bool = True) -> dict:
    """Manual 'Remove': move one file to the review folder, same
    destination apply_plan() sends auto-flagged duplicates to — logged as
    "cdtools-dupe-holding" too, same reasoning. 'Keep' has no function of
    its own: leaving a file where it is IS keeping it."""
    return _move_file(root, path, _review_root(root), dry_run, "cdtools-dupe-holding")


# ─── verify ──────────────────────────────────────────────────────────────────
def _purge_review(root: str) -> int:
    """Delete this release's ENTIRE review folder, permanently. Only ever
    called from verify() right after it has confirmed clean=True — by that
    point every file sitting in there has already been proven surplus
    (every legitimate occurrence of every expected track already has its
    own winner elsewhere, or this would not be clean), so there is nothing
    left for a human to review. Returns how many files were removed."""
    review_root = _review_root(root)
    if not P._under(review_root, CD_REVIEW_BASE) or not os.path.isdir(review_root):
        return 0
    count = len(L.audio_files(review_root))
    try:
        shutil.rmtree(review_root)
    except OSError:
        return 0
    P._log_move("cdtools-cleanup", review_root, "(deleted)", True,
                f"{count} file(s) purged after a clean verify")
    return count


def verify(root: str, release_id: str, discogs) -> dict:
    """Re-run the same checks build_plan()/scan_duplicates() do — for
    AFTER an arrange/tag pass, to confirm the result actually is clean
    rather than trusting apply_plan()'s own report.

    A clean result also purges this release's review folder — see
    _purge_review(). The folder is never hidden or special: while a
    release is NOT yet clean, it is an ordinary folder under
    ~/.local/share/music-organiser/cd_review/<release>/ you can open and
    look through any time (list_review() below, or just Explorer)."""
    dupes = scan_duplicates(root)
    plan = build_plan(root, release_id, discogs)
    clean = (not dupes) and (not plan.get("reasons")) \
        and plan.get("status") in ("auto_fixable", "not_multidisc")
    purged = _purge_review(root) if clean else 0
    return {"ok": True, "clean": clean, "duplicates": dupes, "plan": plan, "purged": purged}


def list_review(root: str) -> list:
    """Every file currently sitting in this release's review/holding
    folder — what apply_plan()/remove_file() have set aside so far.
    Doesn't touch anything, just reports."""
    review_root = _review_root(root)
    if not os.path.isdir(review_root):
        return []
    return sorted(L.audio_files(review_root))


# ─── tag ─────────────────────────────────────────────────────────────────────
def tag_release(root: str, release_id: str, discogs, dry_run: bool = True) -> dict:
    """Write catalog_number/artist/album/year onto every file under root,
    plus per-track title/track_number wherever a track can be matched to a
    file — reuses tag_writer.write_tags_to_file's only_missing=True, so an
    existing tag, right or wrong, is never touched. Same
    label_ref.match_tracks() one-file-answers-one-track matcher the Labels
    tab's own tag_incoming() already relies on."""
    from tag_writer import write_tags_to_file

    if not release_id:
        return {"ok": False, "reason": "no Discogs release given"}
    if discogs is None:
        return {"ok": False, "reason": "no Discogs token configured"}
    try:
        rel = discogs.release(int(release_id))
    except Exception as exc:
        return {"ok": False, "reason": f"could not fetch release: {exc}"}

    artists = rel.get("artists") or []
    artist_name = ", ".join(a.get("name", "").strip() for a in artists if a.get("name")).strip()
    various = (not artist_name) or artist_name.lower() in ("various", "various artists")
    labels = rel.get("labels") or []
    catno = (labels[0].get("catno") or "").strip() if labels else ""
    album = (rel.get("title") or "").strip()
    year = str(rel.get("year") or "").strip()
    release_tags = {k: v for k, v in {
        "catalog_number": catno, "album": album, "year": year,
        **({} if various else {"artist": artist_name}),
    }.items() if v}

    discs = mcd.disc_numbers(root)
    targets = []
    if discs:
        try:
            by_disc = discogs.tracklist_by_disc(int(release_id))
        except Exception:
            by_disc = {}
        for d in discs:
            dd = mcd.disc_dir_path(root, d)
            if dd:
                # tracklist_by_disc() entries are {"title","duration"} —
                # match_tracks() below only wants the titles.
                titles = [t["title"] for t in by_disc.get(d, []) if t.get("title")]
                targets.append((dd, titles))
    else:
        expected = [t.get("title", "").strip() for t in rel.get("tracklist", [])
                   if (t.get("type_") or "track") == "track" and t.get("title")]
        targets.append((root, expected))

    tagged = errors = 0
    for folder, expected in targets:
        info = L.folder_tracks(folder)
        file_paths = info.get("paths") or []
        matched = L.match_tracks(expected, info.get("ids") or []) if expected else {}
        track_tags_by_file = {}
        for ti, track_title in enumerate(expected):
            fi = matched.get(ti)
            if fi is not None and 0 <= fi < len(file_paths):
                track_tags_by_file[fi] = {"title": track_title, "track_number": str(ti + 1)}
        for fi, fpath in enumerate(file_paths):
            tags = dict(release_tags)
            tags.update(track_tags_by_file.get(fi, {}))
            if not tags:
                continue
            res = write_tags_to_file(fpath, tags, only_missing=True, dry_run=dry_run)
            if res.error:
                errors += 1
            elif res.written_fields:
                tagged += 1
    reason = ("would tag %d file(s)" % tagged) if dry_run else (
        "%d error(s) writing tags" % errors if errors else
        ("tagged %d file(s)" % tagged if tagged else "nothing to tag — every file already had these fields"))
    return {"ok": errors == 0, "tagged_files": tagged, "errors": errors, "dry_run": dry_run, "reason": reason}


# ─── artwork ─────────────────────────────────────────────────────────────────
def get_artwork(root: str, release_id: str, discogs, dry_run: bool = True) -> dict:
    """Same local-first-then-Discogs artwork fetch as label_panel.py's
    get_artwork, minus its Labels-tab scan-row coupling — this one takes an
    explicit release id instead. Saves a loose folder.jpg. Never touches an
    audio file, never overwrites artwork that is already there."""
    out = {"path": root, "ok": False, "reason": "", "source": ""}
    try:
        loose = any(n.lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))
                   for n in os.listdir(root))
    except OSError:
        loose = False
    if loose:
        out["ok"] = True
        out["source"] = "folder-image"
        out["reason"] = "already has a loose cover image — nothing to do"
        return out

    info = L.folder_tracks(root)
    picture = None
    file_paths = info.get("paths") or []
    for i, t in enumerate(info.get("tags") or []):
        if t.get("has_picture") and i < len(file_paths):
            picture = L.extract_local_picture(file_paths[i])
            if picture:
                out["source"] = "local"
                break

    if picture is None:
        if not release_id:
            out["reason"] = "no local artwork, and no Discogs release given"
            return out
        if discogs is None:
            out["reason"] = "no local artwork, and no Discogs token configured"
            return out
        try:
            images = discogs.release_images(int(release_id))
        except Exception as exc:
            out["reason"] = "no local artwork; Discogs fetch failed: %s" % exc
            return out
        img = next((im for im in images if im.get("type") == "primary"), None) \
            or (images[0] if images else None)
        url = (img or {}).get("uri") or (img or {}).get("resource_url") or ""
        if not url:
            out["reason"] = "no local artwork, and Discogs has none for this release"
            return out
        picture = L.download_image(url)
        if picture is None:
            out["reason"] = "no local artwork; Discogs image download failed"
            return out
        out["source"] = "discogs"

    data, mime = picture
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg"}.get(mime.lower(), ".jpg")
    dest = os.path.join(root, "folder" + ext)
    kb = len(data) // 1024
    if dry_run:
        out["ok"] = True
        out["reason"] = "would save %s (%d KB) from %s" % (os.path.basename(dest), kb, out["source"])
        return out
    try:
        tmp = dest + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
        out["ok"] = True
        out["reason"] = "saved %s (%d KB) from %s" % (os.path.basename(dest), kb, out["source"])
    except OSError as exc:
        out["reason"] = "found artwork (%s) but could not write %s: %s" % (out["source"], dest, exc)
    return out


# ─── move / copy the finished CD root ───────────────────────────────────────
def _safe_check(src: str, dest_dir: str) -> str:
    """Shared refusals for move_folder/copy_folder. Empty string = ok."""
    if not os.path.isdir(src):
        return "source folder does not exist"
    if os.path.islink(src.rstrip(os.sep)) or P._norm(os.path.realpath(src)) != P._norm(src):
        return "source is a link or junction — refusing to touch it"
    if not os.path.isdir(dest_dir):
        return "destination folder does not exist: " + dest_dir
    if P._under(dest_dir, src):
        return "destination is inside the source — that would nest it into itself"
    return ""


def move_folder(root: str, dest_dir: str, dry_run: bool = True) -> dict:
    """Move the whole CD root into dest_dir (e.g. the real archive) — same
    audio-count-verified, logged, undoable move label_panel.move_folders()
    gives the Labels tab, just without that function's "must be under a
    configured Labels root" restriction, since a CD Tools folder is picked
    ad hoc rather than from a tracked root."""
    src = os.path.abspath(root)
    name = os.path.basename(src.rstrip(os.sep))
    err = _safe_check(src, dest_dir)
    if err:
        return {"ok": False, "reason": err}
    before = P._audio_count(src)
    target = P._unique_dest(dest_dir, name)
    if dry_run:
        return {"ok": True, "dest": target, "reason": "would move %d audio file(s)" % before}
    same_fs = os.stat(src).st_dev == os.stat(dest_dir).st_dev
    if not same_fs:
        need, free = P._dir_bytes(src), P._free_bytes(dest_dir)
        if need > free:
            return {"ok": False, "reason": "not enough room: needs %.1f GB, %.1f GB free"
                                           % (need / 1e9, free / 1e9)}
    try:
        shutil.move(src, target)
    except Exception as exc:
        P._log_move("move", src, target, False, str(exc))
        return {"ok": False, "reason": str(exc)}
    after = P._audio_count(target)
    if after != before:
        P._log_move("move", src, target, False, "ARRIVED SHORT: %d in, %d out" % (before, after))
        return {"ok": False, "dest": target,
                "reason": "ARRIVED SHORT: %d audio file(s) went in, %d came out — left at %s"
                         % (before, after, target)}
    P._log_move("move", src, target, True)
    return {"ok": True, "dest": target, "reason": "moved %d audio file(s)" % after}


def copy_folder(root: str, dest_dir: str, dry_run: bool = True) -> dict:
    """Copy (not move) the whole CD root into dest_dir. Non-destructive by
    nature — the source is never touched — so this is NOT logged to the
    move/undo log; there is nothing to undo, a duplicate is just deleted
    directly if it turns out to be unwanted."""
    src = os.path.abspath(root)
    name = os.path.basename(src.rstrip(os.sep))
    err = _safe_check(src, dest_dir)
    if err:
        return {"ok": False, "reason": err}
    target = P._unique_dest(dest_dir, name)
    if dry_run:
        before = P._audio_count(src)
        return {"ok": True, "dest": target, "reason": "would copy %d audio file(s)" % before}
    need, free = P._dir_bytes(src), P._free_bytes(dest_dir)
    if need > free:
        return {"ok": False, "reason": "not enough room: needs %.1f GB, %.1f GB free" % (need / 1e9, free / 1e9)}
    try:
        shutil.copytree(src, target)
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}
    before, after = P._audio_count(src), P._audio_count(target)
    if after != before:
        return {"ok": False, "dest": target,
                "reason": "ARRIVED SHORT: %d audio file(s) copied, %d found — left at %s"
                         % (before, after, target)}
    return {"ok": True, "dest": target, "reason": "copied %d audio file(s)" % after}


# ─── delete — review/holding folder ONLY, never the working CD root ───────
def delete_review_files(root: str, paths: list) -> dict:
    """PERMANENT delete. Only ever touches files already sitting inside
    this root's own review/holding folder (_review_root) — the place
    apply_plan()/remove_file() move duplicates to, never the working CD
    root itself. That restriction is enforced here, not just by
    convention: any path outside the review folder is refused.

    No dry_run parameter on purpose — a delete preview is just "look at
    what's in the review folder", which the UI already shows before this
    is ever called. This function only runs on an explicit confirm."""
    review_root = _review_root(root)
    results = []
    for p in paths:
        p = os.path.abspath(p)
        row = {"path": p, "ok": False, "reason": ""}
        if not P._under(p, review_root):
            row["reason"] = "outside this release's review folder — refusing to delete it"
            results.append(row)
            continue
        if not os.path.isfile(p):
            row["reason"] = "file is already gone"
            results.append(row)
            continue
        try:
            os.remove(p)
            row["ok"] = True
            row["reason"] = "deleted"
            P._log_move("cdtools-delete", p, "(deleted)", True)
        except OSError as exc:
            row["reason"] = str(exc)
        results.append(row)
    return {"ok": bool(results) and all(r["ok"] for r in results), "results": results}


# ─── split — one continuous file into its individual tracks ───────────────
# Deliberately NOT built on disc_numbers()/build_plan(): those assume a
# release already has CD1/CD2/... subfolders full of per-track files. A
# release that still needs splitting is the opposite case — one flat
# folder holding a single continuous rip, disc structure or not. So this
# looks directly in `root` (not recursively — audio_files() walks a whole
# multi-disc tree, which is the wrong scope for "what's the one file to
# cut here") rather than reusing anything disc-shaped.
def _flat_audio_files(root: str) -> list:
    out = []
    try:
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if os.path.isfile(full) and os.path.splitext(name)[1].lower() in L.AUDIO_EXT:
                out.append(full)
    except OSError:
        pass
    return sorted(out)


def _flat_cue_files(root: str) -> list:
    try:
        return sorted(os.path.join(root, f) for f in os.listdir(root) if f.lower().endswith(".cue"))
    except OSError:
        return []


def plan_split_cue(root: str) -> dict:
    """Where each track would be cut, from a cue sheet in `root` that
    describes ONE continuous file (see track_splitter.parse_continuous_cue
    for why a cue naming several files is refused instead). Read-only —
    computes cut points, touches nothing."""
    cues = _flat_cue_files(root)
    if not cues:
        return {"ok": False, "reason": "no .cue file directly in this folder"}
    tried = []
    for cue in cues:
        parsed = ts.parse_continuous_cue(cue)
        if not parsed["ok"]:
            tried.append(f"{os.path.basename(cue)}: {parsed['reason']}")
            continue
        source = os.path.join(root, parsed["source_file"])
        if not os.path.isfile(source):
            tried.append(f"{os.path.basename(cue)}: names "
                         f"\"{parsed['source_file']}\", not found here")
            continue
        dur = L.audio_duration(source)
        if dur is None:
            tried.append(f"{os.path.basename(cue)}: {parsed['source_file']} is unreadable")
            continue
        points = ts.split_points(parsed["tracks"], dur)
        return {"ok": True, "reason": "", "cue": cue, "source": source,
                "points": points, "estimate": False}
    return {"ok": False, "reason": "; ".join(tried)}


def plan_split_discogs(root: str, release_id: str, discogs) -> dict:
    """Where each track would be cut, ESTIMATED from Discogs' own stated
    per-track durations rather than measured from a cue sheet — see
    track_splitter.durations_to_tracks for why this is never as precise.
    Read-only."""
    if not release_id:
        return {"ok": False, "reason": "no Discogs release given"}
    if discogs is None:
        return {"ok": False, "reason": "no Discogs token configured"}
    files = _flat_audio_files(root)
    if len(files) != 1:
        return {"ok": False,
                "reason": f"expected exactly one audio file directly in this folder "
                          f"to split (found {len(files)}) — Discogs durations can't "
                          "say which one is the continuous rip the way a cue's own "
                          "FILE line can"}
    source = files[0]
    dur = L.audio_duration(source)
    if dur is None:
        return {"ok": False, "reason": f"{os.path.basename(source)} is unreadable"}
    try:
        by_disc = discogs.tracklist_by_disc(int(release_id))
    except L.DiscogsError as exc:
        return {"ok": False, "reason": f"Discogs lookup failed: {exc}"}
    tracks = by_disc.get(1) or next(iter(by_disc.values()), [])
    if not tracks:
        return {"ok": False, "reason": "Discogs release has no tracklist"}
    result = ts.durations_to_tracks(tracks)
    if not result["ok"]:
        return {"ok": False, "reason": result["reason"]}
    points = ts.split_points(result["tracks"], dur)
    return {"ok": True, "reason": "", "source": source, "points": points, "estimate": True}


SPLIT_OUT_DIRNAME = "split_tracks"


def apply_split(root: str, source: str, points: list, dry_run: bool = True) -> dict:
    """Cuts `source` per `points` into a `split_tracks/` subfolder next to
    it — never overwrites or deletes the source, and never writes into
    `root` directly, so a split that needs a second look doesn't get
    mistaken for the release's real, final tracks by every other matcher
    in this app that expects `root` to already hold only those."""
    if not (os.path.isfile(source) and P._under(source, root)):
        return {"ok": False, "reason": "source file is not inside this CD root", "written": []}
    out_dir = os.path.join(root, SPLIT_OUT_DIRNAME)
    result = ts.split_file(source, points, out_dir, dry_run=dry_run)
    if not dry_run and result["ok"]:
        P._log_move("cdtools-split", source, out_dir, True,
                    f"{len(result.get('written', []))} track(s) split out")
    return result
