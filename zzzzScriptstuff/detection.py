"""
detection.py
============

Thorough metadata-pattern detection. Each routine returns a structured
finding so callers can both ACT (e.g. switch the MB lookup to use
'Various Artists' for a compilation) and EXPLAIN (log why this album is
classed as a mix). Honest detection over "best guess" — we'd rather
mark something ambiguous than confidently misclassify.

Module owes a debt to OneTagger's matching heuristics, but goes further
in two specific areas:

  - COMPILATION / DJ-MIX DETECTION
    Many real-world libraries (anime/game OSTs, eurobeat compilations,
    DJ-mix CDs, bootleg "Selection" releases) have a track-artist in
    the `artist` field but a compilation title in `album`. MusicBrainz
    indexes these under album_artist="Various Artists" or the DJ's
    name, so querying with the track-artist produces 100% no-match.
    Detect these structurally and route the query differently.

  - TITLE / FILENAME RESCUE
    When a tagger has pulled garbage into the title field — e.g.
    "01. Artist - Song (Original Mix) [Label 2020]" — or when the
    filename is the only source of structured info, we can extract
    artist/title/year/version from those strings and offer the parsed
    bits as candidates. This doesn't AUTO-FIX (too risky for a 207k
    library) but surfaces the parsed values so the user / MB-fetch
    can use them.

Public API:
  - classify_album(records, cfg) -> AlbumClassification
  - parse_title(title, filename) -> ParsedTitle
  - normalise_query(album_string) -> str (cleans for provider lookup)
  - looks_like_dj_set(record) -> bool (single-file, long duration)
  - looks_like_compilation_album_name(album) -> bool

Everything is regex/string analysis on already-extracted metadata.
We don't open audio files or hit the network here.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# =============================================================================
# CONSTANTS — patterns that strongly suggest "this is a compilation/mix"
# =============================================================================
#
# Compiled regex objects up here. Inline regex in hot loops is fine in
# Python (compile is cached) but keeping them named makes the intent
# explicit and lets tests pin them.

# Words/phrases in an album name that nearly guarantee a compilation or
# mix release. Case-insensitive substring matches. Curated from looking
# at the actual no-match examples in the user's library (eurobeat,
# OSTs, "Best Selection" series, etc.) plus standard general patterns.
COMPILATION_ALBUM_PATTERNS = [
    # Mix / DJ set markers
    r"\bdj\s+\w+\s+(presents?|mix|set)\b",
    r"\bmixed\s+by\b",
    r"\b(continuous|non[\s-]?stop|nonstop)\s+mix\b",
    r"\b(megamix|mega\s+mix|big\s+mix|club\s+mix\s+vol)\b",
    r"\bmashup\b",
    r"\bessential\s+mix\b",
    r"\bpodcast\s*\d*\b",
    r"\b(radio|broadcast)\s+show\b",
    r"\bsessions?\s+\d{2,4}\b",
    # Compilation markers
    r"\b(various\s+artists?|v\.?a\.?)\b",
    r"\bcompilation\b",
    r"\bbest\s+(of|hits|selection|songs?|tracks?|collection|album|eurobeat)\b",
    r"\bgreatest\s+hits\b",
    r"\bnow\s+that'?s\s+what\s+i\s+call\b",
    r"\b(presents?|selected\s+by|curated\s+by|chosen\s+by)\b",
    r"\bthe\s+very\s+best\s+of\b",
    r"\b(top|hot)\s+\d{2,3}\b",
    r"\bclub\s+(classics|anthems|nation)\b",
    r"\b(decade|retrospective|anthology)\b",
    # Date range — but only when adjacent to compilation context.
    # Bare "1985-1992" matches real solo album titles too (Aphex Twin's
    # "Selected Ambient Works 85-92"), so require a comp-marker nearby.
    r"\b(hits|best|classics|collection|selection|anthology|compilation|years?)\b[^a-z]{0,15}\b\d{4}[\s-]+\d{4}\b",
    r"\b\d{4}[\s-]+\d{4}\b[^a-z]{0,15}\b(hits|best|classics|collection|selection)\b",
    # Genre-specific compilation series patterns
    r"\b(super\s+)?eurobeat\b",       # Super Eurobeat series (the user's case)
    r"\binitial\s+d\b",               # Initial D anime series
    r"\b(dance\s+dance\s+revolution|ddr)\b",
    r"\bbemani\b",
    r"\b(touhou|fate|vocaloid)\b",    # often per-track-artist anthologies
    r"\b(ost|o\.s\.t\.?|original\s+soundtrack)\b",
    r"\bbgm\s+collection\b",
    r"\banime\s+(songs?|hits|collection)\b",
    # Volume / series patterns — usually indicates a numbered comp series
    r"\b(vol(ume)?|pt\.?|part)\.?\s*\d+\b",
    r"\b(disc|cd)\s*\d+\s+of\s+\d+\b",
    r"~.+~",                          # Japanese-style ~Title~ markers
    # Anniversary / decade celebrations — usually comps
    r"\b\d+(st|nd|rd|th)?\s+anniversary\b",
    r"\b(20|30|40|50|60)\s*years?\s+(of|in)\b",
    # "feat. Various" type tags
    r"\bfeat(uring)?\.\s+various\b",
]
_compilation_re = re.compile(
    "|".join(COMPILATION_ALBUM_PATTERNS),
    flags=re.IGNORECASE,
)

# Substrings that mark an "unofficial" or unmatched release. Provider
# DBs won't have these. Worth detecting separately so we can mark
# "this album won't match commercial DBs, don't waste a query".
UNOFFICIAL_MARKERS = [
    "[unofficial]", "(unofficial)", "[bootleg]", "(bootleg)",
    "[fan made]", "(fan made)", "[fanmade]", "(fanmade)",
    "[unreleased]", "(unreleased)", "[demo]", "(demo)",
    "[white label]", "(white label)", "[promo only]",
    "[bandcamp exclusive]", "[soundcloud rip]",
]

# NOTE: a previous version of this file had a PROVIDER_FUTILE_MARKERS
# list that triggered `skip=True` on substring matches for "sound pack",
# "podcast", etc. That was wrong. Those compilation series DO exist —
# they're just not findable when querying by the per-track artist name.
# The fix is _parse_compilation_series further down + multi-query
# strategy in metadata_lookup.fill_missing_metadata, NOT skipping.



# Substrings to STRIP from album names before sending to a provider.
# These are version/edition markers that match the provider's release
# version, not the canonical album name. " (Deluxe Edition)" stripped
# from "Discovery (Deluxe Edition)" gives "Discovery" which MB has.
ALBUM_VERSION_STRIPS = [
    r"\s*\(deluxe\s+(edition|version)\)\s*",
    r"\s*\[deluxe\s+(edition|version)\]\s*",
    r"\s*\(expanded\s+(edition|version)\)\s*",
    r"\s*\(special\s+edition\)\s*",
    r"\s*\(remastered?(\s+\d{4})?\)\s*",
    r"\s*\(\d{4}\s+remaster(ed)?\)\s*",
    r"\s*\(anniversary\s+edition\)\s*",
    r"\s*\(bonus\s+track\s+version\)\s*",
    r"\s*\(with\s+bonus\s+tracks?\)\s*",
    r"\s*\(explicit\)\s*",
    r"\s*\(clean\)\s*",
    r"\s*\(international\s+version\)\s*",
    r"\s*\(japan(ese)?\s+(version|edition)\)\s*",
    r"\s*\(uk\s+(version|edition)\)\s*",
    r"\s*\(us\s+(version|edition)\)\s*",
    r"\s*[-(\[]\s*disc\s*\d+(\s+of\s+\d+)?\s*[)\]]?\s*$",
    r"\s*[-(\[]\s*cd\s*\d+(\s+of\s+\d+)?\s*[)\]]?\s*$",
    r"\s*\[\s*hd\s*\]\s*",
    r"\s*\(mp3\s+\d+kbps\)\s*",
]
_album_strip_re = re.compile("|".join(ALBUM_VERSION_STRIPS), flags=re.IGNORECASE)

# Japanese stylistic markers to strip — wave-dash variants used in
# titles like "Initial D ~D Selection 2~". Providers don't index these.
# We also normalise full-width punctuation to ASCII so the search
# string matches what MB's Lucene index sees.
JAPANESE_CHARS_STRIP = {
    "\u301c": " ",   # WAVE DASH 〜
    "\uff5e": " ",   # FULLWIDTH TILDE ～
    "~": " ",        # plain ASCII tilde when used as bracket
    "\u3000": " ",   # IDEOGRAPHIC SPACE
    "\uff01": "!",   # FULLWIDTH EXCLAMATION
    "\uff1f": "?",   # FULLWIDTH QUESTION
    "\uff1a": ":",   # FULLWIDTH COLON
    "\uff5b": "(",   # FULLWIDTH LEFT CURLY → soft paren
    "\uff5d": ")",   # FULLWIDTH RIGHT CURLY
}

# Title-field patterns suggesting the title contains more than just a title.
# Each pattern groups out (track_no, artist, title, version) where present.
# Order matters: more specific patterns first.
TITLE_HEAVY_PATTERNS = [
    # "01. Artist - Title (Mix) [Label 2020]"
    re.compile(
        r"^\s*(?P<track_no>\d{1,3})[\.\)\-]\s+"
        r"(?P<artist>.+?)\s*[-–]\s*"
        r"(?P<title>.+?)"
        r"(?:\s*\((?P<version>[^)]+)\))?"
        r"(?:\s*\[(?P<extra>[^\]]+)\])?\s*$"
    ),
    # "Artist - Title (Mix)"
    re.compile(
        r"^\s*(?P<artist>.+?)\s+[-–]\s+"
        r"(?P<title>.+?)"
        r"(?:\s*\((?P<version>[^)]+)\))?\s*$"
    ),
    # "01 - Title" — track number prefix only (still a problem to fix)
    re.compile(
        r"^\s*(?P<track_no>\d{1,3})\s*[-\.\)]\s+"
        r"(?P<title>.+?)\s*$"
    ),
]

# Filename patterns to extract artist/title from when title is missing
# or matches the filename. Same shape as title patterns but applied to
# the bare filename stem (no extension).
FILENAME_PATTERNS = TITLE_HEAVY_PATTERNS  # same regex set, used differently


# =============================================================================
# DATA SHAPES
# =============================================================================

@dataclass
class AlbumClassification:
    """Result of classify_album. The 'why' list explains the decision so
    the activity log can show the reasoning, not just the verdict."""
    # 'solo', 'mix', 'dj_set', 'unofficial', 'unknown'
    kind: str = "unknown"
    # Confidence 0..1. Used to decide whether to act on the classification.
    confidence: float = 0.0
    # Free-text reasons that contributed to the verdict. Each entry is one
    # signal. Logged at info-level so the user can audit decisions.
    reasons: list[str] = field(default_factory=list)
    # For mix/compilation: the inferred album_artist for provider queries.
    # E.g. "Various Artists" for VA comps, the DJ's name for DJ mixes.
    suggested_album_artist: str = ""
    # The normalised album name to use for provider queries (with
    # version markers / japanese chars / disc info stripped).
    normalised_album: str = ""
    # True if this release is unlikely to exist in commercial DBs.
    skip_provider_lookup: bool = False


@dataclass
class ParsedTitle:
    """Result of parsing a title or filename for embedded info."""
    track_number: str = ""    # leading "01" etc
    artist: str = ""          # extracted artist if present
    title: str = ""           # cleaned title (without artist/version/extras)
    version: str = ""         # "(Original Mix)" / "(Radio Edit)" etc
    extra: str = ""           # "[Label 2020]" etc
    confidence: float = 0.0   # 0..1; >0.7 means we're fairly sure


# =============================================================================
# NORMALISATION
# =============================================================================

def normalise_query(s: str) -> str:
    """
    Clean an album or title string for provider lookup.

    - Strip Japanese stylistic markers (~, full-width punctuation)
    - Strip version markers (Deluxe Edition, Disc 1, Remastered)
    - Strip unofficial markers ([Unofficial], [Bootleg])
    - Collapse whitespace
    - Lowercase NOT applied — providers handle case insensitively
      but case can matter for some indexes (e.g. acronyms)
    """
    if not s:
        return ""
    # Replace japanese-style chars first
    for ch, replacement in JAPANESE_CHARS_STRIP.items():
        s = s.replace(ch, replacement)
    # Strip unofficial markers (these confuse fuzzy match badly)
    s_low = s.lower()
    for marker in UNOFFICIAL_MARKERS:
        if marker in s_low:
            # Find and remove case-insensitively, preserving rest
            pat = re.compile(re.escape(marker), flags=re.IGNORECASE)
            s = pat.sub("", s)
            s_low = s.lower()
    # Strip version/edition markers
    s = _album_strip_re.sub(" ", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    # Trim trailing punctuation/dash junk left behind by strips
    s = re.sub(r"[\s\-_\.,;:]+$", "", s)
    s = re.sub(r"^[\s\-_\.,;:]+", "", s)
    return s


def looks_like_compilation_album_name(album: str) -> tuple[bool, str]:
    """
    Does the album name match a compilation/mix pattern?

    Returns (is_compilation, matched_pattern). The pattern string is
    purely for logging — "this album was flagged because of the word
    'megamix'".
    """
    if not album:
        return False, ""
    m = _compilation_re.search(album)
    if m:
        return True, m.group(0)
    return False, ""


def looks_like_unofficial(album: str) -> tuple[bool, str]:
    """Does the album name carry an unofficial/bootleg marker?"""
    if not album:
        return False, ""
    low = album.lower()
    for marker in UNOFFICIAL_MARKERS:
        if marker in low:
            return True, marker
    return False, ""


# =============================================================================
# FILENAME / FOLDER RECOVERY
# =============================================================================
#
# When a file's artist or album tag is missing, blank, or set to a
# placeholder like "Unknown Artist", the per-album lookup will skip it
# (or worse, query MB with garbage and waste budget). Many such files
# DO have recoverable information in their filename or folder name —
# torrent dumps and DJ collections commonly use clear path conventions:
#
#   "/Artist/Album (Year)/01 - Track.flac"
#   "/Artist - Album (Year)/01. Artist - Track.flac"
#   "/Various Artists/Comp Name [Year]/01 Artist - Track.flac"
#
# This module pulls artist/album candidates out of the path BEFORE the
# fill loop groups records, so blank tags get repaired and become
# queryable.

# Tag-value strings we treat as "missing" even when not empty. The
# Vorbis-comment convention is to omit the field entirely, but some
# taggers (esp. older Windows tools) write placeholders.
UNKNOWN_TAG_VALUES = {
    "", "unknown", "unknown artist", "unknown album", "unknown title",
    "<unknown>", "untitled", "n/a", "none", "(none)",
    "no artist", "no album", "tba", "tbd",
    "track", "audio track",
}

def is_unknown_tag(v: str) -> bool:
    """True if the value looks like a placeholder for missing data."""
    return (v or "").strip().lower() in UNKNOWN_TAG_VALUES


# Fields we consider REQUIRED for a file to count as "properly tagged."
# Missing or placeholder values in any of these mean the file can't be
# reliably identified, organised, or seeded — so it goes to Broken.
REQUIRED_METADATA_FIELDS = ("artist", "album", "title")


def is_record_metadata_broken(record: dict) -> tuple[bool, str]:
    """
    Decide whether a track has metadata too incomplete to organise.

    Returns (is_broken, reason). A record is broken if ANY of the
    REQUIRED_METADATA_FIELDS is missing, blank, or a placeholder value.

    Notes on edge cases (especially VA compilations):
      - VA tracks are NOT special-cased. A track on a "Various Artists"
        compilation still has its OWN artist (e.g. on Dancemania Speed 6
        track 3, artist="Smile.dk", album_artist="Various Artists").
        As long as the per-track `artist` field is populated, the track
        passes — even if album_artist says "Various Artists" or is
        missing. We never read album_artist for the broken check.
      - The placeholder check uses `is_unknown_tag()`, so values like
        "Unknown Artist", "Untitled", "N/A", "<unknown>", and empty
        strings all count as missing.
      - This is purely a metadata-quality check. It says nothing about
        whether the audio itself is corrupt — that's a different status
        ("broken" gets reused for both, which is fine since the routing
        target is the same Broken folder).

    Return value:
      (True, "artist missing")     ← if artist is the (or first) problem
      (True, "album missing")
      (True, "title missing")
      (True, "artist, album missing")  ← if multiple fields are missing
      (False, "")                  ← record is fine
    """
    missing = []
    for field in REQUIRED_METADATA_FIELDS:
        value = record.get(field) if record else None
        if not value or is_unknown_tag(str(value)):
            missing.append(field)
    if not missing:
        return False, ""
    return True, ", ".join(missing) + " missing"


# Fields that are properties of the RELEASE rather than the track. If
# one track in an album has a label and another doesn't, it's very
# likely a metadata-source artefact (provider didn't return label for
# every recording), not a genuine multi-label album. Same for year
# (an album's year is the same across all its tracks) and genre
# (a release has one primary genre — multi-genre tracks within one
# release are rare enough to be ignored).
#
# Notably NOT in this list:
#   - catalog_number: some labels reuse one cat-num for an album,
#     others give each side/track its own (Warp 12"s, dub plates).
#     Safer to leave per-track.
#   - composer/lyricist/producer: these CAN legitimately differ per
#     track even within one album.
#   - ISRC: always per-recording.
ALBUM_LEVEL_FIELDS = ("label", "year", "genre")


def reconcile_album_level_fields(
    records: list[dict],
    *,
    fields: tuple[str, ...] = ALBUM_LEVEL_FIELDS,
    coalesce_conflicts: bool = True,
) -> tuple[dict[str, str], list[str]]:
    """
    Look at a list of records that are all from the SAME album and
    infer the album-level field values from whatever tracks have them.

    `coalesce_conflicts` controls what happens when one field has
    multiple distinct non-empty values across the tracks:
      True (default): resolve via majority vote. Ties broken
        deterministically by alphabetical order so re-runs of the
        same data give the same answer. The conflict is STILL logged
        so you can see it happened, but a value is chosen so the
        album doesn't get split across multiple folders by the
        organiser (label is part of the folder path; year too).
      False (strict): refuse to infer ANYTHING for a conflicting
        field. Caller's responsibility to handle it. Useful for
        forensic audits where you don't want auto-resolution to
        mask real metadata problems.

    Logic per field:
      - Gather the non-empty non-placeholder values across all tracks.
      - 0 values  → can't infer; leave empty.
      - 1 unique value (everyone who has it agrees) → that's the value.
      - 2+ unique values → CONFLICT.
          - coalesce=True:  majority wins, log the resolution.
          - coalesce=False: don't infer, log the conflict.

    Returns (inferred, conflicts):
      inferred  = {field: value} chosen per the above rules
      conflicts = list of human-readable conflict descriptions

    Examples (coalesce=True):
      10 tracks, 8 say label='Warp', 2 have label=''
        → inferred={'label': 'Warp'}, conflicts=[]
      10 tracks, 7 say label='Warp', 3 say label='Rephlex'
        → inferred={'label': 'Warp'},
           conflicts=["label: 'Warp' (7) vs 'Rephlex' (3) → kept 'Warp'"]
      10 tracks, 5 say label='Warp', 5 say label='Rephlex' (tie)
        → inferred={'label': 'Rephlex'},  (alphabetical tiebreak)
           conflicts=["label: 'Rephlex' (5) vs 'Warp' (5) → kept 'Rephlex' (tie, alpha-first)"]
      10 tracks, none have label set
        → inferred={}, conflicts=[]
    """
    inferred: dict[str, str] = {}
    conflicts: list[str] = []

    for field in fields:
        # Tally non-empty, non-placeholder values for this field.
        tally: dict[str, int] = {}
        for r in records:
            raw = r.get(field)
            if raw is None:
                continue
            value = str(raw).strip()
            if not value or is_unknown_tag(value):
                continue
            tally[value] = tally.get(value, 0) + 1

        if not tally:
            # No track has this field — nothing to infer.
            continue
        if len(tally) == 1:
            # Unanimous (among tracks that have a value).
            (the_value, _count), = tally.items()
            inferred[field] = the_value
            continue

        # ----- conflict path -----
        # Multiple distinct values. We sort by (descending count,
        # ascending value) so majority wins, ties broken alphabetically.
        # Alphabetical tiebreak is deterministic so re-runs on the same
        # data give the same answer (matters because this is supposed
        # to be idempotent).
        ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
        top_value, top_count = ranked[0]
        parts = [f"{v!r} ({n})" for v, n in ranked]

        if coalesce_conflicts:
            # Pick the winner. Note whether it was a tie so the log
            # makes clear the choice was arbitrary, not a clean
            # majority.
            second_count = ranked[1][1]
            is_tie = (top_count == second_count)
            if is_tie:
                msg = (f"{field}: " + " vs ".join(parts)
                       + f" → kept {top_value!r} (tie, alpha-first)")
            else:
                msg = (f"{field}: " + " vs ".join(parts)
                       + f" → kept {top_value!r}")
            conflicts.append(msg)
            inferred[field] = top_value
        else:
            # Strict mode: refuse to resolve. Album may end up split
            # across folders if the field is label/year — caller's
            # call to make.
            conflicts.append(f"{field}: " + " vs ".join(parts))

    return inferred, conflicts


def apply_album_level_inference(
    records: list[dict],
    *,
    fields: tuple[str, ...] = ALBUM_LEVEL_FIELDS,
    only_missing: bool = True,
    coalesce_conflicts: bool = True,
) -> tuple[int, list[str]]:
    """
    Run `reconcile_album_level_fields` on `records` and apply the
    inferred values IN PLACE.

    `only_missing=True` (default): only fills tracks where the field
    is currently empty or placeholder. Existing real values are left
    alone.

    `only_missing=False`: overwrites all tracks with the inferred
    value. Use this when you NEED the whole album to share one value
    — typically for label and year, because the organiser uses those
    in the destination folder path, and an album split across two
    folders is worse than an album with one (possibly imperfect)
    label/year applied uniformly.

    `coalesce_conflicts`: passed through to reconcile_album_level_fields.
    Default True (auto-resolve via majority vote so albums stay together).

    Returns (n_writes, conflicts).

    The records ARE mutated. If the caller is iterating a DB result
    and wants the changes persisted, they need to upsert each touched
    record after this returns.
    """
    inferred, conflicts = reconcile_album_level_fields(
        records, fields=fields, coalesce_conflicts=coalesce_conflicts,
    )
    if not inferred:
        return 0, conflicts

    n_writes = 0
    for record in records:
        for field, value in inferred.items():
            current = record.get(field)
            current_str = str(current).strip() if current is not None else ""
            if only_missing:
                # Only fill if currently empty or placeholder
                if current_str and not is_unknown_tag(current_str):
                    continue
            else:
                # Overwrite mode: skip only if the value already matches
                # (no point doing a no-op write)
                if current_str == value:
                    continue
            record[field] = value
            n_writes += 1

    return n_writes, conflicts



# Patterns matched against the FOLDER name (parent directory).
# Highest-confidence first.
_FOLDER_PATTERNS = [
    # "Artist - Album (1992)" / "Artist - Album [1992]"
    re.compile(
        r"^(?P<artist>.+?)\s+[-–—]\s+(?P<album>.+?)"
        r"\s*[\(\[](?:19|20)\d{2}[\)\]]\s*$"
    ),
    # "Artist - Album" — no year
    re.compile(r"^(?P<artist>.+?)\s+[-–—]\s+(?P<album>.+?)\s*$"),
    # "Album (1992)" — album only
    re.compile(r"^(?P<album>.+?)\s*[\(\[](?:19|20)\d{2}[\)\]]\s*$"),
]


# Patterns matched against the FILENAME stem (no extension).
_FILENAME_PATTERNS = [
    # "01 - Artist - Title" / "01. Artist - Title" / "01 Artist - Title"
    re.compile(
        r"^\d{1,3}\s*[\.\-]?\s+(?P<artist>.+?)\s+[-–—]\s+(?P<title>.+?)\s*$"
    ),
    # "Artist - Title" (no track number)
    re.compile(r"^(?P<artist>.+?)\s+[-–—]\s+(?P<title>.+?)\s*$"),
]


def recover_from_path(
    path: str,
    have_artist: bool = False,
    have_album: bool = False,
    have_title: bool = False,
) -> dict[str, str]:
    """
    Try to derive artist / album / title from a file path.

    Returns a dict with up to four keys: 'artist', 'album', 'title',
    'note'. Any key may be absent or empty if no inference was made.
    'note' is a human-readable trail of which heuristic fired.

    The `have_*` flags tell the function NOT to try recovering a field
    that's already populated. We only fill in missing fields — never
    overwrite existing tag data.
    """
    out: dict[str, str] = {}
    if not path:
        return out
    p = Path(path)
    stem = p.stem
    folder = p.parent.name if p.parent else ""
    grandparent = p.parent.parent.name if p.parent and p.parent.parent else ""
    notes: list[str] = []

    # Folder-name parsing for artist + album
    if not have_artist or not have_album:
        for pat in _FOLDER_PATTERNS:
            m = pat.match(folder)
            if not m:
                continue
            gd = m.groupdict()
            if not have_artist and gd.get("artist"):
                out["artist"] = gd["artist"].strip()
            if not have_album and gd.get("album"):
                out["album"] = gd["album"].strip()
            if "artist" in out or "album" in out:
                notes.append(f"folder='{folder}'")
            break

    # Folder-name didn't match a pattern but is a bare string —
    # use it as the album if we need one, and grandparent as artist.
    if (not have_album and "album" not in out
            and folder and " - " not in folder and " – " not in folder):
        out["album"] = folder
        notes.append(f"folder-as-album='{folder}'")
        if (not have_artist and "artist" not in out
                and grandparent and grandparent not in ("", "/")):
            # Avoid using filesystem roots as artist
            if grandparent.lower() not in ("music", "flac", "mp3", "audio",
                                            "downloads", "media", "library"):
                out["artist"] = grandparent
                notes.append(f"grandparent-as-artist='{grandparent}'")

    # Filename-stem parsing for artist + title
    if (not have_artist and "artist" not in out) or (not have_title):
        for pat in _FILENAME_PATTERNS:
            m = pat.match(stem)
            if not m:
                continue
            gd = m.groupdict()
            if not have_artist and "artist" not in out and gd.get("artist"):
                out["artist"] = gd["artist"].strip()
                notes.append(f"filename='{stem}'")
            if not have_title and gd.get("title"):
                out["title"] = gd["title"].strip()
                if "filename" not in (notes[-1] if notes else ""):
                    notes.append(f"filename='{stem}'")
            break

    if notes:
        out["note"] = "; ".join(notes)
    return out


def _DEPRECATED_looks_provider_futile_REMOVED():
    """Removed in v0.23.7. The 'skip on substring match' approach was
    wrong: it skipped real compilation releases. Use
    `_parse_compilation_series` and the multi-query strategy in
    metadata_lookup.fill_missing_metadata instead.

    This stub exists only as a doc trail; nothing imports it."""
    raise NotImplementedError("removed in v0.23.7")


# Catalogue number patterns. Real labels assign cat-numbers like
# "SHADOW 082", "R&S RS 1992", "Warp WAP100". When the user's title
# has "[LABEL ###]" or "(LABEL ###)" embedded, we can extract that
# and use it as a separate MB query parameter — way more accurate
# than searching the noisy title.
#
# Captured group: (label_prefix, number, full_match)
# The label part: 2+ letter alphabetic, possibly with & or - or space.
# The number part: 2-6 digits, possibly with a letter suffix.
_CATALOGUE_BRACKETED = re.compile(
    r"[\[\(]"                                 # opening bracket
    r"\s*"
    r"(?P<label>[A-Za-z][A-Za-z&\-\s]{1,20}?)" # label prefix
    r"\s+"
    r"(?P<num>\d{1,6}[A-Za-z]{0,3})"           # digits + optional 0-3 letter suffix
    r"\s*"
    r"[\]\)]"                                 # closing bracket
)


def parse_catalogue_number(text: str) -> tuple[str, str] | None:
    """
    Extract a label catalogue number from album/title text.

    "Deep Blue - [SHADOW 082] Thursday" -> ("SHADOW", "082")
    "Aphex Twin (Apollo 1992)"          -> None  (the number is a year)
    "Untitled [WAP100]"                 -> None  (no space between label and num)
                                           — intentional, see note below
    "Various [R&S RS 1992]"             -> ("R&S RS", "1992")  (ambiguous: could
                                           be year. caller should sanity-check)

    Returns (label_prefix, number) or None if no match.

    Honest limitation: distinguishing "[Apollo 1992]" (year) from
    "[Apollo 1992]" (catalogue WAP-1992) requires knowing the label's
    numbering scheme, which we don't. We use a heuristic: if the
    number-part is exactly a 4-digit year between 1900-2099, we treat
    it as a year, NOT a catalogue. False negatives possible.
    """
    if not text:
        return None
    m = _CATALOGUE_BRACKETED.search(text)
    if not m:
        return None
    label = m.group("label").strip()
    num = m.group("num").strip()
    # Reject if number looks like a year (1900-2099) — likely just a
    # release year in brackets, not a cat number.
    if num.isdigit() and 1900 <= int(num) <= 2099:
        return None
    # Reject if label is too short or numeric
    if len(label) < 2 or label.isdigit():
        return None
    return label, num



# =============================================================================
# TITLE / FILENAME PARSING
# =============================================================================

def parse_title(title: str, filename: str = "") -> ParsedTitle:
    """
    Try to extract artist/track_number/version from a title field.

    A clean tagger produces title="Xtal". A bad one produces
    title="01. Aphex Twin - Xtal (Original Mix) [Apollo 1992]" — the
    title contains track number, artist, version, label, and year all
    concatenated. This routine spots that and breaks it apart.

    filename (without extension, optional): if title is empty OR matches
    the filename, the same parser applies to the filename instead.

    Returns ParsedTitle with confidence reflecting which pattern matched.
    Pattern 1 (track_no + artist + title + version) gives the highest
    confidence. Pattern 3 (just track_no + title) gives the lowest
    because that's a normal-ish filename.
    """
    candidate = (title or "").strip()
    # If title is empty or just the bare filename, parse the filename.
    if not candidate or candidate == filename:
        candidate = filename
    if not candidate:
        return ParsedTitle()

    # Try patterns in order, highest-confidence first
    for i, pat in enumerate(TITLE_HEAVY_PATTERNS):
        m = pat.match(candidate)
        if not m:
            continue
        gd = m.groupdict()
        # Confidence drops with each less-specific pattern
        confidence = [0.95, 0.75, 0.55][i]
        return ParsedTitle(
            track_number=(gd.get("track_no") or "").strip(),
            artist=(gd.get("artist") or "").strip(),
            title=(gd.get("title") or "").strip(),
            version=(gd.get("version") or "").strip(),
            extra=(gd.get("extra") or "").strip(),
            confidence=confidence,
        )

    # No pattern matched — title looks normal (just a title, no other
    # info baked in). Return as title with zero confidence in "parsing".
    return ParsedTitle(title=candidate, confidence=0.0)


def title_looks_like_filename(title: str, path: str) -> bool:
    """
    Heuristic: does the title field appear to be the filename?

    Happens when a tagger fails and uses the filename as a fallback.
    Returns True if title (no extension) matches the file stem, OR if
    title contains characters typical of filenames (path separators,
    Windows reserved characters, very long with many dashes).
    """
    if not title or not path:
        return False
    stem = Path(path).stem
    if title.strip() == stem.strip():
        return True
    # Very crude: title with 3+ separators is suspiciously filename-like
    seps = title.count(" - ") + title.count("_") + title.count(".")
    if seps >= 3 and len(title) > 30:
        return True
    return False


# =============================================================================
# PER-FILE STRUCTURAL DETECTION
# =============================================================================

def looks_like_dj_set(record: dict[str, Any]) -> bool:
    """
    Is THIS specific file a continuous DJ set or radio show?

    Heuristics:
      - Duration >= 45 minutes (DJ sets are usually 60-120 min)
      - OR title/album contains 'mixed by', 'continuous mix', 'live set',
        'radio show', 'podcast', 'session #N'
    """
    duration = record.get("duration_sec") or 0
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0
    if duration >= 45 * 60:
        return True

    haystack = " ".join([
        (record.get("title") or ""),
        (record.get("album") or ""),
        (record.get("comment") or ""),
    ]).lower()

    dj_markers = [
        "mixed by", "continuous mix", "nonstop mix", "non-stop mix",
        "live set", "live @", "live at", "radio show", "podcast",
        "session #", "session no", "essential mix", "boiler room",
        "bbc radio 1", "dj set", "dj-set",
    ]
    return any(m in haystack for m in dj_markers)


# =============================================================================
# ALBUM-LEVEL CLASSIFICATION (multi-record, the heavy lifter)
# =============================================================================

def classify_album(
    records: list[dict[str, Any]],
    *,
    diversity_threshold: float = 0.5,
    various_artists_tags: list[str] | None = None,
) -> AlbumClassification:
    """
    Thorough classification of an album/folder grouping.

    Decision flow, each step records its reason and contributes to
    confidence; higher confidence = more sure of the verdict:

      1. Unofficial marker in album name?  -> 'unofficial', skip_lookup=True
      2. Single very long file?            -> 'dj_set'
      3. Multiple long files matching DJ patterns? -> 'dj_set'
      4. albumartist explicitly "Various Artists"? -> 'mix', VA-query
      5. Album name matches compilation regex?     -> 'mix', VA-query
      6. Track-artist diversity > threshold?       -> 'mix', VA-query
      7. Catch-all                                  -> 'solo'

    Returns AlbumClassification with:
      - kind: solo / mix / dj_set / unofficial / unknown
      - confidence: 0..1
      - reasons: list of human-readable strings explaining the verdict
      - suggested_album_artist: what to use for provider lookup
        (empty string means "use the per-track artist")
      - normalised_album: cleaned album name for provider lookup
      - skip_provider_lookup: don't query providers (bootleg/unofficial)
    """
    out = AlbumClassification()
    if not records:
        out.kind = "unknown"
        out.reasons.append("empty record group")
        return out

    various_artists_tags = [t.lower() for t in (
        various_artists_tags or ["various artists", "various", "va", "v.a.",
                                  "soundtrack", "compilation"]
    )]

    # Sample one album name (they should all share one in a folder group)
    sample_album = ""
    for r in records:
        if r.get("album"):
            sample_album = (r["album"] or "").strip()
            break

    # ------ 1. unofficial / bootleg ----------------------------------------
    is_unoff, unoff_marker = looks_like_unofficial(sample_album)
    if is_unoff:
        out.kind = "unofficial"
        out.confidence = 0.95
        out.reasons.append(
            f"album name contains '{unoff_marker}' — won't be in commercial DBs"
        )
        out.skip_provider_lookup = True
        out.normalised_album = normalise_query(sample_album)
        return out

    # ------ 2. single long file = DJ set / radio show ----------------------
    if len(records) == 1 and looks_like_dj_set(records[0]):
        out.kind = "dj_set"
        out.confidence = 0.8
        dur_min = (records[0].get("duration_sec") or 0) / 60
        out.reasons.append(
            f"single file, {dur_min:.0f} min duration — likely a DJ set/mix"
        )
        # For DJ sets the per-track artist IS the DJ — leave album_artist
        # blank to let normal lookup proceed with that.
        out.normalised_album = normalise_query(sample_album)
        return out

    # ------ 3. multi-file DJ set series (rare) -----------------------------
    if len(records) >= 2:
        dj_count = sum(1 for r in records if looks_like_dj_set(r))
        if dj_count >= len(records) / 2:
            out.kind = "dj_set"
            out.confidence = 0.6
            out.reasons.append(
                f"{dj_count}/{len(records)} files look like DJ sets "
                f"(long duration or 'mixed by' / 'live set' markers)"
            )
            out.normalised_album = normalise_query(sample_album)
            return out

    # ------ 4. explicit Various Artists in album_artist --------------------
    aa_values = [
        (r.get("albumartist") or "").strip().lower()
        for r in records if r.get("albumartist")
    ]
    if aa_values:
        # Pick the most common album_artist value as canonical
        common_aa = Counter(aa_values).most_common(1)[0][0]
        if common_aa in various_artists_tags:
            out.kind = "mix"
            out.confidence = 0.95
            out.reasons.append(
                f"album_artist tag is '{common_aa}' — this is a Various Artists "
                f"compilation"
            )
            out.suggested_album_artist = "Various Artists"
            out.normalised_album = normalise_query(sample_album)
            return out

    # ------ 5. album name pattern matches compilation regex ----------------
    is_comp, pattern_hit = looks_like_compilation_album_name(sample_album)
    if is_comp:
        out.kind = "mix"
        out.confidence = 0.85
        out.reasons.append(
            f"album name matches compilation pattern: '{pattern_hit}'"
        )
        # When a real album_artist exists and is NOT VA, it might be the
        # mixer/DJ — keep it. Otherwise default to "Various Artists".
        if aa_values:
            common_aa = Counter(aa_values).most_common(1)[0][0]
            # If it's a single name (not VA / not generic), it's the DJ
            if common_aa and common_aa not in various_artists_tags:
                out.suggested_album_artist = common_aa.title()
                out.reasons.append(
                    f"keeping album_artist='{common_aa}' as the mix's curator"
                )
            else:
                out.suggested_album_artist = "Various Artists"
        else:
            out.suggested_album_artist = "Various Artists"
        out.normalised_album = normalise_query(sample_album)
        return out

    # ------ 6. ADVANCED: multi-signal artist-diversity detection ----------
    # The simple "modal artist share" check from earlier versions missed
    # legitimate solo albums with guest features (one Drake album with
    # 5 featured artists isn't a compilation), and missed real comps
    # where exactly one artist had 2 tracks but everyone else had 1.
    # We now compute several signals and combine them.
    #
    # Signal A: count of DISTINCT primary artists (after stripping
    #   "feat. X" / "with X" / "vs Y"). 3+ distinct primaries is strong.
    # Signal B: share of tracks whose primary artist isn't the modal one
    #   (the original heuristic, retained as one signal).
    # Signal C: year-spread across tracks. A folder with tracks spanning
    #   8+ years strongly suggests a compilation (real albums are recorded
    #   in a single window). Singles drop are < 2 years apart.
    # Signal D: when album_artist is consistently a single name AND it's
    #   NOT in various_artists_tags AND it's NOT the modal track artist,
    #   that single name is probably a DJ/curator → still a mix.
    #
    # Combine: any TWO signals at confidence >= 0.6 → 'mix'.
    # One strong signal (count >= 5 distinct primaries) → 'mix' alone.

    def split_primary(artist_str: str) -> str:
        """Strip 'feat. X', 'with X', 'vs Y', '& Z' from artist string,
        leaving just the primary. 'Drake feat. Future' -> 'Drake'.
        'Jay-Z & Beyonce' -> 'Jay-Z' (the first one). We don't split
        'A vs B' as that's a single collaboration, not multiple artists."""
        s = artist_str.strip()
        # Strip 'feat. X', 'ft. X', 'featuring X'
        s = re.sub(r"\s+(feat(?:uring)?\.?|ft\.?)\s+.*$", "", s, flags=re.IGNORECASE)
        # Strip 'with X' (used as guest credit)
        s = re.sub(r"\s+with\s+.*$", "", s, flags=re.IGNORECASE)
        # 'Artist A & Artist B' — take first (could go either way; first
        # is the convention for primary credit).
        s = re.sub(r"\s+&\s+.*$", "", s)
        s = re.sub(r"\s+and\s+.*$", "", s, flags=re.IGNORECASE)
        return s.strip()

    raw_artists = [
        (r.get("artist") or "").strip()
        for r in records if r.get("artist")
    ]
    primary_artists = [split_primary(a).lower() for a in raw_artists if a]
    primary_artists = [p for p in primary_artists if p]  # drop empties

    signals: list[tuple[str, str]] = []   # (label, why)

    # Floor lowered from >=3 tracks to >=2 — a 2-track release with two
    # different artists is still meaningfully a comp/split. The threshold
    # logic below uses ratios so 2 tracks with 2 artists registers as
    # diff_share=0.5 which doesn't accidentally trip on a solo album.
    if primary_artists and len(primary_artists) >= 2:
        distinct = set(primary_artists)
        counts = Counter(primary_artists)
        most_common_count = counts.most_common(1)[0][1]
        diff_share = 1.0 - (most_common_count / len(primary_artists))

        # Signal A: many distinct primary artists
        if len(distinct) >= 5:
            signals.append((
                "many_distinct_primaries",
                f"{len(distinct)} distinct primary artists in {len(primary_artists)} "
                f"tracks (after stripping 'feat./with/&')",
            ))
        elif len(distinct) >= 3:
            # Mild signal — could be a collaboration-heavy solo album
            if diff_share >= 0.5:
                signals.append((
                    "moderate_distinct_primaries",
                    f"{len(distinct)} distinct primaries, {diff_share:.0%} differ "
                    f"from the modal artist",
                ))

        # Signal B: modal share is low (original heuristic, refined)
        if diff_share >= diversity_threshold:
            signals.append((
                "low_modal_share",
                f"only {1-diff_share:.0%} of tracks share the modal artist",
            ))

    # Signal C: year-spread across tracks. Solo album tracks share a
    # release window of usually < 2 years (sometimes singles compiled
    # for an album get released over 12-18 months). Compilations
    # routinely span 5-20+ years. 8+ year spread = strong mix signal.
    years = []
    for r in records:
        y_str = (r.get("year") or "").strip()
        # First 4 digits — year fields can be "1992-01-15" or "1992"
        m = re.match(r"(\d{4})", y_str)
        if m:
            try:
                years.append(int(m.group(1)))
            except ValueError:
                pass
    if len(years) >= 3:
        year_spread = max(years) - min(years)
        if year_spread >= 8:
            signals.append((
                "wide_year_spread",
                f"tracks span {year_spread} years ({min(years)}-{max(years)}) — "
                f"too wide for a single album",
            ))

    # Signal D: album_artist is a single non-VA name but doesn't match
    # the modal track artist → it's a curator (DJ Foo presents...).
    if aa_values and primary_artists:
        common_aa = Counter(aa_values).most_common(1)[0][0]
        modal_primary = Counter(primary_artists).most_common(1)[0][0]
        if (common_aa
            and common_aa not in various_artists_tags
            and common_aa != modal_primary
            and len(set(primary_artists)) >= 2):
            signals.append((
                "curator_album_artist",
                f"album_artist='{common_aa}' but {len(set(primary_artists))} "
                f"different track artists — looks curated",
            ))

    # Signal E: track titles encode 'Artist - Title' or 'Artist — Title'.
    # Many compilations and shared/torrented releases have artist baked
    # INTO the title field (because tagging was sloppy or because the
    # source was a mix file split into tracks). E.g. all 18 tracks named
    # "DJ Foo - Track 01", "MC Bar - Track 02", etc. If a majority of
    # tracks have this pattern AND the parsed-out artists differ from
    # each other, that's strong evidence of a compilation.
    title_parsed_artists: list[str] = []
    for r in records:
        title = (r.get("title") or "").strip()
        if not title:
            continue
        # Match "Artist - Title" or "Artist – Title" or "Artist — Title"
        # The separator must have spaces around it to avoid false hits
        # on hyphenated song names like "Hard-Edged Sun".
        m = re.match(r"^(.+?)\s+[-–—]\s+(.+)$", title)
        if m:
            candidate = m.group(1).strip().lower()
            # Sanity: artist names usually 2-60 chars, not entirely numbers
            if 2 <= len(candidate) <= 60 and not candidate.isdigit():
                title_parsed_artists.append(candidate)
    # Need a majority (>= 60%) of tracks matching, AND at least 3 distinct
    # parsed-artists, to fire. Avoids false positives on solo albums where
    # one or two tracks happen to have dashes in titles.
    if (len(records) >= 3 and
        len(title_parsed_artists) >= 0.6 * len(records) and
        len(set(title_parsed_artists)) >= 3):
        signals.append((
            "title_field_holds_artist",
            f"{len(title_parsed_artists)}/{len(records)} track titles match "
            f"'Artist - Title' with {len(set(title_parsed_artists))} distinct "
            f"parsed artists — title field is doubling as artist field",
        ))

    # Decide based on signals collected
    strong_alone = {"many_distinct_primaries", "wide_year_spread",
                    "curator_album_artist", "title_field_holds_artist"}
    has_strong = any(name in strong_alone for name, _ in signals)
    if has_strong or len(signals) >= 2:
        out.kind = "mix"
        # Confidence scales with signal count
        out.confidence = min(0.95, 0.65 + 0.1 * len(signals))
        for _, reason in signals:
            out.reasons.append(reason)
        # If a curator signal fired, use that name; otherwise VA
        curator = next((sig for sig in signals
                        if sig[0] == "curator_album_artist"), None)
        if curator and aa_values:
            common_aa = Counter(aa_values).most_common(1)[0][0]
            out.suggested_album_artist = common_aa.title()
        else:
            out.suggested_album_artist = "Various Artists"
        out.normalised_album = normalise_query(sample_album)
        return out

    # ------ 7. default: solo album -----------------------------------------
    out.kind = "solo"
    out.confidence = 0.5
    out.reasons.append("no compilation/mix signals found — treating as solo")
    out.normalised_album = normalise_query(sample_album)
    return out


