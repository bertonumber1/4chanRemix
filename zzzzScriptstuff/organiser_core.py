"""
organiser_core.py
=================

The rules that decide where a file goes in the organised tree.

Two top-level layouts depending on album type:

  SOLO album / single (one main artist):
    <dest_root>/<quality>/<catno> - <year> - <artist>/album/<NN> - <title>.<ext>
    <dest_root>/<quality>/<catno> - <year> - <artist>/single/<NN> - <title>.<ext>

  MIX / COMPILATION (many artists, or marked as such):
    <dest_root>/<quality>/<catno> - <year> - <mix_name>/mix/<NN> - <artist> - <title>.<ext>

  catno is catalog_number from tags/DB, falling back to discogs_release_id.
  Omitted from the folder name if neither is populated.

The album-type decision is per *source folder*, not per file — you have to
look at all tracks together to know if it's a compilation. The importer
gathers a folder's metadata, calls `decide_album_type`, then calls
`build_destination_path` once per file with the decided type.

This module is filesystem-agnostic and has no side effects. Pure functions
in, paths out. That makes it cheap to unit-test and easy to reason about.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any
from label_normaliser import normalise_label


# =============================================================================
# NAME SANITISING
# =============================================================================

def clean_name(name: str | None, *, fallback: str = "Unknown") -> str:
    """
    Tidy up artist / label / album names before they become folder names.

    - Collapses multi-artist strings to "A & B" (dedups, normalises separators).
    - Strips surrounding whitespace.
    - Returns `fallback` if the input is empty/None.
    """
    if not name:
        return fallback
    s = str(name).strip()
    if not s:
        return fallback

    # Split on common multi-artist separators, dedup, re-join with " & ".
    # We deliberately don't split "and" — too many band names contain it
    # (Earth, Wind & Fire; Florence and the Machine; etc).
    #
    # Crucially, we REQUIRE whitespace around '&' so we don't shred names
    # like "R&S Records", "AT&T", "Salt-N-Pepa". Same for comma — we
    # require it to be followed by space, so "AC,DC" doesn't split (it
    # wouldn't appear with that comma anyway, but be safe).
    parts = re.split(r"\s*;\s*|\s*\|\s*|\s+&\s+|,\s+", s)
    seen: list[str] = []
    seen_lower: set[str] = set()
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p.lower() in seen_lower:
            continue
        seen.append(p)
        seen_lower.add(p.lower())

    return " & ".join(seen) if seen else fallback


def sanitise_path_part(
    part: str | None,
    *,
    illegal_chars: str = '<>:"/\\|?*\x00',
    max_length: int = 200,
    fallback: str = "Unknown",
) -> str:
    """
    Make a string safe to use as a single path component.

    - Replaces illegal characters with underscore (one '_' per illegal char).
    - Strips trailing dots and spaces (Windows is hostile to those; Linux
      tolerates them but they look bad and cause "is this a hidden file?"
      bugs on copy to other systems).
    - Truncates to `max_length` bytes-ish (we measure characters, which
      undercounts multibyte UTF-8 but stays well under ext4's 255-byte limit
      unless every char is 4 bytes, which doesn't happen in practice).
    - Returns `fallback` if the result is empty.
    """
    if not part:
        return fallback

    s = clean_name(part, fallback=fallback)

    # Replace illegal chars with '_'.
    if illegal_chars:
        s = "".join("_" if c in illegal_chars else c for c in s)

    # Collapse runs of underscores or whitespace.
    s = re.sub(r"_{2,}", "_", s)
    s = re.sub(r"\s{2,}", " ", s)

    # Strip leading/trailing dots, spaces, underscores.
    s = s.strip(" ._")

    # Length cap.
    if len(s) > max_length:
        s = s[:max_length].rstrip(" ._")

    return s or fallback


# =============================================================================
# YEAR / TRACK NORMALISATION
# =============================================================================

def normalise_year(value: str | None) -> str:
    """Pull a 4-digit year out of a date-like tag value. Returns '' if none."""
    if not value:
        return ""
    m = re.search(r"\b(\d{4})\b", str(value))
    if m:
        y = int(m.group(1))
        # sanity check: music is mostly 1900-current+1
        if 1900 <= y <= 2100:
            return str(y)
    return ""


def normalise_track(value: str | None) -> str:
    """
    Track number tag can be '5', '5/12', '05', or '01.05' (CD/track on multi-disc).
    Return a two-digit zero-padded leading-track string, or '' if unparseable.
    """
    if not value:
        return ""
    s = str(value).split("/")[0].split(".")[-1].strip()
    if s.isdigit():
        return s.zfill(2)
    # Sometimes embedded inside text: "Track 5"
    m = re.search(r"\d+", s)
    if m:
        return m.group(0).zfill(2)
    return ""


# =============================================================================
# ALBUM-TYPE DETECTION
# =============================================================================

def decide_album_type(
    records: list[dict[str, Any]],
    *,
    diversity_threshold: float = 0.5,
    various_artists_tags: list[str] | None = None,
    mix_keywords: list[str] | None = None,
) -> str:
    """
    Classify a group of files (typically one source folder) as:
      'solo'    — single-artist album
      'mix'     — compilation, mix, or various-artists release
      'unknown' — empty input

    `records` is a list of metadata dicts (the output of metadata.extract_metadata).

    Decision order:
      1. If albumartist looks like "Various Artists" -> mix.
      2. If album name contains mix-keywords ("DJ Foo presents...", "Essential Mix...") -> mix.
      3. If the share of tracks whose `artist` differs from the modal
         artist exceeds `diversity_threshold` -> mix.
      4. Otherwise -> solo.
    """
    if not records:
        return "unknown"

    various_artists_tags = [t.lower() for t in (various_artists_tags or [])]
    mix_keywords = [k.lower() for k in (mix_keywords or [])]

    # --- check 1: explicit Various Artists tag ---------------------------
    aa_values = [
        (r.get("albumartist") or "").strip().lower()
        for r in records if r.get("albumartist")
    ]
    if aa_values and any(av in various_artists_tags for av in aa_values):
        return "mix"

    # --- check 2: mix keywords in the album name -------------------------
    album_values = {
        (r.get("album") or "").strip().lower()
        for r in records if r.get("album")
    }
    for album in album_values:
        for kw in mix_keywords:
            if kw in album:
                return "mix"

    # --- check 3: track-artist diversity ---------------------------------
    track_artists = [
        (r.get("artist") or "").strip().lower()
        for r in records if r.get("artist")
    ]
    if track_artists:
        counts = Counter(track_artists)
        most_common_count = counts.most_common(1)[0][1]
        diff_share = 1.0 - (most_common_count / len(track_artists))
        if diff_share >= diversity_threshold:
            return "mix"

    return "solo"


# =============================================================================
# PRETTY FILENAME BUILDER
# =============================================================================
#
# The "fix filenames" menu and the do-everything organise both want
# filenames in a consistent shape. The format the user asked for:
#
#   NN - Artist - Title - [freeform middle] - Album - Year (rip by NAME).ext
#
# Where the freeform middle holds things like "(Original Mix)",
# "feat. X", "VIP Mix", "Remastered 2014" — anything that's
# ATTACHED TO THE TRACK rather than the album.
#
# Source of the freeform middle: we extract parenthesised/bracketed
# trailing fragments from the title field. So a title like
#   "Helicopter Tune (J Majik VIP Remix)"
# becomes
#   title="Helicopter Tune"  freeform="J Majik VIP Remix"
# and the resulting filename has both pieces.
#
# Design notes:
#   - We avoid Unicode prettification in filenames (em dashes, smart
#     quotes, kaomojis). Soulseek users search with ASCII; pretty
#     Unicode in filenames hurts searchability for everyone else.
#   - The "rip by NAME" suffix is optional and controlled by the user;
#     it sits at the very end, before the extension.
#   - All parts go through `sanitise_path_part` so the filesystem is
#     happy (Windows-hostile characters get replaced).

# Pattern for fragments at the END of a title that describe THIS specific
# rendition: "(Original Mix)", "[VIP Remix]", "(feat. X)", "(2014 Remaster)".
# We extract these out so the title stays clean and the rendition goes
# into the freeform middle slot of the filename.
_TRAILING_PAREN_RE = re.compile(
    r"\s*[\(\[]\s*"
    r"("                                            # capture content
    r"(?:"
    r"(?:original|extended|radio|club|dub|vip|"
    r"remastered?|remaster|remix|edit|mix|version|"
    r"feat|featuring|ft|with|vs|live|acoustic|"
    r"instrumental|acapella|bonus|cd|disc|"
    r"\d{4})"                                       # year-ish remaster tags
    r"[^\)\]]*"                                     # anything after the keyword
    r"|"
    r"[^\)\]]*(?:mix|remix|edit|version|feat\.?|ft\.?|remaster)[^\)\]]*"
    r")"
    r")"
    r"\s*[\)\]]\s*$",
    re.IGNORECASE,
)


def extract_freeform_middle(title: str) -> tuple[str, str]:
    """
    Split a title into (clean_title, freeform). If the title ends with a
    rendition-describing parenthetical or bracket, pull it out; otherwise
    return (title, "").

    Examples:
        "Helicopter Tune (J Majik VIP Remix)" -> ("Helicopter Tune", "J Majik VIP Remix")
        "Music for Strings (Original Mix)"    -> ("Music for Strings", "Original Mix")
        "Idioteque (2014 Remaster)"           -> ("Idioteque", "2014 Remaster")
        "Strawberry Letter 23"                -> ("Strawberry Letter 23", "")
        "Quoth (Tea & Cucumber Mix)"          -> ("Quoth", "Tea & Cucumber Mix")

    Strategy: ONLY extract when the parenthetical contains keywords
    suggesting it describes a version/mix/feature. Don't strip arbitrary
    trailing parens — "(Live at Wembley)" should come out, but a title
    that just happens to end in parens shouldn't lose them.
    """
    if not title:
        return "", ""
    m = _TRAILING_PAREN_RE.search(title)
    if not m:
        return title.strip(), ""
    clean = title[:m.start()].strip()
    freeform = m.group(1).strip()
    # If stripping leaves nothing, keep the original (rare case where
    # the WHOLE title was parenthesised).
    if not clean:
        return title.strip(), ""
    return clean, freeform


def build_pretty_filename(
    record: dict[str, Any],
    *,
    illegal_chars: str = '<>:"/\\|?*\x00',
    max_length: int = 200,
    rip_by: str = "",
    star_prefix: str = "✰",
) -> str | None:
    """
    Build a filename in the canonical pretty format. Returns None if
    the record lacks the minimum required tags (artist + title); the
    caller should skip files we can't reliably rename.

    Format:

        ✰ [NN - Artist - Title - (freeform) - Album - Year - CODEC] Ripped By NAME.ext

    Example:

        ✰ [01 - Nujabes - 羽 Feather - Modal Soul - 2005 - FLAC] Ripped By anon.flac

    Where:
      ✰           : decorative star prefix. Configurable via `star_prefix`;
                    pass "" to omit it. Soulseek shares are UTF-8 so this
                    Unicode glyph survives the network and the host OS's
                    filesystem (ext4/NTFS/APFS all accept it).
      NN          : track number, zero-padded to 2 digits (omitted if missing)
      Artist      : track artist (NOT albumartist — for VA comps you want
                    the per-track credit, not "Various Artists")
      Title       : the song title. May contain Japanese / Korean / Cyrillic
                    / accented Latin / anything Unicode — these are preserved
                    intact. Any "(Original Mix)"-style fragment gets pulled
                    out into the `freeform` slot.
      freeform    : extracted rendition info (mix/remix/feat/remaster/etc).
                    Wrapped in parens. Omitted if the title had no such
                    fragment.
      Album       : album name (omitted if missing — common for singles)
      Year        : 4-digit year (omitted if missing)
      CODEC       : audio codec from `record['codec']`, uppercased
                    (flac → FLAC, mp3 → MP3). Omitted if missing.
      Ripped By NAME : Soulseek-style attribution. Omitted if rip_by is "".

    Character handling:
      The default `illegal_chars` is the Windows-illegal set: < > : " / \\ | ? *
      plus null. EVERYTHING else passes through — including Japanese
      (羽 Feather), Korean (방탄소년단), Cyrillic (Ленинград), accented
      Latin (Björk, Sigur Rós), em dashes, smart quotes, all of it. This
      matches what Soulseek itself supports (any UTF-8) restricted to what
      the host filesystem allows.

      Cross-platform note: if a Windows user downloads from you, their
      filesystem (NTFS) handles all the above fine. APFS (macOS) likewise.
      Only the original 9-char illegal set is genuinely problematic.

    Extension comes from the record's `path` (preserved). The function
    returns ONLY the filename, no directory.
    """
    artist = (record.get("artist") or "").strip()
    title_raw = (record.get("title") or "").strip()
    if not artist or not title_raw:
        return None

    # Track number — normalise to "NN" form. Skip if invalid/missing.
    track = normalise_track(record.get("track_number"))
    if track and len(track) < 2:
        track = track.zfill(2)

    # Split title into (clean, freeform). The freeform piece gets its
    # own slot in the bracket so it's visible at a glance.
    clean_title, freeform = extract_freeform_middle(title_raw)

    album = (record.get("album") or "").strip()
    year = normalise_year(record.get("year") or record.get("date"))

    # Codec — uppercased. Pulled from the DB column, with a fallback to
    # parsing the source extension if the column is empty (older imports
    # may have NULL codec). "FLAC" / "MP3" / "M4A" etc.
    codec = (record.get("codec") or "").strip().upper()
    if not codec:
        src_ext = Path(record.get("path") or "").suffix.lstrip(".").upper()
        codec = src_ext or ""

    def s(part: str, fallback: str = "") -> str:
        return sanitise_path_part(
            part, illegal_chars=illegal_chars,
            max_length=max_length, fallback=fallback,
        )

    # Build the bracket contents.
    #
    # Format: `[ NN. Artist - Title - (freeform) - Album - Year - CODEC]`
    #   - Space after the opening `[` (visual breathing room).
    #   - Track number gets its OWN segment, separated by `.` (period)
    #     from the artist. This visually emphasises track ordering —
    #     "01. Aphex Twin..." reads like a numbered list rather than
    #     just another dash-joined field.
    #   - Everything after the track number is dash-joined as before.
    #   - If track number is missing, the leading space is omitted to
    #     avoid an orphan empty slot at the start.
    rest_parts: list[str] = []
    rest_parts.append(s(artist, "Unknown Artist"))
    rest_parts.append(s(clean_title, "Untitled"))
    if freeform:
        rest_parts.append(f"({s(freeform, '')})")
    if album:
        rest_parts.append(s(album, ""))
    if year:
        rest_parts.append(year)
    if codec:
        rest_parts.append(codec)

    rest = " - ".join(p for p in rest_parts if p)

    if track:
        # `[ 01. Artist - Title - ...]`  — note the space after `[`
        # and the period after the track number.
        bracket = f"[ {track}. {rest}]"
    else:
        # No track number — keep it compact: `[Artist - Title - ...]`
        bracket = f"[{rest}]"

    # Assemble the full stem: optional star prefix, then bracket, then
    # optional "Ripped By NAME" suffix.
    stem_pieces: list[str] = []
    if star_prefix:
        stem_pieces.append(star_prefix)
    stem_pieces.append(bracket)
    if rip_by:
        # "Ripped By NAME" — capitalised, no parens, separated by a space.
        # Sanitised the same way as the other parts so it can't contain
        # path-hostile characters (someone with a `/` in their username
        # would be a problem otherwise).
        rip_by_clean = s(rip_by, "")
        if rip_by_clean:
            stem_pieces.append(f"Ripped By {rip_by_clean}")

    stem = " ".join(stem_pieces)

    # Final length cap on the full stem. We sanitise but with NO further
    # illegal-char replacement (the parts went through individually) —
    # this pass just enforces length and strips trailing whitespace/dots.
    # The brackets and star MUST survive, so we pass illegal_chars="".
    stem = sanitise_path_part(
        stem, illegal_chars="", max_length=max_length, fallback="Untitled Track"
    )

    # Extension: preserve case from source so we don't accidentally
    # convert .FLAC → .flac (some systems care).
    src_path = Path(record.get("path") or "")
    ext = src_path.suffix or ".flac"
    return f"{stem}{ext}"


# =============================================================================
# SELF-RELEASE DETECTION + ALBUM-LEVEL LABEL DECISION
# =============================================================================
#
# The organiser's folder layout puts the label name in the path:
#     <quality>/<label>/<artist> - <album> - <year>/
# So per-track label disagreement → album splits across folders. The
# decide_album_label() function below picks ONE label per album so this
# can't happen, and detect_self_release() routes label-less releases
# to a dedicated tree instead of the visually-ugly "Unknown Label/"
# bucket.

# Names that strongly indicate "no label / self-released" rather than
# "we just don't know the label." MusicBrainz uses "[no label]" and
# similar; Discogs uses "Not On Label" and the cat-num "none";
# Bandcamp / SoundCloud often surface "Self-Released" verbatim.
_SELF_RELEASE_LABEL_VALUES = {
    "", "unknown label", "no label", "[no label]", "(no label)",
    "not on label", "self-released", "self released", "self release",
    "selfreleased", "independent", "unsigned", "n/a", "none", "(none)",
    "diy",
}

# Tags that explicitly mark a self-release as such (release-status,
# release-group-type, etc.). When we see these we don't need other
# evidence.
_SELF_RELEASE_STATUS_MARKERS = {
    "self-released", "selfreleased", "independent", "unsigned",
}


def _normalise_for_compare(value: str) -> str:
    """Lowercased, whitespace-normalised, accent-stripped string for
    comparison. Mirrors what `normalise_for_match` in metadata_providers
    does but kept local to avoid a circular import."""
    import unicodedata
    if not value:
        return ""
    s = unicodedata.normalize("NFKD", value)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.lower().split())


def detect_self_release(
    records: list[dict[str, Any]],
    *,
    album_label: str = "",
) -> tuple[bool, str]:
    """
    Decide whether an album is a self-release (artist released their
    own material with no label) vs an album with a label we just
    haven't found yet.

    Returns (is_self_release, reason).

    Signals checked, in order of strength:
      1. ANY track has a status/release_type marker explicitly saying
         "self-released" or "independent" → yes, with that reason
      2. The album's label value (passed in or derived) matches one of
         the well-known self-release strings ("[no label]",
         "Not On Label", "Self-Released", etc.) → yes
      3. Label name (case-insensitive, accent-stripped) matches the
         primary artist name → yes (artist's "vanity" label is usually
         a self-release)
      4. Label is empty/NULL across ALL tracks AND we have a
         provider_searched_at marker indicating providers were queried
         (i.e. this isn't just an un-fetched album) → yes, weak
      5. Otherwise → no (probably just an unlabeled album we haven't
         identified yet)

    The reason string is for logging — surfaces WHY we routed this
    album to Self-Released/ so the user can verify when they see it.
    """
    if not records:
        return False, ""

    # Signal 1: explicit marker in release_status / release_type
    for r in records:
        for col in ("release_status", "release_type", "release_group_type"):
            val = (r.get(col) or "").strip().lower()
            if val in _SELF_RELEASE_STATUS_MARKERS:
                return True, f"{col}={val!r}"

    # Build a canonical label value: prefer the album_label kwarg
    # (which the album-folder decider passes in), fall back to majority
    # of per-track labels.
    label = (album_label or "").strip()
    if not label:
        from collections import Counter
        counts: Counter[str] = Counter()
        for r in records:
            l = (r.get("label") or "").strip()
            if l:
                counts[l] += 1
        if counts:
            label = counts.most_common(1)[0][0]

    label_norm = _normalise_for_compare(label)

    # Signal 2: label matches a known self-release string. We require
    # a NON-EMPTY normalised label to fire this — an empty label is
    # "we don't know yet," not "self-released." The empty case is
    # caught in signal 4 below only if we have other context.
    if label_norm and label_norm in _SELF_RELEASE_LABEL_VALUES:
        return True, f"label={label!r} is a self-release marker"

    # Signal 3: label name matches the artist name
    if label_norm:
        # Use the album-artist if present, else the most common
        # per-track artist. For VA comps neither match so this signal
        # quietly fails (correct — VA comps with one artist's name as
        # the label are vanishingly rare).
        from collections import Counter
        artist_counts: Counter[str] = Counter()
        for r in records:
            for col in ("albumartist", "primary_artist", "artist"):
                v = (r.get(col) or "").strip()
                if v:
                    artist_counts[_normalise_for_compare(v)] += 1
                    break
        if artist_counts:
            top_artist = artist_counts.most_common(1)[0][0]
            if top_artist and top_artist == label_norm:
                return True, f"label={label!r} matches artist name"

    # Signal 4 was originally going to look at provider_searched_at but
    # this column doesn't yet exist on every row. For now: if EVERY
    # track has a totally empty label, treat as "unknown not self-
    # released" — the inline-lookup code in importer.py will try to
    # resolve it before falling back here.
    return False, ""


def decide_album_label(
    records: list[dict[str, Any]],
    *,
    unknown_label_fallback: str = "Unknown Label",
) -> tuple[str, str]:
    """
    Given all tracks of one album (already grouped by the caller),
    decide what ONE label name to use for the album's folder. The
    organiser uses this so every track of one album lands in the same
    folder regardless of per-track label disagreement.

    Returns (label, reason).

    Algorithm:
      - Tally non-empty non-placeholder labels across the tracks.
      - 0 distinct values → use `unknown_label_fallback`, reason="all empty"
      - 1 distinct value → use it, reason="unanimous"
      - 2+ distinct values → majority wins. Ties broken alphabetically
        for determinism. Reason includes the counts so the log shows
        what was overridden.

    Note: this is the SAME logic as detection.reconcile_album_level_fields
    in coalesce mode, but isolated here so the path-building step doesn't
    have to import from detection.py (which would create a cycle).
    """
    from collections import Counter
    # Lazy import — detection.py doesn't depend on organiser_core, so
    # this isn't a cycle. is_unknown_tag covers a broader set than
    # _SELF_RELEASE_LABEL_VALUES (e.g. "Unknown" without "Label").
    try:
        from detection import is_unknown_tag
    except ImportError:
        def is_unknown_tag(v):  # type: ignore
            return (v or "").strip().lower() in _SELF_RELEASE_LABEL_VALUES

    tally: Counter[str] = Counter()
    for r in records:
        raw = (r.get("label") or "").strip()
        if not raw:
            continue
        if is_unknown_tag(raw) or raw.lower() in _SELF_RELEASE_LABEL_VALUES:
            continue
        tally[raw] += 1

    if not tally:
        return unknown_label_fallback, "all empty/placeholder"

    if len(tally) == 1:
        (the_label, _count), = tally.items()
        return the_label, "unanimous"

    # Multiple distinct — majority wins, alpha tiebreak.
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    top_label, top_count = ranked[0]
    parts = [f"{v!r}({n})" for v, n in ranked]
    return top_label, "majority: " + " vs ".join(parts) + f" → {top_label!r}"


# =============================================================================
# PATH BUILDING
# =============================================================================

def build_destination_path(
    record: dict[str, Any],
    album_type: str,
    *,
    destination_root: str | Path,
    organise_cfg: dict[str, Any] | None = None,
    self_release_info: dict[str, Any] | None = None,
) -> Path:
    """
    Compute where `record` should live in the organised tree.

    `record` is a metadata dict (output of metadata.extract_metadata).
    `album_type` is 'solo' or 'mix' (typically from decide_album_type).
    `self_release_info`: optional dict from the importer carrying:
       - 'is_self_release': bool
       - 'is_single':       bool (1 track in the album group)
       - 'forced_label':    str   (the album-level label to use,
                                    overriding the per-track value;
                                    set by decide_album_label so the
                                    whole album lands in one folder)
    When None, the function falls back to using `record['label']`
    directly and skipping the self-release branch.

    Returns a Path. Does NOT touch the filesystem.

    Layout (when not broken):
      Standard labeled album:
        <catno> - <year> - <artist>/album/NN - <title>.<ext>
      Single / EP:
        <catno> - <year> - <artist>/single/NN - <title>.<ext>
      VA / mix compilation:
        <catno> - <year> - <mix_name>/mix/NN - <artist> - <title>.<ext>
      Self-released:
        Self-Released/<catno> - <year> - <artist>/album|single/NN - <title>.<ext>
      Broken:
        Broken/<original-basename>  (dumped flat)

      catno is catalog_number if present, else discogs_release_id; omitted if
      neither is available. No quality subdirectory — library is FLAC-only.
    """
    cfg = organise_cfg or {}
    unknown_label = cfg.get("unknown_label", "Unknown Label")
    unknown_artist = cfg.get("unknown_artist", "Unknown Artist")
    unknown_album = cfg.get("unknown_album", "Unknown Album")
    illegal = cfg.get("illegal_path_chars", '<>:"/\\|?*\x00')
    max_len = cfg.get("max_component_length", 200)

    def s(value: str | None, fallback: str) -> str:
        return sanitise_path_part(
            value, illegal_chars=illegal, max_length=max_len, fallback=fallback,
        )

    dest_root = Path(destination_root).expanduser()
    src_path = Path(record["path"])
    ext = src_path.suffix.lower() or ""

    # --- BROKEN files just go to a flat dump folder ----------------------
    if record.get("status") == "broken":
        return dest_root / "Broken" / src_path.name

    # --- shared fields ---------------------------------------------------
    # If the importer pre-decided an album-level label for cohesion,
    # use THAT instead of the per-track value. Otherwise fall back to
    # the per-track label.
    sri = self_release_info or {}
    is_self_release = bool(sri.get("is_self_release", False))
    is_single = bool(sri.get("is_single", False))
    forced_label = sri.get("forced_label", "")

    label_raw = forced_label if forced_label else (record.get("label") or "")
    label = s(normalise_label(label_raw), unknown_label)
    album = s(record.get("album"), unknown_album)
    year = normalise_year(record.get("year") or record.get("date"))
    track = normalise_track(record.get("track_number"))

    title = s(
        record.get("title"),
        fallback=sanitise_path_part(src_path.stem, illegal_chars=illegal,
                                    max_length=max_len, fallback="Unknown Title"),
    )

    # Catalogue number — prefer catalog_number, fall back to discogs_release_id.
    catno_raw = (record.get("catalog_number") or "").strip()
    if not catno_raw:
        catno_raw = str(record.get("discogs_release_id") or "").strip()
    catno = s(catno_raw, "")

    # Release type subfolder: album / mix / single
    if album_type == "mix":
        type_folder = "mix"
    elif (is_single or
          (record.get("release_type") or "").lower() in ("single", "ep")):
        type_folder = "single"
    else:
        type_folder = "album"

    if album_type == "mix":
        # Mix layout: catno - year - mix_name / mix / NN - Artist - Title.ext
        track_artist = s(record.get("artist"), unknown_artist)

        owner_parts = [p for p in [catno, year, album] if p]
        owner_folder = s(
            " - ".join(owner_parts) if owner_parts else album,
            unknown_album,
        )

        if track:
            filename = f"{track} - {track_artist} - {title}{ext}"
        else:
            filename = f"{track_artist} - {title}{ext}"

    else:
        # Solo layout: catno - year - artist / album|single / NN - Title.ext
        primary = record.get("primary_artist") or record.get("albumartist") or record.get("artist")
        artist_folder = s(primary, unknown_artist)

        owner_parts = [p for p in [catno, year, artist_folder] if p]
        owner_folder = s(
            " - ".join(owner_parts) if owner_parts else artist_folder,
            unknown_artist,
        )

        if track:
            filename = f"{track} - {title}{ext}"
        else:
            filename = f"{title}{ext}"

    # Also sanitise the filename as a path component (extension preserved).
    name_stem, _, name_ext = filename.rpartition(".")
    if name_stem:
        name_stem = s(name_stem, "Unknown Track")
        filename = f"{name_stem}.{name_ext}" if name_ext else name_stem

    # --- routing ---------------------------------------------------------
    # Standard:  <quality>/<catno - year - artist>/<type>/<filename>
    # Self-rel:  <quality>/Self-Released/<catno - year - artist>/<type>/<filename>
    if is_self_release:
        sr_root = dest_root / "Self-Released"
        primary = record.get("primary_artist") or record.get("albumartist") or record.get("artist")
        artist_folder = s(primary, unknown_artist)
        owner_parts = [p for p in [catno, year, artist_folder] if p]
        sr_owner = s(
            " - ".join(owner_parts) if owner_parts else artist_folder,
            unknown_artist,
        )
        return sr_root / sr_owner / type_folder / filename

    return dest_root / owner_folder / type_folder / filename
