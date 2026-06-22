"""
metadata.py
===========

Extract metadata from an audio file into a flat dict matching the columns
in `database.py`. Uses mutagen.

Why this is a separate module:
- The old code mixed tag extraction with quality classification, skip-list
  logic, and DB caching. Each of those is a different concern. Here we do
  one thing: file in, dict out.
- The dict is keyed to match `database.py`'s WRITABLE_COLUMNS exactly, so
  the importer can do `db.upsert_file(extract(path))` with no transform.

Tag mapping notes:
- ID3 (mp3) uses TPE1, TPE2, TALB etc. Mutagen's EasyID3 normalises some
  of these to lowercase names but not all containers do. We try each
  possible tag name in priority order.
- FLAC / Vorbis comments are already lowercase ("artist", "album", ...).
- MP4/M4A uses 4-char tags like '©ART', '©alb', 'aART'.
- We keep the FULL raw tag dict in `tags_raw` (JSON) so anything we miss
  is recoverable later via json_extract() in SQL.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
    MUTAGEN_AVAILABLE = True
except ImportError:
    MutagenFile = None  # type: ignore
    FLAC = None  # type: ignore
    MP3 = None  # type: ignore
    MUTAGEN_AVAILABLE = False


# Tag-name priority lists. The first key that has a non-empty value wins.
# Each tuple covers a different container: (vorbis/flac, id3, mp4, wma).
TAG_ALIASES: dict[str, list[str]] = {
    "artist":            ["artist", "TPE1", "©ART", "Author"],
    "albumartist":       ["albumartist", "album artist", "TPE2", "aART",
                          "WM/AlbumArtist"],
    "album":             ["album", "TALB", "©alb", "WM/AlbumTitle"],
    "title":             ["title", "TIT2", "©nam", "Title"],
    "track_number":      ["tracknumber", "track", "TRCK", "trkn",
                          "WM/TrackNumber"],
    "disc_number":       ["discnumber", "disc", "TPOS", "disk",
                          "WM/PartOfSet"],
    "year":              ["year", "date", "TDRC", "TYER", "©day",
                          "WM/Year"],
    "date":              ["originaldate", "date", "TDOR", "TDRC"],
    "genre":             ["genre", "TCON", "©gen", "WM/Genre"],
    "label":             ["label", "publisher", "organization", "TPUB",
                          "WM/Publisher", "©pub"],
    "catalog_number":    ["catalognumber", "catalog#", "TXXX:CATALOGNUMBER",
                          "WM/CatalogNo"],
    "isrc":              ["isrc", "TSRC", "TXXX:ISRC"],
    "barcode":           ["barcode", "TXXX:BARCODE"],
    "composer":          ["composer", "TCOM", "©wrt", "WM/Composer"],
    "lyricist":          ["lyricist", "TEXT"],
    "producer":          ["producer", "TXXX:PRODUCER"],
    "comment":           ["comment", "COMM::eng", "COMM", "©cmt",
                          "WM/Comments"],
    "musicbrainz_trackid":        ["musicbrainz_trackid",
                                   "TXXX:MusicBrainz Release Track Id"],
    "musicbrainz_albumid":        ["musicbrainz_albumid",
                                   "TXXX:MusicBrainz Album Id"],
    "musicbrainz_artistid":       ["musicbrainz_artistid",
                                   "TXXX:MusicBrainz Artist Id"],
    "musicbrainz_albumartistid":  ["musicbrainz_albumartistid",
                                   "TXXX:MusicBrainz Album Artist Id"],
    "discogs_release_id":         ["discogs_release_id",
                                   "TXXX:DISCOGS_RELEASE_ID"],
    # DJ / production tags (important for your use case — these are what
    # OneTagger fills in, plus what serato/rekordbox/mixxx read)
    "bpm":                        ["bpm", "TBPM", "tmpo",
                                   "TXXX:BPM", "WM/BeatsPerMinute"],
    "musical_key":                ["initialkey", "initial_key",
                                   "TKEY", "TXXX:INITIALKEY",
                                   "TXXX:initialkey", "TXXX:KEY"],
    "mood":                       ["mood", "TMOO", "TXXX:MOOD"],
    "compilation":                ["compilation", "TCMP", "cpil",
                                   "TXXX:COMPILATION"],
    "replaygain_track_gain":      ["replaygain_track_gain",
                                   "TXXX:REPLAYGAIN_TRACK_GAIN",
                                   "----:com.apple.iTunes:replaygain_track_gain"],
    "replaygain_track_peak":      ["replaygain_track_peak",
                                   "TXXX:REPLAYGAIN_TRACK_PEAK"],
    "replaygain_album_gain":      ["replaygain_album_gain",
                                   "TXXX:REPLAYGAIN_ALBUM_GAIN"],
    "replaygain_album_peak":      ["replaygain_album_peak",
                                   "TXXX:REPLAYGAIN_ALBUM_PEAK"],
    # --- v0.18 Picard-aligned additions ----------------------------------
    # Encoder identity — read these so rip_detection can spot LAME etc.
    "encodedby":                  ["encodedby", "TENC", "©too"],
    "encodersettings":            ["encodersettings", "encoder_settings",
                                   "TSSE", "encoder"],
    "website":                    ["website", "WOAR", "URL"],
    "copyright":                  ["copyright", "TCOP", "cprt"],
    "license":                    ["license", "TXXX:LICENSE"],
    # Picard sort variants — these often exist in well-tagged files
    "albumartistsort":            ["albumartistsort", "TSO2", "soaa"],
    "artistsort":                 ["artistsort", "TSOP", "soar"],
    "albumsort":                  ["albumsort", "TSOA", "soal"],
    "titlesort":                  ["titlesort", "TSOT", "sonm"],
    "composersort":               ["composersort", "TSOC", "soco"],
    "artists":                    ["artists", "TXXX:ARTISTS"],
    # Disc / track totals
    "totaltracks":                ["totaltracks", "TXXX:TOTALTRACKS"],
    "totaldiscs":                 ["totaldiscs", "TXXX:TOTALDISCS"],
    "discsubtitle":               ["discsubtitle", "TSST"],
    # Original date — earliest release in group, set by Picard
    "originaldate":               ["originaldate", "TDOR", "TXXX:originaldate"],
    "originalyear":               ["originalyear", "TXXX:originalyear"],
    "originalalbum":              ["originalalbum", "TOAL"],
    "originalartist":             ["originalartist", "TOPE"],
    "originalfilename":           ["originalfilename", "TOFN"],
    # ASIN (Amazon) — Picard writes this
    "asin":                       ["asin", "TXXX:ASIN"],
    # Recording / work IDs (advanced Picard)
    "musicbrainz_recordingid":    ["musicbrainz_recordingid",
                                   "TXXX:MusicBrainz Track Id",
                                   "UFID:http://musicbrainz.org"],
    "musicbrainz_workid":         ["musicbrainz_workid",
                                   "TXXX:MusicBrainz Work Id"],
    "musicbrainz_releasegroupid": ["musicbrainz_releasegroupid",
                                   "TXXX:MusicBrainz Release Group Id"],
    # Track relationships from Picard "Use track relationships" mode
    "arranger":                   ["arranger", "TXXX:ARRANGER", "TIPL"],
    "conductor":                  ["conductor", "TPE3"],
    "director":                   ["director", "TXXX:DIRECTOR"],
    "djmixer":                    ["djmixer", "TXXX:DJMIXER"],
    "engineer":                   ["engineer", "TXXX:ENGINEER", "TIPL:engineer"],
    "mixer":                      ["mixer", "TXXX:MIXER", "TIPL:mix"],
    "remixer":                    ["remixer", "TPE4"],
    "writer":                     ["writer", "TXXX:Writer"],
    "work":                       ["work", "TXXX:WORK", "©wrk"],
    # Classical
    "showmovement":               ["showmovement", "shwm"],
    "movement":                   ["movement", "MVNM", "©mvn"],
    "movementnumber":             ["movementnumber", "MVIN", "©mvi"],
    "movementtotal":              ["movementtotal", "TXXX:MOVEMENTTOTAL"],
    "subtitle":                   ["subtitle", "TIT3"],
    "lyrics":                     ["lyrics", "USLT", "©lyr"],
    "syncedlyrics":               ["syncedlyrics", "SYLT"],
    "release_type":               ["releasetype", "TXXX:MusicBrainz Album Type"],
    "release_status":             ["releasestatus", "TXXX:MusicBrainz Album Status"],
    "country":                    ["releasecountry", "TXXX:RELEASECOUNTRY"],
    "language":                   ["language", "TLAN"],
    "script":                     ["script", "TXXX:SCRIPT"],
    "media_format":               ["media", "TMED"],
    "acoustid_id":                ["acoustid_id", "TXXX:Acoustid Id"],
    "acoustid_fingerprint":       ["acoustid_fingerprint",
                                   "TXXX:Acoustid Fingerprint"],
}


# Codec name from mutagen object class -> canonical short string.
def _detect_codec(audio) -> str:
    """Return a short codec identifier from a mutagen file object."""
    if audio is None:
        return ""
    cls = audio.__class__.__name__.lower()
    # Direct hits first
    for known in ("flac", "mp3", "mp4", "opus", "vorbis", "wavpack",
                  "monkeysaudio", "wave", "trueaudio", "musepack",
                  "asf", "aiff"):
        if known in cls:
            # Special cases — mutagen's MP4 wraps both .m4a (AAC) and ALAC
            if known == "mp4":
                info = getattr(audio, "info", None)
                codec_attr = getattr(info, "codec", "") if info else ""
                if "alac" in str(codec_attr).lower():
                    return "alac"
                return "m4a"
            if known == "monkeysaudio":
                return "ape"
            if known == "wavpack":
                return "wv"
            return known
    # Fall back to extension if class name didn't match.
    return ""


def _is_lossless(codec: str) -> int | None:
    """0/1, or None if we can't tell."""
    if not codec:
        return None
    lossless = {"flac", "alac", "ape", "wv", "wave", "aiff",
                "trueaudio", "dsf", "dff"}
    lossy = {"mp3", "m4a", "opus", "vorbis", "musepack", "asf"}
    if codec in lossless:
        return 1
    if codec in lossy:
        return 0
    return None


