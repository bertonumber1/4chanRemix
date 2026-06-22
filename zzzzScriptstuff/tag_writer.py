"""
tag_writer.py
=============

Write tags BACK into audio files using mutagen. This is the "embed in
file, not just DB" side of the metadata pipeline — what you want when
you share via Soulseek and the receiving end reads the file's embedded
tags, not your local DB.

Format coverage:
  - FLAC      → Vorbis Comments (in metadata blocks)
  - MP3       → ID3v2.4 (TXXX frames for unmapped fields)
  - OGG/Opus  → Vorbis Comments
  - M4A/AAC   → MP4 atoms (limited mapping; some fields go to ----:com.apple.iTunes:KEY)
  - AIFF/WAV  → ID3 chunk if present, otherwise skipped

Things this module does NOT do:
  - Re-encode audio. Only metadata blocks change. FLAC audio frames
    are untouched, so the *audio MD5* (FLAC's STREAMINFO MD5) is
    preserved. Full-file hash will change, but audio content won't.
  - Write to files marked status='broken' in the DB
  - Overwrite tags by default — passes `only_missing` per field
  - Write fields the format can't represent natively

Verified-rip protection (the Soulseek concern):
  - If a `.log` file (typical EAC output) sits next to the audio,
    we DON'T retag by default. The log records the audio file's
    bytes and embedded-tag state; touching tags makes the log
    no longer match for trackers that verify via EAC logs.
  - Config flag `tag_writer.touch_verified_rips = true` overrides this.

Mappings reference: https://picard.musicbrainz.org/docs/mappings/
We follow the Picard convention where possible since that's what most
DBs and downstream tools expect.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


logger = logging.getLogger("music-organiser")


# Field name -> Vorbis Comment key. Vorbis (FLAC, OGG, Opus) is the most
# permissive — accepts any uppercase key. We follow Picard's conventions
# (https://picard-docs.musicbrainz.org/en/variables/tags_basic.html)
# so downstream tools (foobar2000, Roon, beets) recognise them.
VORBIS_MAP = {
    "artist":          "ARTIST",
    "artists":         "ARTISTS",
    "albumartist":     "ALBUMARTIST",
    "album":           "ALBUM",
    "albumsort":       "ALBUMSORT",
    "albumartistsort": "ALBUMARTISTSORT",
    "artistsort":      "ARTISTSORT",
    "titlesort":       "TITLESORT",
    "composersort":    "COMPOSERSORT",
    "title":           "TITLE",
    "subtitle":        "SUBTITLE",
    "track_number":    "TRACKNUMBER",
    "totaltracks":     "TOTALTRACKS",
    "disc_number":     "DISCNUMBER",
    "totaldiscs":      "TOTALDISCS",
    "discsubtitle":    "DISCSUBTITLE",
    "year":            "DATE",            # DATE in Vorbis = year or full date
    "originaldate":    "ORIGINALDATE",
    "originalyear":    "ORIGINALYEAR",
    "originalalbum":   "ORIGINALALBUM",
    "originalartist":  "ORIGINALARTIST",
    "releasedate":     "RELEASEDATE",
    "genre":           "GENRE",
    "label":           "LABEL",           # also ORGANIZATION sometimes
    "catalog_number":  "CATALOGNUMBER",
    "barcode":         "BARCODE",
    "asin":            "ASIN",
    "country":         "RELEASECOUNTRY",
    "isrc":            "ISRC",
    "composer":        "COMPOSER",
    "lyricist":        "LYRICIST",
    "lyrics":          "LYRICS",
    "syncedlyrics":    "SYNCEDLYRICS",
    "producer":        "PRODUCER",
    "arranger":        "ARRANGER",
    "conductor":       "CONDUCTOR",
    "director":        "DIRECTOR",
    "djmixer":         "DJMIXER",
    "engineer":        "ENGINEER",
    "mixer":           "MIXER",
    "remixer":         "REMIXER",
    "writer":          "WRITER",
    "work":            "WORK",
    "movement":        "MOVEMENTNAME",
    "movementnumber":  "MOVEMENT",
    "movementtotal":   "MOVEMENTTOTAL",
    "showmovement":    "SHOWMOVEMENT",
    "comment":         "COMMENT",
    "bpm":             "BPM",
    "musical_key":     "INITIALKEY",
    "language":        "LANGUAGE",
    "script":          "SCRIPT",
    "media_format":    "MEDIA",
    "release_type":    "RELEASETYPE",
    "release_status":  "RELEASESTATUS",
    "website":         "WEBSITE",
    "copyright":       "COPYRIGHT",
    "license":         "LICENSE",
    "encodedby":       "ENCODEDBY",
    "encodersettings": "ENCODERSETTINGS",
    # Custom — flags the file as having been touched by music-organiser
    "ripper_software": "RIPPER",
    "ripper_version":  "RIPPER_VERSION",
    "ripper_settings": "RIPPER_SETTINGS",
    "musicbrainz_albumid":         "MUSICBRAINZ_ALBUMID",
    "musicbrainz_albumartistid":   "MUSICBRAINZ_ALBUMARTISTID",
    "musicbrainz_artistid":        "MUSICBRAINZ_ARTISTID",
    "musicbrainz_releasegroupid":  "MUSICBRAINZ_RELEASEGROUPID",
    "musicbrainz_recordingid":     "MUSICBRAINZ_TRACKID",   # Picard uses TRACKID for recording
    "musicbrainz_workid":          "MUSICBRAINZ_WORKID",
    "musicbrainz_labelid":         "MUSICBRAINZ_LABELID",
    "musicbrainz_discid":          "MUSICBRAINZ_DISCID",
    "musicbrainz_originalalbumid": "MUSICBRAINZ_ORIGINALALBUMID",
    "musicbrainz_originalartistid":"MUSICBRAINZ_ORIGINALARTISTID",
    "discogs_release_id":          "DISCOGS_RELEASE_ID",
    "discogs_master_id":           "DISCOGS_MASTER_ID",
    "acoustid_id":                 "ACOUSTID_ID",
    "acoustid_fingerprint":        "ACOUSTID_FINGERPRINT",
}

# ID3v2.4 standard frame mapping. Anything not in this dict goes into
# a TXXX (user-defined text) frame with the field name as description.
ID3_FRAME_MAP = {
    "artist":         "TPE1",
    "albumartist":    "TPE2",
    "album":          "TALB",
    "title":          "TIT2",
    "track_number":   "TRCK",
    "disc_number":    "TPOS",
    "year":           "TDRC",   # ID3v2.4 recording time
    "genre":          "TCON",
    "label":          "TPUB",
    "composer":       "TCOM",
    "lyricist":       "TEXT",
    "comment":        "COMM",
    "bpm":            "TBPM",
    "musical_key":    "TKEY",
    "language":       "TLAN",
    "isrc":           "TSRC",
}
# ID3 TXXX fields — Picard's conventions for things ID3 doesn't natively cover.
ID3_TXXX_MAP = {
    "catalog_number":              "CATALOGNUMBER",
    "barcode":                     "BARCODE",
    "country":                     "RELEASECOUNTRY",
    "release_type":                "MusicBrainz Album Type",
    "release_status":              "MusicBrainz Album Status",
    "musicbrainz_albumid":         "MusicBrainz Album Id",
    "musicbrainz_albumartistid":   "MusicBrainz Album Artist Id",
    "musicbrainz_artistid":        "MusicBrainz Artist Id",
    "musicbrainz_releasegroupid":  "MusicBrainz Release Group Id",
    "musicbrainz_workid":          "MusicBrainz Work Id",
    "musicbrainz_labelid":         "MusicBrainz Label Id",
    "acoustid_id":                 "Acoustid Id",
    "acoustid_fingerprint":        "Acoustid Fingerprint",
}

# MP4/M4A: standard atoms first, then freeform ----:com.apple.iTunes:KEY
MP4_ATOM_MAP = {
    "artist":         "\xa9ART",
    "albumartist":    "aART",
    "album":          "\xa9alb",
    "title":          "\xa9nam",
    "track_number":   "trkn",
    "disc_number":    "disk",
    "year":           "\xa9day",
    "genre":          "\xa9gen",
    "comment":        "\xa9cmt",
    "bpm":            "tmpo",
    "composer":       "\xa9wrt",
    "compilation":    "cpil",
}


@dataclass
class WriteResult:
    """One file's writeback outcome."""
    path: str = ""
    written_fields: list[str] = field(default_factory=list)
    skipped_fields: list[tuple[str, str]] = field(default_factory=list)  # (field, why)
    error: str = ""
    skipped_entirely: bool = False
    skip_reason: str = ""