# =============================================================================
# CONVENIENCE: classify a single (artist, album) pair without records
# =============================================================================
#
# Used by metadata_lookup.py: it groups records by (artist, album)
# string keys and runs the heavy classify_album on each group. But the
# UNIQUE pairs that get queried are just strings — we want a cheap
# pre-check on the album name alone before deciding whether the
# expensive multi-provider query is even worth doing.

@dataclass
class QuickAlbumCheck:
    """Lightweight per-(artist, album) check used before any provider hit."""
    skip: bool = False              # don't query at all
    skip_reason: str = ""
    use_various_artists: bool = False
    normalised_album: str = ""
    note: str = ""                  # extra context for the log
    # Extracted catalogue number, used as an additional query hint
    # for providers that support cat-number search (MusicBrainz does).
    catalogue_label: str = ""
    catalogue_number: str = ""
    # NEW: when the album title looks like a recurring compilation
    # series (Beatport Drum & Bass: Sound Pack #348, Basstronic:
    # Underground Electric Bass Session, etc.) — these ARE real
    # releases that exist, just under varying naming conventions.
    # When set, the fill loop should try multiple query strategies
    # (cat-number, series-with-issue, artist-only fallback) instead
    # of just the noisy default.
    is_compilation_series: bool = False
    series_name: str = ""            # e.g. "Beatport Drum & Bass: Sound Pack"
    series_issue: str = ""           # e.g. "348"


