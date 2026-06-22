"""
indexer.py
==========

Read-only walk over a directory tree (typically your already-organised
library) that populates / refreshes the database WITHOUT moving any files.

Two use cases:

1. You want to query your library by metadata but you haven't imported
   anything new — just index what's already on disk into SQL.
2. You did an import on machine A, machine B doesn't have the DB —
   rebuild it from the organised tree.

Skips files that are already in the DB AND haven't changed (same path,
same size, same mtime). Re-extracts metadata for new or changed files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from database import Database
from metadata import extract_metadata
from scanner import find_audio_files_flat


@dataclass
class IndexStats:
    files_seen: int = 0
    files_new: int = 0
    files_refreshed: int = 0
    files_unchanged: int = 0
    files_failed: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"audio files seen: {self.files_seen}\n"
            f"new: {self.files_new}\n"
            f"refreshed (changed): {self.files_refreshed}\n"
            f"unchanged (cached): {self.files_unchanged}\n"
            f"failed: {self.files_failed}"
        )


ProgressCallback = Callable[[str, int, int], None]


def index_tree(
    root: str | Path,
    *,
    cfg: dict[str, Any],
    db: Database,
    progress_cb: ProgressCallback | None = None,
    ui: Any = None,
    force: bool = False,
) -> IndexStats:
    """
    Walk `root`, extract metadata for every audio file, upsert into the DB.

    `force` disables the size+mtime cache check and re-extracts everything.
    Use it after you change `metadata.py` and want fresh values.

    `ui` is an optional LiveImportUI / PlainImportUI for the live panels.

    Performance notes for large libraries:
      - We DON'T materialise the whole file list before walking. We stream
        via the generator. There's an initial "counting" pass for the
        progress total — that's two walks total, which on cold cache costs
        I/O but on warm cache is cheap. For 12TB / 23k files expect the
        count pass to take ~5-10 seconds depending on backing storage.
      - DB writes batch every 500 rows in a single transaction. SQLite
        throughput jumps ~50x vs autocommit.
      - Cache hit path does ONE db.get_by_path() call; no duplicate query.
    """
    stats = IndexStats()
    import_cfg = cfg.get("import", {})

    audio_exts = import_cfg.get("audio_extensions", [".flac", ".mp3"])
    skip_hidden = import_cfg.get("skip_hidden", True)
    follow_symlinks = import_cfg.get("follow_symlinks", False)
    min_size = import_cfg.get("min_size_bytes", 0)

    # Initial pass: count files. We announce progress via the UI so the
    # user doesn't think the program froze on big trees.
    if ui is not None:
        ui.log("info", f"counting audio files under {root}…")
        ui.log("info", "(this can take a few minutes on cold-cache external storage)")
        ui.update(current_folder=str(root))

    from scanner import count_audio_files

    def _count_progress(current_dir: str, files_so_far: int, folders_so_far: int) -> None:
        if ui is None:
            return
        # Update the Folder panel with the current path being scanned,
        # and use File panel to show the running tally.
        ui.update(
            current_folder=current_dir or str(root),
            current_file=f"counting: {files_so_far:,} audio files in {folders_so_far:,} folders so far",
        )
        # Log every ~500 folders so the activity panel has a heartbeat.
        if folders_so_far % 500 == 0 and folders_so_far > 0:
            ui.log("info", f"still counting: {files_so_far:,} files in {folders_so_far:,} folders…")

    def _count_cache_hit(root_str: str, file_count: int, folder_count: int) -> None:
        if ui is None:
            return
        ui.log("info",
               f"using cached count: {file_count:,} files in {folder_count:,} "
               f"folders (re-walk skipped — bust by adding/removing files at the root)")

    total = count_audio_files(
        [root],
        audio_extensions=audio_exts,
        skip_hidden=skip_hidden,
        follow_symlinks=follow_symlinks,
        progress_cb=_count_progress,
        cache_hit_cb=_count_cache_hit,
    )

    if ui is not None:
        ui.set_total(total)
        if total == 0:
            ui.log("warning", f"no audio files found under {root}")
            ui.log("info", "(if you haven't imported yet, run option 1 first)")
            return stats
        ui.log("info", f"indexing {total:,} files under {root}")

    # Batch the DB writes. SQLite throughput on a single transaction
    # is dramatically better than autocommit-per-row. Size comes from
    # the active speed tier (Ludicrous = 2000, Plaid = 5000, etc).
    from speed import get_active_batch_size, get_active_level, hot_loop
    BATCH = get_active_batch_size()
    pending: list[dict[str, Any]] = []
    _speed_level = get_active_level()

    last_folder = ""
    folder_idx = 0
    # Time-based throttle for the scrolling activity log. We update
    # Folder/File panels every file (cheap, in-place), but only push a
    # new ROW into the activity log every 100ms — otherwise fast walks
    # turn the panel into an unreadable blur. Per-folder transitions
    # always log regardless of timing so you can see album boundaries.
    import time as _t
    last_log_time = 0.0
    LOG_MIN_INTERVAL = 0.1  # seconds

    file_iter = find_audio_files_flat(
        root,
        audio_extensions=audio_exts,
        skip_hidden=skip_hidden,
        follow_symlinks=follow_symlinks,
        min_size_bytes=min_size,
    )
    # hot_loop disables Python's cyclic GC during the tight per-file loop
    # if the active speed tier asks for it (Ridiculous and above). On
    # exit it re-enables GC and forces one collection to clean up the
    # accumulated garbage. ~5-10% speedup on bulk imports.
    with hot_loop(_speed_level):
      for idx, f in enumerate(file_iter, 1):
        stats.files_seen += 1
        if progress_cb:
            progress_cb(str(f), idx, total)

        # Track folder transitions for the UI. Folder boundaries always
        # log so the user can see album-by-album progress regardless of
        # the time-throttled per-file logs.
        cur_folder = str(f.parent)
        if cur_folder != last_folder:
            last_folder = cur_folder
            folder_idx = 1
            if ui is not None:
                ui.update(current_folder=cur_folder, file_index_in_folder=1)
                ui.log("info", f"→ {Path(cur_folder).name}")
        else:
            folder_idx += 1

        path_str = str(f.resolve())

        # Single DB lookup per file — used for both cache check and is_new.
        existing = db.get_by_path(path_str)

        # Cache check
        if not force and existing is not None:
            try:
                st = f.stat()
                if (existing.get("size_bytes") == st.st_size
                        and existing.get("mtime") is not None
                        and abs((existing["mtime"]) - st.st_mtime) < 1.0):
                    stats.files_unchanged += 1
                    if ui is not None:
                        ui.update(
                            current_file=str(f),
                            file_index_in_folder=folder_idx,
                            file_size_bytes=existing.get("size_bytes") or 0,
                            file_codec=existing.get("codec") or "",
                            organising_to=path_str,
                        )
                        ui.set_grabbing(existing)
                        ui.advance(size_bytes=existing.get("size_bytes") or 0)
                    continue
            except OSError:
                pass

        # Extract & queue
        try:
            rec = extract_metadata(f)
            rec["organised_path"] = path_str  # we're indexing in-place
            is_new = existing is None
            if is_new:
                stats.files_new += 1
            else:
                stats.files_refreshed += 1
            pending.append(rec)

            if ui is not None:
                fmt_detail = ""
                if rec.get("sample_rate") and rec.get("bit_depth"):
                    fmt_detail = f"{rec['bit_depth']}-bit / {rec['sample_rate'] / 1000:.1f} kHz"
                elif rec.get("bitrate"):
                    fmt_detail = f"{rec['bitrate'] // 1000} kbps"
                ui.update(
                    current_file=str(f),
                    file_index_in_folder=folder_idx,
                    file_size_bytes=rec.get("size_bytes") or 0,
                    file_codec=rec.get("codec") or "",
                    file_format_detail=fmt_detail,
                    organising_to=path_str,
                )
                ui.set_grabbing(rec)
                ui.advance(imported=is_new, size_bytes=rec.get("size_bytes") or 0)
                # Time-throttled log push: at most one entry per 100ms.
                # Last file always logs.
                now = _t.monotonic()
                if (now - last_log_time) >= LOG_MIN_INTERVAL or idx == total:
                    ui.log("imported" if is_new else "info",
                           f"{f.name}  ({'new' if is_new else 'refreshed'})")
                    last_log_time = now
        except Exception as e:
            stats.files_failed += 1
            stats.errors.append(f"{f}: {e}")
            if ui is not None:
                ui.advance(broken=True)
                ui.log("broken", f"{f.name}  ({e})")
            continue

        if len(pending) >= BATCH:
            with db.transaction():
                for r in pending:
                    db.upsert_file(r)
            pending.clear()

    if pending:
        with db.transaction():
            for r in pending:
                db.upsert_file(r)

    if ui is not None:
        ui.log("info", f"done. seen={stats.files_seen:,} "
                       f"new={stats.files_new:,} "
                       f"refreshed={stats.files_refreshed:,} "
                       f"unchanged={stats.files_unchanged:,} "
                       f"failed={stats.files_failed:,}")
    return stats