def _first_value(tags: Any, key: str) -> str | None:
    """
    Pull a tag value out regardless of mutagen container. Tags can be:
    - a dict-like mapping key -> list of str
    - a dict-like mapping key -> str
    - a dict-like mapping key -> Frame object (ID3) with .text
    """
    if tags is None:
        return None
    try:
        v = tags.get(key)
    except (TypeError, AttributeError, ValueError, KeyError):
        # VorbisComment raises ValueError for invalid keys; ID3 raises
        # KeyError sometimes. Just return None and try the next alias.
        return None
    if v is None:
        return None

    # ID3 Frame objects expose .text (list)
    if hasattr(v, "text"):
        try:
            txt = v.text
            if isinstance(txt, (list, tuple)) and txt:
                return str(txt[0]).strip() or None
            if txt:
                return str(txt).strip() or None
        except Exception:
            pass

    if isinstance(v, (list, tuple)):
        if not v:
            return None
        # Recurse for nested Frame in list (rare)
        item = v[0]
        if hasattr(item, "text"):
            return _first_value({"_x": item}, "_x")
        return str(item).strip() or None

    return str(v).strip() or None


def _resolve_tag(tags: Any, aliases: list[str]) -> str | None:
    for key in aliases:
        val = _first_value(tags, key)
        if val:
            return val
    return None