def quick_check_album(artist: str, album: str) -> QuickAlbumCheck:
    """
    Cheap check applied per (artist, album) tuple in the fill loop,
    BEFORE hitting providers.

      - Unofficial / bootleg -> skip (genuinely not on any DB)
      - Compilation series   -> mark for multi-strategy query
                                (DON'T skip — these releases DO exist)
      - Catalogue number     -> extract for fallback query
      - Compilation pattern  -> mark VA for query routing
      - Otherwise            -> normal lookup
    """
    out = QuickAlbumCheck(normalised_album=normalise_query(album))

    is_unoff, marker = looks_like_unofficial(album)
    if is_unoff:
        out.skip = True
        out.skip_reason = f"unofficial release ('{marker}') — skipping provider lookup"
        return out

    # Compilation-series detection. PRIOR behaviour was: skip these
    # entirely as "won't be in MB". That was wrong — many of these
    # (Beatport store packs, DJ promo series, label compilation series)
    # DO appear in MusicBrainz as Various-Artists releases under the
    # full series name. They just don't match when queried with the
    # individual track artist's name.
    series = _parse_compilation_series(album)
    if series is not None:
        out.is_compilation_series = True
        out.series_name, out.series_issue = series
        out.use_various_artists = True
        out.note = (f"compilation series '{out.series_name}'"
                     + (f" #{out.series_issue}" if out.series_issue else ""))

    # Try to extract a catalogue number from the album title — it's
    # often more reliable than the noisy album string for finding the
    # actual release. We don't gate the lookup on success; if no cat
    # number is found, fall through to the normal text search.
    cat = parse_catalogue_number(album)
    if cat is not None:
        out.catalogue_label, out.catalogue_number = cat
        cat_note = f"catalogue: {out.catalogue_label} {out.catalogue_number}"
        out.note = f"{out.note}; {cat_note}" if out.note else cat_note

    is_comp, pattern = looks_like_compilation_album_name(album)
    if is_comp:
        out.use_various_artists = True
        comp_note = f"compilation pattern matched: '{pattern}'"
        out.note = f"{out.note}; {comp_note}" if out.note else comp_note
        return out

    return out


