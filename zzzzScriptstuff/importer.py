"""
importer.py
===========

Drives the actual import: walk source folders, extract metadata, decide
solo-vs-mix per album, copy/move files into the organised tree, write DB
rows.

This is the orchestration layer — it composes scanner / metadata /
organiser_core / database. Each step is replaceable.

Resilience principles:
- Per-file errors are caught and logged; we keep going. One unreadable
  flac shouldn't abort an 80-hour import.
- We write the DB row BEFORE the copy. If the copy fails, the row is
  flagged status='broken' and we move on. (Some people prefer the reverse
  — only DB after success — but then a crash mid-import loses the
  metadata work. With write-first, a retry re-uses the cached metadata.)
  Actually, let's think again: if we write before copy and copy fails,
  we have a DB row pointing at a destination path that doesn't exist.
  That's worse for SELECT * queries. So: we extract metadata first,
  attempt copy, then write DB with the FINAL outcome including the
  status flag. If extraction failed, we write status='broken' immediately.
- Duplicate handling: if the destination path already exists with the
  same content_hash, skip. If it exists with different content, append
  a counter to the filename (" (2).flac", " (3).flac", ...).
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from database import Database
from metadata import extract_metadata
from detection import is_record_metadata_broken
from organiser_core import (
    build_destination_path,
    decide_album_type,
    sanitise_path_part,
)
from scanner import find_audio_folders, find_extras


# =============================================================================
# HELPERS
# =============================================================================

def fast_hash_file(path: Path, chunk_size: int = 64 * 1024) -> str | None:
    """
    Fast non-cryptographic hash: file size + first 64 KB + last 64 KB.
    Good enough for "is this the same file I imported last week" — not
    suitable for cryptographic integrity.

    Why not full SHA: 12 TB of FLAC is many days of hashing. The size+ends
    hash takes ~1 ms per file and catches everything except adversarial
    collisions, which aren't a real threat for an organiser.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    h = hashlib.sha1(usedforsecurity=False)
    h.update(str(st.st_size).encode())
    try:
        with open(path, "rb") as fp:
            head = fp.read(chunk_size)
            h.update(head)
            if st.st_size > chunk_size * 2:
                fp.seek(-chunk_size, os.SEEK_END)
                tail = fp.read(chunk_size)
                h.update(tail)
    except OSError:
        return None
    return h.hexdigest()


