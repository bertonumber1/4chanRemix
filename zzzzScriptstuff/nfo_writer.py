"""
nfo_writer.py
=============

Generate `album.nfo` files for each album folder. NFO files are the
scene/private-tracker convention for documenting a release: a plain
text file with tracklist, codec, bitrate, MusicBrainz/Discogs IDs,
runtime, and release metadata.

Format: width-fixed plain ASCII (with a UTF-8 fallback for non-ASCII
text fields like Japanese album names). Looks like:

    ╔═══════════════════════════════════════════════════════════════╗
    ║   Aphex Twin - Selected Ambient Works 85-92                   ║
    ║   1992  •  R&S Records  •  RS92043CD                          ║
    ╚═══════════════════════════════════════════════════════════════╝

    Source       : MusicBrainz
    Type         : Album (Official)
    Format       : CD
    Country      : Belgium
    Language     : English
    Barcode      : 5413356430431
    Catalog #    : RS92043CD
    MusicBrainz  : 9d5c5e85-...
    Discogs      : https://discogs.com/...
    Wikipedia    : https://en.wikipedia.org/...

    Tracklist
    ─────────
     1.  Xtal                       (04:50)  FLAC 1058 kbps / 44.1 kHz / 16-bit
     2.  Tha                        (09:00)  FLAC 1058 kbps / 44.1 kHz / 16-bit
     ...

    Total runtime: 74:23  •  Total size: 412 MB

Generated YYYY-MM-DD by music-organiser v0.17.0

Two output styles:
  - `nfo`  → traditional scene-style box-drawing chars, monospace
  - `txt`  → minimal, plain (for readers that mangle box chars)

Default extension is `.nfo` per user request. Override via config.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger("music-organiser")


# Box drawing characters used in the NFO header. Scene NFOs traditionally
# use either CP437 box chars or these unicode equivalents. We use unicode.
NFO_BOX = {
    "tl": "╔", "tr": "╗", "bl": "╚", "br": "╝",
    "h":  "═", "v":  "║",
}


def _format_duration(seconds: float | int) -> str:
    """Seconds -> 'M:SS' or 'H:MM:SS'."""
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "?:??"
    if s < 0:
        return "?:??"
    if s >= 3600:
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}"
    m, sec = divmod(s, 60)
    return f"{m}:{sec:02d}"


def _format_size(bytes_: int | float) -> str:
    """Human-readable byte count."""
    try:
        b = float(bytes_)
    except (TypeError, ValueError):
        return "? KB"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def generate_album_nfo(
    album_rows: list[dict[str, Any]],
    *,
    output_path: Path | None = None,
    style: str = "nfo",
    line_width: int = 65,
) -> str:
    """
    Generate NFO text from a list of DB rows for one album. Returns the
    text; if output_path is provided, also writes it.

    Rows should be the dict rows from db.iter_all() — same shape as
    everywhere else in the codebase. Assumes all rows belong to the
    same album (caller groups them).

    style: 'nfo' = with box-drawing chars; 'txt' = plain ASCII only.
    """
    if not album_rows:
        return ""

    # Pick representative metadata from the first row that has it
    def first_nonempty(field: str) -> str:
        for r in album_rows:
            v = r.get(field)
            if v:
                return str(v).strip()
        return ""

    artist = first_nonempty("albumartist") or first_nonempty("artist") or "Unknown Artist"
    album  = first_nonempty("album") or "Unknown Album"
    year   = first_nonempty("year")
    label  = first_nonempty("label")
    catalog = first_nonempty("catalog_number")
    country = first_nonempty("country")
    barcode = first_nonempty("barcode")
    asin = first_nonempty("asin")
    release_type = first_nonempty("release_type")
    release_status = first_nonempty("release_status")
    language = first_nonempty("language")
    media_format = first_nonempty("media_format")
    packaging = first_nonempty("packaging")
    mb_albumid = first_nonempty("musicbrainz_albumid")
    mb_rgid    = first_nonempty("musicbrainz_releasegroupid")
    mb_labelid = first_nonempty("musicbrainz_labelid")
    discogs_id = first_nonempty("discogs_release_id")
    annotation = first_nonempty("annotation")
    originaldate = first_nonempty("originaldate")
    # Rip software info — included so the NFO is a complete provenance record
    ripper_software = first_nonempty("ripper_software")
    ripper_version  = first_nonempty("ripper_version")
    ripper_settings = first_nonempty("ripper_settings")

    # Parse url_relations JSON if present
    url_rels: dict[str, str] = {}
    raw_urls = first_nonempty("url_relations")
    if raw_urls:
        try:
            url_rels = json.loads(raw_urls) if isinstance(raw_urls, str) else dict(raw_urls)
        except Exception:
            url_rels = {}

    # Aggregate technical stats from the rows
    codecs = sorted({(r.get("codec") or "").lower() for r in album_rows if r.get("codec")})
    bitrates = [r.get("bitrate") for r in album_rows if r.get("bitrate")]
    sample_rates = sorted({r.get("sample_rate") for r in album_rows if r.get("sample_rate")})
    bit_depths = sorted({r.get("bit_depth") for r in album_rows if r.get("bit_depth")})
    total_duration = sum(
        float(r.get("duration_seconds") or 0) for r in album_rows
    )
    total_size = sum(int(r.get("size_bytes") or 0) for r in album_rows)

    lines: list[str] = []

    if style == "nfo":
        # Box header
        h = NFO_BOX["h"]
        v = NFO_BOX["v"]
        inner_w = line_width - 4   # 2 for borders, 2 for padding
        title_line = f"{artist} - {album}"
        meta_parts = [p for p in (year, label, catalog) if p]
        meta_line = "  •  ".join(meta_parts) if meta_parts else ""
        lines.append(NFO_BOX["tl"] + h * (line_width - 2) + NFO_BOX["tr"])
        lines.append(f"{v}  {title_line[:inner_w]:<{inner_w}}  {v}")
        if meta_line:
            lines.append(f"{v}  {meta_line[:inner_w]:<{inner_w}}  {v}")
        lines.append(NFO_BOX["bl"] + h * (line_width - 2) + NFO_BOX["br"])
    else:
        # Plain header
        lines.append(f"{artist} - {album}")
        lines.append("=" * len(f"{artist} - {album}"))
        meta_parts = [p for p in (year, label, catalog) if p]
        if meta_parts:
            lines.append("  |  ".join(meta_parts))

    lines.append("")

    # Metadata section
    fields = [
        ("Source",       "MusicBrainz" if mb_albumid else "(unknown)"),
        ("Type",         f"{release_type}{f' ({release_status})' if release_status else ''}"),
        ("Format",       media_format),
        ("Packaging",    packaging),
        ("Country",      country),
        ("Language",     language),
        ("Original date", originaldate),
        ("Barcode",      barcode),
        ("Catalog #",    catalog),
        ("ASIN",         asin),
        ("MusicBrainz",  mb_albumid),
        ("Rel. Group",   mb_rgid),
        ("MB Label",     mb_labelid),
        ("Discogs",      discogs_id),
    ]
    # Rip software section — added v0.18 so the NFO records HOW the file was made.
    if ripper_software:
        ripper_str = ripper_software
        if ripper_version:
            ripper_str += f" v{ripper_version}"
        if ripper_settings:
            ripper_str += f"  ({ripper_settings})"
        fields.append(("Ripped by", ripper_str))
    for url_type, url in sorted(url_rels.items()):
        # Make the relationship-type human readable
        label_str = url_type.replace("_", " ").title()[:14]
        fields.append((label_str, url))

    for k, val in fields:
        if val:
            lines.append(f"  {k:<13}: {val}")
    lines.append("")

    # Annotation if present
    if annotation:
        lines.append("Notes")
        lines.append("─────" if style == "nfo" else "-----")
        for chunk in annotation.split("\n"):
            lines.append(f"  {chunk}")
        lines.append("")

    # Tracklist
    if style == "nfo":
        lines.append("Tracklist")
        lines.append("─────────")
    else:
        lines.append("Tracklist")
        lines.append("---------")

    # Sort by (disc_number, track_number) — both stored as strings,
    # parse leading int
    def sort_key(r):
        def to_int(x):
            try:
                # Strip "/total" suffix if present
                s = str(x or "").split("/")[0].strip()
                return int(s)
            except (ValueError, TypeError):
                return 9999
        return (to_int(r.get("disc_number")), to_int(r.get("track_number")))

    sorted_rows = sorted(album_rows, key=sort_key)
    for r in sorted_rows:
        tn_raw = str(r.get("track_number") or "").split("/")[0].strip() or "?"
        try:
            tn = f"{int(tn_raw):>2}"
        except ValueError:
            tn = f"{tn_raw:>2}"
        title = (r.get("title") or "(untitled)")[:32]
        dur   = _format_duration(r.get("duration_seconds") or 0)
        codec = (r.get("codec") or "?").upper()
        br    = r.get("bitrate") or 0
        sr    = r.get("sample_rate") or 0
        bd    = r.get("bit_depth") or 0
        tech_bits = [codec]
        if br: tech_bits.append(f"{br//1000} kbps")
        if sr: tech_bits.append(f"{sr/1000:.1f} kHz")
        if bd: tech_bits.append(f"{bd}-bit")
        tech = " / ".join(tech_bits)
        lines.append(f"  {tn}. {title:<33} ({dur:>7})  {tech}")

    lines.append("")
    # Summary line
    sum_parts = [
        f"Tracks: {len(album_rows)}",
        f"Runtime: {_format_duration(total_duration)}",
        f"Size: {_format_size(total_size)}",
    ]
    if codecs:
        sum_parts.append("Codec: " + "/".join(codecs).upper())
    lines.append("  " + "  •  ".join(sum_parts))
    lines.append("")

    # Footer
    today = datetime.date.today().strftime("%Y-%m-%d")
    lines.append(f"  Generated {today} by music-organiser")
    lines.append("")

    text = "\n".join(lines)

    if output_path is not None:
        try:
            output_path.write_text(text, encoding="utf-8")
        except OSError as e:
            logger.warning("could not write NFO to %s: %s", output_path, e)

    return text


def write_album_nfos_for_db(
    db,
    *,
    style: str = "nfo",
    overwrite: bool = False,
    log_cb=None,
) -> dict[str, int]:
    """
    Walk the DB, group by album folder, generate one NFO per folder.

    Returns {written, skipped_exists, skipped_no_folder, errors}.
    """
    stats = {"written": 0, "skipped_exists": 0, "skipped_no_folder": 0, "errors": 0}

    def log(level: str, msg: str) -> None:
        if log_cb:
            log_cb(level, msg)

    # Group by folder
    by_folder: dict[str, list[dict[str, Any]]] = {}
    for row in db.iter_all():
        path = row.get("path")
        if not path:
            continue
        folder = str(Path(path).parent)
        by_folder.setdefault(folder, []).append(dict(row))

    for folder, rows in by_folder.items():
        folder_p = Path(folder)
        if not folder_p.exists():
            stats["skipped_no_folder"] += 1
            continue
        nfo_path = folder_p / f"album.{style}"
        if nfo_path.exists() and not overwrite:
            stats["skipped_exists"] += 1
            continue
        try:
            generate_album_nfo(rows, output_path=nfo_path, style=style)
            stats["written"] += 1
            log("info", f"NFO: {folder_p.name}")
        except Exception as e:
            stats["errors"] += 1
            logger.warning("NFO write failed for %s: %s", folder, e)

    return stats


def make_signature_comment(row: dict[str, Any]) -> str:
    """
    Build a one-line provenance signature suitable for writing into the
    file's `comment` tag. Includes ripper info, MBID, and a marker that
    we touched the file. NOT the full NFO — full NFO into comment would
    bloat files and most players truncate long comments.

    Example outputs:
       "Ripped with EAC v1.6 | MB:9d5c5e85... | tagged by music-organiser 2026-05-25"
       "MB:abc-mbid | tagged by music-organiser 2026-05-25"
    """
    import datetime
    parts = []
    rs = (row.get("ripper_software") or "").strip()
    rv = (row.get("ripper_version") or "").strip()
    if rs:
        ripper = f"Ripped with {rs}"
        if rv:
            ripper += f" v{rv}"
        parts.append(ripper)
    mbid = (row.get("musicbrainz_albumid") or "").strip()
    if mbid:
        parts.append(f"MB:{mbid[:13]}")  # truncated for length
    today = datetime.date.today().strftime("%Y-%m-%d")
    parts.append(f"tagged by music-organiser {today}")
    return " | ".join(parts)
