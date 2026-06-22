"""
metadata_providers.py
=====================

Pluggable metadata-source providers, modelled on OneTagger's
`AutotaggerSource` trait pattern (see crates/onetagger-tagger/src/lib.rs
in the upstream Rust source).

Each provider implements:

    class Provider:
        id: str              # unique short id, e.g. "itunes"
        name: str            # display name
        requires_auth: bool  # does the user need to supply a token/key?
        supported_tags: list[str]  # which DB columns this source can fill

        def configure(self, cfg, ask) -> bool:
            # Set up auth/config. Use the `ask` callback to prompt the
            # user for missing values. Return False to disable this
            # provider (e.g. auth declined).

        def search_release(self, artist, album) -> list[Release]:
            # Search the platform for releases matching (artist, album).
            # Return ranked list, best first.

        def search_track(self, artist, title) -> list[Track]:
            # Optional. Search for a specific recording. Used by BPM
            # lookup. Default impl returns [].

        def fetch_cover(self, release, out_path) -> bool:
            # Download front cover art into out_path. Default impl
            # returns False (no cover support).

A `Release` is a dataclass with normalised tag-like fields. A `Track`
adds per-recording details (BPM, key, ISRC, duration).

To add a new platform: write a class implementing the methods above and
register it in `ALL_PROVIDERS`. The cmd_fetch_metadata flow picks it
up automatically.

What's implemented today:
    - MusicBrainz (no auth) — broad coverage, the canonical open DB
    - iTunes Search (no auth) — Apple's public search API
    - Deezer (no auth) — straightforward JSON API
    - Bandcamp (no auth) — JSON-LD scraping from public release pages

Deferred (each needs significant per-platform work):
    - Beatport / Beatsource — Cloudflare bypass + private API
    - Spotify — OAuth2 client credentials flow
    - BPM Supreme — paid account login
    - Shazam — audio fingerprinting algorithm (sub-project)
    - Discogs — needs personal access token; infrastructure is here
                via the config-on-demand pattern, just no client yet
    - Musixmatch — lyrics scraping, ToS-greyzone

The config-on-demand pattern (`cfg_ask` in organiser.py) means when one
of these IS added later, it'll prompt for the auth at first use and
persist it — no manual config editing needed.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata as _unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher as _SequenceMatcher
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode as _urlencode
from urllib.request import Request, urlopen

# Precompiled patterns for normalise_for_match (hot path — runs per
# search result during scoring).
_NONWORD_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")

# Shared User-Agent for all outbound requests. MusicBrainz and Discogs
# both throttle generic UA strings harder, and MB's ToS requires a
# meaningful UA. Versioned so server-side logs can correlate issues to
# a release. (Kept as a module constant rather than reading __version__
# to avoid a circular import with organiser.py.)
_USER_AGENT = "music-organiser/0.23 (+https://github.com/anon/music-organiser)"


logger = logging.getLogger("music-organiser")


# =============================================================================
# DATA SHAPES (modelled on OneTagger's Track struct, simplified to fit our
# DB schema and to be JSON-serialisable so we can cache them)
# =============================================================================

@dataclass
class Release:
    """One album/EP/single from a metadata provider. The fields here
    map 1:1 to columns in our `files` table (so writing back is a flat
    dict update). Empty string means "not provided by this source"."""
    platform: str = ""
    artist: str = ""
    album: str = ""
    year: str = ""
    label: str = ""
    catalog_number: str = ""
    country: str = ""
    barcode: str = ""
    genre: str = ""
    release_id: str = ""           # provider-specific ID (mbid, deezer id...)
    url: str = ""                  # public URL for this release
    art_url: str = ""              # direct image URL (we download later)
    score: float = 0.0             # 0..100, ranking from provider's own ranker
    # --- archival extras (filled by MusicBrainzProvider in deep_harvest mode)
    # Default empty; only the MB deep harvest currently populates these.
    musicbrainz_releasegroupid: str = ""
    musicbrainz_albumartistid:  str = ""
    musicbrainz_labelid:        str = ""
    release_status: str = ""       # 'official' / 'promotion' / 'bootleg'
    release_type:   str = ""       # 'album' / 'single' / 'compilation'
    language:       str = ""       # ISO 639-3 like 'eng'
    script:         str = ""       # ISO 15924 like 'Latn'
    packaging:      str = ""
    media_format:   str = ""       # 'CD' / 'Vinyl' / 'Digital Media'
    media_track_count: int = 0
    annotation:     str = ""       # MB's free-text annotation
    aliases:        list[str] = field(default_factory=list)   # artist + album aliases
    mb_tags:        list[str] = field(default_factory=list)   # folksonomy tags
    mb_genres:      list[str] = field(default_factory=list)   # curated genres
    url_relations:  dict[str, str] = field(default_factory=dict)  # {wikipedia, discogs, ...}
    # Per-recording (track-level) data — list of dicts, one per track,
    # in track order. When populated by MB deep_harvest each entry has:
    # {track_number, title, length_ms, recording_id, work_id, isrc[]}
    tracks: list[dict[str, Any]] = field(default_factory=list)

    def to_tag_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        # Map provider field -> DB column. Both happen to share names
        # here; if we add per-platform IDs (e.g. mb_release_id) we'd
        # do that mapping per-provider before writing.
        for f in ("artist", "album", "year", "label", "catalog_number",
                  "country", "barcode", "genre"):
            v = getattr(self, f, "")
            if v:
                out[f] = v
        return out


@dataclass
class TrackInfo:
    """One specific recording. Extends Release with per-track fields.
    BPM is the headline reason this exists; OneTagger's DJ-focused
    platforms (Beatport, Beatsource, BPM Supreme) populate BPM here.
    For our currently-implemented free no-auth platforms BPM is rare,
    but the plumbing is in place."""
    platform: str = ""
    artist: str = ""
    title: str = ""
    album: str = ""
    duration_ms: int = 0
    bpm: int = 0           # 0 = unknown
    key: str = ""          # musical key (e.g. "C minor", "5A")
    genre: str = ""
    isrc: str = ""
    track_id: str = ""
    release_id: str = ""
    score: float = 0.0
    release: Release | None = None


# =============================================================================
# PROVIDER BASE CLASS
# =============================================================================
#
# Modelled on OneTagger's AutotaggerSource trait. Each concrete provider
# overrides what it supports and inherits no-op defaults for the rest.

class Provider:
    id: str = ""
    name: str = ""
    requires_auth: bool = False
    rate_limit_seconds: float = 1.0  # min seconds between requests

    # Which `files` table columns can this provider populate?
    supported_tags: list[str] = []

    def __init__(self) -> None:
        self._last_request: float = 0.0
        self._enabled = True

    # ----- safety guard for outbound queries -----
    # Strings we MUST refuse to send to any provider's search API.
    # Sending "Unknown Artist" / "Unknown Album" wastes rate-limit
    # budget on guaranteed-misses, and worse, can accidentally match
    # the literal MB entries that exist for those placeholder names —
    # contaminating real-track records with bogus tags.
    _UNKNOWN_QUERY_VALUES = frozenset({
        "", "unknown", "unknown artist", "unknown album", "unknown title",
        "<unknown>", "untitled", "n/a", "none", "(none)",
        "no artist", "no album", "tba", "tbd",
        "track", "audio track", "[unknown]", "[unknown artist]",
        "[unknown album]",
    })

    @classmethod
    def is_safe_query(cls, artist: str, album: str) -> bool:
        """True if (artist, album) is non-empty and neither field is a
        recognised placeholder. Callers MUST check this before invoking
        search_release. See _UNKNOWN_QUERY_VALUES for the reject set."""
        a = (artist or "").strip().lower()
        al = (album or "").strip().lower()
        if not a or not al:
            return False
        if a in cls._UNKNOWN_QUERY_VALUES:
            return False
        if al in cls._UNKNOWN_QUERY_VALUES:
            return False
        return True

    def configure(self, cfg: dict[str, Any], ask: Callable[..., str]) -> bool:
        """Default: providers without auth are always ready."""
        return True

    def rate_limit(self) -> None:
        """Sleep enough to honour our rate limit."""
        now = time.monotonic()
        elapsed = now - self._last_request
        if elapsed < self.rate_limit_seconds:
            time.sleep(self.rate_limit_seconds - elapsed)
        self._last_request = time.monotonic()

    def http_get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
    ) -> Any:
        """Common HTTP helper. Rate-limits, GETs, parses JSON."""
        self.rate_limit()
        if params:
            url = f"{url}?{_urlencode(params)}"
        h = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
        if headers:
            h.update(headers)
        req = Request(url, headers=h)
        try:
            with urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except HTTPError as e:
            if e.code == 429 or e.code == 503:
                # Rate-limit response: back off, retry once
                logger.warning("%s rate limit hit, sleeping 5s", self.id)
                time.sleep(5)
                self.rate_limit()
                with urlopen(req, timeout=timeout) as r:
                    raw = r.read().decode("utf-8", errors="replace")
                return json.loads(raw)
            raise

    def http_get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
    ) -> str:
        """For HTML/text endpoints (Bandcamp's release pages)."""
        self.rate_limit()
        h = {"User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) "
            "Gecko/20100101 Firefox/120.0"
        )}
        if headers:
            h.update(headers)
        req = Request(url, headers=h)
        with urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")

    # --- Methods subclasses override ----------------------------------------

    def search_release(self, artist: str, album: str) -> list[Release]:
        return []

    def search_track(self, artist: str, title: str) -> list[TrackInfo]:
        return []

    def fetch_cover(self, release: Release, out_path: Path) -> bool:
        """Download front cover art. Default: skip if art_url is empty."""
        if not release.art_url:
            return False
        if out_path.exists():
            return False
        try:
            self.rate_limit()
            req = Request(release.art_url, headers={"User-Agent": _USER_AGENT})
            with urlopen(req, timeout=30) as r:
                data = r.read()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "wb") as fp:
                fp.write(data)
            return True
        except (HTTPError, URLError, TimeoutError) as e:
            logger.warning("%s cover fetch failed: %s", self.id, e)
            return False