def _extract_embedded_art(audio) -> dict[str, Any]:
    """
    Detect embedded album art in a mutagen file object.

    Different containers expose art differently:
      - FLAC:  `audio.pictures` is a list of Picture objects with .data, .mime
      - ID3:   tags with key 'APIC:...' are picture frames; .data, .mime
      - MP4:   tags['covr'] is a list of MP4Cover objects with .imageformat
      - OGG/Vorbis: 'metadata_block_picture' is base64-encoded Picture data

    Returns a dict with keys:
      has_embedded_art:        1/0
      embedded_art_count:      int
      embedded_art_size_bytes: int (total)
      embedded_art_mime:       str (first picture's mime; ", "-joined if multiple)
    """
    out: dict[str, Any] = {
        "has_embedded_art": 0,
        "embedded_art_count": 0,
        "embedded_art_size_bytes": 0,
        "embedded_art_mime": None,
    }
    if audio is None:
        return out

    mimes: list[str] = []
    total_bytes = 0
    count = 0

    # FLAC native pictures
    try:
        pics = getattr(audio, "pictures", None)
        if pics:
            for p in pics:
                count += 1
                data = getattr(p, "data", b"") or b""
                total_bytes += len(data)
                mime = getattr(p, "mime", "") or ""
                if mime:
                    mimes.append(str(mime))
    except Exception:
        pass

    # ID3 APIC frames
    tags = getattr(audio, "tags", None)
    if tags is not None:
        try:
            for key in list(tags.keys()):
                k = str(key)
                # ID3 picture frames
                if k.startswith("APIC"):
                    frame = tags.get(key)
                    if frame is not None:
                        data = getattr(frame, "data", b"") or b""
                        total_bytes += len(data)
                        count += 1
                        mime = getattr(frame, "mime", "") or ""
                        if mime:
                            mimes.append(str(mime))
                # OGG/Vorbis base64 picture block
                elif k.lower() == "metadata_block_picture":
                    val = tags.get(key)
                    if val:
                        # list of base64 strings — just count, don't decode
                        if isinstance(val, (list, tuple)):
                            count += len(val)
                            for s in val:
                                # rough byte estimate from base64 length
                                total_bytes += int(len(str(s)) * 3 / 4)
                        else:
                            count += 1
                            total_bytes += int(len(str(val)) * 3 / 4)
                        mimes.append("image/unknown")
        except Exception:
            pass

        # MP4 cover art
        try:
            try:
                covers = tags.get("covr") if hasattr(tags, "get") else None
            except (TypeError, ValueError, KeyError, AttributeError):
                covers = None
            if covers:
                for cov in covers:
                    count += 1
                    raw = bytes(cov) if hasattr(cov, "__bytes__") else (cov or b"")
                    total_bytes += len(raw)
                    fmt = getattr(cov, "imageformat", None)
                    if fmt == 13:   # MP4Cover.FORMAT_JPEG
                        mimes.append("image/jpeg")
                    elif fmt == 14: # MP4Cover.FORMAT_PNG
                        mimes.append("image/png")
                    else:
                        mimes.append("image/unknown")
        except Exception:
            pass

    if count > 0:
        out["has_embedded_art"] = 1
        out["embedded_art_count"] = count
        out["embedded_art_size_bytes"] = total_bytes
        if mimes:
            # Dedup while preserving order
            seen = []
            for m in mimes:
                if m not in seen:
                    seen.append(m)
            out["embedded_art_mime"] = ", ".join(seen)
    return out


