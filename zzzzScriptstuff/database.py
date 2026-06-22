"""
database.py
===========

SQLite-backed library index.

Schema philosophy:
- One row per audio file. The PRIMARY KEY is the absolute path because
  paths are unique on disk and we look files up by path constantly during
  re-scans.
- Every metadata field mutagen can plausibly produce gets its own column.
  Storing tags as TEXT (even numeric-looking ones like track / year) keeps
  us safe from weirdly-tagged files ("1/12", "Vinyl Rip 2003", etc.).
- A `tags_raw` JSON column captures anything we don't have a column for,
  so nothing is lost — you can query it later with SQLite's json_extract().
- We use WAL so reads don't block writes during long imports.

Public API:
    Database(path)            — open or create
    db.upsert_file(record)    — insert or replace by path
    db.get_by_path(path)
    db.find_album_tracks(album_dir)
    db.iter_all()
    db.count()
    db.close()
"""

from __future__ import annotations

import json
import os
import sqlite3
import time as _time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


# last_seen timestamps have 1-second resolution, so recomputing the
# formatted string on every upsert (200k+ times per fetch) is wasted
# work. Cache it and only refresh when the wall clock has advanced a
# second. Measured ~8x faster than `SELECT CURRENT_TIMESTAMP` and ~13x
# faster than a fresh strftime per call.
_TS_CACHE = {"at": 0.0, "str": ""}


