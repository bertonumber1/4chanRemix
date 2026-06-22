"""
metadata_lookup.py
==================

Multi-provider metadata lookup engine. Modelled on OneTagger's tagging
pipeline: query several platforms in sequence, normalise their responses
to a common shape, merge per-field with conflict resolution.

Conflict resolution rules:

  Text fields (artist, album, label, catalog_number, year, etc):
      - "only-missing" mode (default, safe): existing value wins, never
        overwrite. If empty, take the first non-empty value in
        provider-priority order.
      - "overwrite" mode: take the value from the highest-scoring
        release across all providers.

  Numeric fields (BPM):
      - When multiple providers return a value, take the MOST COMMON
        (mode). With ties, take the median. With only one source,
        take it as-is.
      - This handles the user's pain point: DAWs that write bogus
        estimated BPMs get overruled by the consensus from real
        platforms.

  Cover art:
      - Download from the highest-scoring release that has an art URL.
        Falls back through providers in order.

What this module owes to OneTagger:

  - The Provider abstraction is named after `AutotaggerSource` from
    crates/onetagger-tagger/src/lib.rs.
  - The Release/TrackInfo shapes are direct simplifications of
    OneTagger's Track struct.
  - The matching strategy (search -> score -> pick best) mirrors
    OneTagger's `match_track` flow, minus the strictness slider and
    "match by duration" extras.

What this module deliberately does NOT do:

  - Write back to the audio file itself. We only update the DB row.
    Use option 5 (OneTagger) for filesystem write-back, then option 2
    (rebuild database) to refresh the DB from the new file tags.
  - Fingerprint audio. Shazam-style identification needs the actual
    Shazam algorithm; that is a separate sub-project.
"""

from __future__ import annotations

import logging
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Precompiled regex for stripping a bracketed catalogue number from an
# album title, e.g. "[SHADOW 082] Helicopter '97" -> "Helicopter '97".
# Compiled once at module load rather than per-album in the fetch loop.
_CAT_STRIP_RE = re.compile(
    r"\s*[\[\(]\s*[A-Za-z][A-Za-z&\-\s]{1,20}?\s+\d{1,6}[A-Za-z]{0,3}\s*[\]\)]\s*"
)
_WHITESPACE_RE = re.compile(r"\s+")

from metadata_providers import (
    Provider, Release, TrackInfo, make_provider,
)


logger = logging.getLogger("music-organiser")


# Whether the legacy MusicBrainz client is still importable. Kept as
# a flag because cmd_fetch_metadata's first-run-install path references
# it; we always have a provider implementation now so this is True.
MUSICBRAINZ_AVAILABLE = True


# Tags the user can pick to fill. (column, label) tuples, displayed by
# the menu helper. The dataclass-Release fields drive what's available.
FILLABLE_TAGS: list[tuple[str, str]] = [
    ("artist",         "Artist"),
    ("album",          "Album name"),
    ("year",           "Year of release"),
    ("label",          "Record label"),
    ("catalog_number", "Catalogue number"),
    ("country",        "Release country"),
    ("barcode",        "Barcode (UPC/EAN)"),
    ("genre",          "Genre"),
    ("bpm",            "BPM (multi-source consensus)"),
    ("mb_release_id",  "MusicBrainz release ID"),
]


# Pretty display names for ALL DB columns / tag fields. Used in the
# CUSTOM tag picker, the fetch-completion log, and anywhere the user
# would otherwise see raw snake_case identifiers. Falls back to title-
# cased column name when not in the map.
#
# Keys are DB column names (snake_case). Values are human display.
TAG_DISPLAY_NAMES: dict[str, str] = {
    # Core
    "artist": "Artist",
    "albumartist": "Album artist",
    "artists": "Artists (all credited)",
    "albumartistsort": "Album artist sort name",
    "artistsort": "Artist sort name",
    "album": "Album",
    "albumsort": "Album sort name",
    "title": "Track title",
    "titlesort": "Title sort name",
    "track_number": "Track number",
    "disc_number": "Disc number",
    "totaltracks": "Total tracks",
    "totaldiscs": "Total discs",
    "discsubtitle": "Disc subtitle",
    # Dates
    "year": "Year",
    "originaldate": "Original release date",
    "originalyear": "Original release year",
    "releasedate": "Release date",
    # Release identity
    "label": "Record label",
    "catalog_number": "Catalogue number",
    "country": "Release country",
    "barcode": "Barcode (UPC/EAN)",
    "asin": "Amazon ASIN",
    "release_type": "Release type",
    "release_status": "Release status",
    "media_format": "Media format",
    "packaging": "Packaging",
    "language": "Language",
    "script": "Script (writing system)",
    # Genre & content
    "genre": "Genre",
    "mb_genres": "MusicBrainz genres",
    "tags": "Folksonomy tags",
    "annotation": "Annotation",
    # Credits
    "composer": "Composer",
    "composersort": "Composer sort name",
    "lyricist": "Lyricist",
    "arranger": "Arranger",
    "conductor": "Conductor",
    "djmixer": "DJ / mixer",
    "engineer": "Engineer",
    "mixer": "Mixer",
    "producer": "Producer",
    "remixer": "Remixer",
    "writer": "Writer",
    # Classical
    "work": "Work",
    "movement": "Movement",
    "movementnumber": "Movement number",
    "movementtotal": "Movement total",
    "showmovement": "Show movement",
    # DJ / technical
    "bpm": "BPM",
    "musical_key": "Musical key",
    "isrc": "ISRC",
    # MusicBrainz IDs
    "mb_release_id": "MusicBrainz release ID",
    "musicbrainz_albumid": "MusicBrainz album ID",
    "musicbrainz_albumartistid": "MusicBrainz album artist ID",
    "musicbrainz_artistid": "MusicBrainz artist ID",
    "musicbrainz_recordingid": "MusicBrainz recording ID",
    "musicbrainz_workid": "MusicBrainz work ID",
    "musicbrainz_releasegroupid": "MusicBrainz release group ID",
    "musicbrainz_labelid": "MusicBrainz label ID",
    "musicbrainz_originalalbumid": "MusicBrainz original album ID",
    "musicbrainz_originalartistid": "MusicBrainz original artist ID",
    "musicbrainz_composerid": "MusicBrainz composer ID",
    # Discogs / AcoustID
    "discogs_release_id": "Discogs release ID",
    "discogs_master_id": "Discogs master ID",
    "acoustid_id": "AcoustID",
    "acoustid_fingerprint": "AcoustID fingerprint",
    # Originals
    "originalalbum": "Original album",
    "originalartist": "Original artist",
    # Misc
    "url_relations": "URL relations",
    "website": "Official website",
    "copyright": "Copyright",
    "license": "License",
    "aliases": "Aliases",
}


def tag_display_name(column_name: str) -> str:
    """Return a human-readable display name for a DB column. Falls back
    to title-cased snake_case if no mapping exists. Examples:
        'musicbrainz_albumid' -> 'MusicBrainz album ID'
        'foo_bar_baz'         -> 'Foo bar baz'
    """
    if column_name in TAG_DISPLAY_NAMES:
        return TAG_DISPLAY_NAMES[column_name]
    # Generic title-case fallback for unmapped columns. Replace
    # underscores with spaces, capitalize first letter.
    spaced = column_name.replace("_", " ")
    return spaced[:1].upper() + spaced[1:] if spaced else column_name