# =============================================================================
# UTILS — string matching, score normalising. OneTagger's `MatchingUtils`
# is a complex bit of Rust; we keep ours simple. Good-enough for "did MB
# return the album I asked for".
# =============================================================================

def normalise_for_match(s: str) -> str:
    """Lowercase, strip punctuation/diacritics for fuzzy comparison."""
    if not s:
        return ""
    # Strip combining diacritics. unicodedata is imported at module
    # level (_unicodedata); the regexes are precompiled (_NONWORD_RE,
    # _WS_RE) — this runs twice per similarity call, thousands of times
    # per fetch run.
    s = _unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not _unicodedata.combining(c))
    s = s.lower()
    s = _NONWORD_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def string_similarity(a: str, b: str) -> float:
    """Levenshtein-based similarity, 0..1. No external dep — uses stdlib."""
    if not a or not b:
        return 0.0
    a, b = normalise_for_match(a), normalise_for_match(b)
    if a == b:
        return 1.0
    # SequenceMatcher imported at module level (_SequenceMatcher) — this
    # function is called once per search result, potentially thousands
    # of times per run, so the import must not be in the hot path.
    return _SequenceMatcher(None, a, b).ratio()


# =============================================================================
# MUSICBRAINZ — the open canonical DB. No auth required, but a hard
# 1 req/sec rate limit. Returns deep release metadata including labels
# and catalog numbers, which is what we mostly care about.
# =============================================================================