# Patterns that identify a recurring compilation series. Each entry is
# a compiled regex with named groups `name` (the series name) and
# optionally `issue` (the volume/episode number).
#
# Captures the full series name as the user would search it on MB:
# "Beatport Drum & Bass: Sound Pack #348" → series="Beatport Drum & Bass: Sound Pack", issue="348"
#
# We're being MORE conservative than the old futility list — we only
# match when the structure is clearly "Series Name #N" or "Series
# Name: Episode N", not random text that happens to contain a word
# like "session".
_COMPILATION_SERIES_PATTERNS = [
    # "Beatport Drum & Bass: Sound Pack #348" / "Sound Pack vol 12"
    re.compile(
        r"^(?P<name>.*?(?:sound\s+pack|sample\s+pack))"
        r"\s*[:#\-]?\s*"
        r"(?:#|vol(?:ume)?\.?\s*|no\.?\s*)?"
        r"(?P<issue>\d{1,4})\s*$",
        re.IGNORECASE,
    ),
    # "Basstronic: Underground Electric Bass Session" / "Drum & Bass Podcast"
    # — episodic-feeling structures. Match when "session" or "podcast" or
    # "show" is the LAST significant word.
    re.compile(
        r"^(?P<name>.+?(?:session|podcast|radio\s+show|episode|mix\s+series))"
        r"\s*(?:[:#]\s*)?(?P<issue>\d{0,4})\s*$",
        re.IGNORECASE,
    ),
    # Generic "Series Title vol N" / "Series Title #N" — at least 2 words
    # before the number to avoid false positives on real album names that
    # happen to end with a number.
    re.compile(
        r"^(?P<name>(?:\w+\s+){1,5}\w+)"
        r"\s+(?:vol(?:ume)?\.?|#|no\.?)\s*"
        r"(?P<issue>\d{1,4})\s*$",
        re.IGNORECASE,
    ),
]