# Field-importance ranking for the activity-log display. When we report
# "+14 tags (...)", the user wants to see the fields that MATTER most
# first — the ones they'd actually notice missing — not an arbitrary
# dict-iteration order that buries "Album" behind six MusicBrainz UUIDs.
#
# Lower number = more important = shown first. Anything not listed
# falls into the default bucket (rank 50) and sorts alphabetically
# after the ranked fields. The tiers:
#   0-9   : the human-facing essentials (what shows in a file browser)
#   10-19 : release identity (label, catalogue, year, country)
#   20-29 : genre / credits
#   30-39 : technical (bpm, key, isrc, formats)
#   40-49 : cover art and similar
#   50    : default (unranked)
#   60+   : machine IDs (MBIDs, Discogs IDs) — least interesting to a human
_FIELD_IMPORTANCE: dict[str, int] = {
    # Tier 0: the essentials a human reads first
    "albumart": 0, "cover": 0, "cover_art": 0,   # if ever surfaced as a field
    "album": 1,
    "title": 2,
    "artist": 3,
    "albumartist": 4,
    "track_number": 5,
    "disc_number": 6,
    "year": 7,
    "genre": 8,
    # Tier 10: release identity
    "label": 10,
    "catalog_number": 11,
    "country": 12,
    "release_type": 13,
    "release_status": 14,
    "media_format": 15,
    "originaldate": 16,
    "originalyear": 17,
    "barcode": 18,
    # Tier 20: credits
    "composer": 20, "producer": 21, "remixer": 22, "djmixer": 23,
    "arranger": 24, "conductor": 25, "engineer": 26, "mixer": 27,
    "lyricist": 28, "writer": 29,
    # Tier 30: DJ / technical
    "bpm": 30, "musical_key": 31, "isrc": 32, "asin": 33,
    "language": 34, "script": 35, "packaging": 36,
    # Tier 40: extended content
    "mb_genres": 40, "tags": 41, "annotation": 42, "aliases": 43,
    "url_relations": 44, "website": 45, "copyright": 46, "license": 47,
    # Tier 60: machine identifiers — least human-interesting
    "mb_release_id": 60,
    "musicbrainz_albumid": 61,
    "musicbrainz_releasegroupid": 62,
    "musicbrainz_albumartistid": 63,
    "musicbrainz_artistid": 64,
    "musicbrainz_recordingid": 65,
    "musicbrainz_labelid": 66,
    "musicbrainz_workid": 67,
    "musicbrainz_composerid": 68,
    "musicbrainz_originalalbumid": 69,
    "musicbrainz_originalartistid": 70,
    "discogs_release_id": 71,
    "discogs_master_id": 72,
    "acoustid_id": 73,
    "acoustid_fingerprint": 74,
}


def field_importance(column_name: str) -> int:
    """Return the importance rank for a DB column (lower = more
    important). Unranked columns get the default middle rank so they
    sort after the explicitly-prioritised essentials but before the
    machine IDs."""
    # The bpm=174 style entries carry a value suffix — strip it first.
    base = column_name.split("=", 1)[0]
    return _FIELD_IMPORTANCE.get(base, 50)


# =============================================================================
# CONSENSUS PICKERS
# =============================================================================

def best_text_value(candidates: list[tuple[str, float]]) -> str:
    """
    Given [(value, score)...] pairs, return the value whose score is
    highest. Filter out empty strings first.
    """
    pool = [(v, s) for v, s in candidates if v]
    if not pool:
        return ""
    pool.sort(key=lambda x: x[1], reverse=True)
    return pool[0][0]


def consensus_int_value(values: list[int]) -> int:
    """
    Pick the most common integer. Ties broken by median.

    BPM merger. Three providers report 128, 127, 128 -> 128.
    Two providers report 120, 150 -> median = 135.
    """
    pool = [v for v in values if v and v > 0]
    if not pool:
        return 0
    if len(pool) == 1:
        return pool[0]
    counts = Counter(pool)
    most_common = counts.most_common()
    top_count = most_common[0][1]
    if top_count == 1:
        return int(statistics.median(pool))
    if len(most_common) == 1 or most_common[1][1] < top_count:
        return most_common[0][0]
    tied = [v for v, c in most_common if c == top_count]
    return int(statistics.median(tied))


# =============================================================================
# MERGED RELEASE
# =============================================================================

@dataclass
class MergedRelease:
    """Consensus best-guess for one album, across all providers."""
    artist: str = ""
    album: str = ""
    year: str = ""
    label: str = ""
    catalog_number: str = ""
    country: str = ""
    barcode: str = ""
    genre: str = ""
    # Archival fields — populated when MB's deep_harvest returns them.
    # These map 1:1 to columns in the files table.
    musicbrainz_albumid: str = ""
    musicbrainz_albumartistid: str = ""
    musicbrainz_releasegroupid: str = ""
    musicbrainz_labelid: str = ""
    release_status: str = ""
    release_type: str = ""
    language: str = ""
    script: str = ""
    packaging: str = ""
    media_format: str = ""
    media_track_count: int = 0
    annotation: str = ""
    aliases: str = ""           # JSON-encoded list
    mb_tags: str = ""           # JSON-encoded list
    mb_genres: str = ""         # JSON-encoded list
    url_relations: str = ""     # JSON-encoded dict
    ids_by_platform: dict[str, str] = field(default_factory=dict)
    art_url: str = ""
    art_source: str = ""
    sources: list[str] = field(default_factory=list)

    def to_db_update(self, columns: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for col in columns:
            v = getattr(self, col, "")
            if v:
                out[col] = v
        return out


def pick_confident_match(
    results: list,                  # list[Release] sorted score-desc
    provider_id: str,
    *,
    min_confidence: float = 80.0,
    ambiguity_margin: float = 5.0,
    per_provider_thresholds: dict[str, float] | None = None,
) -> tuple[object | None, str]:
    """
    Decide whether the top result from a provider is confident enough
    to trust. Returns (release_or_None, reason).

    The caller has historically used `results[0]` unconditionally — this
    function exists to replace that with "results[0] IF it clears the
    bar." We're optimising for ACCURACY over coverage: an album with
    no high-confidence match stays unfetched until a future run gets
    a better hit. Wrong tags are worse than missing tags because they
    drive the organiser to the wrong destination folder.

    Decision tree:
      1. No results → (None, "no results from provider")
      2. results[0].score below the threshold for this provider →
         (None, "top score X below threshold Y")
      3. results[0] and results[1] within `ambiguity_margin` of each
         other → (None, "ambiguous: 95.2 vs 93.5") — can't tell which
         pressing/remaster/region is right
      4. Otherwise → (results[0], "confident: 96.4 (threshold 85.0)")

    `per_provider_thresholds` overrides the global `min_confidence` per
    provider since MusicBrainz/Discogs server scores and local
    Levenshtein scores have different distributions.

    The reason string is for logging — both confident matches and
    rejections get logged so the user can see what's happening during
    a long fetch.
    """
    if not results:
        return None, "no results"

    threshold = min_confidence
    if per_provider_thresholds and provider_id in per_provider_thresholds:
        threshold = float(per_provider_thresholds[provider_id])

    top = results[0]
    top_score = float(getattr(top, "score", 0) or 0)

    if top_score < threshold:
        return None, f"top score {top_score:.1f} below threshold {threshold:.1f}"

    # Ambiguity check: if results[1] is within margin, we can't reliably
    # pick the right pressing/region/remaster. This catches "two valid
    # MB matches for slightly different issues of the same album" which
    # is exactly the kind of case where a wrong pick poisons the label
    # tag, splitting the album folder later.
    if len(results) >= 2:
        second = results[1]
        second_score = float(getattr(second, "score", 0) or 0)
        if (top_score - second_score) < ambiguity_margin:
            return None, (f"ambiguous: top {top_score:.1f} vs "
                          f"runner-up {second_score:.1f} "
                          f"(margin {ambiguity_margin:.1f})")

    return top, f"confident: {top_score:.1f} (threshold {threshold:.1f})"


def merge_releases(releases: list[tuple[Provider, Release]]) -> MergedRelease:
    merged = MergedRelease()
    if not releases:
        return merged

    # Text fields where we pick the highest-scoring provider's value
    fields_ = ["artist", "album", "year", "label", "catalog_number",
               "country", "barcode", "genre",
               # Archival text fields — only MB populates these today
               "musicbrainz_albumartistid", "musicbrainz_releasegroupid",
               "musicbrainz_labelid",
               "release_status", "release_type", "language", "script",
               "packaging", "media_format", "annotation"]
    for f in fields_:
        candidates = [(getattr(r, f, ""), r.score) for _, r in releases]
        setattr(merged, f, best_text_value(candidates))

    # The release with the highest score contributes its list/dict fields
    # (we don't try to merge across providers for these — too risky)
    if releases:
        top = max(releases, key=lambda pr: pr[1].score)[1]
        if top.aliases:
            import json as _j
            merged.aliases = _j.dumps(top.aliases, ensure_ascii=False)
        if top.mb_tags:
            import json as _j
            merged.mb_tags = _j.dumps(top.mb_tags, ensure_ascii=False)
        if top.mb_genres:
            import json as _j
            merged.mb_genres = _j.dumps(top.mb_genres, ensure_ascii=False)
        if top.url_relations:
            import json as _j
            merged.url_relations = _j.dumps(top.url_relations, ensure_ascii=False)
        if top.media_track_count:
            merged.media_track_count = top.media_track_count
        if top.release_id:
            merged.musicbrainz_albumid = top.release_id

    art_candidates = [(p.id, r) for p, r in releases if r.art_url]
    if art_candidates:
        art_candidates.sort(key=lambda x: x[1].score, reverse=True)
        merged.art_url = art_candidates[0][1].art_url
        merged.art_source = art_candidates[0][0]

    merged.sources = [p.id for p, _ in releases]
    merged.ids_by_platform = {p.id: r.release_id for p, r in releases if r.release_id}
    return merged


def merge_bpms(tracks: list[TrackInfo]) -> int:
    return consensus_int_value([t.bpm for t in tracks])


# =============================================================================
# HIGH-LEVEL FILL ENGINE
# =============================================================================

@dataclass
class FillStats:
    files_considered: int = 0
    files_updated: int = 0
    fields_updated: int = 0
    files_no_match: int = 0
    files_skipped: int = 0
    covers_downloaded: int = 0
    bpm_resolved: int = 0
    # New: detection-routing counters
    albums_skipped_unofficial: int = 0
    albums_routed_to_va: int = 0
    # Albums skipped because a resumed run already processed them
    albums_skipped_resume: int = 0
    # New v0.17 counters
    tags_written_to_file: int = 0     # files where mutagen successfully wrote
    tags_skipped_eac:     int = 0     # files skipped due to EAC/XLD log
    # v0.23.27 — confidence gating. Counts the cases where a provider
    # returned results but the top one wasn't confident enough to use.
    # `provider_results_low_confidence` = top score below threshold;
    # `provider_results_ambiguous` = top vs runner-up too close to call.
    # Both increment per (provider, album) — so one album can count
    # multiple times if multiple providers were inconclusive.
    provider_results_low_confidence: int = 0
    provider_results_ambiguous:      int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"considered:    {self.files_considered:,}",
            f"updated:       {self.files_updated:,} files / "
            f"{self.fields_updated:,} fields total",
            f"BPM resolved:  {self.bpm_resolved:,}",
            f"no match:      {self.files_no_match:,}",
            f"skipped:       {self.files_skipped:,}",
            f"covers:        {self.covers_downloaded:,}",
            f"detection:     routed {self.albums_routed_to_va:,} to "
            f"VA / skipped {self.albums_skipped_unofficial:,} unofficial",
            f"tags written:  {self.tags_written_to_file:,} files / "
            f"{self.tags_skipped_eac:,} skipped (EAC log)",
        ]
        # Confidence-gating counters only worth showing if non-zero
        # (most fully-tagged libraries won't trigger any).
        if (self.provider_results_low_confidence
                or self.provider_results_ambiguous):
            lines.append(
                f"low confidence: {self.provider_results_low_confidence:,} "
                f"rejected (top score below threshold)"
            )
            lines.append(
                f"ambiguous:     {self.provider_results_ambiguous:,} "
                f"rejected (top vs runner-up too close)"
            )
        if self.albums_skipped_resume:
            lines.insert(-1,
                         f"resume skip:   {self.albums_skipped_resume:,} albums "
                         f"already processed (from checkpoint)")
        return "\n".join(lines)