class MusicBrainzProvider(Provider):
    id = "musicbrainz"
    name = "MusicBrainz"
    requires_auth = False
    rate_limit_seconds = 1.05
    supported_tags = [
        "artist", "album", "year", "label", "catalog_number",
        "country", "barcode", "release_id",
    ]

    BASE = "https://musicbrainz.org/ws/2"
    CAA = "https://coverartarchive.org"

    # When True, every match triggers a second /release/{mbid}?inc=...
    # query to harvest every field MB has. Costs an extra ~1.05s per
    # matched album. Set via configure() from the cfg flag, or by passing
    # to constructor.
    DEEP_INC = (
        "labels+recordings+release-groups+artist-credits+isrcs"
        "+url-rels+aliases+annotation+tags+genres+media"
    )

    def __init__(self, deep_harvest: bool = False) -> None:
        super().__init__()
        self.deep_harvest = deep_harvest

    def configure(self, cfg: dict[str, Any], ask) -> bool:
        # Pull the deep_harvest flag from config. Defaults to True
        # because the user explicitly asked for archival-grade coverage.
        prov_cfg = (cfg.get("providers") or {}).get("musicbrainz") or {}
        self.deep_harvest = bool(prov_cfg.get("deep_harvest", True))
        return True

    def search_release(self, artist: str, album: str) -> list[Release]:
        # Provider-level guard: refuse to send placeholders to the API.
        # See Provider._UNKNOWN_QUERY_VALUES for the full reject set.
        if not Provider.is_safe_query(artist, album):
            return []
        # MB Lucene-style query
        q = f'artist:"{artist}" AND release:"{album}"'
        try:
            data = self.http_get_json(
                f"{self.BASE}/release/",
                params={"query": q, "fmt": "json", "limit": "10"},
            )
        except Exception as e:
            logger.warning("MB search failed for %r / %r: %s", artist, album, e)
            return []

        out: list[Release] = []
        for r in data.get("releases", []):
            try:
                # Artist string: join all artist-credit entries
                ac = r.get("artist-credit", [])
                artist_str = "".join(
                    (a.get("name", "") if isinstance(a, dict) else "")
                    + (a.get("joinphrase", "") if isinstance(a, dict) else "")
                    for a in ac
                ).strip()

                # Label / catalog from label-info
                label_str = ""
                cat_str = ""
                label_mbid = ""
                for li in r.get("label-info", []) or []:
                    if "label" in li and isinstance(li["label"], dict):
                        label_str = label_str or li["label"].get("name", "")
                        label_mbid = label_mbid or li["label"].get("id", "")
                    if "catalog-number" in li:
                        cat_str = cat_str or li.get("catalog-number", "")

                # Year
                date_str = r.get("date", "") or ""
                year_str = date_str.split("-")[0] if date_str else ""

                # Artist MBID — first credit
                aa_mbid = ""
                if ac and isinstance(ac[0], dict) and "artist" in ac[0]:
                    aa_mbid = ac[0]["artist"].get("id", "") or ""

                rel = Release(
                    platform=self.id,
                    artist=artist_str,
                    album=r.get("title", "") or "",
                    year=year_str,
                    label=label_str,
                    catalog_number=cat_str,
                    country=r.get("country", "") or "",
                    barcode=r.get("barcode", "") or "",
                    release_id=r.get("id", "") or "",
                    score=float(r.get("score", 0) or 0),
                    musicbrainz_labelid=label_mbid,
                    musicbrainz_albumartistid=aa_mbid,
                    release_status=r.get("status", "") or "",
                    media_format=", ".join(
                        m.get("format", "") for m in (r.get("media") or [])
                        if isinstance(m, dict) and m.get("format")
                    ),
                    media_track_count=sum(
                        int(m.get("track-count", 0) or 0)
                        for m in (r.get("media") or [])
                        if isinstance(m, dict)
                    ),
                )
                # Cover Art Archive lookup URL — we don't fetch yet, just store
                if rel.release_id:
                    rel.url = f"https://musicbrainz.org/release/{rel.release_id}"
                    rel.art_url = f"{self.CAA}/release/{rel.release_id}/front"
                out.append(rel)
            except Exception as e:
                logger.warning("MB release parse failed: %s", e)
                continue

        out.sort(key=lambda r: r.score, reverse=True)

        # Deep harvest: take the top result and re-fetch with full inc.
        # We only do this for the winner because each fetch costs ~1.05s.
        # All shallow data already in `out`, we just enrich the [0] one.
        if self.deep_harvest and out and out[0].release_id:
            try:
                enriched = self._deep_fetch(out[0].release_id)
                if enriched:
                    # Merge new fields into the existing top release object,
                    # preserving the score
                    for f_name in ("musicbrainz_releasegroupid", "release_type",
                                   "language", "script", "packaging",
                                   "annotation", "aliases", "mb_tags",
                                   "mb_genres", "url_relations", "tracks"):
                        v = getattr(enriched, f_name)
                        if v:
                            setattr(out[0], f_name, v)
                    # Genre from MB curated list, fallback to first tag
                    if enriched.mb_genres and not out[0].genre:
                        out[0].genre = enriched.mb_genres[0]
                    elif enriched.mb_tags and not out[0].genre:
                        out[0].genre = enriched.mb_tags[0]
            except Exception as e:
                logger.warning("MB deep harvest failed for %s: %s",
                               out[0].release_id, e)

        return out

    def search_by_recording(self, artist: str, track_title: str) -> list[Release]:
        """
        Search MB by RECORDING (track) rather than RELEASE (album).

        Used as a fallback by the fetch loop when album-level queries
        miss. Many compilations have their individual tracks indexed
        in MB even when the album release isn't easily findable by its
        full title — querying by `recording:"Spilt Personality" AND
        artist:"Filthy Habits"` returns the recordings, and each result
        includes the releases (compilation albums) it appears on.

        Returns a list of Release stubs — `release_id` (MBID) and
        `album` (release title) populated, other fields empty. The
        caller is expected to deduplicate by release_id and decide
        which release to fully fetch.

        Cheap: one MB request per call. Use sparingly — we trigger this
        only after the full album-level fallback chain has missed.
        """
        # Reuse the same placeholder rejection logic. We treat
        # track_title as if it were "album" for the safety check —
        # both are user-facing text fields and the placeholder set
        # ("Unknown Track", "Untitled") applies equally.
        if not Provider.is_safe_query(artist, track_title):
            return []
        q = f'artist:"{artist}" AND recording:"{track_title}"'
        try:
            data = self.http_get_json(
                f"{self.BASE}/recording/",
                params={"query": q, "fmt": "json", "limit": "5"},
            )
        except Exception as e:
            logger.warning("MB recording search failed for %r / %r: %s",
                            artist, track_title, e)
            return []

        out: list[Release] = []
        for rec in data.get("recordings", []) or []:
            try:
                rec_score = float(rec.get("score", 0) or 0)
                # Each recording has a list of releases it appears on
                for rel in rec.get("releases", []) or []:
                    rel_id = rel.get("id", "") or ""
                    rel_title = rel.get("title", "") or ""
                    if not rel_id or not rel_title:
                        continue
                    # Release status: "Official" releases are way more
                    # likely to match what the user has on disk than
                    # bootleg/promo entries. Use it as a tiebreaker.
                    status = rel.get("status", "") or ""
                    status_bonus = 5.0 if status.lower() == "official" else 0.0
                    out.append(Release(
                        platform=self.id,
                        release_id=rel_id,
                        album=rel_title,
                        score=rec_score + status_bonus,
                        release_status=status,
                    ))
            except Exception:
                continue
        return out

    def fetch_release_by_id(self, release_mbid: str) -> Release | None:
        """
        Fetch a specific MB release by its MBID, without going through
        the text-search path. Used by the per-recording-sampling
        fallback to materialise a release once we've voted on it from
        track-level results.

        Honours deep_harvest if enabled. Returns None on failure.
        """
        if not release_mbid:
            return None
        try:
            data = self.http_get_json(
                f"{self.BASE}/release/{release_mbid}",
                params={"fmt": "json",
                        "inc": "labels+artist-credits+release-groups"},
            )
        except Exception as e:
            logger.warning("MB fetch_by_id %s failed: %s", release_mbid, e)
            return None
        try:
            ac = data.get("artist-credit", []) or []
            artist_str = "".join(
                (a.get("name", "") if isinstance(a, dict) else "")
                + (a.get("joinphrase", "") if isinstance(a, dict) else "")
                for a in ac
            ).strip()
            label_str = ""
            cat_str = ""
            for li in data.get("label-info", []) or []:
                if "label" in li and isinstance(li["label"], dict):
                    label_str = label_str or li["label"].get("name", "")
                if "catalog-number" in li:
                    cat_str = cat_str or li.get("catalog-number", "")
            year_str = ""
            d = data.get("date", "") or ""
            if d:
                year_str = d.split("-")[0]
            rel = Release(
                platform=self.id,
                artist=artist_str,
                album=data.get("title", "") or "",
                year=year_str,
                label=label_str,
                catalog_number=cat_str,
                country=data.get("country", "") or "",
                barcode=data.get("barcode", "") or "",
                release_id=release_mbid,
                score=100.0,    # exact MBID match → max confidence
            )
        except Exception as e:
            logger.warning("MB fetch_by_id parse %s: %s", release_mbid, e)
            return None
        # Deep-harvest enrichment, mirroring the search-winner path
        if self.deep_harvest:
            try:
                enriched = self._deep_fetch(release_mbid)
                if enriched:
                    for f_name in ("musicbrainz_releasegroupid", "release_type",
                                    "language", "script", "packaging",
                                    "annotation", "aliases", "mb_tags",
                                    "mb_genres", "url_relations", "tracks"):
                        v = getattr(enriched, f_name)
                        if v:
                            setattr(rel, f_name, v)
                    if enriched.mb_genres and not rel.genre:
                        rel.genre = enriched.mb_genres[0]
                    elif enriched.mb_tags and not rel.genre:
                        rel.genre = enriched.mb_tags[0]
            except Exception:
                pass
        return rel

    def fetch_releases_for_recording_id(self, recording_id: str) -> list[Release]:
        """
        Given a MusicBrainz recording ID (e.g. from AcoustID), return the
        releases that recording appears on as stubs. Caller picks the best
        and fully fetches it via fetch_release_by_id.

        Official releases are scored higher than promos/bootlegs.
        """
        if not recording_id:
            return []
        self.rate_limit()
        try:
            data = self.http_get_json(
                f"{self.BASE}/recording/{recording_id}",
                params={"fmt": "json", "inc": "releases"},
            )
        except Exception as e:
            logger.warning("MB recording lookup %s failed: %s", recording_id, e)
            return []

        out: list[Release] = []
        try:
            for rel in data.get("releases", []) or []:
                rel_id = rel.get("id", "") or ""
                if not rel_id:
                    continue
                status = rel.get("status", "") or ""
                out.append(Release(
                    platform=self.id,
                    release_id=rel_id,
                    album=rel.get("title", "") or "",
                    score=90.0 if status.lower() == "official" else 70.0,
                    release_status=status,
                ))
        except Exception as e:
            logger.warning("MB recording parse %s: %s", recording_id, e)
        return out

    def _deep_fetch(self, release_mbid: str) -> Release | None:
        """
        Pull every available field for one release. Used by deep_harvest.

        MB's response with inc=labels+recordings+release-groups+artist-credits
        +isrcs+url-rels+aliases+annotation+tags+genres+media is enormous
        (~50-200KB JSON per release). We extract what maps onto our schema,
        ignore the rest.
        """
        try:
            data = self.http_get_json(
                f"{self.BASE}/release/{release_mbid}",
                params={"fmt": "json", "inc": self.DEEP_INC},
            )
        except Exception as e:
            logger.warning("MB deep fetch %s: %s", release_mbid, e)
            return None

        rel = Release(platform=self.id, release_id=release_mbid)

        # Release-group: 'album', 'single', 'compilation', 'soundtrack'...
        rg = data.get("release-group") or {}
        if isinstance(rg, dict):
            rel.musicbrainz_releasegroupid = rg.get("id", "") or ""
            rel.release_type = (
                rg.get("primary-type", "") or
                ", ".join(rg.get("secondary-types", []) or [])
            )

        # Text representation: language + script
        tr = data.get("text-representation") or {}
        if isinstance(tr, dict):
            rel.language = tr.get("language", "") or ""
            rel.script   = tr.get("script", "") or ""

        rel.packaging  = data.get("packaging", "") or ""
        rel.annotation = data.get("annotation", "") or ""

        # Aliases (artist + album)
        aliases = []
        for a in data.get("aliases", []) or []:
            if isinstance(a, dict) and a.get("name"):
                aliases.append(a["name"])
        for ac in data.get("artist-credit", []) or []:
            if isinstance(ac, dict) and "artist" in ac:
                for al in ac["artist"].get("aliases", []) or []:
                    if isinstance(al, dict) and al.get("name"):
                        aliases.append(al["name"])
        rel.aliases = aliases

        # Folksonomy tags (community-applied)
        rel.mb_tags = [
            t.get("name", "") for t in (data.get("tags") or [])
            if isinstance(t, dict) and t.get("name")
        ]
        # Curated genres (subset of tags, vetted)
        rel.mb_genres = [
            g.get("name", "") for g in (data.get("genres") or [])
            if isinstance(g, dict) and g.get("name")
        ]

        # URL relationships: Wikipedia, Discogs, Bandcamp, Spotify...
        url_rels: dict[str, str] = {}
        for relationship in data.get("relations", []) or []:
            if not isinstance(relationship, dict):
                continue
            rtype = relationship.get("type", "")
            url_obj = relationship.get("url", {})
            if isinstance(url_obj, dict):
                resource = url_obj.get("resource", "")
                if rtype and resource:
                    url_rels[rtype] = resource
        rel.url_relations = url_rels

        # Per-track data: track number, title, length, recording_id, isrcs
        tracks: list[dict[str, Any]] = []
        for medium in data.get("media", []) or []:
            if not isinstance(medium, dict):
                continue
            for tr in medium.get("tracks", []) or []:
                if not isinstance(tr, dict):
                    continue
                recording = tr.get("recording") or {}
                tracks.append({
                    "track_number": tr.get("number") or tr.get("position", ""),
                    "title":        tr.get("title") or recording.get("title", ""),
                    "length_ms":    int(tr.get("length", 0) or 0),
                    "recording_id": recording.get("id", "") if isinstance(recording, dict) else "",
                    "isrcs":        recording.get("isrcs", []) if isinstance(recording, dict) else [],
                })
        rel.tracks = tracks
        return rel


