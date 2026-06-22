"""
checkpoint.py
=============

Persistent state for long-running operations:

  - Count cache:  remember how many files are under a root, keyed on
                  (root_path, root_mtime). Skips the multi-minute count
                  phase entirely when nothing has changed.

  - Import checkpoint: track in-progress import/organise runs. On Ctrl+C
                  or crash, the checkpoint is left on disk; on next
                  startup we detect it and offer to resume.

Storage location: ~/.cache/music-organiser/ (XDG_CACHE_HOME respected).
Falls back to /tmp/music-organiser/ if cache is unwritable.

Both formats are plain JSON so you can inspect them by hand if you're
debugging weird resume behaviour.

Why JSON and not SQLite? These files are small (<1KB usually), are
read once per operation, and the format needs to be human-debuggable.
The library DB stays for the heavy work.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Cache directory resolution — mirrors organiser.py's debug log placement.
# ---------------------------------------------------------------------------

def cache_dir() -> Path:
    """Return a writable cache directory. Create it if needed."""
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "music-organiser"
    try:
        base.mkdir(parents=True, exist_ok=True)
        # Verify writability with a quick touch.
        probe = base / ".probe"
        probe.touch(exist_ok=True)
        probe.unlink(missing_ok=True)
        return base
    except OSError:
        pass
    # Fall back to /tmp
    fallback = Path("/tmp/music-organiser")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


# ---------------------------------------------------------------------------
# Atomic JSON write — avoid half-written files on crash mid-flush.
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, data: Any) -> None:
    """
    Write JSON atomically: write to a sibling temp file, then rename.
    On POSIX this is atomic — readers either see the old version or the
    new version, never a partial write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2, default=str)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    """Read JSON, returning None if the file is missing or corrupt."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, json.JSONDecodeError):
        return None


# =============================================================================
# COUNT CACHE
# =============================================================================
#
# Keyed on (root_path, root_mtime). When the count function finishes
# walking a tree, it caches the result. Next time we want a count for
# the same root, we check the cache first.
#
# Invalidation: if root_mtime has changed (any sub-add/remove updates it
# on most filesystems), we re-count. This is a conservative invalidator
# — on filesystems where mtime doesn't propagate, the cache appears
# permanent. That's why we expose an explicit `force` flag.
#
# Why not also include sub-dir mtimes? Because the whole point is to
# avoid walking the tree. If we check every sub-dir to validate the
# cache, we've already done the expensive part. Top-level mtime is
# good-enough for most use cases; correctness is restorable via force.

COUNT_CACHE_FILE = "count_cache.json"
COUNT_CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600  # 1 week — re-count older entries


def _count_cache_path() -> Path:
    return cache_dir() / COUNT_CACHE_FILE


def _path_signature(root: str | Path) -> dict[str, Any]:
    """Compute the cache key components for a directory tree."""
    root = Path(root).expanduser()
    if not root.exists():
        return {"path": str(root), "exists": False}
    try:
        st = root.stat()
        return {
            "path": str(root.resolve()),
            "exists": True,
            "mtime": st.st_mtime,
        }
    except OSError:
        return {"path": str(root), "exists": False}


def get_cached_count(root: str | Path) -> dict[str, Any] | None:
    """
    Return cached count info for `root` if available and fresh.

    Returns a dict with keys: file_count, folder_count, cached_at, age_seconds
    or None if no usable cache exists.
    """
    cache = _safe_read_json(_count_cache_path()) or {}
    sig = _path_signature(root)
    if not sig["exists"]:
        return None
    entries = cache.get("entries", {})
    entry = entries.get(sig["path"])
    if entry is None:
        return None
    # Match on mtime — if the tree's top-level mtime changed, invalidate.
    # Tolerance is small enough to catch any deliberate change but loose
    # enough that filesystem rounding (some FS have <1s resolution but
    # round on storage) doesn't cause false misses.
    if abs(entry.get("mtime", 0) - sig["mtime"]) > 0.1:
        return None
    # Age check
    age = time.time() - entry.get("cached_at", 0)
    if age > COUNT_CACHE_MAX_AGE_SECONDS:
        return None
    return {
        "file_count": entry.get("file_count", 0),
        "folder_count": entry.get("folder_count", 0),
        "cached_at": entry.get("cached_at", 0),
        "age_seconds": age,
    }


def save_cached_count(
    root: str | Path,
    file_count: int,
    folder_count: int,
) -> None:
    """Record a count result for future fast retrieval."""
    cache = _safe_read_json(_count_cache_path()) or {"entries": {}}
    entries = cache.setdefault("entries", {})
    sig = _path_signature(root)
    if not sig["exists"]:
        return
    entries[sig["path"]] = {
        "path": sig["path"],
        "mtime": sig["mtime"],
        "file_count": file_count,
        "folder_count": folder_count,
        "cached_at": time.time(),
    }
    try:
        _atomic_write_json(_count_cache_path(), cache)
    except OSError:
        pass  # cache failure is non-fatal


def clear_count_cache(root: str | Path | None = None) -> None:
    """
    Drop one entry (when root is given) or the whole cache.
    """
    if root is None:
        try:
            _count_cache_path().unlink(missing_ok=True)
        except OSError:
            pass
        return
    cache = _safe_read_json(_count_cache_path()) or {"entries": {}}
    entries = cache.get("entries", {})
    sig = _path_signature(root)
    entries.pop(sig["path"], None)
    try:
        _atomic_write_json(_count_cache_path(), cache)
    except OSError:
        pass


# =============================================================================
# IMPORT CHECKPOINT
# =============================================================================
#
# When an import (or rebuild) starts, we write a checkpoint with the
# operation parameters. As work proceeds we update progress fields. On
# normal completion the checkpoint is deleted. On Ctrl+C / crash, the
# file is left on disk.
#
# On next startup, cmd_import checks for a matching checkpoint. If found
# AND the sources + destination match, it offers to resume.
#
# "Resume" in our case is mostly automatic: the database tracks
# already-imported files, and the importer's per-file path lookup skips
# them. The checkpoint just enables the prompt and shows the user where
# they were.

CHECKPOINT_FILE = "import_checkpoint.json"


@dataclass
class ImportCheckpoint:
    """Persistent state for an in-progress import / rebuild operation."""
    operation: str = "import"       # "import" or "rebuild"
    started_at: float = 0.0
    last_updated_at: float = 0.0
    sources: list[str] = field(default_factory=list)
    destination_root: str = ""
    phase: str = "starting"          # starting | counting | organising | complete
    total_files: int = 0
    files_done: int = 0
    last_file_processed: str = ""
    stats_imported: int = 0
    stats_duplicate: int = 0
    stats_broken: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _checkpoint_path() -> Path:
    return cache_dir() / CHECKPOINT_FILE


def load_checkpoint() -> ImportCheckpoint | None:
    """Read the on-disk checkpoint, if any."""
    data = _safe_read_json(_checkpoint_path())
    if data is None:
        return None
    try:
        return ImportCheckpoint(**data)
    except TypeError:
        # Schema drift — old format. Discard.
        return None


def save_checkpoint(cp: ImportCheckpoint) -> None:
    """Persist the checkpoint atomically."""
    cp.last_updated_at = time.time()
    try:
        _atomic_write_json(_checkpoint_path(), cp.to_dict())
    except OSError:
        pass


def clear_checkpoint() -> None:
    """Remove the checkpoint (after successful completion)."""
    try:
        _checkpoint_path().unlink(missing_ok=True)
    except OSError:
        pass


def checkpoint_matches(
    cp: ImportCheckpoint,
    sources: list[str],
    destination_root: str,
) -> bool:
    """
    Does this checkpoint describe the same operation as what we're
    about to run? Used to decide whether to offer resume.
    """
    if cp.operation != "import":
        return False
    if sorted(cp.sources) != sorted(sources):
        return False
    if cp.destination_root != destination_root:
        return False
    # Don't offer to resume a "complete" checkpoint — that's stale.
    if cp.phase == "complete":
        return False
    return True


def describe_checkpoint(cp: ImportCheckpoint) -> str:
    """Human-readable summary used in the resume prompt."""
    import datetime
    when = datetime.datetime.fromtimestamp(cp.started_at).strftime("%Y-%m-%d %I:%M %p").lstrip("0").lower()
    if cp.total_files > 0:
        pct = (cp.files_done / cp.total_files) * 100
        progress = f"{cp.files_done:,} / {cp.total_files:,} files ({pct:.1f}%)"
    else:
        progress = f"phase: {cp.phase}"
    return (
        f"  Started: {when}\n"
        f"  Progress: {progress}\n"
        f"  imp={cp.stats_imported:,}  dup={cp.stats_duplicate:,}  bad={cp.stats_broken:,}\n"
        f"  Last file: {cp.last_file_processed[:80]}"
    )


# ---------------------------------------------------------------------------
# Fetch-metadata checkpoint
# ---------------------------------------------------------------------------
# The metadata-fetch pass iterates over (artist, album) tuples and queries
# online providers for each. On a 100k+ album library this takes many
# hours. When the run is interrupted (Ctrl+C, freeze, power loss, OOM),
# we want the NEXT run to skip over albums we've already finished.
#
# Strategy:
#   - At the start of a fetch run, write a checkpoint identifying the run
#     (timestamp, db path, provider list, target columns, only_missing
#     flag — enough to know whether the next run is "the same operation").
#   - After EACH album finishes, append its (artist, album) tuple to the
#     checkpoint's `processed_albums` list and rewrite the file atomically.
#   - On startup, if a matching checkpoint exists, offer to resume — and
#     when the user says yes, skip any (artist, album) tuples already in
#     the processed list.
#
# Storage format: plain JSON (same dir as import checkpoint), but with a
# different filename so the two never collide. `processed_albums` is
# stored as a list-of-pairs rather than a set (JSON has no native set);
# the consumer converts back to a set for O(1) lookup.
#
# Failure modes:
#   - JSON write fails → silently skipped, run continues (we'd rather
#     re-do work than crash the whole pass).
#   - Schema drift between versions → checkpoint discarded, full re-run.
#   - User changes the db path or provider list → checkpoint considered
#     stale by `fetch_checkpoint_matches`, full re-run.

FETCH_CHECKPOINT_FILE = "fetch_checkpoint.json"


@dataclass
class FetchCheckpoint:
    """Persistent state for an in-progress metadata-fetch operation."""
    started_at: float = 0.0
    last_updated_at: float = 0.0
    db_path: str = ""                       # which library this is for
    target_columns: list[str] = field(default_factory=list)
    provider_ids: list[str] = field(default_factory=list)
    only_missing: bool = True
    skip_complete_albums: bool = False
    write_to_files: bool = True
    total_albums: int = 0
    # The album tuples we've finished processing. JSON can't store
    # tuples or sets — flatten to a list of [artist, album] pairs.
    # Consumer side converts back to set[tuple[str,str]] for fast lookup.
    processed_albums: list[list[str]] = field(default_factory=list)
    stats_files_updated: int = 0
    stats_files_no_match: int = 0
    stats_fields_updated: int = 0
    last_album: list[str] = field(default_factory=list)   # [artist, album]
    phase: str = "starting"                  # starting | fetching | complete

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fetch_checkpoint_path() -> Path:
    return cache_dir() / FETCH_CHECKPOINT_FILE


def load_fetch_checkpoint() -> FetchCheckpoint | None:
    """Read the on-disk fetch checkpoint, if any."""
    data = _safe_read_json(_fetch_checkpoint_path())
    if data is None:
        return None
    try:
        return FetchCheckpoint(**data)
    except TypeError:
        # Schema drift between versions — discard so the user gets a
        # clean run rather than a confusing partial-skip behaviour.
        return None


def save_fetch_checkpoint(cp: FetchCheckpoint) -> None:
    """Persist the fetch checkpoint atomically. Called once per album."""
    cp.last_updated_at = time.time()
    try:
        _atomic_write_json(_fetch_checkpoint_path(), cp.to_dict())
    except OSError:
        # Per-album writes are best-effort. A failure here loses at
        # most one album's worth of resume info — not worth crashing
        # the whole fetch run.
        pass


def clear_fetch_checkpoint() -> None:
    """Remove the fetch checkpoint (after successful completion)."""
    try:
        _fetch_checkpoint_path().unlink(missing_ok=True)
    except OSError:
        pass


def fetch_checkpoint_matches(
    cp: FetchCheckpoint,
    db_path: str,
    target_columns: list[str],
    provider_ids: list[str],
    only_missing: bool,
    write_to_files: bool,
) -> bool:
    """
    Does this checkpoint describe the SAME fetch operation as what we're
    about to run? Several signals:

      - Same database file → required
      - Same provider IDs and order → required (different providers might
        return different data, can't blindly skip)
      - Same target_columns → required (user might want to ADD bpm in a
        second pass; we can't skip albums that haven't gone through the
        bpm path)
      - Same only_missing flag → required
      - Same write_to_files flag → required
      - phase != "complete"

    If ANY of these differ, the checkpoint is for a different operation
    and we should NOT skip its `processed_albums` list. Better to re-do
    work than to silently leave a column unfilled.
    """
    if cp.phase == "complete":
        return False
    if cp.db_path != db_path:
        return False
    if sorted(cp.target_columns) != sorted(target_columns):
        return False
    if sorted(cp.provider_ids) != sorted(provider_ids):
        return False
    if cp.only_missing != only_missing:
        return False
    if cp.write_to_files != write_to_files:
        return False
    return True


def describe_fetch_checkpoint(cp: FetchCheckpoint) -> str:
    """Human-readable summary for the resume prompt."""
    import datetime
    when = datetime.datetime.fromtimestamp(cp.started_at).strftime(
        "%Y-%m-%d %I:%M %p"
    ).lstrip("0").lower()
    done = len(cp.processed_albums)
    pct = (done / cp.total_albums * 100) if cp.total_albums else 0.0
    last = " - ".join(cp.last_album) if cp.last_album else "(none)"
    return (
        f"  Started: {when}\n"
        f"  Progress: {done:,} / {cp.total_albums:,} albums ({pct:.1f}%)\n"
        f"  Files updated: {cp.stats_files_updated:,}  "
        f"fields: {cp.stats_fields_updated:,}  "
        f"no-match: {cp.stats_files_no_match:,}\n"
        f"  Providers: {', '.join(cp.provider_ids)}\n"
        f"  Last album: {last[:80]}"
    )

