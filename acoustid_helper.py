"""
acoustid_helper.py
==================
Thin wrapper kept for backwards compatibility. The real fingerprinting
logic now lives in AcoustIDProvider (metadata_providers.py).

Call identify_file() directly if you want a one-shot fingerprint outside
the normal fetch pipeline; for the full fetch flow AcoustID is wired as a
last-resort provider in fill_missing_metadata (metadata_lookup.py).

Requires: pip install pyacoustid
          fpcalc (Chromaprint) installed and on PATH
"""

from __future__ import annotations


def identify_file(api_key: str, path: str) -> dict | None:
    """
    Fingerprint one audio file. Returns the best match dict or None.

    Return shape:
        {"score": float,        # 0.0–1.0
         "recording_id": str,   # MusicBrainz recording UUID
         "title": str,
         "artist": str}
    """
    try:
        import acoustid
        results = list(acoustid.match(api_key, path))
    except Exception:
        return None

    if not results:
        return None

    try:
        score, recording_id, title, artist = results[0]
        return {
            "score": float(score),
            "recording_id": str(recording_id or ""),
            "title": str(title or ""),
            "artist": str(artist or ""),
        }
    except (IndexError, TypeError, ValueError):
        return None