# =============================================================================
# iTunes SEARCH API — Apple's public, no-auth search. Very slow rate limit
# (Apple is undocumented but ~20 reqs/min is the OneTagger default). Returns
# high-res cover art URLs, accurate release dates, primary genre.
# =============================================================================

class ITunesProvider(Provider):
    id = "itunes"
    name = "iTunes"
    requires_auth = False
    rate_limit_seconds = 3.0  # ~20 req/min — Apple is strict
    supported_tags = [
        "artist", "album", "year", "genre", "release_id",
    ]

    BASE = "https://itunes.apple.com"

    def __init__(self, art_resolution: int = 1000) -> None:
        super().__init__()
        self.art_resolution = max(100, min(5000, art_resolution))

    def search_release(self, artist: str, album: str) -> list[Release]:
        # Provider-level guard: refuse to send placeholders to the API.
        # See Provider._UNKNOWN_QUERY_VALUES for the full reject set.
        if not Provider.is_safe_query(artist, album):
            return []
        q = f"{artist} {album}"
        try:
            data = self.http_get_json(
                f"{self.BASE}/search",
                params={"term": q, "entity": "album", "limit": "10"},
            )
        except Exception as e:
            logger.warning("iTunes search failed: %s", e)
            return []

        out: list[Release] = []
        for r in data.get("results", []):
            try:
                # iTunes returns 100x100 by default; rewrite URL for higher res
                art_url = r.get("artworkUrl100", "") or ""
                if art_url:
                    art_url = art_url.replace(
                        "100x100bb",
                        f"{self.art_resolution}x{self.art_resolution}bb",
                    )
                rel_date = r.get("releaseDate", "") or ""
                year = rel_date[:4] if rel_date else ""
                out.append(Release(
                    platform=self.id,
                    artist=r.get("artistName", "") or "",
                    album=r.get("collectionName", "") or "",
                    year=year,
                    country=r.get("country", "") or "",
                    genre=r.get("primaryGenreName", "") or "",
                    release_id=str(r.get("collectionId", "") or ""),
                    url=r.get("collectionViewUrl", "") or "",
                    art_url=art_url,
                    # iTunes doesn't give a search score; we compute fuzzy match
                    score=string_similarity(
                        f"{artist} {album}",
                        f"{r.get('artistName', '')} {r.get('collectionName', '')}",
                    ) * 100,
                ))
            except Exception as e:
                logger.warning("iTunes parse failed: %s", e)
                continue
        out.sort(key=lambda r: r.score, reverse=True)
        return out


