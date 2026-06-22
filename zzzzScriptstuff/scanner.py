"""
scanner.py
==========

Walks a directory tree, yielding audio files grouped by their immediate
parent folder. Grouping matters because album-type detection (solo vs mix)
needs all the tracks in an album together.

Why we don't just call `os.walk` and process files one-by-one:
- The importer has to decide "is this album a mix?" before placing any
  of its files. That decision is made from the whole album's tags.
- So we yield (folder, [files]) tuples rather than individual paths.

The folder == "album" assumption is rough but standard. If a single folder
contains multiple albums (e.g. someone dumped all of an artist's discography
into one flat dir), the heuristic will still classify by tags — it just
might call the whole thing "one mix" because of artist diversity. The
config's `mix_artist_diversity_threshold` is the knob for that.
"""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterator


def _is_hidden(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def find_audio_folders(
    root: str | Path,
    *,
    audio_extensions: list[str],
    skip_hidden: bool = True,
    follow_symlinks: bool = False,
    min_size_bytes: int = 0,
) -> Iterator[tuple[Path, list[Path]]]:
    """
    Walk `root` recursively. Yield (folder_path, [audio_files_in_folder]).

    Only yields folders that contain at least one audio file. Files in
    subfolders are reported under their own parent, not the root.

    `audio_extensions` should be lowercase, leading-dot (`['.flac', '.mp3', ...]`).
    """
    root_path = Path(root).expanduser()
    if not root_path.exists():
        return

    ext_set = {e.lower() for e in audio_extensions}

    # We use os.walk with topdown=True so we can prune hidden dirs in-place.
    for dirpath, dirnames, filenames in os.walk(
        str(root_path), followlinks=follow_symlinks
    ):
        if skip_hidden:
            # Modify dirnames in place to prune the walk.
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        folder = Path(dirpath)
        if skip_hidden and _is_hidden(folder.relative_to(root_path)):
            continue

        audio_files: list[Path] = []
        for fn in filenames:
            if skip_hidden and fn.startswith("."):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in ext_set:
                continue
            full = folder / fn
            if min_size_bytes > 0:
                try:
                    if full.stat().st_size < min_size_bytes:
                        continue
                except OSError:
                    continue
            audio_files.append(full)

        if audio_files:
            yield folder, audio_files


def find_audio_files_flat(
    root: str | Path,
    *,
    audio_extensions: list[str],
    skip_hidden: bool = True,
    follow_symlinks: bool = False,
    min_size_bytes: int = 0,
) -> Iterator[Path]:
    """
    Flat iterator over every audio file under `root`. Convenient for the
    re-index command (where grouping doesn't matter, we're just rebuilding
    the DB from the already-organised tree).
    """
    for _, files in find_audio_folders(
        root,
        audio_extensions=audio_extensions,
        skip_hidden=skip_hidden,
        follow_symlinks=follow_symlinks,
        min_size_bytes=min_size_bytes,
    ):
        yield from files


def find_extras(
    folder: Path,
    *,
    extras_extensions: list[str],
    audio_extensions: list[str] | None = None,
    take_all_non_audio: bool = False,
    junk_filenames: list[str] | None = None,
    skip_hidden: bool = True,
) -> list[Path]:
    """
    Non-audio companion files (album art, log, cue, nfo, video, etc) in
    a folder. Used by the importer to drag bundled files along with the
    audio they belong to.

    Two modes:
      - take_all_non_audio=False (legacy strict): only files whose
        extension is in `extras_extensions` are returned.
      - take_all_non_audio=True (default in current config): EVERY file
        in the folder is returned UNLESS its extension is in
        `audio_extensions` (those go through the audio pipeline) or its
        filename is in `junk_filenames` (OS cruft).

    The take-all model is what most users want: if an album folder has
    a `signature.sig`, a `tour-flyer.pdf`, a `live-bonus.mkv`, or any
    other thing the rip came bundled with, it travels to the destination.
    """
    ext_set = {e.lower() for e in extras_extensions}
    audio_set = {e.lower() for e in (audio_extensions or [])}
    junk_set = {f.lower() for f in (junk_filenames or [])}

    result: list[Path] = []
    try:
        for entry in folder.iterdir():
            if not entry.is_file():
                continue
            if skip_hidden and entry.name.startswith("."):
                # Even in take-all mode, hidden files are usually OS junk
                # or VCS metadata we don't want.
                continue
            if entry.name.lower() in junk_set:
                continue
            ext = entry.suffix.lower()
            if take_all_non_audio:
                # Take anything that's NOT audio.
                if ext in audio_set:
                    continue
                result.append(entry)
            else:
                # Strict: only known-extras extensions.
                if ext in ext_set:
                    result.append(entry)
    except OSError:
        pass
    return result


def count_audio_files(
    roots: list[str | Path],
    *,
    audio_extensions: list[str],
    skip_hidden: bool = True,
    follow_symlinks: bool = False,
    progress_cb: Callable[[str, int, int], None] | None = None,
    use_cache: bool = True,
    cache_hit_cb: Callable[[str, int, int], None] | None = None,
) -> int:
    """
    Quick pre-scan count for progress bar setup. Walks every root but
    only stats filenames (no metadata read). Fast even on huge trees in
    cache; can be I/O-bound on cold external storage.

    `progress_cb(current_dir, files_so_far, folders_so_far)` is called
    every ~50 folders so callers can show a heartbeat to the user. With
    no callback, walks silently.

    `use_cache` (default True): consult the count cache before walking.
    On a cache hit, return the cached count immediately and skip the
    walk entirely. The cache key is (root_path, root_mtime), so adding
    or removing files in the tree busts it.

    `cache_hit_cb(root, files, folders)` is fired once per root when
    the cache short-circuits the walk, so the UI can announce
    "(using cached count: 23,462 files from 2 hours ago)" instead of
    silently skipping.
    """
    from checkpoint import get_cached_count, save_cached_count

    n = 0
    folders_seen = 0
    announce_every = 50
    ext_set = {e.lower() for e in audio_extensions}
    for root in roots:
        root_path = Path(root).expanduser()
        if not root_path.exists():
            continue

        # ----- cache short-circuit ---------------------------------
        if use_cache:
            hit = get_cached_count(root_path)
            if hit is not None:
                if cache_hit_cb:
                    try:
                        cache_hit_cb(str(root_path), hit["file_count"], hit["folder_count"])
                    except Exception:
                        pass
                n += hit["file_count"]
                folders_seen += hit["folder_count"]
                continue  # next root

        # ----- walk + count ----------------------------------------
        root_n_start = n
        root_folders_start = folders_seen
        for dirpath, dirnames, filenames in os.walk(
            str(root_path), followlinks=follow_symlinks
        ):
            if skip_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            folders_seen += 1
            for fn in filenames:
                if skip_hidden and fn.startswith("."):
                    continue
                if os.path.splitext(fn)[1].lower() in ext_set:
                    n += 1
            if progress_cb and folders_seen % announce_every == 0:
                try:
                    progress_cb(dirpath, n, folders_seen)
                except Exception:
                    pass  # never let a UI bug break the count

        # Save this root's count to the cache for next time.
        if use_cache:
            save_cached_count(
                root_path,
                file_count=n - root_n_start,
                folder_count=folders_seen - root_folders_start,
            )

    # Final callback so the caller knows the count is final.
    if progress_cb:
        try:
            progress_cb("", n, folders_seen)
        except Exception:
            pass
    return n