# Folder-art filenames we'll detect (case-insensitive match against directory listing).
FOLDER_ART_NAMES = (
    "cover", "folder", "album", "front", "albumart", "albumartsmall",
    "thumb",
)
FOLDER_ART_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def _detect_folder_art(audio_path: Path) -> dict[str, Any]:
    """
    Look for a cover.jpg / folder.png / front.jpeg sibling next to the
    audio file. Returns:
      has_folder_art:  1/0
      folder_art_path: str or None
    """
    out: dict[str, Any] = {"has_folder_art": 0, "folder_art_path": None}
    try:
        parent = audio_path.parent
        if not parent.is_dir():
            return out
        # Cheap directory scan — folders usually have <20 files, this is fine.
        candidates = []
        for entry in parent.iterdir():
            if not entry.is_file():
                continue
            name = entry.stem.lower()
            ext = entry.suffix.lower()
            if ext not in FOLDER_ART_EXTENSIONS:
                continue
            # Score: exact match in our preferred names wins, then prefix match.
            score = 0
            for i, want in enumerate(FOLDER_ART_NAMES):
                if name == want:
                    score = 100 - i
                    break
                elif name.startswith(want):
                    score = max(score, 50 - i)
            if score > 0:
                candidates.append((score, entry))
        if candidates:
            candidates.sort(reverse=True)
            out["has_folder_art"] = 1
            out["folder_art_path"] = str(candidates[0][1])
    except OSError:
        pass
    return out



def _extract_primary_artist(artist_string: str | None) -> str | None:
    """
    "Artist1 ft. Artist2 & Artist3" -> "Artist1".

    Used purely for folder naming on solo albums — the DB still stores the
    full `artist` and `albumartist` strings. Order of separators matters:
    longest pattern first so 'featuring' isn't eaten by 'feat'.
    """
    if not artist_string:
        return None
    s = str(artist_string).strip()
    if not s:
        return None

    # Build the pattern lazily — we can't import re at module level only
    # because keeping this fast-pathable for tight loops matters; the
    # cost of `import re` is negligible after first call.
    import re
    separators = [
        r"\s+featuring\s+",
        r"\s+presents\s+",
        r"\s+feat\.?\s+",
        r"\s+ft\.?\s+",
        r"\s+with\s+",
        r"\s+vs\.?\s+",
        r"\s+x\s+",                # "A x B"
        r"\s*&\s*",
        r"\s*;\s*",
        r"\s*\|\s*",
        r"\s*,\s*",
    ]
    for sep in separators:
        m = re.search(sep, s, flags=re.IGNORECASE)
        if m:
            head = s[: m.start()].strip()
            if head:
                return head
    return s


