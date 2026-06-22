"""
folder_classifier.py
====================

Content-aware classifier for IMPORT-TIME triage. Walks a source folder
and decides what each subfolder actually is, based on file CONTENTS
(not just folder names — that approach false-positives on legit comp
folders with bland names like "VA" or "various").

Four classifications, in priority order:

  MUSIC         — contains real audio (1+ audio files). The importer
                  handles this normally. Cover art, log files, cue
                  sheets, NFOs, m3u playlists are EXPECTED and don't
                  change the classification.

  ORPHAN_BONUS  — contains a small amount of audio (1-2 files) BUT
                  the folder looks non-musical overall: lots of images
                  / docs / videos / random downloads alongside. The
                  audio files should be rescued to `orphan_folder` but
                  the rest of the folder is junk.

  RANDOM_CRAP   — no audio at all, but contains 3+ files of various
                  types (images, archives, docs, executables, etc).
                  This is downloads-folder detritus. Move the WHOLE
                  folder to `out_of_library_dest` so it stops showing
                  up on every import run.

  EMPTY_OR_TRIVIAL — empty folder, or just 1-2 stray non-audio files
                  (a cover.jpg sitting alone, an empty README.txt).
                  Not worth moving — leave it alone.

Why "3+ random files" is the threshold for RANDOM_CRAP: empirically,
a folder with 1-2 non-audio files alongside zero audio is almost
always either a folder waiting for music to be added, or junk the user
already meant to clean up later. 3+ files indicates active mess.

KNOWN BUGS / LIMITS (honest):
  - This is per-folder; nested junk (e.g. /downloads/Album/extra junk/)
    gets routed by its own classification, not the parent's.
  - We don't peek INSIDE archives (.zip, .rar). A .zip full of audio
    looks like "1 random file" to us.
  - Music releases with a *huge* artwork dump (50+ images, sometimes
    seen on Discogs uploads) will still classify as MUSIC because
    they have audio — the "many extras" don't override audio presence.
    That's intentional: if there's audio, we treat it as music.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable


class FolderKind(str, Enum):
    MUSIC = "music"
    ORPHAN_BONUS = "orphan_bonus"
    RANDOM_CRAP = "random_crap"
    EMPTY_OR_TRIVIAL = "empty_or_trivial"


# Extensions that are EXPECTED in a music release folder. Their presence
# doesn't push the folder toward "random crap".
_MUSIC_RELEASE_COMPANIONS = {
    # Cover art / scans
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif",
    ".gif",
    # Rip logs and verification
    ".log", ".txt", ".md", ".nfo", ".cue", ".accurip", ".sfv",
    ".m3u", ".m3u8", ".pls", ".cuesheet",
    # Tag-side data
    ".xml", ".json", ".yaml", ".yml",
}

# Extensions that scream "not a music release". Many of these are
# perfectly normal in someone's downloads folder, but they don't belong
# in a music library.
_NON_MUSIC_MARKERS = {
    # Documents and ebooks
    ".pdf", ".epub", ".mobi", ".azw", ".azw3", ".djvu",
    ".doc", ".docx", ".odt", ".rtf", ".pages",
    # Archives — could be anything, but they shouldn't sit in a music
    # release alongside the audio. If extracted properly, the audio
    # would be present.
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tar.gz",
    ".tgz", ".tbz", ".iso", ".bin",
    # Video files (rare in music releases — sometimes a music-video
    # bonus, but more often it's a misplaced movie download)
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv",
    # Executables, installers, software
    ".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm",
    ".apk", ".ipa", ".appimage",
    # Source / code (clearly not music)
    ".py", ".js", ".html", ".css", ".c", ".cpp", ".h",
    ".java", ".rs", ".go",
    # Office / spreadsheet
    ".xls", ".xlsx", ".csv", ".ods", ".ppt", ".pptx", ".odp",
    # Random downloads (torrent state files, etc)
    ".torrent", ".part", ".crdownload", ".aria2",
}

# Default thresholds — tunable via classify_folder() params.
DEFAULT_RANDOM_CRAP_MIN_FILES = 3
DEFAULT_ORPHAN_BONUS_MAX_AUDIO = 2


@dataclass
class FolderProfile:
    """Detailed breakdown of one folder's contents."""
    path: Path
    audio_files: list[Path] = field(default_factory=list)
    companion_files: list[Path] = field(default_factory=list)
    non_music_files: list[Path] = field(default_factory=list)
    other_files: list[Path] = field(default_factory=list)  # extension not in either set
    subdirs: list[Path] = field(default_factory=list)
    total_size_bytes: int = 0

    @property
    def n_audio(self) -> int:
        return len(self.audio_files)

    @property
    def n_companion(self) -> int:
        return len(self.companion_files)

    @property
    def n_non_music(self) -> int:
        return len(self.non_music_files)

    @property
    def n_other(self) -> int:
        return len(self.other_files)

    @property
    def n_total_files(self) -> int:
        return (self.n_audio + self.n_companion
                + self.n_non_music + self.n_other)