# =============================================================================
# DEEZER — open JSON API at api.deezer.com. No auth, broad pop/commercial
# coverage, decent cover art quality. Track search supports BPM lookup via
# their /track/{id} endpoint but the field is rarely populated.
# =============================================================================

class DeezerProvider(Provider):
    id = "deezer"
    name = "Deezer"
    requires_auth = False
    rate_limit_seconds = 0.3  # Deezer's official limit is 50 req/5sec
    supported_tags = [
        "artist", "album", "year", "genre", "release_id",
    ]

    BASE = "https://api.deezer.com"

    def __init__(self, art_resolution: int = 1000) -> None:
        super().__init__()
        self.art_resolution = max(100, min(1800, art_resolution))

    def search_release(self, artist: str, album: str) -> list[Release]:
        # Provider-level guard: refuse to send placeholders to the API.
        # See Provider._UNKNOWN_QUERY_VALUES for the full reject set.
        if not Provider.is_safe_query(artist, album):
            return []
        q = f'artist:"{artist}" album:"{album}"'
        try:
            data = self.http_get_json(
                f"{self.BASE}/search/album",
                params={"q": q},
            )
        except Exception as e:
            logger.warning("Deezer search failed: %s", e)
            return []

        out: list[Release] = []
        for r in data.get("data", []):
            try:
                # Pull cover_md5 if available; otherwise use cover_xl
                cover_url = (
                    r.get("cover_xl") or r.get("cover_big") or
                    r.get("cover_medium") or r.get("cover_small") or
                    r.get("cover") or ""
                )
                # Year: needs a follow-up /album/{id} hit to get release date.
                # We skip that here to avoid the extra rate-limited round trip;
                # the year stays empty unless we extend later.
                artist_obj = r.get("artist") or {}
                rel = Release(
                    platform=self.id,
                    artist=artist_obj.get("name", "") if isinstance(artist_obj, dict) else "",
                    album=r.get("title", "") or "",
                    release_id=str(r.get("id", "") or ""),
                    url=r.get("link", "") or "",
                    art_url=cover_url,
                    score=string_similarity(
                        f"{artist} {album}",
                        f"{artist_obj.get('name', '') if isinstance(artist_obj, dict) else ''} "
                        f"{r.get('title', '')}",
                    ) * 100,
                )
                out.append(rel)
            except Exception as e:
                logger.warning("Deezer parse failed: %s", e)
                continue
        out.sort(key=lambda r: r.score, reverse=True)
        return out

    def search_track(self, artist: str, title: str) -> list[TrackInfo]:
        """Deezer track search — supports BPM via /track/{id} but the field
        is sparse. Returns top matches; BPM=0 if Deezer didn't have it."""
        if not (artist and title):
            return []
        q = f'artist:"{artist}" track:"{title}"'
        try:
            data = self.http_get_json(f"{self.BASE}/search/track", params={"q": q})
        except Exception as e:
            logger.warning("Deezer track search failed: %s", e)
            return []
        out: list[TrackInfo] = []
        for r in data.get("data", [])[:5]:  # top 5 only — track lookup is expensive
            try:
                artist_obj = r.get("artist") or {}
                album_obj = r.get("album") or {}
                ti = TrackInfo(
                    platform=self.id,
                    artist=artist_obj.get("name", "") if isinstance(artist_obj, dict) else "",
                    title=r.get("title", "") or "",
                    album=album_obj.get("title", "") if isinstance(album_obj, dict) else "",
                    duration_ms=int(r.get("duration", 0) or 0) * 1000,
                    track_id=str(r.get("id", "") or ""),
                    score=string_similarity(
                        f"{artist} {title}",
                        f"{artist_obj.get('name', '') if isinstance(artist_obj, dict) else ''} "
                        f"{r.get('title', '')}",
                    ) * 100,
                )
                out.append(ti)
            except Exception as e:
                logger.warning("Deezer track parse failed: %s", e)
        return sorted(out, key=lambda t: t.score, reverse=True)