def extract_metadata(path: str | Path, source_root: str | None = None) -> dict[str, Any]:
    """
    Read tags + audio properties from `path`, return a dict ready to
    pass to `Database.upsert_file`.

    Always returns at least: path, size_bytes, mtime, codec, status.
    On read failure, status is 'broken' and tags will be empty.
    """
    p = Path(path)
    abs_path = str(p.resolve())

    out: dict[str, Any] = {"path": abs_path}

    # --- filesystem stats (cheap, always work) ---------------------------
    try:
        st = p.stat()
        out["size_bytes"] = st.st_size
        out["mtime"] = st.st_mtime
    except OSError:
        out["size_bytes"] = None
        out["mtime"] = None

    if source_root:
        out["source_root"] = str(source_root)

    # --- mutagen ---------------------------------------------------------
    if not MUTAGEN_AVAILABLE:
        out["status"] = "indexed"
        out["codec"] = p.suffix.lower().lstrip(".")
        return out

    try:
        audio = MutagenFile(str(p))
    except Exception as e:
        out["status"] = "broken"
        out["comment"] = f"mutagen error: {e}"
        out["codec"] = p.suffix.lower().lstrip(".")
        return out

    if audio is None:
        out["status"] = "broken"
        out["comment"] = "mutagen returned None (unsupported or corrupt)"
        out["codec"] = p.suffix.lower().lstrip(".")
        return out

    # --- audio info ------------------------------------------------------
    info = getattr(audio, "info", None)
    if info is not None:
        out["duration_seconds"] = getattr(info, "length", None)
        out["bitrate"] = getattr(info, "bitrate", None)
        out["sample_rate"] = getattr(info, "sample_rate", None)
        out["channels"] = getattr(info, "channels", None)
        # bit_depth lives on FLAC/WAV but not MP3.
        for attr in ("bits_per_sample", "bits_per_raw_sample", "bit_depth"):
            v = getattr(info, attr, None)
            if v:
                out["bit_depth"] = v
                break

    codec = _detect_codec(audio) or p.suffix.lower().lstrip(".")
    out["codec"] = codec
    out["lossless"] = _is_lossless(codec)

    # --- tags ------------------------------------------------------------
    tags = audio.tags
    for col, aliases in TAG_ALIASES.items():
        val = _resolve_tag(tags, aliases)
        if val is not None:
            out[col] = val

    # Derived: primary artist (folder-name use only)
    out["primary_artist"] = _extract_primary_artist(
        out.get("albumartist") or out.get("artist")
    )

    # --- embedded album art ---------------------------------------------
    art = _extract_embedded_art(audio)
    out.update(art)

    # --- folder-level album art (cover.jpg etc) -------------------------
    folder_art = _detect_folder_art(p)
    out.update(folder_art)

    # --- raw tag dump (catch-all) ----------------------------------------
    if tags is not None:
        raw: dict[str, Any] = {}
        try:
            for k in list(tags.keys()):
                ks = str(k)
                # Skip the binary picture frames — they'd bloat tags_raw
                # by megabytes per row. Image data lives in its own columns.
                if ks.startswith("APIC") or ks.lower() == "metadata_block_picture" or ks == "covr":
                    continue
                v = _first_value(tags, k)
                if v is not None:
                    raw[ks] = v
        except Exception:
            pass
        if raw:
            out["tags_raw"] = raw

    # --- rip software detection (v0.18) -----------------------------------
    # Identify what software produced this rip (EAC, XLD, LAME, dBpoweramp,
    # etc) from the tags we just extracted plus any .log file in the
    # folder. Fast — adds maybe 1ms per file. The DB columns are filled
    # if we recognise a signature; otherwise they stay NULL.
    try:
        from rip_detection import detect_rip_software, detection_to_dict
        # Build a tag dict in the shape detect_rip_software wants
        rip_input = {
            "encodedby":      out.get("encodedby") or "",
            "encoder":        "",
            "encodersettings": out.get("encodersettings") or "",
            "comment":        out.get("comment") or "",
            "vendor":         "",
        }
        # FLAC/Vorbis vendor string lives on tags.vendor
        if hasattr(tags, "vendor"):
            try:
                rip_input["vendor"] = tags.vendor or ""
            except Exception:
                pass
        rip = detect_rip_software(path, existing_tags=rip_input)
        if rip.software:
            out.update(detection_to_dict(rip))
    except Exception:
        # Rip detection is best-effort — never fail extract_metadata for it
        pass

    out["status"] = "indexed"
    return out
