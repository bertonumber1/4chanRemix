"""
integrations/onetagger.py
=========================

Integration with OneTagger (https://github.com/Marekkon5/onetagger), an
open-source music tagger that pulls metadata from Beatport, Discogs,
MusicBrainz, Spotify, Traxsource, Juno, and others.

What we actually do (honest scope):

1. **Detect** an installed OneTagger binary on $PATH or in a few common
   locations.
2. **Launch the GUI**, optionally with `--path <folder>` to pre-point it
   at folders our DB has flagged as having bad metadata.
3. **Generate work-lists**: dump CSVs of files our audits flagged so you
   can drag-and-drop those folders into OneTagger.

What we don't do (yet):

- Programmatic batch tagging via a OneTagger-CLI. OneTagger has a CLI
  binary in `crates/onetagger-cli`, but its interface is config-file
  driven and not super stable across versions. Adding that would mean
  writing and maintaining a config template that mirrors OneTagger's
  internal types — risky. If you want it, we can revisit once you've
  picked a OneTagger version.

After OneTagger writes new tags back to files, just run the indexer
(menu option 2) and the database will pick up the new metadata.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


# Names of the binary OneTagger ships as. The official release is
# `onetagger` on Linux; the AppImage and the dev build sometimes use
# `OneTagger` (capitalised).
BINARY_NAMES = ("onetagger", "OneTagger", "onetagger-cli", "OneTagger-cli")

# Common manual-install locations on Linux to check beyond $PATH.
SEARCH_PATHS = (
    "/usr/local/bin",
    "/opt/onetagger",
    "/opt/OneTagger",
    "~/.local/bin",
    "~/Applications",
    "~/bin",
)


def find_onetagger() -> Path | None:
    """Return the path to a OneTagger binary if one is installed, else None."""
    # PATH first
    for name in BINARY_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)

    # Then a few common manual-install paths
    for base in SEARCH_PATHS:
        base_path = Path(base).expanduser()
        if not base_path.exists():
            continue
        for name in BINARY_NAMES:
            candidate = base_path / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        # AppImage often lives directly inside one of these dirs
        try:
            for entry in base_path.iterdir():
                if not entry.is_file():
                    continue
                lower = entry.name.lower()
                if "onetagger" in lower and (
                    entry.suffix.lower() in (".appimage", "") and os.access(entry, os.X_OK)
                ):
                    return entry
        except OSError:
            continue

    return None


def launch_onetagger(
    binary: Path | None = None,
    *,
    folder: str | Path | None = None,
    detach: bool = True,
) -> subprocess.Popen | None:
    """
    Launch OneTagger. If `folder` is provided, OneTagger is started with
    `--path <folder>` (which OneTagger interprets as "open this folder").

    Returns the Popen object if launched, None if no binary was found.
    `detach` runs it in the background so your menu doesn't block.

    Note: OneTagger's CLI flags vary by version. We pass `--path` because
    that's the common one. If your version uses something different, edit
    the args list below.
    """
    binary = binary or find_onetagger()
    if binary is None:
        return None

    args = [str(binary)]
    if folder is not None:
        args += ["--path", str(folder)]

    try:
        if detach:
            # Detach so the menu can continue immediately; OneTagger has
            # its own GUI.
            return subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            return subprocess.Popen(args)
    except Exception:
        return None


def install_hint() -> str:
    """Friendly hint for the user about installing OneTagger."""
    return (
        "OneTagger isn't installed (or isn't on $PATH).\n"
        "Download a release from:\n"
        "  https://github.com/Marekkon5/onetagger/releases\n"
        "On Arch:\n"
        "  yay -S onetagger     # AUR\n"
        "Or grab the Linux AppImage and put it in ~/.local/bin/."
    )


def collect_bad_metadata_folders(db, limit: int = 50) -> list[str]:
    """
    Walk the database for files with missing/junk metadata, return the
    list of unique folder paths so the user can hand them to OneTagger
    in batches.

    "Bad metadata" = missing artist OR missing album OR title contains
    track-number prefix OR placeholder artist.
    """
    placeholders = (
        "unknown artist", "unknown", "various", "n/a", "untagged",
    )
    placeholder_clause = " OR ".join(f"LOWER(TRIM(artist)) = ?" for _ in placeholders)
    sql = f"""
        SELECT DISTINCT
            CASE
              WHEN organised_path IS NOT NULL AND organised_path != ''
                THEN organised_path
              ELSE path
            END as p
        FROM files
        WHERE status != 'broken'
          AND (
            artist IS NULL OR TRIM(artist) = ''
            OR album IS NULL OR TRIM(album) = ''
            OR title IS NULL OR TRIM(title) = ''
            OR ({placeholder_clause})
            OR title GLOB '[0-9][0-9]*-*'
          )
    """
    rows = db.conn.execute(sql, list(placeholders)).fetchall()
    folders: list[str] = []
    seen: set[str] = set()
    for r in rows:
        p = r["p"]
        if not p:
            continue
        folder = str(Path(p).parent)
        if folder not in seen:
            seen.add(folder)
            folders.append(folder)
            if len(folders) >= limit:
                break
    return folders