def _utcnow_sqlite() -> str:
    """Current UTC time formatted to match SQLite's CURRENT_TIMESTAMP
    ('YYYY-MM-DD HH:MM:SS'). Cached at 1-second resolution since that's
    the precision of the column anyway."""
    now = _time.time()
    if now - _TS_CACHE["at"] >= 1.0:
        _TS_CACHE["str"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        _TS_CACHE["at"] = now
    return _TS_CACHE["str"]


# Every column in the `files` table, with its SQL type. Keeping this as a
# Python list (not a giant SQL string) means we can:
#   - generate the CREATE TABLE statement
#   - generate INSERT/UPDATE statements
#   - check at runtime which columns the user's metadata dict is providing
# from one source of truth.
FILE_COLUMNS: list[tuple[str, str]] = [
    # --- identity / filesystem -------------------------------------------
    ("path",              "TEXT PRIMARY KEY"),  # absolute path on disk
    ("size_bytes",        "INTEGER"),
    ("mtime",             "REAL"),              # epoch seconds, from os.stat
    ("content_hash",      "TEXT"),              # fast hash (head+tail+size)
    ("source_root",       "TEXT"),              # which configured source this came from
    # Original-filename traceability. Set ONCE when a file is first
    # imported and never overwritten thereafter. Lets us reconstruct
    # what a file used to be called even after rename-during-organise
    # has changed `path`. `original_path` is the absolute path the file
    # first appeared at (so you can grep your import logs to find it);
    # `original_filename` is just the basename for quick eyeballing.
    # These columns are added by ALTER TABLE on schema upgrade — they
    # stay NULL for files imported before the upgrade, and that's fine.
    ("original_path",     "TEXT"),
    ("original_filename", "TEXT"),
    # Timestamp of the last successful organise pass that touched this
    # row. Set after a move (or after detecting the file's already at
    # its canonical path). Used by the "normal vs fresh pass" choice
    # in cmd_organise: a normal pass can skip rows whose organised_at
    # is newer than their last metadata change, since their canonical
    # destination can't have shifted. A fresh pass ignores this and
    # re-evaluates every row.
    ("organised_at",      "TEXT"),
    # --- core tags --------------------------------------------------------
    ("artist",            "TEXT"),
    ("albumartist",       "TEXT"),
    ("primary_artist",    "TEXT"),              # derived: first of "A & B feat C"
    ("album",             "TEXT"),
    ("title",             "TEXT"),
    ("track_number",      "TEXT"),
    ("disc_number",       "TEXT"),
    ("year",              "TEXT"),
    ("date",              "TEXT"),              # full date if present
    ("genre",             "TEXT"),
    ("label",             "TEXT"),
    ("catalog_number",    "TEXT"),
    ("isrc",              "TEXT"),
    ("barcode",           "TEXT"),
    ("composer",          "TEXT"),
    ("lyricist",          "TEXT"),
    ("producer",          "TEXT"),
    ("comment",           "TEXT"),
    # --- audio properties (from mutagen.info) -----------------------------
    ("duration_seconds",  "REAL"),
    ("bitrate",           "INTEGER"),
    ("sample_rate",       "INTEGER"),
    ("channels",          "INTEGER"),
    ("bit_depth",         "INTEGER"),
    ("codec",             "TEXT"),              # 'flac', 'mp3', 'opus', ...
    ("lossless",          "INTEGER"),           # 1 / 0 / NULL
    # --- DJ / production tags --------------------------------------------
    ("bpm",               "TEXT"),              # often non-integer; "128.5"
    ("musical_key",       "TEXT"),              # "Am", "8A" (Camelot), etc
    ("mood",              "TEXT"),
    ("compilation",       "TEXT"),              # "1" / "0" — mutagen returns str
    ("replaygain_track_gain", "TEXT"),          # e.g. "-6.42 dB"
    ("replaygain_track_peak", "TEXT"),
    ("replaygain_album_gain", "TEXT"),
    ("replaygain_album_peak", "TEXT"),
    # --- embedded album art ----------------------------------------------
    ("has_embedded_art",  "INTEGER"),           # 1 / 0; NULL = not checked
    ("embedded_art_count", "INTEGER"),
    ("embedded_art_size_bytes", "INTEGER"),     # total bytes across all pics
    ("embedded_art_mime", "TEXT"),              # 'image/jpeg', etc
    # --- folder-level art (cover.jpg / folder.jpg / etc next to file) ----
    ("has_folder_art",    "INTEGER"),
    ("folder_art_path",   "TEXT"),
    # --- fake-FLAC / transcode detection ---------------------------------
    # populated by fake_flac.analyse(); see that module's docstring for
    # what each value means.
    ("transcode_checked",     "INTEGER"),       # 1 = analyse() has run on this file
    ("transcode_suspected",   "INTEGER"),       # 1 = high-freq cutoff indicates lossy origin
    ("transcode_cutoff_hz",   "REAL"),          # detected cutoff frequency
    ("transcode_confidence",  "REAL"),          # 0.0 - 1.0
    ("transcode_notes",       "TEXT"),          # human-readable explanation
    # --- musicbrainz / discogs IDs ---------------------------------------
    ("musicbrainz_trackid",       "TEXT"),
    ("musicbrainz_albumid",       "TEXT"),
    ("musicbrainz_artistid",      "TEXT"),
    ("musicbrainz_albumartistid", "TEXT"),
    ("discogs_release_id",        "TEXT"),
    # --- archival fields (added v0.17 for "fetch EVERYTHING" mode) -------
    # These extend the MB harvest beyond basic tags. Querying MB with
    # the full `inc=labels+recordings+release-groups+...` set returns
    # all of these per release. Each one is useful for archive-grade
    # libraries; most are URL relations or external IDs that wouldn't
    # appear in a standard tagger.
    ("musicbrainz_recordingid",    "TEXT"),       # per-track recording MBID
    ("musicbrainz_workid",         "TEXT"),       # composition-level MBID
    ("musicbrainz_releasegroupid", "TEXT"),       # release-group (album family) MBID
    ("musicbrainz_labelid",        "TEXT"),       # label MBID
    ("country",                    "TEXT"),       # release country (e.g. 'US', 'JP')
    ("release_status",             "TEXT"),       # 'official' / 'promotion' / 'bootleg'
    ("release_type",               "TEXT"),       # 'album' / 'single' / 'compilation' / 'soundtrack'
    ("language",                   "TEXT"),       # release script language (e.g. 'eng', 'jpn')
    ("script",                     "TEXT"),       # writing script (e.g. 'Latn', 'Cyrl')
    ("packaging",                  "TEXT"),       # 'Jewel Case' / 'Digipak' / 'Digital Media'
    ("media_format",               "TEXT"),       # 'CD' / 'Vinyl' / 'Digital Media' / 'SACD'
    ("media_track_count",          "INTEGER"),    # total tracks on the medium
    ("annotation",                 "TEXT"),       # MB's free-text annotation
    ("aliases",                    "TEXT"),       # JSON list of artist/album aliases
    ("tags",                       "TEXT"),       # JSON list of MB folksonomy tags
    ("mb_genres",                  "TEXT"),       # JSON list of MB-curated genres
    # URL relations — Wikipedia, Discogs, Bandcamp, etc. Stored as JSON
    # dict (relation_type -> URL). Format example:
    # {"wikipedia": "https://en.wikipedia.org/...", "discogs": "...",
    #  "bandcamp": "...", "official_homepage": "...", "purchase_for_download": "..."}
    ("url_relations",              "TEXT"),       # JSON dict
    # Discogs additions (filled when Discogs provider is enabled)
    ("discogs_master_id",          "TEXT"),
    ("discogs_artist_id",          "TEXT"),
    ("discogs_label_id",           "TEXT"),
    # Acoustid fingerprint (filled when AcoustID lookup is enabled later)
    ("acoustid_id",                "TEXT"),
    ("acoustid_fingerprint",       "TEXT"),
    # --- Picard-aligned extensions (added v0.18) -------------------------
    # Field names match what Picard writes so downstream tools that read
    # our files (foobar2000, beets, Roon, etc.) recognise them. See
    # picard-docs.musicbrainz.org for the canonical list. These fill from
    # MB deep-harvest when available, or from existing file tags via
    # mutagen during indexing.
    ("artists",              "TEXT"),    # multi-value artist list, semicolon-separated
    ("albumartistsort",      "TEXT"),    # "Beatles, The"
    ("artistsort",           "TEXT"),
    ("albumsort",            "TEXT"),
    ("titlesort",            "TEXT"),
    ("composersort",         "TEXT"),
    ("asin",                 "TEXT"),    # Amazon Standard Identification
    ("discsubtitle",         "TEXT"),
    ("totaldiscs",           "INTEGER"),
    ("totaltracks",           "INTEGER"),
    ("originaldate",         "TEXT"),    # earliest release in release group, YYYY-MM-DD
    ("originalyear",         "TEXT"),
    ("originalalbum",        "TEXT"),
    ("originalartist",       "TEXT"),
    ("releasedate",          "TEXT"),    # release date explicitly (Picard 2.9+)
    ("musicbrainz_discid",   "TEXT"),
    ("musicbrainz_originalalbumid",  "TEXT"),
    ("musicbrainz_originalartistid", "TEXT"),
    ("musicbrainz_composerid",       "TEXT"),
    ("website",              "TEXT"),    # official artist site
    ("copyright",            "TEXT"),
    ("license",              "TEXT"),
    ("encodedby",            "TEXT"),    # encoder app/name (e.g. "LAME3.100", "FLAC 1.3.4")
    ("encodersettings",      "TEXT"),    # full encoder settings string
    ("arranger",             "TEXT"),
    ("conductor",            "TEXT"),
    ("director",             "TEXT"),
    ("djmixer",              "TEXT"),
    ("engineer",             "TEXT"),
    ("mixer",                "TEXT"),
    ("remixer",              "TEXT"),
    ("writer",               "TEXT"),
    ("work",                 "TEXT"),
    ("showmovement",         "TEXT"),
    ("movement",             "TEXT"),
    ("movementnumber",       "TEXT"),
    ("movementtotal",        "TEXT"),
    ("subtitle",             "TEXT"),
    ("lyrics",               "TEXT"),
    ("syncedlyrics",         "TEXT"),
    ("originalfilename",     "TEXT"),
    # --- Rip detection (v0.18) -------------------------------------------
    # Populated by detect_rip_software() during indexing OR by the
    # explicit rip-detection menu. Stored separately from `encodedby` so
    # we keep the original tag intact AND have our normalised guess.
    ("ripper_software",      "TEXT"),    # e.g. 'EAC' / 'XLD' / 'dBpoweramp' / 'LAME'
    ("ripper_version",       "TEXT"),    # e.g. 'V1.6 Beta 1' / '3.100'
    ("ripper_settings",      "TEXT"),    # raw settings string (CBR320, VBR V0, etc.)
    ("ripper_confidence",    "REAL"),    # 0.0..1.0 — how sure we are about the detection
    # --- organisation outcome --------------------------------------------
    ("organised_path",    "TEXT"),              # where we put it (or planned to)
    ("album_type",        "TEXT"),              # 'solo' | 'mix' | 'unknown'
    ("quality_tier",      "TEXT"),              # 'high' | 'low' | 'broken'
    ("status",            "TEXT"),              # 'indexed' | 'imported' | 'broken' | 'duplicate'
    # --- catch-all --------------------------------------------------------
    ("tags_raw",          "TEXT"),              # JSON dump of every tag mutagen returned
    # --- bookkeeping -----------------------------------------------------
    ("first_seen",        "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ("last_seen",         "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
]

# Columns that user code can write to (everything except autogenerated ones).
WRITABLE_COLUMNS = [
    name for name, _ in FILE_COLUMNS
    if name not in ("first_seen",)  # set by DEFAULT
]

# Columns that should be JSON-encoded on write if the caller passes a list/dict.
JSON_COLUMNS = {"tags_raw"}


# Indexes — what we'll actually filter/sort on in practice.
INDEXES: list[tuple[str, str]] = [
    ("idx_files_artist",        "files(artist)"),
    ("idx_files_albumartist",   "files(albumartist)"),
    ("idx_files_label",         "files(label)"),
    ("idx_files_album",         "files(album)"),
    ("idx_files_hash",          "files(content_hash)"),
    ("idx_files_status",        "files(status)"),
    ("idx_files_album_type",    "files(album_type)"),
    ("idx_files_codec",         "files(codec)"),
    ("idx_files_year",          "files(year)"),
    # for audit & FLAC verify queries
    ("idx_files_lossless",        "files(lossless)"),
    ("idx_files_has_embedded_art","files(has_embedded_art)"),
    ("idx_files_transcode_susp",  "files(transcode_suspected)"),
    ("idx_files_transcode_chk",   "files(transcode_checked)"),
]


class Database:
    """Thin wrapper over sqlite3 with our schema baked in."""

    def __init__(self, path: str | Path, pragmas: dict[str, Any] | None = None):
        # Special path ":memory:" creates an in-memory ephemeral database.
        # Used by the importer when the user opts out of persisting to
        # their real library DB ("Organise without database" mode).
        if str(path) == ":memory:":
            self.path = Path(":memory:")
            self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            self.path = Path(path).expanduser()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False so a single Database instance can be
            # used from a worker thread later if we add parallel imports.
            # We protect writes ourselves with explicit transactions.
            self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        # Cache for built upsert SQL, keyed by column-tuple. See
        # upsert_file. Cleared implicitly when the object is GC'd.
        self._upsert_sql_cache: dict[tuple[str, ...], str] = {}

        self._apply_pragmas(pragmas or {})
        self._create_schema()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _apply_pragmas(self, pragmas: dict[str, Any]) -> None:
        # In-memory databases reject WAL — they use MEMORY journal mode
        # by default and that's fine; we have nothing to recover anyway.
        is_memory = (str(self.path) == ":memory:")
        defaults = {
            "journal_mode": "MEMORY" if is_memory else "WAL",
            "synchronous": "NORMAL",
            "cache_size": -65536,    # negative = KB
            "temp_store": "MEMORY",
        }
        # Translate config-style pragmas to sqlite-style.
        if "cache_size_kb" in pragmas:
            defaults["cache_size"] = -int(pragmas["cache_size_kb"])
        for k in ("journal_mode", "synchronous", "temp_store"):
            if k in pragmas and not (is_memory and k == "journal_mode"):
                # Don't let user-supplied journal_mode override our forced
                # MEMORY for in-memory databases.
                defaults[k] = pragmas[k]
        for k, v in defaults.items():
            # PRAGMA values can't be parameterised, sadly.
            self.conn.execute(f"PRAGMA {k} = {v}")

    def _create_schema(self) -> None:
        cols_sql = ",\n  ".join(f"{name} {typ}" for name, typ in FILE_COLUMNS)
        self.conn.execute(f"CREATE TABLE IF NOT EXISTS files (\n  {cols_sql}\n)")

        # Migrate: if an old DB exists and is missing any columns we now
        # expect, ALTER TABLE them in. SQLite only supports ADD COLUMN with
        # no DEFAULT changes, which is fine here.
        existing = {row["name"] for row in
                    self.conn.execute("PRAGMA table_info(files)")}
        for name, typ in FILE_COLUMNS:
            if name not in existing:
                # Strip "PRIMARY KEY" / "DEFAULT ..." for ADD COLUMN —
                # sqlite won't accept PRIMARY KEY in ALTER, and a fresh
                # column doesn't need a default.
                base_typ = typ.split()[0]
                try:
                    self.conn.execute(f"ALTER TABLE files ADD COLUMN {name} {base_typ}")
                except sqlite3.OperationalError:
                    # Already exists in a parallel process, or some other
                    # benign race. Move on.
                    pass

        for idx_name, idx_def in INDEXES:
            self.conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_def}")

        self.conn.commit()

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self):
        """
        Usage:
            with db.transaction():
                for f in files:
                    db.upsert_file(f)
        Rolls back on exception, commits on success.
        """
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def commit(self) -> None:
        """
        Explicitly commit the current transaction. SQLite auto-commits
        when the WAL grows past the checkpoint threshold, so calling
        this isn't normally necessary — the WAL is durable on its own.

        Useful, however, for "commit per file" mode in long-running
        operations: each commit() forces the row into the main DB file
        rather than leaving it in the WAL. Trade-off:
          - PRO: a crash mid-run leaves you with EVERY tagged file
            visible in the main DB, even before the next checkpoint.
          - CON: ~50ms-2s extra per commit on USB-attached HDD due to
            fsync(). For a 200k-file run this dominates the wall time.

        Use sparingly — once per file is overkill, once per album is
        fine.
        """
        self.conn.commit()

    def upsert_file(self, record: dict[str, Any]) -> None:
        """
        Insert or replace a row by `path`.

        `record` can contain any subset of WRITABLE_COLUMNS. Unknown keys
        are dropped silently (a warning would spam during import if a tag
        is unexpectedly weird).
        """
        if "path" not in record:
            raise ValueError("record must contain 'path'")

        # Normalise: JSON-encode catch-all dict columns.
        clean: dict[str, Any] = {}
        for k, v in record.items():
            if k not in WRITABLE_COLUMNS:
                continue
            if k in JSON_COLUMNS and not isinstance(v, str) and v is not None:
                try:
                    clean[k] = json.dumps(v, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    clean[k] = json.dumps(str(v))
            else:
                clean[k] = v

        # Always bump last_seen on every upsert. Generate the timestamp
        # in Python rather than via `SELECT CURRENT_TIMESTAMP` — the old
        # code issued an extra SQLite round-trip on EVERY upsert (200k+
        # times during a full fetch). datetime.utcnow formatted to match
        # SQLite's CURRENT_TIMESTAMP format ('YYYY-MM-DD HH:MM:SS') keeps
        # existing rows comparable.
        clean["last_seen"] = _utcnow_sqlite()

        cols = list(clean.keys())
        # The INSERT...ON CONFLICT SQL only depends on WHICH columns are
        # present, not their values. During a fetch run the same column
        # set recurs constantly (e.g. every album fills the same fields),
        # so cache the built statement keyed by the column tuple. Saves
        # rebuilding three joined strings on every one of 200k+ upserts.
        cache_key = tuple(cols)
        sql = self._upsert_sql_cache.get(cache_key)
        if sql is None:
            placeholders = ", ".join("?" for _ in cols)
            col_list = ", ".join(cols)
            # Columns that should be set ONCE on first insert and never
            # overwritten by later upserts. `COALESCE(existing, new)`
            # keeps the existing value when present; falls back to the
            # incoming value only if NULL. Lets first-import populate
            # them and all subsequent upserts ignore them.
            _SET_ONCE = ("original_path", "original_filename")
            update_parts = []
            for c in cols:
                if c == "path":
                    continue
                if c in _SET_ONCE:
                    update_parts.append(f"{c}=COALESCE({c}, excluded.{c})")
                else:
                    update_parts.append(f"{c}=excluded.{c}")
            update_clause = ", ".join(update_parts)
            sql = (
                f"INSERT INTO files ({col_list}) VALUES ({placeholders}) "
                f"ON CONFLICT(path) DO UPDATE SET {update_clause}"
            )
            self._upsert_sql_cache[cache_key] = sql
        self.conn.execute(sql, [clean[c] for c in cols])

    def delete_by_path(self, path: str) -> int:
        cur = self.conn.execute("DELETE FROM files WHERE path = ?", (path,))
        self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_by_path(self, path: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
        return dict(row) if row else None

    def find_by_hash(self, content_hash: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM files WHERE content_hash = ?", (content_hash,)
        ).fetchall()
        return [dict(r) for r in rows]

    def find_album_tracks(self, source_album_dir: str) -> list[dict[str, Any]]:
        """
        Return all rows whose `path` lives directly inside the given
        directory. Used by the importer to look at a whole album at once
        (for the solo-vs-mix decision).
        """
        prefix = str(source_album_dir).rstrip(os.sep) + os.sep
        rows = self.conn.execute(
            "SELECT * FROM files WHERE path LIKE ? || '%' "
            "AND instr(substr(path, length(?) + 1), '/') = 0",
            (prefix, prefix),
        ).fetchall()
        return [dict(r) for r in rows]

    def iter_all(self, batch_size: int = 1000) -> Iterator[dict[str, Any]]:
        cursor = self.conn.execute("SELECT * FROM files")
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            for r in rows:
                yield dict(r)

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    def stats(self) -> dict[str, Any]:
        """Quick summary for the menu / dashboard."""
        c = self.conn.cursor()
        total = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        by_codec = dict(c.execute(
            "SELECT codec, COUNT(*) FROM files GROUP BY codec"
        ).fetchall())
        by_status = dict(c.execute(
            "SELECT status, COUNT(*) FROM files GROUP BY status"
        ).fetchall())
        total_size = c.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM files"
        ).fetchone()[0]
        artists = c.execute(
            "SELECT COUNT(DISTINCT primary_artist) FROM files WHERE primary_artist IS NOT NULL"
        ).fetchone()[0]
        labels = c.execute(
            "SELECT COUNT(DISTINCT label) FROM files WHERE label IS NOT NULL"
        ).fetchone()[0]
        albums = c.execute(
            "SELECT COUNT(DISTINCT album) FROM files WHERE album IS NOT NULL"
        ).fetchone()[0]
        return {
            "total_files": total,
            "total_size_bytes": total_size,
            "by_codec": by_codec,
            "by_status": by_status,
            "distinct_artists": artists,
            "distinct_labels": labels,
            "distinct_albums": albums,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")

    def analyse(self) -> None:
        self.conn.execute("ANALYZE")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