# Known canonical compilation-series prefixes. When an album name starts
# with one of these (case-insensitive), it's almost certainly part of the
# series — regardless of whether it has a Vol/#/No marker. This catches
# series like:
#   "Dancemania Speed 6", "Dancemania Happy Paradise (Promo #2)"
#   "NOW That's What I Call Music 100"
#   "Hed Kandi: The Mix 2009"
#
# Strict curation rule: each prefix must be unambiguous. "Best of" is
# NOT in this list because "Best of Bowie" is an artist comp, not a VA
# release. Add a prefix only if EVERY album starting with it is a VA
# compilation series.
#
# These prefixes are matched at the START of the album string only;
# substring matches are too dangerous (e.g. an album named "Welcome to
# Dancemania" would falsely trigger on a midstring search).
KNOWN_COMP_SERIES_PREFIXES = [
    "dancemania",                # Toshiba EMI Japanese eurobeat/parapara
    "now that's what i call",    # NOW series
    "now thats what i call",
    "ministry of sound",         # MoS comps (some artist releases exist but rare)
    "trance nation",
    "hed kandi",
    "global underground",
    "ibiza annual",
    "bonzai progressive",
    "fabriclive",
    "back to mine",
    "essential mix",
    "this is house",
    "in the mix",                # generic but distinctive
    "soulseekerz",
    "uk garage",
    "drum & bass arena",
    "drum and bass arena",
    "ram nation",                # Ram Records comp series
    "hospitality presents",      # Hospital Records
    "hospital records presents",
    "metalheadz presents",
    "ministry magazine",
    "kiss in ibiza",
    "cream ibiza",
    "godskitchen",
]