def profile_folder(
    folder: Path | str,
    audio_extensions: Iterable[str],
    *,
    skip_hidden: bool = True,
) -> FolderProfile:
    """
    Inventory a folder's IMMEDIATE contents (not recursive). Returns
    a FolderProfile with counts and lists for each file category.

    NOTE: companion-file recognition is CONDITIONAL on audio presence.
    A `.jpg` in a folder with audio is treated as cover art (companion).
    A `.jpg` in a folder with NO audio is treated as a regular file
    (counts toward random-crap detection). Same for `.txt`, `.log`,
    `.nfo` etc — they're "companion" only when there's music for them
    to accompany. This stops the classifier mis-labelling a folder of
    screenshots + a readme as "empty_or_trivial" just because all the
    extensions overlap with music-companion types.
    """
    p = Path(folder)
    profile = FolderProfile(path=p)
    if not p.is_dir():
        return profile

    audio_exts = {e.lower().lstrip(".") for e in audio_extensions}
    audio_exts = {f".{e}" for e in audio_exts}   # normalise to ".flac" form

    # First pass: collect everything into buckets, but defer the
    # companion-vs-other decision until we know if audio is present.
    pending_companion: list[Path] = []
    try:
        with os.scandir(p) as it:
            for entry in it:
                name = entry.name
                if skip_hidden and name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        profile.subdirs.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                ext = os.path.splitext(name)[1].lower()
                file_path = Path(entry.path)
                try:
                    profile.total_size_bytes += entry.stat().st_size
                except OSError:
                    pass
                if ext in audio_exts:
                    profile.audio_files.append(file_path)
                elif ext in _MUSIC_RELEASE_COMPANIONS:
                    # Defer — decide after we know if audio is present.
                    pending_companion.append(file_path)
                elif ext in _NON_MUSIC_MARKERS:
                    profile.non_music_files.append(file_path)
                else:
                    profile.other_files.append(file_path)
    except OSError:
        return profile

    # Now resolve companion vs other. If audio is present in this
    # folder, treat companion-eligible files as legitimate music
    # companions. Otherwise treat them as regular junk that counts
    # toward random-crap detection.
    if profile.audio_files:
        profile.companion_files = pending_companion
    else:
        # No audio → these aren't companions, they're just files.
        # Use the same "other" bucket so they count toward junk.
        profile.other_files.extend(pending_companion)

    return profile


def classify_folder(
    folder: Path | str,
    audio_extensions: Iterable[str],
    *,
    skip_hidden: bool = True,
    random_crap_min_files: int = DEFAULT_RANDOM_CRAP_MIN_FILES,
    orphan_bonus_max_audio: int = DEFAULT_ORPHAN_BONUS_MAX_AUDIO,
) -> tuple[FolderKind, FolderProfile]:
    """
    Classify a single folder by its IMMEDIATE contents.

    Decision rules, evaluated in order:

      1. If audio count > orphan_bonus_max_audio (default 2) → MUSIC
         (a proper release; >2 audio files = album, even if other
         junk is alongside)

      2. If audio count == 0:
         - If non-music + other files >= random_crap_min_files
           (default 3) → RANDOM_CRAP
         - Else → EMPTY_OR_TRIVIAL

      3. If audio count is 1-2 AND non-music files >= 2 → ORPHAN_BONUS
         (small amount of audio swimming in junk — rescue the audio
         but the folder isn't a music release)

      4. Otherwise → MUSIC (audio present, no overwhelming junk)

    Companion files (cover.jpg, log, cue, nfo) NEVER push toward
    non-music classification. They're expected in a music release.

    Returns (kind, profile) — caller may want the profile for logging
    or for moving specific files (orphan rescue).
    """
    profile = profile_folder(folder, audio_extensions, skip_hidden=skip_hidden)

    # Rule 1: clearly a music release — >2 audio files.
    if profile.n_audio > orphan_bonus_max_audio:
        return FolderKind.MUSIC, profile

    # Rule 2: no audio at all.
    if profile.n_audio == 0:
        # The "junk count" excludes companion files — having a cover.jpg
        # alone shouldn't trip random-crap.
        junk_count = profile.n_non_music + profile.n_other
        if junk_count >= random_crap_min_files:
            return FolderKind.RANDOM_CRAP, profile
        return FolderKind.EMPTY_OR_TRIVIAL, profile

    # Rule 3: 1-2 audio files, but lots of non-music stuff alongside.
    # This is the trickiest case. We require at least 2 non-music
    # markers (not just companions — those are expected even around
    # a single-track release).
    if profile.n_non_music >= 2:
        return FolderKind.ORPHAN_BONUS, profile

    # Rule 4: default to MUSIC when audio is present and no clear
    # junk signal. A solo track in a folder with just a cover.jpg
    # is a legitimate single release.
    return FolderKind.MUSIC, profile


def classify_tree(
    root: Path | str,
    audio_extensions: Iterable[str],
    *,
    skip_hidden: bool = True,
    random_crap_min_files: int = DEFAULT_RANDOM_CRAP_MIN_FILES,
    orphan_bonus_max_audio: int = DEFAULT_ORPHAN_BONUS_MAX_AUDIO,
):
    """
    Walk `root` recursively and yield (folder, kind, profile) for every
    subdirectory. Used by the import flow to triage everything before
    starting the actual import.

    Yields in os.walk order (parents before children) — but each yield
    is independent. A child folder's classification doesn't depend on
    its parent.

    Important: this iterator does NOT prune the walk when it hits
    RANDOM_CRAP. The caller may want to know everything that's down
    there for logging purposes, even if it's all going to be moved.
    """
    root_path = Path(root).expanduser()
    if not root_path.exists():
        return

    for dirpath, dirnames, _ in os.walk(str(root_path), topdown=True):
        if skip_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        folder = Path(dirpath)
        kind, profile = classify_folder(
            folder,
            audio_extensions=audio_extensions,
            skip_hidden=skip_hidden,
            random_crap_min_files=random_crap_min_files,
            orphan_bonus_max_audio=orphan_bonus_max_audio,
        )
        yield folder, kind, profile