# =============================================================================
# BANDCAMP — independent label/artist self-publishing. Crucial for
# underground electronic, ambient, experimental music. OneTagger parses
# JSON-LD embedded in release pages.
#
# Bandcamp has no public search API but has an internal autocomplete
# endpoint that returns enough to find release URLs. We use the same.
# =============================================================================

class BandcampProvider(Provider):
    id = "bandcamp"
    name = "Bandcamp"
    requires_auth = False
    rate_limit_seconds = 1.5
    supported_tags = [
        "artist", "album", "year", "genre", "release_id",
    ]

    SEARCH_URL = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"

    def search_release(self, artist: str, album: str) -> list[Release]:
        # Provider-level guard: refuse to send placeholders to the API.
        # See Provider._UNKNOWN_QUERY_VALUES for the full reject set.
        if not Provider.is_safe_query(artist, album):
            return []
        query = f"{artist} {album}"
        try:
            self.rate_limit()
            payload = json.dumps({
                "fan_id": None,
                "full_page": False,
                "search_filter": "a",  # 'a' = album
                "search_text": query,
            }).encode("utf-8")
            req = Request(
                self.SEARCH_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/120.0",
                },
            )
            with urlopen(req, timeout=15) as r:
                raw = r.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
        except Exception as e:
            logger.warning("Bandcamp search failed: %s", e)
            return []

        out: list[Release] = []
        results = (data.get("auto") or {}).get("results", []) or []
        for r in results[:10]:
            try:
                # Bandcamp's autocomplete fields vary; defensive parsing.
                rel_artist = r.get("band_name") or r.get("artist") or ""
                rel_album = r.get("name") or r.get("title") or ""
                rel_url = r.get("item_url_path") or r.get("url") or ""
                if rel_url and not rel_url.startswith("http"):
                    rel_url = "https:" + rel_url if rel_url.startswith("//") else rel_url
                art = r.get("img") or r.get("art_url") or ""
                if art and not art.startswith("http"):
                    art = "https:" + art if art.startswith("//") else art
                rel = Release(
                    platform=self.id,
                    artist=rel_artist,
                    album=rel_album,
                    release_id=str(r.get("id", "") or ""),
                    url=rel_url,
                    art_url=art,
                    score=string_similarity(query, f"{rel_artist} {rel_album}") * 100,
                )
                out.append(rel)
            except Exception as e:
                logger.warning("Bandcamp parse failed: %s", e)
        out.sort(key=lambda r: r.score, reverse=True)
        return out

    def enrich_from_page(self, release: Release) -> Release:
        """Fetch a release URL and extract the JSON-LD embedded in it.
        Bandcamp puts most of what we want (year, label, genre) inside
        a <script type="application/ld+json"> tag. Returns an updated
        copy of the release with extra fields filled in."""
        if not release.url:
            return release
        try:
            html = self.http_get_text(release.url)
        except Exception as e:
            logger.warning("Bandcamp page fetch failed: %s", e)
            return release

        # Regex extract the JSON-LD script. We don't pull a full HTML
        # parser dep — this pattern is stable on Bandcamp.
        m = re.search(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html, flags=re.DOTALL,
        )
        if not m:
            return release
        try:
            ld = json.loads(m.group(1))
        except Exception:
            return release

        # JSON-LD structure: top-level is a MusicAlbum.
        updates = {}
        date = ld.get("datePublished", "") or ld.get("uploadDate", "")
        if date and len(date) >= 4:
            updates["year"] = date[:4]
        # publisher.name often holds the label for label-released albums
        publisher = ld.get("publisher") or {}
        if isinstance(publisher, dict) and publisher.get("name"):
            updates["label"] = publisher["name"]
        # Genre — Bandcamp uses keywords for tags
        kw = ld.get("keywords") or ld.get("genre") or ""
        if isinstance(kw, list) and kw:
            updates["genre"] = kw[0]
        elif isinstance(kw, str) and kw:
            updates["genre"] = kw

        # Apply updates without overwriting non-empty existing fields
        new = Release(**release.__dict__)
        for k, v in updates.items():
            if v and not getattr(new, k, ""):
                setattr(new, k, v)
        return new