def _starts_with_known_comp_prefix(album: str) -> str:
    """Return the matched prefix (original casing from list) if album
    starts with a known comp-series prefix, else empty string."""
    if not album:
        return ""
    low = album.strip().lower()
    for prefix in KNOWN_COMP_SERIES_PREFIXES:
        if low.startswith(prefix):
            return prefix
    return ""


def _parse_compilation_series(album: str) -> tuple[str, str] | None:
    """
    Detect compilation-series structure in an album name.

    Returns (series_name, issue_string) or None.

    Strategy: try regex patterns first; fall back to known-prefix match.

    Examples:
      "Beatport Drum & Bass: Sound Pack #348" -> ("Beatport Drum & Bass: Sound Pack", "348")
      "Basstronic: Underground Electric Bass Session" -> ("Basstronic: Underground Electric Bass Session", "")
      "Dancemania Speed 6"                    -> ("Dancemania", "")  via known-prefix
      "Dancemania Happy Paradise (Promo #2)"  -> ("Dancemania", "")  via known-prefix
      "Selected Ambient Works 85-92"           -> None  (not a series — a real album)
      "Now That's What I Call Music 100"      -> ("Now That's What I Call Music", "100")
    """
    if not album:
        return None
    a = album.strip()
    # Regex patterns first — they extract a clean series name + issue
    for pat in _COMPILATION_SERIES_PATTERNS:
        m = pat.match(a)
        if m:
            name = m.group("name").strip().rstrip(":-,#")
            issue = (m.groupdict().get("issue") or "").strip()
            if name:
                return name, issue
    # Known-prefix fallback for series the regex patterns don't catch.
    # The issue field is left empty since we can't reliably extract one
    # from the noisy suffix ("Happy Paradise (Promo #2)" doesn't have a
    # well-defined issue). The multi-query strategy in metadata_lookup
    # will try both "VA + full album string" and "VA + just the prefix"
    # — so the prefix alone is usually enough to find the series page.
    prefix = _starts_with_known_comp_prefix(a)
    if prefix:
        # Title-case the prefix for display: "dancemania" -> "Dancemania"
        return prefix.title(), ""
    return None


