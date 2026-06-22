"""
audit.py
========

Quality audits over the indexed library. Pure SQL queries — fast, even
on a million-row database.

Audit categories:

1. **missing_embedded_art**     — file has no embedded album art at all
2. **missing_folder_art**       — file has no cover.jpg etc next to it
3. **missing_all_art**          — neither embedded nor folder art
4. **missing_artist**           — `artist` tag empty
5. **missing_album**            — `album` tag empty
6. **missing_title**            — `title` tag empty (or matches filename, meaning
                                   the tagger fell back to filename)
7. **missing_label**            — `label` tag empty
8. **missing_year**             — `year` tag empty
9. **unknown_artist_literal**   — artist is literally "Unknown Artist" / similar
10. **track_no_in_title**       — title starts with "01 -" or similar (un-stripped)
11. **odd_year**                — year < 1900 or > current+1
12. **suspected_transcode**     — fake-FLAC analysis flagged this file
13. **lossless_no_bit_depth**   — codec is lossless but bit_depth missing
                                  (often a sign of a re-encode)

Each audit returns a list of dicts with at least {path, reason, ...extras}.

Exporters:
- `dump_report(rows, path, fmt='csv')` — write to CSV, JSON, or plain text
- `audit_all(db)` — run every audit, return a combined report grouped by issue
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


# =============================================================================
# AUDITS — each is a function that takes a Database and returns rows
# =============================================================================

# These are unknown-artist placeholders we expect to find. Case-insensitive.
UNKNOWN_PLACEHOLDERS = (
    "unknown artist", "unknown", "various", "various artists",
    "[unknown artist]", "unknownartist", "n/a", "na", "untagged",
)


def missing_embedded_art(db) -> list[dict[str, Any]]:
    """Files where mutagen found no embedded APIC/picture frame."""
    rows = db.conn.execute("""
        SELECT path, artist, album, codec, organised_path
        FROM files
        WHERE has_embedded_art IS NOT NULL
          AND has_embedded_art = 0
          AND status != 'broken'
        ORDER BY label, artist, album
    """).fetchall()
    return [
        {"path": r["path"], "reason": "no embedded album art",
         "artist": r["artist"], "album": r["album"], "codec": r["codec"]}
        for r in rows
    ]


def missing_folder_art(db) -> list[dict[str, Any]]:
    """Files where no cover.jpg/folder.png lives next to them."""
    rows = db.conn.execute("""
        SELECT path, artist, album, codec
        FROM files
        WHERE has_folder_art IS NOT NULL
          AND has_folder_art = 0
          AND status != 'broken'
        ORDER BY label, artist, album
    """).fetchall()
    return [
        {"path": r["path"], "reason": "no cover.jpg / folder.png in album folder",
         "artist": r["artist"], "album": r["album"], "codec": r["codec"]}
        for r in rows
    ]


def missing_all_art(db) -> list[dict[str, Any]]:
    """Files with neither embedded nor folder art — the worst case."""
    rows = db.conn.execute("""
        SELECT path, artist, album, codec
        FROM files
        WHERE COALESCE(has_embedded_art, 0) = 0
          AND COALESCE(has_folder_art, 0) = 0
          AND status != 'broken'
        ORDER BY label, artist, album
    """).fetchall()
    return [
        {"path": r["path"], "reason": "no embedded art AND no folder art",
         "artist": r["artist"], "album": r["album"], "codec": r["codec"]}
        for r in rows
    ]


def missing_tag(db, column: str, label: str | None = None) -> list[dict[str, Any]]:
    """
    Generic missing-tag audit. `column` is one of: artist, album, title,
    label, year, etc. Returns rows where the column is NULL or empty.
    """
    # column is checked against a whitelist before being interpolated
    allowed = {"artist", "albumartist", "album", "title", "label",
               "year", "genre", "track_number"}
    if column not in allowed:
        raise ValueError(f"unsupported column: {column}")

    sql = f"""
        SELECT path, artist, album, title, codec
        FROM files
        WHERE ({column} IS NULL OR TRIM({column}) = '')
          AND status != 'broken'
        ORDER BY label, artist, album
    """
    rows = db.conn.execute(sql).fetchall()
    return [
        {"path": r["path"],
         "reason": f"missing {label or column}",
         "artist": r["artist"], "album": r["album"], "title": r["title"],
         "codec": r["codec"]}
        for r in rows
    ]


def unknown_artist_literal(db) -> list[dict[str, Any]]:
    """Artist field is literally a placeholder string."""
    placeholders = list(UNKNOWN_PLACEHOLDERS)
    placeholder_clause = " OR ".join(f"LOWER(TRIM(artist)) = ?" for _ in placeholders)
    sql = f"""
        SELECT path, artist, album, title, codec
        FROM files
        WHERE ({placeholder_clause})
          AND status != 'broken'
        ORDER BY label, artist, album
    """
    rows = db.conn.execute(sql, placeholders).fetchall()
    return [
        {"path": r["path"], "reason": f"placeholder artist: '{r['artist']}'",
         "album": r["album"], "title": r["title"], "codec": r["codec"]}
        for r in rows
    ]


def track_number_in_title(db) -> list[dict[str, Any]]:
    """
    Title starts with a digit-dash pattern like '01 -' — usually means
    the tagger fell back to the filename and didn't strip the prefix.
    """
    rows = db.conn.execute("""
        SELECT path, artist, album, title, codec
        FROM files
        WHERE status != 'broken'
          AND title GLOB '[0-9][0-9]*-*'
        ORDER BY label, artist, album
    """).fetchall()
    return [
        {"path": r["path"], "reason": f"title looks like a filename: '{r['title']}'",
         "artist": r["artist"], "album": r["album"], "codec": r["codec"]}
        for r in rows
    ]


def odd_year(db) -> list[dict[str, Any]]:
    """Year < 1900 or > current+1. Catches bad rips with '0000' etc."""
    cur_year = datetime.now().year + 1
    rows = db.conn.execute("""
        SELECT path, artist, album, title, year, codec
        FROM files
        WHERE status != 'broken'
          AND year IS NOT NULL
          AND TRIM(year) != ''
          AND (
            CAST(SUBSTR(year, 1, 4) AS INTEGER) < 1900
            OR CAST(SUBSTR(year, 1, 4) AS INTEGER) > ?
          )
        ORDER BY year
    """, (cur_year,)).fetchall()
    return [
        {"path": r["path"], "reason": f"odd year: '{r['year']}'",
         "artist": r["artist"], "album": r["album"], "codec": r["codec"]}
        for r in rows
    ]


def suspected_transcode(db) -> list[dict[str, Any]]:
    """Files that fake_flac.analyse() flagged as suspected transcodes."""
    rows = db.conn.execute("""
        SELECT path, artist, album, codec,
               transcode_cutoff_hz, transcode_confidence, transcode_notes
        FROM files
        WHERE transcode_suspected = 1
        ORDER BY transcode_confidence DESC
    """).fetchall()
    return [
        {"path": r["path"],
         "reason": f"suspected transcode: {r['transcode_notes']}",
         "artist": r["artist"], "album": r["album"], "codec": r["codec"],
         "cutoff_hz": r["transcode_cutoff_hz"],
         "confidence": r["transcode_confidence"]}
        for r in rows
    ]


def lossless_no_bit_depth(db) -> list[dict[str, Any]]:
    """
    Files claiming to be lossless but missing bit-depth info.
    Real FLAC always has bit_depth populated by mutagen.
    """
    rows = db.conn.execute("""
        SELECT path, artist, album, codec
        FROM files
        WHERE lossless = 1
          AND bit_depth IS NULL
          AND status != 'broken'
        ORDER BY codec, path
    """).fetchall()
    return [
        {"path": r["path"], "reason": f"lossless codec ({r['codec']}) but no bit_depth",
         "artist": r["artist"], "album": r["album"], "codec": r["codec"]}
        for r in rows
    ]


# =============================================================================
# REGISTRY — for the menu
# =============================================================================

AUDITS: dict[str, tuple[str, Callable]] = {
    "missing_embedded_art":    ("Missing embedded album art",
                                missing_embedded_art),
    "missing_folder_art":      ("Missing folder art (cover.jpg etc)",
                                missing_folder_art),
    "missing_all_art":         ("Missing BOTH embedded and folder art",
                                missing_all_art),
    "missing_artist":          ("Missing artist tag",
                                lambda db: missing_tag(db, "artist")),
    "missing_album":           ("Missing album tag",
                                lambda db: missing_tag(db, "album")),
    "missing_title":           ("Missing title tag",
                                lambda db: missing_tag(db, "title")),
    "missing_label":           ("Missing label tag",
                                lambda db: missing_tag(db, "label")),
    "missing_year":            ("Missing year tag",
                                lambda db: missing_tag(db, "year")),
    "unknown_artist_literal":  ("Artist is literally 'Unknown'",
                                unknown_artist_literal),
    "track_no_in_title":       ("Title looks like a filename",
                                track_number_in_title),
    "odd_year":                ("Year out of plausible range",
                                odd_year),
    "suspected_transcode":     ("Suspected fake-FLAC (run VERIFY FLACS first)",
                                suspected_transcode),
    "lossless_no_bit_depth":   ("Lossless codec but no bit-depth",
                                lossless_no_bit_depth),
}


def mark_broken_metadata(db, *, dry_run: bool = False) -> tuple[int, int]:
    """
    Scan the DB and flip status='broken' on every row that fails the
    metadata-quality check (missing artist OR album OR title).

    Also UN-marks rows currently flagged broken but which now have all
    three fields populated — typical after a successful metadata fetch
    that filled in what was missing. (We don't un-mark rows whose
    'broken' status came from a different reason, like a copy error
    or a fake-FLAC quarantine.)

    Returns (n_newly_broken, n_un_broken). Use dry_run=True to count
    without writing.

    Statuses preserved (never overwritten by this function):
      - 'duplicate'    — set by import dedup
      - 'orphan'       — set by orphan-folder triage
      - 'quarantine'   — set by fake-FLAC sweep
    """
    from detection import is_record_metadata_broken

    _PRESERVED = {"duplicate", "orphan", "quarantine"}
    n_broken = 0
    n_unbroken = 0
    paths_to_break: list[str] = []
    paths_to_unbreak: list[str] = []

    for row in db.iter_all():
        current_status = (row.get("status") or "").strip().lower()
        if current_status in _PRESERVED:
            continue
        is_broken, _reason = is_record_metadata_broken(dict(row))
        path = row.get("path")
        if is_broken and current_status != "broken":
            paths_to_break.append(path)
        elif (not is_broken) and current_status == "broken":
            # Only un-mark if the broken reason looks metadata-related.
            # If the existing comment says "copy error" or "fake-flac",
            # leave it alone — we'd be lying about its real condition.
            comment = (row.get("comment") or "").lower()
            if any(marker in comment for marker in
                   ("copy error", "path build error", "fake-flac",
                    "metadata error", "quarantine")):
                continue
            paths_to_unbreak.append(path)

    if not dry_run:
        for p in paths_to_break:
            try:
                db.conn.execute(
                    "UPDATE files SET status='broken' WHERE path=?", (p,)
                )
                n_broken += 1
            except Exception:
                pass
        for p in paths_to_unbreak:
            try:
                db.conn.execute(
                    "UPDATE files SET status='ok' WHERE path=?", (p,)
                )
                n_unbroken += 1
            except Exception:
                pass
        db.conn.commit()
    else:
        n_broken = len(paths_to_break)
        n_unbroken = len(paths_to_unbreak)

    return n_broken, n_unbroken


def reconcile_album_fields(
    db,
    *,
    dry_run: bool = False,
    fields: tuple[str, ...] | None = None,
    only_missing: bool = True,
    coalesce_conflicts: bool = True,
) -> tuple[int, int, list[str]]:
    """
    Scan the DB and fill album-level fields (label/year/genre) on
    tracks that share an album.

    Why this matters: `build_destination_path` uses `label` and `year`
    as folder-name components (`High Quality/Warp/Aphex Twin - SAW
    85-92 - 1992/`). If two tracks of one album have different labels,
    they end up in DIFFERENT folders — the album splits. The library
    needs albums whole, so this function exists to make sure every
    track of an album shares the same album-level metadata.

    Grouping: `(parent_folder, album)`. Same folder + same album name
    = "same release on disk". Folder grouping prevents two unrelated
    albums that happen to share a name from being merged. We don't
    group by `(artist, album)` because VA compilations have varying
    per-track artist — they'd never group together that way even
    though they're one physical release.

    Parameters:
      `only_missing=True` (default): only fills tracks where the field
        is currently empty/placeholder. Existing real values are left
        alone. Safe for general use.
      `only_missing=False`: OVERWRITES every track with the chosen
        value. Use this when you specifically need to UNIFY a field
        across an album that's currently split (e.g. fix-labels
        operation). Combined with coalesce_conflicts=True, picks the
        majority value and stamps the whole album with it.

      `coalesce_conflicts=True` (default): when an album has multiple
        non-empty values for a field, pick the majority and continue.
        Ties broken alphabetically. Keeps albums together at the cost
        of possibly stamping the "wrong" label on a few tracks.
      `coalesce_conflicts=False`: strict mode. Conflicting fields are
        left untouched. Useful for forensic audits where you want to
        see real conflicts, not auto-resolution. May leave albums
        split across folders.

    Returns (n_groups_filled, n_cells_written, conflict_log).
    """
    from detection import apply_album_level_inference, ALBUM_LEVEL_FIELDS
    from pathlib import Path as _Path

    if fields is None:
        fields = ALBUM_LEVEL_FIELDS

    # First pass: build the grouping. We hold all the row dicts
    # in memory because we need to iterate them per-group; the DB
    # iterator can only be walked once per call.
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in db.iter_all():
        path = row.get("path") or ""
        album = (row.get("album") or "").strip()
        if not path or not album:
            continue
        # Strip placeholder album names — those should be in Broken/
        # and shouldn't drive inference.
        from detection import is_unknown_tag
        if is_unknown_tag(album):
            continue
        folder = str(_Path(path).parent)
        groups.setdefault((folder, album), []).append(dict(row))

    n_groups_filled = 0
    n_cells_written = 0
    conflict_log: list[str] = []

    for (folder, album), records in groups.items():
        if len(records) < 2:
            # A single-track "album" has nothing to reconcile against.
            continue

        n_writes, conflicts = apply_album_level_inference(
            records,
            fields=fields,
            only_missing=only_missing,
            coalesce_conflicts=coalesce_conflicts,
        )
        if conflicts:
            short_folder = folder if len(folder) <= 80 else "…" + folder[-77:]
            for c in conflicts:
                conflict_log.append(f"  {short_folder} / {album}: {c}")
        if n_writes == 0:
            continue

        n_groups_filled += 1
        n_cells_written += n_writes

        if not dry_run:
            try:
                with db.transaction():
                    for r in records:
                        db.upsert_file(r)
            except Exception as e:
                conflict_log.append(
                    f"  {short_folder} / {album}: write failed: {e}"
                )

    return n_groups_filled, n_cells_written, conflict_log


def unify_record_labels(
    db,
    *,
    dry_run: bool = False,
) -> tuple[int, int, list[str]]:
    """
    Aggressive "make sure every track in an album has the SAME label"
    pass. This is the dedicated fix-labels operation: it overwrites
    differing per-track labels with the album's majority value so the
    organiser will place every track in the same folder.

    Unlike the general `reconcile_album_fields`, this:
      - acts ONLY on the label column (year/genre untouched)
      - uses `only_missing=False` (overwrites differing labels)
      - uses `coalesce_conflicts=True` (majority wins for conflicts,
        ties broken alphabetically)
      - reports conflicts in the log so you can review them, but the
        resolution has already been applied

    Use this specifically when albums are getting split across folders
    by the organiser due to inconsistent label tags. After running
    this, run option 2 (Organise) and they'll consolidate.

    Returns (n_groups_filled, n_cells_written, conflict_log).
    """
    return reconcile_album_fields(
        db,
        dry_run=dry_run,
        fields=("label",),
        only_missing=False,        # overwrite differing values
        coalesce_conflicts=True,   # majority wins
    )




@dataclass
class AuditReport:
    """Combined result of audit_all()."""
    issues_by_audit: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def total_issues(self) -> int:
        return sum(len(v) for v in self.issues_by_audit.values())

    def summary(self) -> str:
        lines = []
        for audit_key, rows in self.issues_by_audit.items():
            label = AUDITS.get(audit_key, (audit_key,))[0]
            lines.append(f"  {label}: {len(rows)} files")
        return "\n".join(lines)


def audit_all(db) -> AuditReport:
    """Run every audit. Returns AuditReport."""
    report = AuditReport()
    for key, (_, fn) in AUDITS.items():
        try:
            report.issues_by_audit[key] = fn(db)
        except Exception as e:
            # Don't crash the whole audit because one query failed.
            report.issues_by_audit[key] = []
            print(f"  audit '{key}' failed: {e}")
    return report


# =============================================================================
# EXPORT
# =============================================================================

def dump_report(
    rows: list[dict[str, Any]],
    out_path: str | Path,
    *,
    fmt: str = "csv",
) -> Path:
    """
    Write `rows` to `out_path` in the requested format.

    fmt: 'csv', 'json', or 'txt' (plain text, one path per line preceded
         by reason as a comment).

    Returns the resolved output path.
    """
    out = Path(out_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        out.write_text("# (no rows)\n", encoding="utf-8")
        return out

    if fmt == "csv":
        # Collect all keys across all rows so the CSV columns are stable.
        keys: list[str] = []
        seen: set[str] = set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    keys.append(k)
                    seen.add(k)
        with open(out, "w", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)

    elif fmt == "json":
        with open(out, "w", encoding="utf-8") as fp:
            json.dump(rows, fp, indent=2, default=str, ensure_ascii=False)

    elif fmt == "txt":
        with open(out, "w", encoding="utf-8") as fp:
            for r in rows:
                reason = r.get("reason", "")
                path = r.get("path", "")
                fp.write(f"# {reason}\n{path}\n")

    else:
        raise ValueError(f"unsupported format: {fmt}")

    return out


def dump_full_report(
    report: AuditReport,
    out_dir: str | Path,
    *,
    fmt: str = "csv",
) -> list[Path]:
    """
    Write the combined report — one file per audit category.

    Returns the list of paths written.
    """
    out_root = Path(out_dir).expanduser()
    out_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for audit_key, rows in report.issues_by_audit.items():
        if not rows:
            continue
        fname = f"audit_{audit_key}_{timestamp}.{fmt}"
        path = dump_report(rows, out_root / fname, fmt=fmt)
        written.append(path)

    # Also write a summary file
    summary_path = out_root / f"audit_summary_{timestamp}.txt"
    with open(summary_path, "w", encoding="utf-8") as fp:
        fp.write(f"music-organiser audit summary\n")
        fp.write(f"generated: {datetime.now().isoformat()}\n\n")
        fp.write(f"total issues: {report.total_issues()}\n\n")
        fp.write(report.summary())
        fp.write("\n")
    written.append(summary_path)
    return written