def _has_eac_log(audio_path: Path) -> bool:
    """Does this audio file's folder contain a .log file? EAC logs name
    the verified rip and contain track-level checksums; retagging makes
    them no longer match."""
    folder = audio_path.parent
    if not folder.exists():
        return False
    for entry in folder.iterdir():
        if entry.suffix.lower() == ".log" and entry.is_file():
            # Sniff first line for EAC signature
            try:
                head = entry.read_text(encoding="utf-8", errors="replace")[:200]
                if any(s in head for s in (
                    "Exact Audio Copy",
                    "EAC extraction",
                    "exactaudiocopy",
                    "X Lossless Decoder",
                    "XLD ",
                )):
                    return True
            except OSError:
                pass
    return False


def _stringify(value: Any) -> str:
    """Coerce a tag value to a string suitable for writing."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "; ".join(_stringify(v) for v in value if v)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def write_tags_to_file(
    audio_path: Path | str,
    tags: dict[str, Any],
    *,
    only_missing: bool = True,
    touch_verified_rips: bool = False,
    dry_run: bool = False,
) -> WriteResult:
    """
    Write tag values into an audio file.

    - `tags`: dict keyed by FILE_COLUMNS-style names (artist, album,
      catalog_number, ...). Values get coerced to strings.
    - `only_missing`: per field, skip if the file already has a value.
      Otherwise overwrite. Recommended True; False is "force resync".
    - `touch_verified_rips`: when False (default), skip files whose
      folder contains an EAC/XLD log. Protects verified-rip integrity.
    - `dry_run`: skip the actual save, just record what would happen.

    Returns a WriteResult. On error, .error is set; on full-file skip,
    .skipped_entirely is True with .skip_reason.
    """
    path = Path(audio_path)
    result = WriteResult(path=str(path))

    if not path.exists():
        result.error = "file does not exist"
        return result

    if not touch_verified_rips and _has_eac_log(path):
        result.skipped_entirely = True
        result.skip_reason = "EAC/XLD verified rip — touching would break log integrity"
        return result

    # Lazy mutagen import so the rest of the codebase doesn't require it
    try:
        from mutagen import File as MutagenFile
        from mutagen.id3 import (
            ID3, TXXX, TIT2, TPE1, TPE2, TALB, TRCK, TPOS, TDRC, TCON,
            TPUB, TCOM, TEXT, COMM, TBPM, TKEY, TLAN, TSRC,
        )
        from mutagen.flac import FLAC
        from mutagen.oggvorbis import OggVorbis
        from mutagen.oggopus import OggOpus
        from mutagen.mp4 import MP4, MP4FreeForm
        from mutagen.easyid3 import EasyID3
    except ImportError as e:
        result.error = f"mutagen not installed: {e}"
        return result

    try:
        f = MutagenFile(str(path))
    except Exception as e:
        result.error = f"mutagen could not parse: {e}"
        return result
    if f is None:
        result.error = "unknown file format (mutagen returned None)"
        return result

    # Dispatch on actual class — more reliable than extension matching
    cls_name = type(f).__name__

    if cls_name == "FLAC" or isinstance(f, (FLAC,)):
        _write_vorbis(f, tags, only_missing, result)
    elif cls_name in ("OggVorbis", "OggOpus") or isinstance(f, (OggVorbis, OggOpus)):
        _write_vorbis(f, tags, only_missing, result)
    elif cls_name == "MP3" or path.suffix.lower() == ".mp3":
        _write_id3(f, tags, only_missing, result, path)
    elif cls_name == "MP4" or path.suffix.lower() in (".m4a", ".mp4", ".m4b"):
        _write_mp4(f, tags, only_missing, result)
    else:
        # Other formats: try EasyID3 (covers AIFF, WAV with ID3 chunk)
        try:
            tags_obj = EasyID3(str(path))
            _write_easyid3(tags_obj, tags, only_missing, result)
        except Exception as e:
            result.error = f"unsupported format {cls_name}: {e}"
            return result

    if dry_run:
        return result

    try:
        f.save()
    except Exception as e:
        # Save failed — clear the "written" list since none actually
        # made it to disk
        result.error = f"save failed: {e}"
        result.written_fields = []
        # Reduce skipped reasons to nothing-happened
        return result

    return result


def _write_vorbis(f, tags: dict[str, Any], only_missing: bool, result: WriteResult) -> None:
    """FLAC / OGG / Opus writeback via Vorbis Comments."""
    for field_name, value in tags.items():
        vorbis_key = VORBIS_MAP.get(field_name)
        if vorbis_key is None:
            # Unknown field — store as a custom MO_FIELDNAME comment so
            # we don't lose data. Vorbis allows arbitrary uppercase keys.
            vorbis_key = "MO_" + field_name.upper()

        new_val = _stringify(value)
        if not new_val:
            continue

        existing = f.get(vorbis_key, [])
        if only_missing and existing:
            result.skipped_fields.append((field_name, "already has a value"))
            continue

        f[vorbis_key] = new_val
        result.written_fields.append(field_name)


def _write_id3(f, tags: dict[str, Any], only_missing: bool,
               result: WriteResult, path: Path) -> None:
    """MP3 writeback. f is a mutagen MP3 object, but its .tags is the
    ID3 object we actually manipulate."""
    from mutagen.id3 import (
        ID3, ID3NoHeaderError, TXXX, TIT2, TPE1, TPE2, TALB, TRCK, TPOS,
        TDRC, TCON, TPUB, TCOM, TEXT, COMM, TBPM, TKEY, TLAN, TSRC,
    )
    # If MP3 has no ID3 tag at all, create one
    if f.tags is None:
        try:
            f.add_tags()
        except Exception as e:
            result.error = f"could not add ID3 tag: {e}"
            return

    id3 = f.tags

    frame_classes = {
        "TIT2": TIT2, "TPE1": TPE1, "TPE2": TPE2, "TALB": TALB,
        "TRCK": TRCK, "TPOS": TPOS, "TDRC": TDRC, "TCON": TCON,
        "TPUB": TPUB, "TCOM": TCOM, "TEXT": TEXT, "TBPM": TBPM,
        "TKEY": TKEY, "TLAN": TLAN, "TSRC": TSRC,
    }

    for field_name, value in tags.items():
        new_val = _stringify(value)
        if not new_val:
            continue

        frame_id = ID3_FRAME_MAP.get(field_name)
        if frame_id and frame_id in frame_classes:
            existing = id3.get(frame_id)
            if only_missing and existing and existing.text and existing.text[0]:
                result.skipped_fields.append((field_name, "already has a value"))
                continue
            id3.add(frame_classes[frame_id](encoding=3, text=[new_val]))
            result.written_fields.append(field_name)
        elif field_name == "comment":
            existing = id3.getall("COMM")
            if only_missing and existing:
                result.skipped_fields.append((field_name, "already has a value"))
                continue
            id3.add(COMM(encoding=3, lang="eng", desc="", text=[new_val]))
            result.written_fields.append(field_name)
        else:
            # TXXX user-defined
            desc = ID3_TXXX_MAP.get(field_name, field_name.upper())
            # Check existing TXXX with same desc
            existing = [t for t in id3.getall("TXXX") if t.desc == desc]
            if only_missing and existing and existing[0].text:
                result.skipped_fields.append((field_name, "already has TXXX"))
                continue
            # Remove existing same-desc TXXX before adding (avoid dup frames)
            for t in existing:
                id3.delall(f"TXXX:{desc}")
            id3.add(TXXX(encoding=3, desc=desc, text=[new_val]))
            result.written_fields.append(field_name)


def _write_mp4(f, tags: dict[str, Any], only_missing: bool, result: WriteResult) -> None:
    """M4A/MP4 writeback via mutagen atoms."""
    from mutagen.mp4 import MP4FreeForm
    for field_name, value in tags.items():
        atom = MP4_ATOM_MAP.get(field_name)
        new_val = _stringify(value)
        if not new_val:
            continue

        if atom in ("trkn", "disk"):
            # Track/disc are tuples (number, total) in MP4
            try:
                n = int(new_val.split("/")[0])
            except ValueError:
                result.skipped_fields.append((field_name, "non-integer"))
                continue
            if only_missing and f.get(atom):
                result.skipped_fields.append((field_name, "already has a value"))
                continue
            f[atom] = [(n, 0)]
            result.written_fields.append(field_name)
            continue
        if atom == "tmpo":
            try:
                f[atom] = [int(float(new_val))]
                result.written_fields.append(field_name)
            except ValueError:
                result.skipped_fields.append((field_name, "non-numeric bpm"))
            continue
        if atom == "cpil":
            f[atom] = bool(int(new_val)) if new_val.isdigit() else False
            result.written_fields.append(field_name)
            continue
        if atom:
            if only_missing and f.get(atom):
                result.skipped_fields.append((field_name, "already has a value"))
                continue
            f[atom] = [new_val]
            result.written_fields.append(field_name)
        else:
            # Freeform iTunes atom
            key = f"----:com.apple.iTunes:{field_name.upper()}"
            if only_missing and f.get(key):
                result.skipped_fields.append((field_name, "already has freeform"))
                continue
            f[key] = [MP4FreeForm(new_val.encode("utf-8"))]
            result.written_fields.append(field_name)


def _write_easyid3(tags_obj, tags: dict[str, Any], only_missing: bool,
                   result: WriteResult) -> None:
    """Generic EasyID3 path for AIFF/WAV/anything else with ID3 chunks."""
    easy_map = {
        "artist": "artist", "albumartist": "albumartist", "album": "album",
        "title": "title", "track_number": "tracknumber", "disc_number": "discnumber",
        "year": "date", "genre": "genre", "composer": "composer",
        "bpm": "bpm", "isrc": "isrc",
    }
    for field_name, value in tags.items():
        easy_key = easy_map.get(field_name)
        if easy_key is None:
            continue
        new_val = _stringify(value)
        if not new_val:
            continue
        if only_missing and tags_obj.get(easy_key):
            result.skipped_fields.append((field_name, "already has a value"))
            continue
        try:
            tags_obj[easy_key] = [new_val]
            result.written_fields.append(field_name)
        except Exception as e:
            result.skipped_fields.append((field_name, f"easyid3 rejected: {e}"))
    try:
        tags_obj.save()
    except Exception as e:
        result.error = f"easyid3 save failed: {e}"