def _unique_destination(dest: Path) -> Path:
    """If `dest` exists, return `dest (2)`, `dest (3)`, ... until free."""
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    i = 2
    while True:
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def _copy_or_move(src: Path, dst: Path, mode: str) -> None:
    """
    Copy or move `src` to `dst`, creating parent dirs.

    `mode` is 'copy' or 'move'. 'move' is implemented as copy+unlink so
    we work across filesystems (your sources are on different drives).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if mode == "move":
        # shutil.move handles cross-fs via copy+delete; we use copy2 +
        # explicit unlink to guarantee atomic-ish behaviour: copy success
        # is required before original is removed.
        shutil.copy2(src, dst)
        try:
            src.unlink()
        except OSError:
            # Couldn't delete source — leave it. Better than losing data.
            pass
    else:
        shutil.copy2(src, dst)


# =============================================================================
# RESULTS DATACLASS
# =============================================================================

@dataclass
class ImportStats:
    folders_scanned: int = 0
    files_seen: int = 0
    files_imported: int = 0
    files_skipped_duplicate: int = 0
    files_failed: int = 0
    folders_emptied: int = 0   # source folders removed after move-mode import
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"folders scanned: {self.folders_scanned}",
            f"audio files seen: {self.files_seen}",
            f"imported: {self.files_imported}",
            f"skipped (already present, same content): {self.files_skipped_duplicate}",
            f"failed: {self.files_failed}",
        ]
        if self.folders_emptied:
            lines.append(f"empty source folders removed: {self.folders_emptied}")
        return "\n".join(lines)


# =============================================================================
# IMPORT
# =============================================================================

ProgressCallback = Callable[[str, int, int], None]
# called as: cb(current_file_path, files_done, files_total)


def import_sources(
    sources: list[str],
    *,
    cfg: dict[str, Any],
    db: Database,
    progress_cb: ProgressCallback | None = None,
    ui: Any = None,
    dry_run: bool = False,
) -> ImportStats:
    """
    Top-level import: walk every source root, organise everything found
    into the destination tree, populate the database.

    `cfg` is the loaded config dict (output of config.load_config()).

    `ui` is an optional LiveImportUI / PlainImportUI instance (see ui.py).
    If provided, it gets richer updates than progress_cb — the four-panel
    layout needs to know folder, file, what we're grabbing, and where it's
    going. progress_cb is kept for backwards compat and gets called too.
    """
    stats = ImportStats()

    import_cfg = cfg.get("import", {})
    paths_cfg = cfg.get("paths", {})
    organise_cfg = cfg.get("organise", {})

    audio_exts = import_cfg.get("audio_extensions", [".flac", ".mp3"])
    extras_exts = import_cfg.get("extras_extensions", [".jpg", ".log"])
    take_all_non_audio = import_cfg.get("take_all_non_audio", True)
    junk_filenames = import_cfg.get("junk_filenames", [".ds_store", "thumbs.db"])
    skip_hidden = import_cfg.get("skip_hidden", True)
    follow_symlinks = import_cfg.get("follow_symlinks", False)
    min_size = import_cfg.get("min_size_bytes", 0)
    mode = import_cfg.get("mode", "copy")

    dest_root = Path(paths_cfg.get("destination_root", "")).expanduser()
    if not dest_root:
        raise ValueError("config.paths.destination_root is empty — set it first")

    diversity = organise_cfg.get("mix_artist_diversity_threshold", 0.5)
    va_tags = organise_cfg.get("various_artists_tags", [])
    mix_keywords = organise_cfg.get("mix_keywords", [])

    # Streaming mode: skip the precount walk entirely. Folders are
    # processed AS THEY ARE DISCOVERED rather than after a full walk
    # collects them all. The progress bar shows X imported / ? total
    # (indeterminate) instead of a percentage with ETA. This saves the
    # full precount walk time (up to ~45 minutes on cold-cache USB-
    # attached multi-TB libraries) at the cost of losing the ETA.
    #
    # Defaults to False (precount, percentage-based progress). Enable
    # via config.import.skip_precount = true, or pass skip_precount=True
    # to this function directly.
    skip_precount = import_cfg.get("skip_precount", False)

    # ----- checkpoint: announce we've started ---------------------------
    # We write a checkpoint file at the start so Ctrl+C / power loss leaves
    # a trace. On the next launch, cmd_import detects it and offers to
    # resume. Note: actual "resume" is mostly automatic — the DB knows
    # what's already imported, so re-running just skips done files.
    from checkpoint import (
        ImportCheckpoint, save_checkpoint, clear_checkpoint, load_checkpoint,
    )
    import time as _t
    cp = ImportCheckpoint(
        operation="import",
        started_at=_t.time(),
        sources=list(sources),
        destination_root=str(dest_root),
        phase="counting" if not skip_precount else "organising",
    )
    save_checkpoint(cp)

    # ----- PRE-PASS: triage non-music folders ---------------------------
    # Before the main audio-import walk, run a content-aware classifier
    # over each source root and divert random-crap / orphan-bonus
    # folders to dedicated destinations. This stops them clogging the
    # main scan and stops `find_audio_folders` from silently skipping
    # them every time (which means they'd persist in the source tree
    # forever).
    #
    # Two destinations from config:
    #   paths.out_of_library_dest  -- random-crap goes here, OUTSIDE
    #                                 the organised tree (so it doesn't
    #                                 get re-scanned next run)
    #   paths.orphan_folder        -- orphaned audio files go to this
    #                                 subdir UNDER destination_root
    #
    # Disabled by default — opt-in via import.triage_non_music = true.
    # Reason: the user must explicitly OK us moving non-music folders
    # around. We won't silently relocate someone's /downloads/ of
    # e-books just because they pointed sources at it.
    triage_enabled = import_cfg.get("triage_non_music", False)
    triage_stats = {"random_crap_folders": 0, "orphan_files": 0,
                     "random_crap_moved": 0, "orphan_files_moved": 0}
    if triage_enabled and not dry_run:
        orphan_subdir = paths_cfg.get("orphan_folder", "orphaned bonus files")
        out_of_lib = paths_cfg.get("out_of_library_dest", "")
        if not out_of_lib:
            if ui is not None:
                ui.log("warning",
                       "triage enabled but paths.out_of_library_dest is empty — "
                       "skipping random-crap routing")
        else:
            out_of_lib_path = Path(out_of_lib).expanduser()
            orphan_path = dest_root / orphan_subdir
            if ui is not None:
                ui.log("info",
                       f"triage pass: classifying non-music folders "
                       f"(opt-in via import.triage_non_music)")
                ui.log("info", f"  random-crap → {out_of_lib_path}")
                ui.log("info", f"  orphan bonus audio → {orphan_path}")

            from folder_classifier import classify_tree, FolderKind
            for src_root in sources:
                src_root_path = Path(src_root).expanduser()
                if not src_root_path.exists():
                    continue
                for folder, kind, profile in classify_tree(
                    src_root_path,
                    audio_extensions=audio_exts,
                    skip_hidden=skip_hidden,
                ):
                    # Never triage the source root itself, only children
                    if folder == src_root_path:
                        continue
                    # Never triage anything inside our destination
                    try:
                        folder.relative_to(dest_root)
                        continue   # under destination_root → skip
                    except ValueError:
                        pass
                    # Never triage anything inside the out-of-library
                    # destination (avoid moving things we already moved)
                    try:
                        folder.relative_to(out_of_lib_path)
                        continue
                    except ValueError:
                        pass

                    if kind == FolderKind.RANDOM_CRAP:
                        triage_stats["random_crap_folders"] += 1
                        # Move the WHOLE folder to out_of_library.
                        # Preserve a hint of the source layout by
                        # mirroring the folder name only (not the full
                        # path) — flat dump under OrganiserStuff/.
                        try:
                            target = out_of_lib_path / folder.name
                            target = _unique_destination(target)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            import shutil as _sh
                            _sh.move(str(folder), str(target))
                            triage_stats["random_crap_moved"] += 1
                            if ui is not None:
                                ui.log("imported",
                                       f"random-crap → moved '{folder.name}' "
                                       f"({profile.n_total_files} files) to "
                                       f"OrganiserStuff/")
                        except Exception as e:
                            if ui is not None:
                                ui.log("broken",
                                       f"could not move {folder}: {e}")
                    elif kind == FolderKind.ORPHAN_BONUS:
                        # Move ONLY the audio files (not the junk
                        # alongside them) to the orphan folder.
                        # Junk stays where it is — we don't decide
                        # what to do with non-music files that aren't
                        # ours to keep.
                        triage_stats["orphan_files"] += profile.n_audio
                        try:
                            orphan_path.mkdir(parents=True, exist_ok=True)
                            for audio_file in profile.audio_files:
                                target = orphan_path / audio_file.name
                                target = _unique_destination(target)
                                _copy_or_move(audio_file, target, mode)
                                triage_stats["orphan_files_moved"] += 1
                            if ui is not None:
                                ui.log("imported",
                                       f"orphan-rescue → {profile.n_audio} "
                                       f"audio file(s) from '{folder.name}' "
                                       f"→ orphan bonus folder")
                        except Exception as e:
                            if ui is not None:
                                ui.log("broken",
                                       f"could not rescue orphans from "
                                       f"{folder}: {e}")
                    # MUSIC / EMPTY_OR_TRIVIAL: leave alone, the main
                    # importer will handle music; trivial folders are
                    # not worth moving and would clutter destinations.

            if ui is not None:
                ui.log("info",
                       f"triage done: moved {triage_stats['random_crap_moved']} "
                       f"random-crap folders, rescued "
                       f"{triage_stats['orphan_files_moved']} orphan audio files")
    elif triage_enabled and dry_run:
        if ui is not None:
            ui.log("info", "triage enabled but dry_run=true — skipping moves")

    # Decide how to enumerate work. Either:
    #   precount=True  -> walk twice (count, then process). Has ETA.
    #   precount=False -> single streaming pass (no ETA, but starts now)
    if skip_precount:
        if ui is not None:
            ui.log("info", "streaming mode — skipping precount, starting now")
            ui.log("info", "(progress will show X / ? — no ETA in this mode)")
        total_files = 0  # 0 means "unknown" to the UI total
        # Generator: yield (folder, files, src_root) tuples as discovered.
        # No materialisation, no double-walk.
        def folder_stream():
            for src_root in sources:
                for folder, files in find_audio_folders(
                    src_root,
                    audio_extensions=audio_exts,
                    skip_hidden=skip_hidden,
                    follow_symlinks=follow_symlinks,
                    min_size_bytes=min_size,
                ):
                    yield (folder, files, str(src_root))
        folders_to_process = folder_stream()
    else:
        # ----- pre-scan for the progress total ----------------------
        # Cheap walk just to count files — gives the progress bar a sane scale.
        # For huge libraries (12TB+) this walk can take 30+ seconds on cold
        # cache, so we announce progress to the UI every N folders so the user
        # doesn't think the program froze.
        if ui is not None:
            ui.log("info", "scanning sources to count files…")
            ui.log("info", "(this can take minutes on cold-cache external storage)")
            ui.log("info", "(skip this with import.skip_precount=true in config)")
            ui.update(current_folder=", ".join(sources))

        total_files = 0
        folders_to_process_list: list[tuple[Path, list[Path], str]] = []
        scan_announce_every = 100   # update Folder panel every N folders
        scan_log_every = 500        # write Activity heartbeat every N folders
        for src_root in sources:
            for folder, files in find_audio_folders(
                src_root,
                audio_extensions=audio_exts,
                skip_hidden=skip_hidden,
                follow_symlinks=follow_symlinks,
                min_size_bytes=min_size,
            ):
                folders_to_process_list.append((folder, files, str(src_root)))
                total_files += len(files)
                if ui is not None:
                    if len(folders_to_process_list) % scan_announce_every == 0:
                        ui.update(
                            current_folder=str(folder),
                            current_file=f"counting: {total_files:,} files in {len(folders_to_process_list):,} folders so far",
                        )
                    if len(folders_to_process_list) % scan_log_every == 0:
                        ui.log("info",
                               f"still scanning: {total_files:,} files in "
                               f"{len(folders_to_process_list):,} folders…")
        folders_to_process = folders_to_process_list

    # Update checkpoint with what we found.
    cp.total_files = total_files
    cp.phase = "organising"
    save_checkpoint(cp)

    files_done = 0
    # Periodic checkpoint update: every N files. 250 keeps the file fresh
    # enough to be useful for resume without thrashing the disk.
    checkpoint_every = 250

    if ui is not None:
        ui.set_total(total_files)
        if not skip_precount and total_files == 0:
            # Precount mode: 0 truly means empty sources.
            ui.log("warning", "no audio files found in the configured sources")
            ui.log("info", "(check your paths.sources and that the drives are mounted)")
            clear_checkpoint()
            return stats
        if skip_precount:
            ui.log("info", "starting import (streaming mode — no precount)")
        else:
            ui.log("info", f"scan complete: {total_files:,} files across "
                           f"{len(folders_to_process):,} folders — starting import")
        # Punch the launch quote into the activity log right when we
        # kick off the real work. LUDICROUS prints "LUDICROUS SPEED! GO!"
        from speed import get_launch_quote
        ui.log("info", get_launch_quote())

    # ----- process each folder as an "album" ----------------------------
    # hot_loop disables Python's GC during the tight import loop on
    # Ridiculous and above tiers. Negligible memory growth per file;
    # ~5-10% speedup on bulk imports.
    from speed import get_active_level, hot_loop
    _speed_lvl = get_active_level()
    # Folders we move files out of during this import. Used at the end
    # to remove ones that became empty (only when mode == "move").
    _import_touched_folders: set[str] = set()
    with hot_loop(_speed_lvl):
     for folder, files, src_root_str in folders_to_process:
        stats.folders_scanned += 1
        if mode == "move":
            _import_touched_folders.add(str(folder))

        if ui is not None:
            ui.update(
                current_folder=str(folder),
                files_in_folder=len(files),
                file_index_in_folder=0,
            )

        # Extract metadata for every track in the folder up front, so we
        # can make the album-type decision. Cache them by path for re-use
        # when we do the actual copy below.
        records: list[dict[str, Any]] = []
        for f in files:
            try:
                rec = extract_metadata(f, source_root=src_root_str)
            except Exception as e:
                rec = {
                    "path": str(f.resolve()),
                    "status": "broken",
                    "comment": f"metadata error: {e}",
                    "source_root": src_root_str,
                }
                stats.errors.append(f"{f}: metadata error: {e}")

            # ----- METADATA QUALITY CHECK -----
            # If extraction succeeded but the file is missing any of
            # artist/album/title, mark it broken. The file will still be
            # imported (we don't lose audio data), but it'll go to the
            # Broken folder where the user can manually triage or run a
            # metadata fetch to recover the tags.
            #
            # We only run the check if status isn't ALREADY broken — no
            # need to re-decide if metadata extraction itself failed.
            if rec.get("status") != "broken":
                try:
                    is_broken, reason = is_record_metadata_broken(rec)
                    if is_broken:
                        rec["status"] = "broken"
                        # Preserve any existing comment, append the reason
                        existing_comment = (rec.get("comment") or "").strip()
                        sep = "; " if existing_comment else ""
                        rec["comment"] = f"{existing_comment}{sep}metadata incomplete: {reason}"
                        stats.errors.append(f"{f}: marked broken — {reason}")
                except Exception as e:
                    # Soft check — never crash the import for a predicate bug.
                    stats.errors.append(f"{f}: broken-check failed: {e}")

            records.append(rec)

        # Decide the album type ONCE for this folder.
        album_type = decide_album_type(
            records,
            diversity_threshold=diversity,
            various_artists_tags=va_tags,
            mix_keywords=mix_keywords,
        )

        # Find non-audio extras (cover art, videos, signatures, PDFs etc)
        # — placed once per album. With take_all_non_audio=True (default)
        # this drags ALONG every non-audio file in the folder.
        extras = find_extras(
            folder,
            extras_extensions=extras_exts,
            audio_extensions=audio_exts,
            take_all_non_audio=take_all_non_audio,
            junk_filenames=junk_filenames,
            skip_hidden=skip_hidden,
        )

        # Choose the album's destination folder using the FIRST track's
        # path-builder output, then strip the filename. This keeps all
        # tracks in one folder even if their tags disagree about labels.
        album_dest_dir: Path | None = None

        for idx, (rec, src_file) in enumerate(zip(records, files), 1):
            stats.files_seen += 1
            files_done += 1
            if progress_cb:
                progress_cb(str(src_file), files_done, total_files)

            # Periodic checkpoint flush — every N files we save where
            # we are so a Ctrl+C / power loss has a recent recovery point.
            if files_done % checkpoint_every == 0:
                cp.files_done = files_done
                cp.last_file_processed = str(src_file)
                cp.stats_imported = stats.files_imported
                cp.stats_duplicate = stats.files_skipped_duplicate
                cp.stats_broken = stats.files_failed
                save_checkpoint(cp)

            if ui is not None:
                fmt_detail = ""
                if rec.get("sample_rate") and rec.get("bit_depth"):
                    fmt_detail = f"{rec['bit_depth']}-bit / {rec['sample_rate'] / 1000:.1f} kHz"
                elif rec.get("bitrate"):
                    fmt_detail = f"{rec['bitrate'] // 1000} kbps"
                ui.update(
                    current_file=str(src_file),
                    file_index_in_folder=idx,
                    file_size_bytes=rec.get("size_bytes") or 0,
                    file_codec=rec.get("codec") or "",
                    file_format_detail=fmt_detail,
                )
                ui.set_grabbing(rec)

            rec["album_type"] = album_type

            try:
                dest = build_destination_path(
                    rec,
                    album_type=album_type,
                    destination_root=dest_root,
                    organise_cfg=organise_cfg,
                )
            except Exception as e:
                stats.files_failed += 1
                stats.errors.append(f"{src_file}: path build failed: {e}")
                rec["status"] = "broken"
                rec["comment"] = f"path build error: {e}"
                with db.transaction():
                    db.upsert_file(rec)
                if ui is not None:
                    ui.advance(broken=True, size_bytes=rec.get("size_bytes") or 0)
                    ui.log("broken", f"{src_file.name}  (path build: {e})")
                continue

            # Record where we plan to put it.
            rec["organised_path"] = str(dest)
            rec["quality_tier"] = "broken" if rec.get("status") == "broken" else "high"

            if ui is not None:
                ui.update(organising_to=str(dest))

            # Remember the album's directory for the extras copy.
            if album_dest_dir is None and rec.get("status") != "broken":
                album_dest_dir = dest.parent

            if dry_run:
                with db.transaction():
                    db.upsert_file(rec)
                if ui is not None:
                    ui.advance(size_bytes=rec.get("size_bytes") or 0)
                continue

            # Compute hash for dedup detection.
            rec["content_hash"] = fast_hash_file(src_file)

            # Duplicate check: same hash already in DB at a real path?
            if rec["content_hash"]:
                existing = db.find_by_hash(rec["content_hash"])
                # Exclude the source path itself (re-imports of the same file).
                existing = [
                    e for e in existing
                    if Path(e["path"]).resolve() != src_file.resolve()
                    and Path(e["path"]).exists()
                ]
                if existing:
                    stats.files_skipped_duplicate += 1
                    rec["status"] = "duplicate"
                    rec["comment"] = f"duplicate of {existing[0]['path']}"
                    with db.transaction():
                        db.upsert_file(rec)
                    if ui is not None:
                        ui.advance(duplicate=True, size_bytes=rec.get("size_bytes") or 0)
                        ui.log("duplicate", f"{src_file.name}  (already at {Path(existing[0]['path']).name})")
                    continue

            # Resolve filename collisions on disk.
            final_dest = _unique_destination(dest)
            rec["organised_path"] = str(final_dest)

            try:
                _copy_or_move(src_file, final_dest, mode)
                stats.files_imported += 1
                # Preserve 'broken' status — the file is in Broken/ for a
                # reason, even though the copy itself worked. Only flip
                # to 'imported' if the file came in clean.
                was_broken = rec.get("status") == "broken"
                if not was_broken:
                    rec["status"] = "imported"
                # Update path in the DB to the NEW location (organised).
                # We also keep the source_root for provenance.
                old_path = rec["path"]
                rec["path"] = str(final_dest.resolve())
                with db.transaction():
                    db.upsert_file(rec)
                    # Optional: clean up the old (source) row if it differed.
                    if old_path != rec["path"]:
                        db.delete_by_path(old_path)
                if ui is not None:
                    if was_broken:
                        ui.advance(broken=True, size_bytes=rec.get("size_bytes") or 0)
                        ui.log("broken", f"{src_file.name}  → Broken/")
                    else:
                        ui.advance(imported=True, size_bytes=rec.get("size_bytes") or 0)
                        ui.log("imported", f"{src_file.name}")
            except Exception as e:
                stats.files_failed += 1
                stats.errors.append(f"{src_file}: copy failed: {e}")
                rec["status"] = "broken"
                rec["comment"] = f"copy error: {e}"
                with db.transaction():
                    db.upsert_file(rec)
                if ui is not None:
                    ui.advance(broken=True, size_bytes=rec.get("size_bytes") or 0)
                    ui.log("broken", f"{src_file.name}  ({e})")

        # ----- move/copy extras (art, video, sig, pdf...) into the album dir --
        # We respect the import mode: if you said "move", the extras
        # leave the source too. If "copy", originals stay.
        # We always show the user what's being dragged along — these
        # files are easy to forget about and silent moves are surprising.
        if album_dest_dir is not None and extras and not dry_run:
            for extra in extras:
                target = album_dest_dir / extra.name
                if target.exists():
                    if ui is not None:
                        ui.log("info", f"extra exists, skip: {extra.name}")
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if mode == "move":
                        shutil.move(str(extra), str(target))
                        if ui is not None:
                            ui.log("info", f"moved extra: {extra.name}")
                    else:
                        shutil.copy2(extra, target)
                        if ui is not None:
                            ui.log("info", f"copied extra: {extra.name}")
                    if ui is not None:
                        ui.update(organising_to=str(target))
                except Exception as e:
                    stats.errors.append(f"{extra}: extras copy failed: {e}")

    # Normal completion — drop the checkpoint so next launch doesn't
    # prompt to resume a finished run.
    cp.phase = "complete"
    cp.files_done = files_done
    cp.stats_imported = stats.files_imported
    cp.stats_duplicate = stats.files_skipped_duplicate
    cp.stats_broken = stats.files_failed
    save_checkpoint(cp)
    clear_checkpoint()

    # Empty-folder cleanup (move-mode only). Same logic as organise_in_place
    # but inline since the stats class is different. We only clean up
    # folders we actually moved files OUT of, plus their ancestors.
    if mode == "move" and not dry_run and _import_touched_folders:
        if ui is not None:
            ui.log("info", "cleaning up empty source folders after move…")
        OS_JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini",
                          ".directory", ".localized"}
        ORPHAN_OK = bool(organise_cfg.get("delete_orphaned_extras", False))
        ORPHAN_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp",
                              ".bmp", ".tiff", ".log", ".cue", ".m3u",
                              ".m3u8", ".txt", ".nfo", ".pls", ".sfv",
                              ".md5"}
        source_roots = set()
        for s in sources:
            try:
                source_roots.add(str(Path(s).expanduser().resolve()))
            except Exception:
                pass

        def _is_empty_enough(folder: Path) -> tuple[bool, list[Path]]:
            try:
                children = list(folder.iterdir())
            except OSError:
                return False, []
            if not children:
                return True, []
            junk: list[Path] = []
            for c in children:
                if c.is_dir():
                    return False, []
                if c.name in OS_JUNK_FILES:
                    junk.append(c); continue
                if ORPHAN_OK and c.suffix.lower() in ORPHAN_EXTENSIONS:
                    junk.append(c); continue
                return False, []
            return True, junk

        def _try_remove(folder: Path) -> bool:
            if not folder.exists() or not folder.is_dir():
                return False
            try:
                resolved = str(folder.resolve())
            except OSError:
                return False
            if resolved in source_roots:
                return False
            can, junk = _is_empty_enough(folder)
            if not can:
                return False
            for j in junk:
                try:
                    j.unlink()
                except OSError:
                    return False
            try:
                folder.rmdir()
            except OSError:
                return False
            stats.folders_emptied += 1
            if ui is not None:
                msg = f"removed empty folder: {folder.name}"
                if junk:
                    msg += f"  (with {len(junk)} junk file{'s' if len(junk) != 1 else ''})"
                ui.log("info", msg)
            return True

        # Phase 1: deepest touched folders first
        sorted_folders = sorted(_import_touched_folders, key=lambda p: -p.count(os.sep))
        for folder_str in sorted_folders:
            _try_remove(Path(folder_str))
        # Phase 2: walk up
        ancestors_to_check: list[str] = []
        for folder_str in _import_touched_folders:
            p = Path(folder_str).parent
            while True:
                try:
                    rp = str(p.resolve())
                except OSError:
                    break
                if rp in source_roots:
                    break
                ancestors_to_check.append(str(p))
                parent = p.parent
                if parent == p:
                    break
                p = parent
        for p in sorted(set(ancestors_to_check), key=lambda s: -s.count(os.sep)):
            _try_remove(Path(p))

    return stats


# =============================================================================
# REORGANISE IN PLACE
# =============================================================================
#
# Walk the DB. For each file, compute its canonical destination from its
# current (post-MusicBrainz-fetch, post-edit, etc) metadata. If the file
# isn't already at the canonical path, move it. Update the DB row to
# point at the new path.
#
# This is the natural follow-up to `cmd_fetch_metadata`: improving tags
# in the DB only matters for queries until the folder layout reflects
# them too.

@dataclass
class OrganiseStats:
    """Stats for organise_in_place. Distinct shape from ImportStats
    because the operations are different — we're not seeing 'new' or
    'duplicate' files, just moves and no-ops."""
    files_seen: int = 0
    files_moved: int = 0
    files_in_place: int = 0
    files_skipped: int = 0          # missing on disk, or broken
    folders_emptied: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"seen:           {self.files_seen:,}\n"
            f"moved:          {self.files_moved:,}\n"
            f"already in place: {self.files_in_place:,}\n"
            f"skipped:        {self.files_skipped:,}\n"
            f"empty folders cleaned: {self.folders_emptied:,}\n"
            f"errors:         {len(self.errors):,}"
        )


def _lookup_album_label_online(
    rows: list[dict[str, Any]],
    providers: list,
    *,
    cache: dict[tuple[str, str], str],
    min_confidence: float = 80.0,
    ambiguity_margin: float = 5.0,
    per_provider_thresholds: dict[str, float] | None = None,
    log_cb=None,
) -> str:
    """
    Query the metadata providers for an album's record label.

    Called by organise_in_place when an album has missing/inconsistent
    label info AND the user opted in. Returns the recovered label string
    or "" if no confident match was found across all providers.

    Cache key is `(artist_lower, album_lower)`. Once an album is queried
    in this run, the result is reused — important because the same
    album can appear in multiple folder groups (post-rename, drift,
    user manually copying tracks elsewhere) and we don't want to hit
    the providers twice.

    The confidence gate from v0.23.27 (pick_confident_match) is reused
    here — accuracy still trumps coverage. A weak match isn't trusted
    even though our caller really wants an answer.
    """
    from metadata_providers import Provider
    from metadata_lookup import pick_confident_match
    from collections import Counter

    if not rows or not providers:
        return ""

    # Pick the representative artist + album for the query. For VA
    # comps we'd never get here (the caller filters those out), so we
    # can use the most common per-track artist safely.
    artist_counts: Counter[str] = Counter()
    album_counts: Counter[str] = Counter()
    for r in rows:
        a = (r.get("albumartist") or r.get("primary_artist") or r.get("artist") or "").strip()
        if a:
            artist_counts[a] += 1
        al = (r.get("album") or "").strip()
        if al:
            album_counts[al] += 1
    if not artist_counts or not album_counts:
        return ""
    artist = artist_counts.most_common(1)[0][0]
    album = album_counts.most_common(1)[0][0]

    if not Provider.is_safe_query(artist, album):
        return ""

    cache_key = (artist.lower(), album.lower())
    if cache_key in cache:
        return cache[cache_key]

    # Walk providers in order. First confident hit with a non-empty
    # `label` field wins.
    for prov in providers:
        try:
            results = prov.search_release(artist, album)
        except Exception:
            continue
        if not results:
            continue
        pick, _why = pick_confident_match(
            results, prov.id,
            min_confidence=min_confidence,
            ambiguity_margin=ambiguity_margin,
            per_provider_thresholds=per_provider_thresholds,
        )
        if pick is None:
            continue
        candidate = (getattr(pick, "label", "") or "").strip()
        if candidate:
            cache[cache_key] = candidate
            if log_cb:
                log_cb("info",
                       f"online lookup: {artist} - {album} → label={candidate!r} via {prov.id}")
            return candidate

    cache[cache_key] = ""
    return ""


def organise_in_place(
    db: Database,
    *,
    cfg: dict[str, Any],
    ui: Any = None,
    dry_run: bool = False,
    fresh_pass: bool = False,
    online_label_lookup: bool = True,
    progress_cb: ProgressCallback | None = None,
) -> OrganiseStats:
    """
    Re-organise files already in the library, based on current DB metadata.

    Workflow:
      1. Group all DB rows by their current parent folder. This lets us
         apply solo/mix album-type detection per-group, the same way the
         importer does.
      2. For each folder group:
         a. Decide album_type from the records.
         b. For each record, compute its canonical destination path.
         c. If file is missing on disk, mark skip.
         d. If destination == current, no-op (and mark organised_at).
         e. Otherwise: move file, update DB row's path, log to UI.
      3. After processing all files, walk the source folders we touched
         and remove the ones that are now empty.

    dry_run=True: log what WOULD happen, don't actually move anything.

    fresh_pass:
      False (default, "normal pass"): rows with `organised_at` set AND
        no `last_seen` update since (meaning: we organised them, and
        their metadata hasn't been touched since) are skipped entirely.
        Saves a path-build + filesystem stat per skipped row, which
        adds up to real minutes on a 207k-row library.
      True ("fresh pass"): every row is processed regardless of
        organised_at. Use when you suspect drift between DB and disk
        (manual moves, restored backup, edited DB).
    """
    stats = OrganiseStats()
    paths_cfg = cfg.get("paths", {})
    organise_cfg = cfg.get("organise", {})

    dest_root = Path(paths_cfg.get("destination_root", "")).expanduser()
    if not dest_root:
        raise ValueError("paths.destination_root is empty — set it first")
    diversity = organise_cfg.get("mix_artist_diversity_threshold", 0.5)
    va_tags = organise_cfg.get("various_artists_tags", [])
    mix_keywords = organise_cfg.get("mix_keywords", [])

    # --- group rows by current parent folder ---------------------------------
    if ui is not None:
        ui.log("info", "loading file list from database…")

    by_folder: dict[str, list[dict[str, Any]]] = {}
    for row in db.iter_all():
        row_d = dict(row)
        path_str = row_d.get("path") or ""
        if not path_str:
            stats.files_skipped += 1
            continue
        folder = str(Path(path_str).parent)
        by_folder.setdefault(folder, []).append(row_d)

    n_folders = len(by_folder)
    n_files = sum(len(v) for v in by_folder.values())

    if ui is not None:
        ui.set_total(n_files)
        if n_files == 0:
            ui.log("warning", "the DB is empty — nothing to organise")
            ui.log("info", "(run option 1 'Import' or option 3 'Rebuild database' first)")
            return stats
        ui.log("info", f"organising {n_files:,} files in {n_folders:,} folders")
        if dry_run:
            ui.log("info", "DRY RUN — no files will move")

    # --- per-folder processing -----------------------------------------------
    touched_folders: set[str] = set()
    files_done = 0

    # ----- Inline label lookup setup (v0.23.28) -----
    # Cache album → label across the whole run so re-queries don't
    # happen if the same album appears in multiple folder groups.
    # Built once here, populated lazily as albums get queried.
    _label_lookup_cache: dict[tuple[str, str], str] = {}
    _online_providers: list = []
    if online_label_lookup:
        # Build the provider list the same way cmd_fetch_metadata does:
        # walk ALL_PROVIDERS, instantiate via make_provider, try to
        # configure each. Providers that fail to configure (e.g.
        # Discogs without a saved token) get silently skipped — we
        # don't want to prompt for auth during an organise run.
        try:
            from metadata_providers import ALL_PROVIDERS, make_provider
            # Prefer the user's last-used selection from fetch runs, so
            # the providers used for label lookup match what they trust
            # for tag fetching. Fall back to MB+Deezer (free, no auth).
            try:
                from organiser import load_last_used  # type: ignore
                last_ids = load_last_used(cfg, "fetch_metadata", "providers",
                                          fallback=["musicbrainz", "deezer"])
            except Exception:
                last_ids = ["musicbrainz", "deezer"]
            for pid in last_ids:
                prov = make_provider(pid)
                if prov is None:
                    continue
                try:
                    # Pass a no-op asker — we never want to prompt
                    # during organise. If a provider needs auth and
                    # doesn't have a saved token, it should return
                    # False from configure and we skip it.
                    ok = prov.configure(cfg, lambda **kw: None)
                except Exception:
                    ok = False
                if ok and prov.id:
                    _online_providers.append(prov)
            if not _online_providers:
                online_label_lookup = False
                if ui is not None:
                    ui.log("warning",
                           "online label lookup disabled: "
                           "no providers configured (run option 7 first)")
        except Exception as e:
            if ui is not None:
                ui.log("warning",
                       f"online label lookup disabled: provider setup failed: {e}")
            online_label_lookup = False
    # Pull confidence thresholds from the same [fetch_metadata] section
    # the fetch loop uses (consistency).
    fm_cfg = cfg.get("fetch_metadata", {}) or {}
    _min_conf = float(fm_cfg.get("min_confidence_score", 80.0))
    _amb_margin = float(fm_cfg.get("ambiguity_margin", 5.0))
    _pp_thresholds = {str(k): float(v) for k, v in
                      (fm_cfg.get("thresholds", {}) or {}).items()}

    for folder_str, rows in by_folder.items():
        folder = Path(folder_str)
        touched_folders.add(folder_str)

        # Detect album-type from the group's records.
        album_type = decide_album_type(
            rows,
            diversity_threshold=diversity,
            various_artists_tags=va_tags,
            mix_keywords=mix_keywords,
        )

        # ----- ALBUM-LEVEL DECISIONS -----
        # These run ONCE per folder-group and apply to every track
        # inside. The decisions are:
        #   - forced_label: the ONE label name to put in the folder
        #     path for this album, so every track lands in the same
        #     folder regardless of per-track label disagreement.
        #   - is_self_release: whether this album should route to
        #     Self-Released/ instead of <label>/.
        #   - is_single: whether this is a one-track release (singles
        #     get their own subtree inside Self-Released/).
        #
        # Compilations / VA mixes skip the self-release detection
        # entirely — a VA comp by definition has no single artist who
        # could be the "self" releasing it.
        from organiser_core import decide_album_label, detect_self_release

        forced_label, label_why = decide_album_label(
            rows, unknown_label_fallback=organise_cfg.get(
                "unknown_label", "Unknown Label")
        )

        # ----- ONLINE LABEL LOOKUP -----
        # If the album doesn't have a confident label from the existing
        # tags (either all empty/placeholder, OR conflicting), try the
        # providers. Only fires for non-mix releases since VA comps
        # don't benefit from this — the per-track artists are by design
        # different and the comp's own label was already what we tagged.
        unknown_fallback = organise_cfg.get("unknown_label", "Unknown Label")
        needs_lookup = (
            online_label_lookup
            and album_type != "mix"
            and (
                forced_label == unknown_fallback
                or label_why.startswith("majority:")
            )
        )
        if needs_lookup:
            recovered = _lookup_album_label_online(
                rows, _online_providers,
                cache=_label_lookup_cache,
                min_confidence=_min_conf,
                ambiguity_margin=_amb_margin,
                per_provider_thresholds=_pp_thresholds,
                log_cb=(ui.log if ui is not None else None),
            )
            if recovered:
                # Online lookup succeeded — use that label for the
                # folder. We do NOT write it back to the DB here because
                # this is the organise pass, not a fetch pass; if the
                # user wants the recovered label persisted they can run
                # option 7 (Fetch metadata) which uses the same provider
                # chain.
                forced_label = recovered
                label_why = f"online lookup → {recovered!r}"

        is_self_release = False
        self_release_why = ""
        if album_type != "mix":
            is_self_release, self_release_why = detect_self_release(
                rows, album_label=forced_label,
            )

        # Single = exactly one track in this folder group AND not a
        # mix/comp. Singles in compilations don't make sense
        # conceptually so we skip the heuristic for mixes.
        is_single = (len(rows) == 1 and album_type != "mix")

        sri = {
            "is_self_release": is_self_release,
            "is_single":       is_single,
            "forced_label":    forced_label,
        }

        # Log the decision once per group so the user can see what
        # happened during a run. Routine cases (label matches what
        # the tracks said, not a self-release) don't get logged to
        # avoid spam.
        if ui is not None and (is_self_release or label_why != "unanimous"):
            short_folder = folder_str if len(folder_str) <= 60 \
                else "…" + folder_str[-57:]
            if is_self_release:
                ui.log("info",
                       f"{short_folder}: routing to Self-Released/ "
                       f"({self_release_why or 'no label'})")
            if label_why not in ("unanimous", "all empty/placeholder"):
                ui.log("info",
                       f"{short_folder}: {label_why}")

        if ui is not None:
            ui.update(
                current_folder=str(folder),
                files_in_folder=len(rows),
                file_index_in_folder=0,
            )

        moved_to_dirs: set[Path] = set()

        for idx, row in enumerate(rows, 1):
            files_done += 1
            stats.files_seen += 1
            src_path_str = row["path"]
            src_path = Path(src_path_str)

            if progress_cb:
                progress_cb(src_path_str, files_done, n_files)

            if ui is not None:
                ui.update(
                    current_file=str(src_path),
                    file_index_in_folder=idx,
                    file_size_bytes=row.get("size_bytes") or 0,
                    file_codec=row.get("codec") or "",
                )
                ui.set_grabbing(row)

            # File missing on disk? Skip — the rebuild flow should handle
            # cleaning up orphan DB rows separately.
            if not src_path.exists():
                stats.files_skipped += 1
                if ui is not None:
                    ui.advance(broken=True)
                    ui.log("warning", f"missing on disk: {src_path.name}")
                continue

            # ----- NORMAL-PASS SKIP -----
            # If this isn't a fresh pass and the row has an organised_at
            # timestamp that's newer than its last_seen (i.e. we
            # organised it more recently than its metadata was touched),
            # the canonical destination can't have shifted. Skip the
            # whole path-build + filesystem-stat work. Adds up to real
            # time on a 207k-row library where most rows haven't
            # changed since the last run.
            if not fresh_pass:
                organised_at = (row.get("organised_at") or "").strip()
                last_seen = (row.get("last_seen") or "").strip()
                # String comparison works correctly for the
                # 'YYYY-MM-DD HH:MM:SS' format both columns share.
                if organised_at and last_seen and organised_at >= last_seen:
                    stats.files_in_place += 1
                    if ui is not None:
                        ui.advance(size_bytes=row.get("size_bytes") or 0)
                    continue

            # Build the canonical destination path. build_destination_path
            # reads the record's metadata fields, doesn't touch the FS.
            # The `self_release_info` dict (sri) carries the album-level
            # decisions made above so every track in this group lands
            # in the same folder.
            dest_path = build_destination_path(
                row,
                album_type,
                destination_root=dest_root,
                organise_cfg=organise_cfg,
                self_release_info=sri,
            )

            # Already in the right place?
            try:
                already = (dest_path.resolve() == src_path.resolve())
            except OSError:
                already = (str(dest_path) == src_path_str)
            if already:
                stats.files_in_place += 1
                # Stamp organised_at so the next normal pass can skip
                # this row up front without doing the path-build work.
                if not dry_run:
                    try:
                        from database import _utcnow_sqlite
                        db.conn.execute(
                            "UPDATE files SET organised_at = ? WHERE path = ?",
                            (_utcnow_sqlite(), src_path_str),
                        )
                    except Exception:
                        pass
                if ui is not None:
                    ui.advance(size_bytes=row.get("size_bytes") or 0)
                continue

            # Resolve collisions — if there's already a different file at
            # the destination, suffix " (2)" etc. _unique_destination is
            # already defined above in this file.
            final_dest = _unique_destination(dest_path)

            if ui is not None:
                ui.update(organising_to=str(final_dest))

            if dry_run:
                stats.files_moved += 1
                if ui is not None:
                    ui.advance(imported=True, size_bytes=row.get("size_bytes") or 0)
                    ui.log("imported", f"WOULD move: {src_path.name} → "
                                       f"{final_dest.relative_to(dest_root)}")
                continue

            # Actually move + update DB row's path.
            try:
                _copy_or_move(src_path, final_dest, mode="move")
                stats.files_moved += 1
                moved_to_dirs.add(final_dest.parent)
                row_update = dict(row)
                row_update["path"] = str(final_dest.resolve())
                row_update["organised_path"] = str(final_dest.resolve())
                # Stamp the move time so the next normal pass can skip
                # this row up front.
                from database import _utcnow_sqlite
                row_update["organised_at"] = _utcnow_sqlite()
                with db.transaction():
                    db.upsert_file(row_update)
                    if str(final_dest.resolve()) != src_path_str:
                        db.delete_by_path(src_path_str)
                if ui is not None:
                    ui.advance(imported=True, size_bytes=row.get("size_bytes") or 0)
                    ui.log("imported",
                           f"{src_path.name} → "
                           f"{final_dest.relative_to(dest_root)}")
            except Exception as e:
                stats.errors.append(f"{src_path}: {e}")
                if ui is not None:
                    ui.advance(broken=True)
                    ui.log("broken", f"move failed: {src_path.name}  ({e})")

        # --- move extras (folder art, logs etc.) to follow the audio --------
        # If all audio files in this group relocated to the same destination
        # folder, drag any non-audio sidecar files (folder.jpg, .log, .cue…)
        # along with them.  Without this, a re-organise after Fetch Tags
        # leaves folder art stranded in the old pre-tag location.
        if not dry_run and len(moved_to_dirs) == 1:
            new_dir = next(iter(moved_to_dirs))
            extras_exts = set(
                organise_cfg.get("extras_extensions", [".jpg", ".log"])
            )
            try:
                for extra in sorted(folder.iterdir()):
                    if not extra.is_file():
                        continue
                    if extra.suffix.lower() not in extras_exts:
                        continue
                    target = new_dir / extra.name
                    if target.exists():
                        continue
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(extra), str(target))
                        if ui is not None:
                            ui.log("info",
                                   f"moved extra: {extra.name} → "
                                   f"{new_dir.relative_to(dest_root)}")
                    except Exception as e:
                        if ui is not None:
                            ui.log("warning",
                                   f"could not move extra {extra.name}: {e}")
            except OSError:
                pass

    # --- cleanup: remove empty source folders after the moves ----------------
    # Walk every folder we moved files OUT of (deepest first), check if
    # it became empty (or empty-after-removing-OS-junk), and rmdir it.
    # Then walk UP to ancestors — if removing a leaf folder leaves its
    # parent empty, that parent should go too. Stops at any source root
    # (so we never delete a user-configured source folder itself).
    if not dry_run:
        if ui is not None:
            ui.log("info", "cleaning up empty source folders…")

        # OS junk that doesn't count as "real content". Be conservative
        # — we only treat truly disposable filesystem litter this way.
        OS_JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini",
                          ".directory", ".localized"}
        ORPHAN_OK = bool(organise_cfg.get("delete_orphaned_extras", False))
        # Things that are arguably orphan-but-deletable AFTER the audio
        # moves out. Only active when ORPHAN_OK is on.
        ORPHAN_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp",
                              ".bmp", ".tiff", ".log", ".cue", ".m3u",
                              ".m3u8", ".txt", ".nfo", ".pls", ".sfv",
                              ".md5"}

        # Determine the SET of paths we must never delete — user's
        # configured source roots.
        source_roots = set()
        for s in (paths_cfg.get("sources") or []):
            try:
                source_roots.add(str(Path(s).expanduser().resolve()))
            except Exception:
                pass

        def _is_empty_enough(folder: Path) -> tuple[bool, list[Path]]:
            """Return (can_remove, junk_to_delete_first).
            'can_remove' means the folder contains only OS-junk and
            (if orphan-mode is on) disposable orphans."""
            try:
                children = list(folder.iterdir())
            except OSError:
                return False, []
            if not children:
                return True, []
            junk: list[Path] = []
            for c in children:
                if c.is_dir():
                    return False, []
                if c.name in OS_JUNK_FILES:
                    junk.append(c)
                    continue
                if ORPHAN_OK and c.suffix.lower() in ORPHAN_EXTENSIONS:
                    junk.append(c)
                    continue
                # Real content we don't recognise — leave it alone
                return False, []
            return True, junk

        def _try_remove(folder: Path) -> bool:
            """Try to remove `folder` if it's a non-source, empty-enough
            directory. Returns True if removed."""
            if not folder.exists() or not folder.is_dir():
                return False
            try:
                resolved = str(folder.resolve())
            except OSError:
                return False
            if resolved in source_roots:
                return False  # never delete user's configured source root
            can, junk = _is_empty_enough(folder)
            if not can:
                return False
            # Delete any junk first
            for j in junk:
                try:
                    j.unlink()
                except OSError:
                    return False  # bail if any junk file refuses to go
            try:
                folder.rmdir()
            except OSError:
                return False
            stats.folders_emptied += 1
            if ui is not None:
                msg = f"removed empty folder: {folder.name}"
                if junk:
                    msg += f"  (with {len(junk)} junk file{'s' if len(junk) != 1 else ''})"
                ui.log("info", msg)
            return True

        # Phase 1: try each touched folder, deepest first
        sorted_folders = sorted(touched_folders, key=lambda p: -p.count(os.sep))
        for folder_str in sorted_folders:
            _try_remove(Path(folder_str))

        # Phase 2: walk UP from each touched folder, removing now-empty
        # ancestors. This catches "Artist X/" emptying because both of
        # its album subfolders were removed in phase 1. We walk up until
        # we hit a source root or a non-empty folder.
        ancestors_to_check: list[Path] = []
        for folder_str in touched_folders:
            p = Path(folder_str).parent
            while True:
                try:
                    rp = str(p.resolve())
                except OSError:
                    break
                if rp in source_roots:
                    break
                # Don't walk above any source root we know about
                if any(rp == sr or rp.startswith(sr + os.sep) is False
                       for sr in source_roots) and not source_roots:
                    pass
                ancestors_to_check.append(p)
                parent = p.parent
                if parent == p:  # filesystem root
                    break
                p = parent
        # Deepest first
        for p in sorted(set(map(str, ancestors_to_check)),
                         key=lambda s: -s.count(os.sep)):
            _try_remove(Path(p))

    if ui is not None:
        ui.log("info", f"done. moved={stats.files_moved:,} "
                       f"in_place={stats.files_in_place:,} "
                       f"skipped={stats.files_skipped:,}")

    return stats