class DiscogsProvider(Provider):
    """
    Discogs database search. Wide coverage of underground, vinyl-only,
    and DJ/electronic releases that MB doesn't have. Bass music, jungle
    /D&B comps, white labels, promo-only sound packs, regional labels —
    all here.

    Honest caveat about auth:
    Despite Discogs' general docs saying "unauthenticated requests get
    25/min," the /database/search endpoint *requires* a user token.
    Hits without a token return HTTP 403. So this provider effectively
    requires_auth=True.

    Getting a token (free, 30 seconds):
      1. Sign up / log in at https://www.discogs.com
      2. Go to Settings → Developers
      3. Click "Generate new token"
      4. Paste it when prompted, OR set in config under
         `[providers.discogs] token = "..."`

    Rate limit with a token: 60 requests/minute (~1s per request).
    Token does NOT expire and only grants read access — it's not OAuth.
    """
    id = "discogs"
    name = "Discogs"
    requires_auth = True   # /database/search returns 403 without a token
    rate_limit_seconds = 1.05   # 60/min with auth
    supported_tags = [
        "artist", "album", "year", "label", "catalog_number",
        "country", "genre", "release_id",
    ]

    BASE = "https://api.discogs.com"

    def __init__(self) -> None:
        super().__init__()
        self._token: str = ""

    def configure(self, cfg: dict[str, Any], ask) -> bool:
        """Read the Discogs token from config, or prompt for it via
        the supplied `ask` callable. Returns True if a token is present
        (provider ready), False to disable this provider for this run."""
        prov_cfg = (cfg.get("providers") or {}).get("discogs") or {}
        self._token = str(prov_cfg.get("token", "") or "").strip()
        if self._token:
            return True
        # No token in config — ask the user
        print()
        print("  Discogs requires a free API token.")
        print("  Get one here: https://www.discogs.com/settings/developers")
        print("  (sign up if needed, then click 'Generate new token')")
        # `ask` is the wrapper installed by cmd_fetch_metadata:
        #   `lambda **kw: cfg_ask(cfg, **kw)`
        # so we pass only keyword args, no positional cfg/key.
        try:
            entered = ask(
                key="providers.discogs.token",
                default="",
                prompt="Discogs token (leave blank to skip Discogs)",
                description="Free user token from Settings → Developers",
                sensitive=True,
            )
        except Exception:
            entered = ""
        self._token = str(entered or "").strip()
        return bool(self._token)

    def _auth_headers(self) -> dict[str, str]:
        h = {
            # Discogs requires an identifying User-Agent. Generic strings
            # like 'python-requests/...' get throttled harder. Uses the
            # shared module constant.
            "User-Agent": _USER_AGENT,
        }
        if self._token:
            h["Authorization"] = f"Discogs token={self._token}"
        return h

    def search_release(self, artist: str, album: str) -> list[Release]:
        # Provider-level guard: refuse to send placeholders to the API.
        # See Provider._UNKNOWN_QUERY_VALUES for the full reject set.
        if not Provider.is_safe_query(artist, album):
            return []
        # The /database/search endpoint with type=release returns
        # physical releases (CD/vinyl/digital pressings). We could also
        # query type=master for canonical "master releases", but those
        # have less detailed label/cat-num info attached.
        params = {
            "release_title": album,
            "artist": artist,
            "type": "release",
            "per_page": "10",
        }
        try:
            self.rate_limit()
            url = f"{self.BASE}/database/search?{_urlencode(params)}"
            req = Request(url, headers=self._auth_headers())
            with urlopen(req, timeout=20) as r:
                raw = r.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
        except HTTPError as e:
            if e.code == 401:
                logger.warning("Discogs 401 — invalid token? Falling back to unauth")
                self._token = ""
                self.rate_limit_seconds = 2.5
                return []
            if e.code in (429, 503):
                logger.warning("Discogs rate limit hit, sleeping 5s")
                time.sleep(5)
            return []
        except Exception as e:
            logger.warning("Discogs search failed for %r / %r: %s", artist, album, e)
            return []

        out: list[Release] = []
        results = data.get("results", []) or []
        for r in results[:10]:
            try:
                # Title comes as "Artist - Album"; split heuristically.
                title = r.get("title", "") or ""
                rel_artist = ""
                rel_album = title
                if " - " in title:
                    rel_artist, _, rel_album = title.partition(" - ")
                    rel_artist = rel_artist.strip()
                    rel_album = rel_album.strip()
                # Labels and cat-nums are LISTS in Discogs results
                labels = r.get("label", []) or []
                catnos = r.get("catno", "") or ""
                if isinstance(catnos, list):
                    catnos = catnos[0] if catnos else ""
                # Genres + styles concatenated for the genre field.
                # Discogs separates "Electronic" (genre) from "Drum n Bass"
                # (style). Users want the style.
                genres = r.get("genre", []) or []
                styles = r.get("style", []) or []
                if isinstance(genres, str):
                    genres = [genres]
                if isinstance(styles, str):
                    styles = [styles]
                combined_genre = "; ".join(
                    s for s in (styles + genres) if s
                ) or ""

                rel = Release(
                    platform=self.id,
                    artist=rel_artist,
                    album=rel_album,
                    year=str(r.get("year", "") or ""),
                    label=(labels[0] if labels else ""),
                    catalog_number=catnos,
                    country=r.get("country", "") or "",
                    genre=combined_genre,
                    release_id=str(r.get("id", "") or ""),
                    url=f"https://www.discogs.com{r.get('uri', '')}"
                        if r.get("uri") else "",
                    art_url=r.get("cover_image", "") or r.get("thumb", "") or "",
                    media_format=(r.get("format", [""]) or [""])[0]
                                  if isinstance(r.get("format"), list) else "",
                    score=string_similarity(
                        f"{artist} {album}",
                        f"{rel_artist} {rel_album}"
                    ) * 100,
                )
                out.append(rel)
            except Exception as e:
                logger.debug("Discogs result parse failed: %s", e)
                continue
        # Sort by our similarity score, highest first
        out.sort(key=lambda r: r.score, reverse=True)
        return out