# =============================================================================
# CUE-FILE MATCHING
# =============================================================================
#
# Why this exists: cue files (CD-image cue sheets) sometimes get
# separated from their audio. A clean rip has `Album.flac` + `Album.cue`
# in the same folder; messy archives often have the cue elsewhere or
# missing entirely. We can't fetch them from any provider (they don't
# exist on any music DB), but we CAN match orphan cue files in your
# library to their albums by parsing the FILE field inside the cue.
#
# Note on what we CANNOT do, since the user asked: there's no API that
# serves cue files. MusicBrainz doesn't store them. Discogs has release
# scans (front/back/sleeve) but not cue/log files. Those only exist on
# original release uploads (scene releases, EAC log files). If a cue
# is missing from disk, it's gone unless you have the original rip.
# So this routine helps with cue files that DO exist somewhere in your
# library but in the wrong folder, not with fetching missing ones.

CUE_FILE_FIELD_RE = re.compile(
    r'^\s*FILE\s+"([^"]+)"\s+(WAVE|MP3|AIFF|FLAC|APE)',
    flags=re.IGNORECASE | re.MULTILINE,
)
CUE_TITLE_RE = re.compile(
    r'^\s*TITLE\s+"([^"]+)"',
    flags=re.IGNORECASE | re.MULTILINE,
)
CUE_PERFORMER_RE = re.compile(
    r'^\s*PERFORMER\s+"([^"]+)"',
    flags=re.IGNORECASE | re.MULTILINE,
)


