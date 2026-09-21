"""
mp3_check.py
============

Backs the MP3 CHECK tab: what an .mp3 actually is, not just what it claims.

Two questions, kept deliberately separate:

  quick_info()     — the file's own header/tag: declared bitrate, CBR/VBR,
                      sample rate. Instant (no decode), used for the list.
  spectral_check()  — decodes the audio and asks spectral.py (the same
                      analyser SPEK-TRO trusts for lossless files) where the
                      spectrum actually stops. Slower, so it is run on demand
                      per file, not for a whole folder at once.

The SPEK-TRO tab treats a spectral wall as a fake-lossless VERDICT — right
there, because a "FLAC" is supposed to have no wall at all. An mp3 is
*meant* to have one, so this module never says "fake". It only ever
compares the wall spectral_check() finds against what quick_info() claims,
so a 128 kbps file re-saved with a 320 kbps header becomes visible as a
mismatch instead of being invisible on both instruments alone.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MP3_EXTENSIONS = (".mp3",)

# estimate_source()'s label -> the highest declared kbps that bucket can
# plausibly mean. "128 kbps or lower" is open-ended at the BOTTOM (a 96 or a
# 140 kbps file both land there), so the only meaningful question is whether
# the declared rate overshoots the bucket's own TOP. "320 kbps" has no
# ceiling worth naming — None means "never flag".
_ESTIMATE_CEILING_KBPS = {
    "128 kbps or lower": 144,
    "160 kbps": 176,
    "192 kbps": 224,
    "256 kbps / MP3 V2": 288,
    "320 kbps": None,
}


def dependencies_available() -> bool:
    try:
        import spectral  # noqa: F401
        from mutagen import File as _F  # noqa: F401
    except Exception:
        return False
    return True


def missing_dependencies() -> list[str]:
    miss = []
    try:
        import mutagen  # noqa: F401
    except Exception:
        miss.append("mutagen")
    try:
        import spectral  # noqa: F401
    except Exception:
        miss.append("spectral")
    return miss


def find_mp3_files(root: str | Path, recursive: bool = True) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        return []
    it = root.rglob("*") if recursive else root.iterdir()
    return sorted(
        (p for p in it if p.is_file() and p.suffix.lower() in MP3_EXTENSIONS),
        key=lambda p: str(p).lower(),
    )


@dataclass
class Mp3Info:
    path: str = ""
    name: str = ""
    ok: bool = True
    error: str = ""
    size_bytes: int = 0
    duration: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    declared_bitrate: int = 0     # bps, straight off the file's own header
    bitrate_mode: str = ""        # CBR / VBR / ABR / UNKNOWN
    artist: str = ""
    title: str = ""
    album: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def quick_info(path: str | Path) -> Mp3Info:
    """Header + tags only — no audio is decoded. What the file CLAIMS to be."""
    p = Path(path)
    info = Mp3Info(path=str(p), name=p.name)
    try:
        info.size_bytes = p.stat().st_size
    except OSError:
        pass
    try:
        from mutagen import File as MutagenFile

        mf = MutagenFile(str(p))
        if mf is None or mf.info is None:
            raise ValueError("mutagen could not read this file")
        i = mf.info
        info.duration = float(getattr(i, "length", 0.0) or 0.0)
        info.sample_rate = int(getattr(i, "sample_rate", 0) or 0)
        info.channels = int(getattr(i, "channels", 0) or 0)
        info.declared_bitrate = int(getattr(i, "bitrate", 0) or 0)
        mode = getattr(i, "bitrate_mode", None)
        # BitrateMode prints as "BitrateMode.CBR" but, oddly, has no usable
        # .name of its own — str() + split is the reliable way to get "CBR".
        info.bitrate_mode = str(mode).rsplit(".", 1)[-1] if mode is not None else "UNKNOWN"

        from spectral import read_tags

        tags = read_tags(str(p))
        info.artist, info.title, info.album = tags["artist"], tags["title"], tags["album"]
    except Exception as e:
        info.ok = False
        info.error = str(e)
    return info


def spectral_check(path: str | Path) -> dict[str, Any]:
    """Decode + FFT via spectral.py. Purely descriptive: what the audio
    content is consistent with, never a fake/genuine verdict — that
    question only makes sense for a file claiming to be lossless."""
    import spectral

    if not spectral.ffmpeg_available():
        return {"ok": False, "error": "ffmpeg not available"}
    r = spectral.analyse(str(path))
    if not r.ok:
        return {"ok": False, "error": r.error or "analysis failed"}
    return {
        "ok": True,
        "cutoff_hz": round(r.cutoff_hz),
        "nyquist_hz": round(r.nyquist_hz),
        "wall_db": round(r.wall_db, 1),
        "above_db": round(r.above_db, 1),
        "estimate": spectral.estimate_source(r.cutoff_hz),
    }


def declared_vs_spectral_mismatch(declared_bitrate_bps: int, estimate: str) -> bool:
    """Does the header's own bitrate claim outrun what the spectrum backs up?

    Only fires when the declared rate is comfortably above the MATCHED
    bucket's own ceiling — a 134kbps file estimated "128 kbps or lower" is
    agreement (that bucket has no real bottom), not a mismatch.
    """
    ceiling = _ESTIMATE_CEILING_KBPS.get(estimate)
    if not ceiling:
        return False
    declared_kbps = declared_bitrate_bps / 1000.0
    return declared_kbps > ceiling * 1.1