#
# Order matters — when running multi-provider lookup, we query in this
# order and the first non-empty value for each field wins (subject to
# the consensus logic in metadata_lookup.py for BPM and other multi-
# source fields).

# =============================================================================
# AcoustID — audio fingerprint provider. Last-resort fallback only: it
# cannot do text-based album searches. Its role is to fingerprint one file
# from an album that ALL text providers missed, get a MusicBrainz recording
# ID from AcoustID's database, then hand that ID to MusicBrainzProvider to
# resolve the full release. Requires pyacoustid + fpcalc on PATH.
# =============================================================================

class AcoustIDProvider(Provider):
    id = "acoustid"
    name = "AcoustID"
    requires_auth = True
    rate_limit_seconds = 0.35   # ~3 req/s with an API key
    supported_tags = [
        "artist", "album", "year", "label", "catalog_number",
        "mb_release_id", "acoustid_id",
    ]

    def __init__(self) -> None:
        super().__init__()
        self._api_key: str = ""

    def configure(self, cfg: dict[str, Any], ask) -> bool:
        prov_cfg = (cfg.get("providers") or {}).get("acoustid") or {}
        # config key is "api_key" (set by first-run wizard); also accept
        # "token" for consistency with other providers.
        self._api_key = (
            str(prov_cfg.get("api_key", "") or "").strip()
            or str(prov_cfg.get("token", "") or "").strip()
        )
        if self._api_key:
            return True
        print("  AcoustID requires a free API key.")
        print("  Get one at: https://acoustid.org/login (register an application)")
        print("  Also requires fpcalc (Chromaprint) to be installed and on PATH.")
        try:
            entered = ask(
                key="providers.acoustid.api_key",
                default="",
                prompt="AcoustID API key",
                sensitive=True,
            )
        except Exception:
            entered = ""
        self._api_key = str(entered or "").strip()
        return bool(self._api_key)

    def search_release(self, artist: str, album: str) -> list[Release]:
        # AcoustID is fingerprint-only — no text search. Always returns [].
        # The real integration happens via fingerprint_file(), called from
        # the AcoustID fallback block in fill_missing_metadata after all
        # text-based providers have missed.
        return []

    def fingerprint_file(self, path: str) -> list[tuple[float, str]]:
        """
        Fingerprint an audio file using Chromaprint/fpcalc via pyacoustid.

        Returns [(score, mb_recording_id), ...] sorted score-descending,
        where score is 0.0–1.0. Only entries with score >= 0.5 are worth
        acting on; below that the fingerprint match is unreliable.

        Requires:
          - pip install pyacoustid
          - fpcalc (Chromaprint CLI) installed and on PATH
        """
        if not self._api_key:
            return []
        self.rate_limit()
        try:
            import acoustid
            raw = list(acoustid.match(self._api_key, path))
        except Exception as e:
            logger.warning("acoustid: fingerprint failed for %s: %s", path, e)
            return []

        out: list[tuple[float, str]] = []
        for item in raw:
            try:
                score = float(item[0])
                recording_id = str(item[1] or "").strip()
                if recording_id:
                    out.append((score, recording_id))
            except (IndexError, TypeError, ValueError):
                continue
        out.sort(key=lambda x: x[0], reverse=True)
        return out


# =============================================================================
# REGISTRY
# =============================================================================

ALL_PROVIDERS: list[type[Provider]] = [
    DiscogsProvider,
    MusicBrainzProvider,
    DeezerProvider,
    BandcampProvider,
    ITunesProvider,
    AcoustIDProvider,   # last — fingerprint fallback only, no text search
]


def make_provider(provider_id: str) -> Provider | None:
    """Look up a provider class by id, instantiate it, return it."""
    for cls in ALL_PROVIDERS:
        if cls.id == provider_id:
            return cls()
    return None


def list_providers() -> list[tuple[str, str, bool]]:
    """Return [(id, name, requires_auth)] for menu display."""
    return [(p.id, p.name, p.requires_auth) for p in ALL_PROVIDERS]