def parse_cue_file(cue_path) -> dict[str, str]:
    """
    Parse a .cue file's header to extract the album/artist/file it
    refers to. Returns a dict with keys 'audio_filename', 'title',
    'performer'. All values stripped strings, empty if not present.

    We only read the file's first ~8KB — that's where the TITLE/
    PERFORMER/FILE entries live before the per-track listings.
    """
    out = {"audio_filename": "", "title": "", "performer": ""}
    try:
        with open(cue_path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(8192)
    except (OSError, UnicodeDecodeError):
        return out

    m = CUE_FILE_FIELD_RE.search(head)
    if m:
        out["audio_filename"] = m.group(1).strip()
    # First TITLE/PERFORMER are album-level (track-level ones come later
    # inside TRACK blocks; we only read the first 8KB so usually we get
    # the album-level ones).
    m = CUE_TITLE_RE.search(head)
    if m:
        out["title"] = m.group(1).strip()
    m = CUE_PERFORMER_RE.search(head)
    if m:
        out["performer"] = m.group(1).strip()
    return out


def find_orphan_cue_files(root_path) -> list[dict[str, Any]]:
    """
    Walk a directory tree, find every .cue file, parse it, return info
    about each. Caller can then match against the DB to find which
    cues are next to their audio vs which are orphaned.

    Returns list of {
        'cue_path': str,
        'audio_filename': str,   # what FILE field says
        'audio_in_same_folder': bool,
        'title': str,
        'performer': str,
    }.
    """
    from pathlib import Path
    results = []
    root = Path(root_path)
    if not root.exists():
        return results
    for cue_path in root.rglob("*.cue"):
        parsed = parse_cue_file(cue_path)
        target_audio = (
            cue_path.parent / parsed["audio_filename"]
            if parsed["audio_filename"] else None
        )
        results.append({
            "cue_path": str(cue_path),
            "audio_filename": parsed["audio_filename"],
            "audio_in_same_folder": (
                bool(target_audio) and target_audio.exists()
            ),
            "title": parsed["title"],
            "performer": parsed["performer"],
        })
    return results