def fill_missing_metadata(
    db: Any,
    *,
    providers: list[Provider],
    target_columns: list[str],
    only_missing: bool = True,
    skip_complete_albums: bool = False,
    fetch_covers: bool = True,
    resolve_bpm: bool = True,
    write_to_files: bool = True,
    touch_verified_rips: bool = False,
    commit_per_file: bool = False,
    resume: bool = True,
    db_path: str = "",
    min_confidence_score: float = 80.0,
    ambiguity_margin: float = 5.0,
    per_provider_thresholds: dict[str, float] | None = None,
    ui: Any = None,
    log_cb: Callable[[str, str], None] | None = None,
) -> FillStats:
    """
    For each unique (artist, album) in the DB, query every enabled
    provider, merge the responses, and update the DB row.

    `only_missing`: field-level merge — when True, only writes a value
        when the existing one is empty. When False, providers overwrite.

    `skip_complete_albums`: album-level filter — when True, skip the
        network query entirely for albums where (artist, album, year,
        label, genre) are all already populated. Big speedup on a
        mostly-complete library. When False (default), every album
        gets queried regardless.

    `commit_per_file`: when True, calls `db.commit()` after EVERY
        per-file upsert. Forces each row into the main DB file rather
        than leaving it in the WAL. Trade-offs:
          - PRO: crash recovery is row-perfect; nothing is "in flight"
            in the WAL waiting for the next checkpoint.
          - CON: significant slowdown on USB-attached or networked
            storage (extra fsync per commit). For a 200k-file library
            on USB 3.0 HDD this can DOUBLE the run time.
        When False (default), SQLite WAL handles durability via its
        atomic-append journal — power loss is still safe, you'll just
        have to re-fetch any albums that hadn't checkpointed yet.

    `resume`: when True (default), checks for a fetch checkpoint left
        by a previous interrupted run. If found AND the operation
        matches (same DB, same providers, same target_columns, same
        only_missing flag), the (artist, album) tuples already
        processed last time are skipped. Saves potentially HOURS of
        re-querying albums whose tags are already filled in the DB.
        Set to False for a forced full re-run.

    `db_path`: path of the SQLite database. Required for resume to
        recognise "same operation as last time". Pass empty string to
        skip the match check (resume will only check phase != complete).

    When write_to_files=True (default), also writes the resolved tags
    back into the audio file's metadata blocks (so they travel with the
    file, e.g. when seeding on Soulseek). Files in folders with EAC/XLD
    logs are skipped to preserve verified-rip integrity unless
    touch_verified_rips=True.
    """
    stats = FillStats()

    # ----- HOT-PATH IMPORT HOISTING -----
    # These modules are used inside the per-album / per-file loops.
    # Python caches imports in sys.modules so a repeated `from X import
    # Y` isn't a re-parse, but it IS a module-dict lookup + name bind on
    # every iteration. For a 207k-file run the per-file ones (tag_writer,
    # nfo_writer) add up to real time. Bind them ONCE here. Each is
    # wrapped so a missing optional module degrades gracefully rather
    # than killing the whole fetch.
    import re as _re_mod
    import time as _time_mod
    try:
        from detection import quick_check_album as _quick_check_album
    except Exception:
        _quick_check_album = None
    try:
        from tag_writer import write_tags_to_file as _write_tags_to_file
    except Exception:
        _write_tags_to_file = None
    try:
        from nfo_writer import make_signature_comment as _make_signature_comment
    except Exception:
        _make_signature_comment = None
    try:
        from checkpoint import save_fetch_checkpoint as _save_fetch_checkpoint
    except Exception:
        _save_fetch_checkpoint = None

    # ----- RESUME CHECK ----------------------------------------------
    # Look for a leftover fetch checkpoint matching this operation. If
    # found, build a set of (artist, album) tuples to skip during the
    # main loop. Done up-front so the user sees the resume decision
    # before any DB scan.
    already_processed: set[tuple[str, str]] = set()
    fetch_cp = None
    if resume:
        try:
            from checkpoint import (
                FetchCheckpoint, load_fetch_checkpoint,
                fetch_checkpoint_matches,
            )
            existing = load_fetch_checkpoint()
            if existing is not None:
                provider_ids = [p.id for p in providers]
                if fetch_checkpoint_matches(
                    existing,
                    db_path=db_path,
                    target_columns=list(target_columns),
                    provider_ids=provider_ids,
                    only_missing=only_missing,
                    write_to_files=write_to_files,
                ):
                    already_processed = {
                        (pair[0], pair[1])
                        for pair in existing.processed_albums
                        if isinstance(pair, list) and len(pair) == 2
                    }
                    if already_processed:
                        def _emit_resume(level, msg):
                            if log_cb:
                                log_cb(level, msg)
                            elif ui is not None:
                                ui.log(level, msg)
                        _emit_resume(
                            "info",
                            f"resuming previous fetch: skipping "
                            f"{len(already_processed):,} albums already "
                            f"processed",
                        )
                        # Reuse the existing checkpoint object so we
                        # accumulate onto its processed list rather
                        # than starting from zero.
                        fetch_cp = existing
                        # Re-anchor the resumed run's stats so the
                        # summary line shows realistic totals.
                        stats.files_updated = existing.stats_files_updated
                        stats.files_no_match = existing.stats_files_no_match
                        stats.fields_updated = existing.stats_fields_updated
        except Exception as e:
            # Checkpoint load failure is non-fatal — fall through to a
            # fresh run. We log it but don't crash.
            if log_cb:
                log_cb("info", f"resume-check failed (will start fresh): {e}")
            elif ui is not None:
                ui.log("info", f"resume-check failed (will start fresh): {e}")

    # If no resumable checkpoint found, create a fresh one. The fetch
    # loop will append to it after each album finishes.
    if fetch_cp is None:
        try:
            from checkpoint import FetchCheckpoint
            import time as _time
            fetch_cp = FetchCheckpoint(
                started_at=_time.time(),
                db_path=db_path,
                target_columns=list(target_columns),
                provider_ids=[p.id for p in providers],
                only_missing=only_missing,
                skip_complete_albums=skip_complete_albums,
                write_to_files=write_to_files,
                phase="starting",
            )
        except Exception:
            fetch_cp = None    # graceful no-checkpoint mode


    def emit(level: str, msg: str) -> None:
        if log_cb:
            log_cb(level, msg)
        elif ui is not None:
            ui.log(level, msg)

    if not providers:
        emit("warning", "no providers enabled - aborting")
        stats.errors.append("no providers configured")
        return stats

    emit("info", f"providers enabled: {', '.join(p.id for p in providers)}")
    emit("info", "grouping files by (artist, album)...")

    by_release: dict[tuple[str, str], list[dict[str, Any]]] = {}
    # Recovery helpers (is_unknown_tag, recover_from_path) are used in
    # the grouping pass. Counter/Path imported here at function scope.
    from detection import is_unknown_tag, recover_from_path
    from collections import Counter
    from pathlib import Path as _Path

    stats_recovered_from_path = 0
    stats_recovered_from_siblings = 0
    stats_skipped_unknown = 0

    # ----- SINGLE-PASS DB SCAN -----
    # Previous versions scanned the whole table TWICE: once to build the
    # sibling-tag counters, once to group. On a 207k-file library on a
    # USB-attached HDD, a full table scan is the expensive part — so we
    # do it ONCE, materialising rows into memory while building the
    # sibling counters in the same loop, then resolve+group over the
    # in-memory list (no further DB I/O).
    #
    # Memory cost: ~207k row dicts. sqlite3.Row objects are lightweight;
    # we keep references, not copies, until grouping. Peak overhead is
    # a few hundred MB for a very large library — acceptable, and we'd
    # hold most of it in `by_release` anyway.
    #
    # The sibling counters only matter for folders that contain an
    # unknown tag, but we can't know which folders those are without
    # scanning — so we build counters for every folder during the same
    # pass. Counter updates are cheap (dict increment); the dominant
    # cost is the DB read, which we've now halved.
    folder_artist_counts: dict[str, Counter] = {}
    folder_album_counts: dict[str, Counter] = {}
    all_rows: list[Any] = []
    for row in db.iter_all():
        all_rows.append(row)
        try:
            path = (row["path"] or "").strip()
        except (KeyError, IndexError):
            continue
        if not path:
            continue
        folder = str(_Path(path).parent)
        try:
            a = (row["artist"] or "").strip()
        except (KeyError, IndexError):
            a = ""
        if a and not is_unknown_tag(a):
            folder_artist_counts.setdefault(folder, Counter())[a] += 1
        try:
            al = (row["album"] or "").strip()
        except (KeyError, IndexError):
            al = ""
        if al and not is_unknown_tag(al):
            folder_album_counts.setdefault(folder, Counter())[al] += 1

    # ----- GROUPING PASS (in-memory, no DB I/O) -----
    for row in all_rows:
        try:
            artist = (row["artist"] or "").strip()
        except (KeyError, IndexError):
            artist = ""
        try:
            album = (row["album"] or "").strip()
        except (KeyError, IndexError):
            album = ""
        try:
            path = (row["path"] or "").strip()
        except (KeyError, IndexError):
            path = ""

        artist_unknown = is_unknown_tag(artist)
        album_unknown = is_unknown_tag(album)

        # ----- RECOVERY CHAIN -----
        # Strategy 1: path-based recovery (folder name + filename parsing).
        # Recovered values are VALIDATED against is_unknown_tag before
        # being used — we never replace one placeholder with another.
        if (artist_unknown or album_unknown) and path:
            recovered = recover_from_path(
                path,
                have_artist=not artist_unknown,
                have_album=not album_unknown,
            )
            used_anything = False
            r_artist = recovered.get("artist", "").strip()
            r_album = recovered.get("album", "").strip()
            if artist_unknown and r_artist and not is_unknown_tag(r_artist):
                artist = r_artist
                artist_unknown = False
                used_anything = True
            if album_unknown and r_album and not is_unknown_tag(r_album):
                album = r_album
                album_unknown = False
                used_anything = True
            if used_anything:
                stats_recovered_from_path += 1

        # Strategy 2: sibling-tag inference. If other files in the same
        # folder have valid tags, the modal value is almost certainly
        # what this file should have too. Counts already exclude
        # placeholders, so this is always safe.
        if (artist_unknown or album_unknown) and path:
            folder = str(_Path(path).parent)
            used_anything = False
            if artist_unknown:
                ctr = folder_artist_counts.get(folder)
                if ctr:
                    artist = ctr.most_common(1)[0][0]
                    artist_unknown = False
                    used_anything = True
            if album_unknown:
                ctr = folder_album_counts.get(folder)
                if ctr:
                    album = ctr.most_common(1)[0][0]
                    album_unknown = False
                    used_anything = True
            if used_anything:
                stats_recovered_from_siblings += 1

        # ----- FINAL GUARD -----
        # If EITHER field is still unknown/placeholder after all recovery,
        # we refuse to query — sending "Unknown Artist" to MusicBrainz
        # burns rate-limit budget on guaranteed-miss queries AND can
        # accidentally match the (often wrong) "Unknown Artist" entries
        # that exist in MB. Skip silently and count for stats.
        if artist_unknown or album_unknown or not (artist and album):
            stats.files_skipped += 1
            stats_skipped_unknown += 1
            continue

        # Stash post-recovery values onto the row dict so the downstream
        # "no match" log shows what we actually queried with.
        d = dict(row)
        d["_query_artist"] = artist
        d["_query_album"] = album
        by_release.setdefault((artist, album), []).append(d)

    # Recovery-stats logging
    if stats_recovered_from_path:
        emit("info",
             f"path recovery: filled in artist/album for "
             f"{stats_recovered_from_path:,} files from filename/folder names")
    if stats_recovered_from_siblings:
        emit("info",
             f"sibling-tag inference: filled in artist/album for "
             f"{stats_recovered_from_siblings:,} files from other tracks in "
             f"the same folder")
    if stats_skipped_unknown:
        emit("info",
             f"refused to query: {stats_skipped_unknown:,} files have no "
             f"recoverable artist/album — left untagged "
             f"(would have sent 'Unknown Artist' to providers)")

    n_total_albums = len(by_release)

    # Album-level filter: when skip_complete_albums is on, drop entire
    # albums where every key field is already populated. Big speedup on
    # a mostly-complete library — saves both the network query AND the
    # merge logic for albums that have nothing useful to gain.
    if skip_complete_albums:
        KEY_FIELDS = ("year", "label", "genre", "mb_release_id")
        skipped: list[tuple[str, str]] = []
        for key in list(by_release.keys()):
            rows = by_release[key]
            # Album counts as "complete" if EVERY row has values in
            # every key field. (Be conservative: even one missing field
            # in one row means we'd still want to query.)
            if all(
                all(str(r.get(f) or "").strip() for f in KEY_FIELDS)
                for r in rows
            ):
                skipped.append(key)
                del by_release[key]
        if skipped:
            emit("info", f"skip-complete: {len(skipped):,} albums already have "
                         f"year+label+genre+mb_id — not querying them")

    n_albums = len(by_release)
    n_files = sum(len(v) for v in by_release.values())
    if skip_complete_albums and n_total_albums != n_albums:
        emit("info", f"library has {n_files:,} files in {n_albums:,} albums to query "
                     f"(filtered from {n_total_albums:,} total)")
    else:
        emit("info", f"library has {n_files:,} files in {n_albums:,} unique albums")

    # Honest range estimate (matches what the pre-flight prints).
    # The in-UI live ETA — EMA-smoothed — is what the user should
    # actually watch once the run is going.
    if providers:
        rates = [p.rate_limit_seconds for p in providers]
        best_per = min(rates)
        worst_per = sum(rates) * 2.0    # all providers + fallbacks
        likely_per = best_per * 0.6 + sum(rates) * 0.4
        emit("info",
             f"estimated runtime: best ~{(n_albums * best_per) / 60:.0f}m | "
             f"likely ~{(n_albums * likely_per) / 60:.0f}m | "
             f"worst ~{(n_albums * worst_per) / 60:.0f}m "
             f"(live ETA in the progress bar is more accurate once "
             f"the run is past warmup)")

    if ui is not None:
        ui.set_total(n_albums)
        # The metadata fetch advances ONE counter per album (not
        # per-file). Tell the UI to label its rate accordingly so
        # users don't see "1.0 files/s" when we're actually doing
        # one album per second across many files.
        if hasattr(ui, "set_unit"):
            ui.set_unit("albums")

    # Detection helper is applied per-album before any provider hit
    # to (a) skip albums that won't be in any DB, (b) route compilations
    # to "Various Artists" queries which is what MB indexes them under,
    # and (c) clean version/edition junk out of album names. Hoisted to
    # the top of the function as _quick_check_album.

    # Stats counters for detection routing
    stats_skipped_unofficial = 0
    stats_routed_to_va = 0

    seen_albums = 0
    # Set up checkpoint cadence. Writing the JSON on every album is
    # cheap (it's <1MB for 100k albums of pair-strings) but flushing
    # to disk every iteration adds up on USB HDD. Write every N
    # albums OR after at least M seconds have passed since the last
    # write — whichever comes first. This caps worst-case "work lost
    # on crash" at min(N albums, M seconds).
    _CHECKPOINT_EVERY_N_ALBUMS = 10
    _CHECKPOINT_EVERY_N_SECONDS = 30.0
    _last_checkpoint_write = 0.0
    _albums_since_last_write = 0

    import time as _ckp_time

    if fetch_cp is not None:
        fetch_cp.total_albums = n_albums + len(already_processed)
        fetch_cp.phase = "fetching"
        if _save_fetch_checkpoint is not None:
            try:
                _save_fetch_checkpoint(fetch_cp)
                _last_checkpoint_write = _ckp_time.time()
            except Exception:
                pass

    for (artist, album), rows in by_release.items():
        seen_albums += 1
        stats.files_considered += len(rows)

        # ----- RESUME-SKIP -----
        # If this (artist, album) was processed in a previous run we're
        # resuming, skip everything for it. The DB already has whatever
        # tags last run wrote; re-querying would burn rate-limit budget
        # to produce the same result.
        if (artist, album) in already_processed:
            stats.albums_skipped_resume += 1
            if ui is not None:
                ui.advance()
            continue

        if ui is not None:
            ui.update(
                current_folder=f"album {seen_albums:,}/{n_albums:,}",
                current_file=f"{artist} - {album}",
            )

        # ----- pre-flight check ----------------------------------------
        # Cheap regex check on the album name before burning rate-limit
        # budget on a query that's guaranteed to miss.
        qc = _quick_check_album(artist, album)

        # Bootleg/unofficial: don't query any provider. The release is
        # not on MB/Deezer/etc by definition, so it'd be 100% no-match.
        if qc.skip:
            stats.files_no_match += len(rows)
            stats_skipped_unofficial += 1
            stats.albums_skipped_unofficial += 1
            emit("info", f"skip: {artist} - {album}  ({qc.skip_reason})")
            if ui is not None:
                ui.advance()  # not a hard error, but no work to do
            continue

        # Compilation/VA: route the query to "Various Artists". MB indexes
        # compilation albums under that name; querying with the per-track
        # artist is what caused the user's 100% no-match Initial D run.
        if qc.use_various_artists:
            query_artist = "Various Artists"
            stats_routed_to_va += 1
            stats.albums_routed_to_va += 1
            emit("info",
                 f"compilation: {artist} - {album}  "
                 f"-> querying as 'Various Artists' ({qc.note})")
        else:
            query_artist = artist

        # Use the normalised album string for the actual provider query
        # (strips Japanese markers, deluxe-edition tags, disc numbers).
        # The original (artist, album) is still used as the key into
        # `rows` for writing back, so the DB row's original strings are
        # preserved — we only normalise for the lookup itself.
        query_album = qc.normalised_album or album

        # Build a list of fallback (artist, album) query pairs to try if
        # the primary query misses. Order matters: most-specific first.
        # We stop at the first hit.
        fallback_queries: list[tuple[str, str, str]] = []   # (artist, album, why)

        # Fallback 1: catalogue-number stripped query.
        # "[SHADOW 082] Helicopter '97" → "Helicopter '97"
        if qc.catalogue_label or qc.catalogue_number:
            stripped = _CAT_STRIP_RE.sub(" ", query_album).strip()
            stripped = _WHITESPACE_RE.sub(" ", stripped)
            if stripped and stripped != query_album:
                fallback_queries.append((query_artist, stripped,
                                          "cat-num stripped"))

        # Fallback 2 & 3: compilation series.
        # When we recognise the album as part of a recurring series
        # (Beatport Drum & Bass: Sound Pack #348, etc.), the per-track
        # artist won't match — the MB release is filed under Various
        # Artists. Query the series name + issue first; if that misses,
        # the series name alone.
        if qc.is_compilation_series and qc.series_name:
            if qc.series_issue:
                # Only add the "VA + series + issue" fallback if it
                # differs from the primary query (which it does when
                # the primary query_album was the FULL noisy string
                # rather than the cleaned series name).
                fb1_album = f"{qc.series_name} #{qc.series_issue}"
                if (query_artist, fb1_album) != ("Various Artists", query_album):
                    fallback_queries.append((
                        "Various Artists", fb1_album,
                        "VA + series + issue",
                    ))
                # Also try without the issue number — sometimes MB has
                # the parent series indexed but not every individual
                # numbered release.
                if (query_artist, qc.series_name) != ("Various Artists", query_album):
                    fallback_queries.append((
                        "Various Artists", qc.series_name,
                        "VA + series (no issue)",
                    ))
            else:
                if (query_artist, qc.series_name) != ("Various Artists", query_album):
                    fallback_queries.append((
                        "Various Artists", qc.series_name,
                        "VA + series",
                    ))

        # Log the strategy if any fallbacks will be tried
        if fallback_queries:
            strats = ", ".join(f"'{a}' '{b}'" for a, b, _why in fallback_queries[:3])
            emit("info",
                 f"will try {len(fallback_queries)} fallback queries if primary misses")

        # ----- query each provider in turn -----
        # Call-site guard: refuse to invoke search_release with placeholder
        # artist/album. Belt-and-braces with Provider.is_safe_query (which
        # ALSO rejects these) — if either layer ever drifts, the other
        # catches us. This also short-circuits before any rate-limit
        # sleep, so failing the guard costs zero wall time.
        provider_releases: list[tuple[Provider, Release]] = []
        if not Provider.is_safe_query(query_artist, query_album):
            # Should never get here given the recovery chain at grouping
            # time, but defend in depth. Log so we can spot any bug in
            # the recovery code.
            emit("info",
                 f"refusing to query: '{query_artist}' '{query_album}' "
                 f"looks like a placeholder — skipping providers")
            stats.files_no_match += len(rows)
            if ui is not None:
                ui.advance(broken=True)
            continue
        for prov in providers:
            try:
                results = prov.search_release(query_artist, query_album)
            except Exception as e:
                stats.errors.append(f"{prov.id}: {query_artist} - {query_album}: {e}")
                emit("broken",
                     f"{prov.id} failed for {query_artist} - {query_album}: {e}")
                continue
            # If primary search came up empty, try each fallback in
            # order until one hits or all are exhausted.
            if not results:
                for fb_artist, fb_album, fb_why in fallback_queries:
                    # Each fallback also goes through the safety check.
                    if not Provider.is_safe_query(fb_artist, fb_album):
                        continue
                    try:
                        results = prov.search_release(fb_artist, fb_album)
                    except Exception:
                        continue
                    if results:
                        emit("info",
                             f"{prov.id}: matched via '{fb_why}': "
                             f"'{fb_artist}' '{fb_album}'")
                        break
            if results:
                # CONFIDENCE GATE.
                # We used to just pick results[0]. Now we only commit
                # to the top hit if its score is high enough AND it's
                # clearly ahead of the runner-up. Wrong tags are worse
                # than missing tags (they drive the organiser to the
                # wrong folder). An album that doesn't get a confident
                # match here stays unfetched — a future run with
                # better input tags may get a higher score.
                pick, why = pick_confident_match(
                    results, prov.id,
                    min_confidence=min_confidence_score,
                    ambiguity_margin=ambiguity_margin,
                    per_provider_thresholds=per_provider_thresholds,
                )
                if pick is not None:
                    provider_releases.append((prov, pick))
                    # Verbose logging only at debug level so we don't
                    # spam the activity log with one line per provider
                    # per album. The rejection cases are more interesting
                    # and get logged below.
                    if log_cb:
                        log_cb("debug",
                               f"{prov.id} {query_artist} - {query_album}: {why}")
                else:
                    # Record what kind of rejection this was so the
                    # final summary can tell the user.
                    if why.startswith("ambiguous"):
                        stats.provider_results_ambiguous += 1
                    else:
                        stats.provider_results_low_confidence += 1
                    emit("info",
                         f"{prov.id} {query_artist} - {query_album}: "
                         f"rejected ({why})")

        # ----- INTERNALS FALLBACK ------------------------------------
        # Everything above missed. Before logging "no match", peek at
        # the file's OTHER tag fields (albumartist, originalalbum,
        # originalartist) that were extracted during the index pass.
        # These often hold cleaner info than the artist/album fields
        # the user sees in the file picker.
        #
        # Concrete cases this catches:
        #   • Compilation tracks where artist="Bus Stop" but
        #     albumartist="Various Artists" — querying with the album
        #     artist routes to the right MB release.
        #   • Re-issues where album="Greatest Hits Vol 2" but
        #     originalalbum="..." — the original album might be in MB
        #     even when the re-issue isn't.
        #   • Tagger-mangled fields where the canonical names are
        #     hiding in *sort variants (albumartistsort, etc).
        #
        # We're CONSERVATIVE about this: only build a candidate if
        # the field value differs MEANINGFULLY from what we already
        # tried. And we run after all earlier fallbacks because the
        # quality signal is weaker — these tag fields are often less
        # reliable than artist/album.
        if not provider_releases and rows:
            sample = rows[0]
            internals_queries: list[tuple[str, str, str]] = []

            def _norm(s: str) -> str:
                return (s or "").strip().lower()

            already_tried_pairs = {
                (_norm(query_artist), _norm(query_album)),
            }
            for fb_a, fb_al, _ in fallback_queries:
                already_tried_pairs.add((_norm(fb_a), _norm(fb_al)))

            def _add_if_new(a: str, al: str, why: str) -> None:
                a = (a or "").strip()
                al = (al or "").strip()
                if not (a and al):
                    return
                if (_norm(a), _norm(al)) in already_tried_pairs:
                    return
                already_tried_pairs.add((_norm(a), _norm(al)))
                internals_queries.append((a, al, why))

            # Try albumartist instead of artist
            aa = (sample.get("albumartist") or "").strip()
            if aa and aa.lower() not in ("", "various", "various artists"):
                _add_if_new(aa, album, "albumartist field")
            elif aa.lower() in ("various", "various artists"):
                _add_if_new("Various Artists", album, "albumartist=VA")

            # Try originalalbum (re-issue case)
            oa = (sample.get("originalalbum") or "").strip()
            if oa:
                _add_if_new(artist, oa, "originalalbum field")
                # Combined: originalartist + originalalbum
                oar = (sample.get("originalartist") or "").strip()
                if oar:
                    _add_if_new(oar, oa, "originalartist + originalalbum")

            # Last-resort: query the album alone as a VA compilation.
            # This catches Dancemania-style comps that aren't recognised
            # by our compilation-series regex but ARE filed under VA in
            # MB/Discogs.
            _add_if_new("Various Artists", album, "VA + bare album (last-resort)")

            if internals_queries:
                emit("info",
                     f"primary+fallbacks missed → trying {len(internals_queries)} "
                     f"internals-based candidates")
                for fb_artist_i, fb_album_i, fb_why_i in internals_queries:
                    # Same safety guard as the primary loop.
                    if not Provider.is_safe_query(fb_artist_i, fb_album_i):
                        continue
                    for prov in providers:
                        try:
                            results = prov.search_release(fb_artist_i, fb_album_i)
                        except Exception:
                            continue
                        if results:
                            # Require a minimum similarity score to avoid
                            # wrong-release noise. A 60/100 cutoff keeps
                            # plausibly-correct matches and drops obviously
                            # wrong ones (provider returned ANY match for
                            # the query even though it's not the right
                            # release).
                            top = results[0]
                            if top.score < 60:
                                continue
                            provider_releases.append((prov, top))
                            emit("info",
                                 f"{prov.id}: matched via internals "
                                 f"'{fb_why_i}' (score={top.score:.0f}): "
                                 f"'{fb_artist_i}' '{fb_album_i}'")
                            break   # one provider hit, stop polling rest
                    if provider_releases:
                        break       # one query hit, stop trying more

        # ----- PER-RECORDING SAMPLING FALLBACK -----------------------
        # Album-level lookup missed completely. Last resort before
        # giving up: sample a few individual track TITLES from this
        # group and query MB's /recording/ endpoint for each. The
        # response includes the releases each recording appears on.
        # If multiple tracks point to the SAME release MBID, that's
        # almost certainly our album.
        #
        # Why this catches what the album-level path missed:
        #   • Compilations where album field is per-track garbage but
        #     individual track titles are clean
        #   • Releases where MB has the recording entries indexed but
        #     the release-title text-search doesn't surface them
        #   • Tracks split across multiple MB releases (e.g. CD/vinyl
        #     pressings) — we'll find the one with the most votes
        #
        # Cost: up to 3 extra MB requests per missed album. We only
        # do this for the MusicBrainz provider (others don't have a
        # recording-search method yet) and only when nothing else worked.
        if not provider_releases and rows:
            mb_prov = None
            for p in providers:
                if p.id == "musicbrainz" and hasattr(p, "search_by_recording"):
                    mb_prov = p
                    break
            if mb_prov is not None:
                # Sample up to 3 tracks with non-empty title + artist.
                # Take from the start, middle, and end of the rows list
                # so we don't bias toward a single track that might be
                # an outlier (alternate version, bonus track, etc.).
                # Eligibility filter: skip rows whose title or artist
                # is missing OR a placeholder. Sampling on "Unknown
                # Title - Unknown Artist" would query MB with garbage
                # and waste budget.
                eligible = [
                    r for r in rows
                    if (r.get("title") or "").strip()
                       and (r.get("artist") or "").strip()
                       and not is_unknown_tag(r.get("title") or "")
                       and not is_unknown_tag(r.get("artist") or "")
                ]
                if eligible:
                    n = len(eligible)
                    if n <= 3:
                        samples = eligible
                    else:
                        samples = [eligible[0],
                                   eligible[n // 2],
                                   eligible[-1]]
                    # Vote tracking. We count how many DISTINCT sampled
                    # TRACKS point at each release — not raw appearances.
                    # A single track can appear on a release under
                    # multiple recording entries; counting appearances
                    # produced nonsense like "6/3 tracks point to X"
                    # (more votes than tracks). Track-level dedup via a
                    # set of track-indices per release fixes the display
                    # AND makes the threshold meaningful ("2 of 3 tracks
                    # agree" is a real consensus signal).
                    release_votes: dict[str, tuple[set, "Release"]] = {}
                    for track_idx, row in enumerate(samples):
                        t = (row.get("title") or "").strip()
                        a = (row.get("artist") or "").strip()
                        try:
                            rec_results = mb_prov.search_by_recording(a, t)
                        except Exception:
                            continue
                        # A track votes AT MOST ONCE for any given
                        # release, no matter how many recording entries
                        # link it there.
                        seen_this_track: set[str] = set()
                        for rel in rec_results[:5]:
                            if not rel.release_id:
                                continue
                            if rel.release_id in seen_this_track:
                                continue
                            seen_this_track.add(rel.release_id)
                            prev = release_votes.get(rel.release_id)
                            if prev is None:
                                release_votes[rel.release_id] = (
                                    {track_idx}, rel,
                                )
                            else:
                                prev[0].add(track_idx)
                    if release_votes:
                        # Pick the release the most DISTINCT tracks point
                        # to. Tiebreak by the recording score stored on
                        # the Release.
                        best_id, (voting_tracks, best_rel) = max(
                            release_votes.items(),
                            key=lambda x: (len(x[1][0]), x[1][1].score),
                        )
                        vote_count = len(voting_tracks)
                        min_votes = 1 if len(samples) == 1 else 2
                        if vote_count >= min_votes:
                            emit("info",
                                 f"recording sampling: {vote_count}/"
                                 f"{len(samples)} sampled tracks point to "
                                 f"'{best_rel.album}' (mbid={best_id[:8]}…)")
                            # Materialise the full release record via
                            # direct MBID fetch. This populates year,
                            # label, catalog_number, etc — fields the
                            # per-recording response doesn't carry.
                            try:
                                full = mb_prov.fetch_release_by_id(best_id)
                                if full is not None:
                                    provider_releases.append((mb_prov, full))
                                else:
                                    # Fetch failed but we still have a
                                    # partial Release from the vote.
                                    # Use it — at least the album title
                                    # and MBID are correct.
                                    provider_releases.append(
                                        (mb_prov, best_rel)
                                    )
                            except Exception as e:
                                emit("info",
                                     f"recording sampling: fetch_by_id "
                                     f"failed: {e}; using stub")
                                provider_releases.append((mb_prov, best_rel))

        # ----- ACOUSTID FINGERPRINT FALLBACK -------------------------
        # Every text-based path has missed. Last resort: fingerprint one
        # file from this album via AcoustID/Chromaprint to get a MB
        # recording ID, then use MB to resolve which release it belongs to.
        # Only fires when AcoustID is in the provider list AND the
        # fingerprint score meets the confidence threshold (0.5 / 50%).
        if not provider_releases and rows:
            _aid_prov = None
            _mb_for_aid = None
            for _p in providers:
                if _p.id == "acoustid" and hasattr(_p, "fingerprint_file"):
                    _aid_prov = _p
                if _p.id == "musicbrainz" and hasattr(_p, "fetch_releases_for_recording_id"):
                    _mb_for_aid = _p

            if _aid_prov is not None:
                # Pick the first file that exists on disk.
                _fp_path: str | None = None
                for _row in rows:
                    _cand = (_row.get("path") or "").strip()
                    if _cand and Path(_cand).exists():
                        _fp_path = _cand
                        break

                if _fp_path:
                    emit("info",
                         f"acoustid: fingerprinting {Path(_fp_path).name} "
                         f"({artist} - {album})")
                    try:
                        _aid_hits = _aid_prov.fingerprint_file(_fp_path)
                    except Exception as _e:
                        _aid_hits = []
                        emit("info", f"acoustid: fingerprint error: {_e}")

                    for _score, _recording_id in _aid_hits[:3]:
                        if _score < 0.5:
                            emit("info",
                                 f"acoustid: low-confidence fingerprint "
                                 f"(score={_score:.2f}) — skipping")
                            break
                        if _mb_for_aid is not None:
                            try:
                                _rel_stubs = _mb_for_aid.fetch_releases_for_recording_id(
                                    _recording_id
                                )
                            except Exception:
                                _rel_stubs = []
                            if _rel_stubs:
                                try:
                                    _full = _mb_for_aid.fetch_release_by_id(
                                        _rel_stubs[0].release_id
                                    )
                                except Exception:
                                    _full = None
                                if _full is not None:
                                    provider_releases.append((_mb_for_aid, _full))
                                    emit("info",
                                         f"acoustid: matched via fingerprint "
                                         f"(score={_score:.2f}): '{_full.album}' "
                                         f"(mbid={_full.release_id[:8]}…)")
                                    break
                        else:
                            # No MB available — surface what AcoustID gave us
                            from metadata_providers import Release as _Release
                            _stub = _Release(
                                platform="acoustid",
                                artist=artist,
                                album=album,
                                score=_score * 100,
                            )
                            provider_releases.append((_aid_prov, _stub))
                            emit("info",
                                 f"acoustid: partial fingerprint match "
                                 f"(score={_score:.2f}) — no MB to resolve release")
                            break

        if not provider_releases:
            stats.files_no_match += len(rows)
            emit("warning", f"no match: {artist} - {album}")
            if ui is not None:
                ui.advance(broken=True)
            continue

        # ----- merge into one consensus result -----
        merged = merge_releases(provider_releases)
        update_dict = merged.to_db_update(target_columns)
        emit("info",
             f"match: {artist} - {album} "
             f"(from {', '.join(merged.sources)})")

        # ----- BPM resolution (computed first, applied below in the
        # combined update so we don't clobber it with the album-level
        # text updates) -----
        bpm_by_path: dict[str, int] = {}
        if resolve_bpm and "bpm" in target_columns:
            bpm_capable = [p for p in providers
                           if type(p).search_track is not Provider.search_track]
            if bpm_capable:
                for row in rows:
                    title = (row.get("title") or "").strip()
                    if not title or is_unknown_tag(title):
                        # Don't query providers with "Unknown Title"
                        # or blank — those queries always miss and
                        # waste rate-limit budget.
                        continue
                    # Treat empty string, '0', None, and 0 all as "missing".
                    raw_bpm = row.get("bpm")
                    try:
                        current_bpm = int(raw_bpm) if raw_bpm else 0
                    except (TypeError, ValueError):
                        current_bpm = 0
                    if only_missing and current_bpm > 0:
                        continue
                    track_results = []
                    for prov in bpm_capable:
                        try:
                            hits = prov.search_track(artist, title)
                            if hits:
                                track_results.append(hits[0])
                        except Exception as e:
                            stats.errors.append(f"{prov.id} track lookup: {e}")
                    consensus = merge_bpms(track_results)
                    if consensus:
                        bpm_by_path[row["path"]] = consensus

        # ----- per-file combined updates (text fields + BPM in one upsert) ---
        for row in rows:
            row_update_dict = dict(row)
            updated_fields = 0
            # Track which specific fields we filled, so the log line can
            # say "+3 tags (label, year, genre)" instead of just "+3 tags".
            filled_fields: list[str] = []
            for col, new_val in update_dict.items():
                if col == "bpm":
                    continue  # handled separately via bpm_by_path
                current_val = (row.get(col) or "")
                if isinstance(current_val, str):
                    current_val = current_val.strip()
                if only_missing and current_val:
                    continue
                row_update_dict[col] = new_val
                updated_fields += 1
                filled_fields.append(col)

            # Layer in any BPM we resolved for this specific file
            if row["path"] in bpm_by_path:
                row_update_dict["bpm"] = bpm_by_path[row["path"]]
                stats.bpm_resolved += 1
                updated_fields += 1
                filled_fields.append(f"bpm={bpm_by_path[row['path']]}")

            if "mb_release_id" in target_columns:
                mbid = merged.ids_by_platform.get("musicbrainz", "")
                if mbid and (only_missing and not row.get("mb_release_id")):
                    row_update_dict["mb_release_id"] = mbid
                    updated_fields += 1
                    filled_fields.append("mb_release_id")

            # ----- BROKEN-STATUS CHECK -----
            # After merging in whatever the providers gave us, decide
            # whether this row still has the minimum required tags
            # (artist + album + title). If not, mark status='broken' so
            # the next organise pass moves it to the Broken folder.
            #
            # We check `row_update_dict` (the post-merge state), NOT the
            # pre-merge `row`, so a successful fetch that filled in a
            # missing title correctly UN-breaks the row.
            #
            # We don't fire on EVERY row — only when:
            #   - we actually updated something, OR
            #   - the current status is missing/blank/wrong relative to
            #     the new check (e.g. row is marked 'ok' but is actually
            #     broken — typical for files imported before this check
            #     existed)
            # This keeps the DB write count down on a fully-tagged
            # library where nothing needs to change.
            try:
                from detection import is_record_metadata_broken
                is_broken, broken_reason = is_record_metadata_broken(row_update_dict)
                current_status = (row.get("status") or "").strip().lower()
                desired_status = "broken" if is_broken else "ok"
                # Only touch status if it would actually change.
                # Special case: if status is currently something specific
                # like 'duplicate' or 'orphan', don't overwrite — those
                # mean something different and shouldn't be clobbered by
                # the metadata-quality check.
                _PRESERVED_STATUSES = {"duplicate", "orphan", "quarantine"}
                if current_status in _PRESERVED_STATUSES:
                    pass  # leave it alone
                elif current_status != desired_status:
                    row_update_dict["status"] = desired_status
                    if updated_fields == 0:
                        # No tag updates happened, but the status itself
                        # needs to change. Count this as an update so
                        # the row gets upserted.
                        updated_fields += 1
                    if is_broken:
                        filled_fields.append(f"status=broken ({broken_reason})")
                        stats.errors.append(
                            f"marked broken: {Path(row['path']).name} — {broken_reason}"
                        )
                    else:
                        filled_fields.append("status=ok (un-broken)")
            except Exception as e:
                # Broken-check is a soft check — a bug in the predicate
                # shouldn't kill the whole fetch run for this file.
                if log_cb:
                    log_cb("info", f"broken-check failed for {row.get('path')}: {e}")

            if updated_fields > 0:
                try:
                    db.upsert_file(row_update_dict)
                    if commit_per_file:
                        # Force this row to disk immediately. See the
                        # `commit_per_file` docstring above for the
                        # full trade-off. On USB-attached storage,
                        # this fsync-per-file pattern can dominate
                        # wall time; on SSDs it's barely noticeable.
                        try:
                            db.commit()
                        except Exception as e:
                            # Commit failure is non-fatal — the row is
                            # still in the WAL, just not in the main
                            # DB yet. Log and continue.
                            emit("info",
                                 f"per-file commit failed (will be "
                                 f"flushed at next checkpoint): {e}")
                    stats.files_updated += 1
                    stats.fields_updated += updated_fields
                    if ui is not None:
                        ui.advance(imported=True)
                    # Build a readable field list. Cap at 6 fields shown
                    # to keep activity-log lines from blowing out wider
                    # than the panel; trailing "+N more" indicates the
                    # overflow so the user knows everything DID get filled
                    # even if not shown.
                    #
                    # Render each field through tag_display_name() so the
                    # user sees "Release country, MusicBrainz album ID,
                    # Media format" rather than raw DB column names like
                    # "country, musicbrainz_albumid, media_format".
                    # The bpm=NNN entry is special-cased (it already
                    # carries a value) — only the bare column names get
                    # prettified.
                    def _pretty(field: str) -> str:
                        # bpm=174 style entries pass through unchanged
                        if "=" in field:
                            name, _, val = field.partition("=")
                            return f"{tag_display_name(name)}={val}"
                        return tag_display_name(field)

                    # Sort by importance FIRST, so when we cap the
                    # display at 6 fields the ones that survive are the
                    # human-facing essentials (album, title, year, label)
                    # rather than an arbitrary handful of MusicBrainz
                    # UUIDs. Ties (same rank) sort alphabetically by
                    # display name for stable, readable output.
                    sorted_fields = sorted(
                        filled_fields,
                        key=lambda f: (field_importance(f), _pretty(f).lower()),
                    )
                    pretty_fields = [_pretty(f) for f in sorted_fields]
                    if len(pretty_fields) <= 6:
                        fields_str = ", ".join(pretty_fields)
                    else:
                        fields_str = (", ".join(pretty_fields[:6])
                                      + f", +{len(pretty_fields)-6} more")
                    emit("imported",
                         f"+{updated_fields} tags ({fields_str}) for "
                         f"{Path(row['path']).name}")

                    # Write the tags into the actual audio file so they
                    # travel with the file (e.g. when seeded on Soulseek).
                    # We pass ONLY the fields we just resolved/changed,
                    # not the whole row, so existing in-file tags aren't
                    # disturbed for fields we didn't touch.
                    if write_to_files:
                        write_dict = {
                            k: v for k, v in row_update_dict.items()
                            if k in update_dict or k == "bpm"
                            or k == "mb_release_id"
                        }
                        # Add the provenance signature — a one-line note
                        # in the file's comment tag identifying ripper +
                        # tagger. Only added if comment isn't already set
                        # OR if not in only_missing mode.
                        if _make_signature_comment is not None:
                            try:
                                sig = _make_signature_comment(row_update_dict)
                                if sig:
                                    write_dict.setdefault("comment", sig)
                            except Exception:
                                pass
                        if _write_tags_to_file is not None:
                            try:
                                wr = _write_tags_to_file(
                                    row["path"],
                                    write_dict,
                                    only_missing=only_missing,
                                    touch_verified_rips=touch_verified_rips,
                                )
                                if wr.skipped_entirely:
                                    stats.tags_skipped_eac += 1
                                    # don't spam the log per file; one
                                    # info is enough at first then suppress
                                    if stats.tags_skipped_eac <= 5:
                                        emit("info",
                                             f"skip writeback (EAC log): "
                                             f"{Path(row['path']).name}")
                                elif wr.error:
                                    stats.errors.append(
                                        f"writeback {row['path']}: {wr.error}"
                                    )
                                elif wr.written_fields:
                                    stats.tags_written_to_file += 1
                            except Exception as e:
                                stats.errors.append(
                                    f"writeback exception {row['path']}: {e}"
                                )
                except Exception as e:
                    stats.errors.append(f"DB update {row['path']}: {e}")
                    emit("broken", f"DB update failed: {Path(row['path']).name}")
            elif ui is not None:
                ui.advance()

        # ----- cover art (once per album folder) -----
        if fetch_covers and merged.art_url and rows:
            album_dirs = {Path(r["path"]).parent for r in rows}
            for album_dir in album_dirs:
                if not album_dir.exists():
                    continue
                cover_path = album_dir / "folder.jpg"
                if cover_path.exists():
                    continue
                art_provider = next(
                    (p for p in providers if p.id == merged.art_source),
                    providers[0],
                )
                fake_rel = Release(art_url=merged.art_url)
                if art_provider.fetch_cover(fake_rel, cover_path):
                    stats.covers_downloaded += 1
                    emit("info", f"cover art ({merged.art_source}): {album_dir.name}")

        # ----- CHECKPOINT SAVE (per-album) -----
        # Record that this album is done, mirror current stats onto the
        # checkpoint, and flush to disk on the cadence configured at the
        # top of the loop. Worst case on crash: lose up to N albums (or
        # M seconds) of progress, configured above.
        if fetch_cp is not None:
            fetch_cp.processed_albums.append([artist, album])
            fetch_cp.last_album = [artist, album]
            fetch_cp.stats_files_updated = stats.files_updated
            fetch_cp.stats_files_no_match = stats.files_no_match
            fetch_cp.stats_fields_updated = stats.fields_updated
            _albums_since_last_write += 1
            now = _ckp_time.time()
            should_save = (
                _albums_since_last_write >= _CHECKPOINT_EVERY_N_ALBUMS
                or (now - _last_checkpoint_write) >= _CHECKPOINT_EVERY_N_SECONDS
            )
            if should_save and _save_fetch_checkpoint is not None:
                try:
                    _save_fetch_checkpoint(fetch_cp)
                    _last_checkpoint_write = now
                    _albums_since_last_write = 0
                except Exception:
                    # Per-album checkpoint failures are non-fatal — we
                    # try again on the next album. Worst case: resume
                    # info is slightly stale, but the DB itself is fine.
                    pass

    # Run completed normally — mark the checkpoint as done and clean up.
    # We don't unlink the file immediately because the user may want to
    # see "where did the last run leave off" via debug commands; the
    # `phase = complete` marker means resume won't try to re-use it.
    if fetch_cp is not None:
        try:
            from checkpoint import (
                save_fetch_checkpoint, clear_fetch_checkpoint,
            )
            fetch_cp.phase = "complete"
            save_fetch_checkpoint(fetch_cp)
            # Now delete it — the run is complete and a stale file
            # here just confuses the next resume check.
            clear_fetch_checkpoint()
        except Exception:
            pass

    # Final summary including detection routing
    emit("info", f"detection: routed {stats_routed_to_va:,} albums to "
                 f"'Various Artists' query (compilations); skipped "
                 f"{stats_skipped_unofficial:,} as unofficial/bootleg")
    emit("info", "done.")
    return stats


# =============================================================================
# BACKWARDS COMPAT — old function name kept for cmd_fetch_metadata until
# rewired. Wraps the multi-provider engine with only MusicBrainz enabled.
# =============================================================================

def fill_missing_from_musicbrainz(
    db: Any,
    *,
    target_columns: list[str],
    fetch_covers: bool = True,
    only_missing: bool = True,
    ui: Any = None,
    progress_cb: Any = None,
) -> FillStats:
    """Legacy single-provider entry point. Prefer fill_missing_metadata."""
    mb = make_provider("musicbrainz")
    if not mb:
        s = FillStats()
        s.errors.append("MusicBrainz provider unavailable")
        return s
    return fill_missing_metadata(
        db,
        providers=[mb],
        target_columns=target_columns,
        only_missing=only_missing,
        fetch_covers=fetch_covers,
        ui=ui,
    )
