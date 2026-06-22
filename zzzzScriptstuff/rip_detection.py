"""
rip_detection.py
================

Identify the software that produced an audio rip.

Signals (in priority order, most reliable first):

  1. .log file in the album folder
       EAC and XLD write detailed extraction logs. The first ~200 bytes
       reliably identify which tool. If present, we trust this over tags.

  2. `encodedby` / `encoder` tags inside the file
       LAME signs MP3s with strings like "LAME3.100" in the XING header.
       FLAC libFLAC writes "FLAC 1.3.4" in the vendor string. dBpoweramp,
       fre:ac and others write signatures here.

  3. `comment` tag
       EAC sometimes writes "Exact Audio Copy: ..." into the comment.
       Some scene rip groups embed their tooling here.

  4. `encodersettings` tag
       Vorbis comment field. Often "lame VBR -V0" or "FLAC -8".

  5. File-format hints
       If FLAC vendor string is "reference libFLAC 1.x.y", at least we
       know it was encoded by the reference encoder. Sox, ffmpeg, etc
       have their own vendor strings.

The output is structured as RipDetection — a dataclass with the canonical
software name, version, settings string, and a confidence score 0..1.

What this module does NOT do:
  - Audio analysis. The Vamp lossy-encoding-detector plugin does that
    but is impractically slow at 207k-file scale (see comments in
    rip_audio.py for the integration that gates it to suspects only).
  - Decide whether the rip is "good" or "bad". We report what was used,
    not whether it should have been used differently.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


logger = logging.getLogger("music-organiser")


# Each entry is (canonical_name, list of regex patterns).
# Patterns are applied to a haystack built from the relevant tags + any
# folder log file. First match wins for `software`; version is captured
# via the named group `ver`.
SIGNATURES: list[tuple[str, list[str]]] = [
    # EAC and XLD are the high-quality rippers. Their logs have very
    # specific markers we can detect from a few hundred bytes of the .log.
    ("Exact Audio Copy", [
        r"Exact Audio Copy\s+V?(?P<ver>[\w\d\.\s]+?)(?:from|on|\n|\s{2,})",
        r"\bEAC\s+extraction\b",
        r"exactaudiocopy\.de",
    ]),
    ("X Lossless Decoder", [
        r"X\s*Lossless\s*Decoder\s+version\s+(?P<ver>\S+)",
        r"\bXLD\s+version\b",
    ]),
    ("dBpoweramp", [
        r"dBpoweramp\s+(?:CD\s+Ripper\s+)?(?:Release\s+)?(?P<ver>[\d\.]+)",
        r"dBpoweramp Music Converter",
    ]),
    ("fre:ac", [
        r"\bfre:ac\s+v?(?P<ver>[\d\.]+)",
        r"BonkEnc",  # earlier name
    ]),
    ("CUERipper", [
        r"CUERipper\s+v?(?P<ver>[\d\.]+)",
    ]),
    ("Whipper", [
        r"\bwhipper\s+(?P<ver>[\d\.]+)",
        r"morituri",  # whipper's predecessor
    ]),
    ("RipIt", [
        r"\bRipIt\s+(?P<ver>[\d\.]+)",
    ]),
    ("Max", [
        r"\bMax (?:CD Ripper )?(?P<ver>[\d\.]+)\s+\(",
    ]),
    ("iTunes", [
        r"\bcom\.apple\.iTunes\b",
        r"\biTunNORM\b",
        r"iTunes\s+(?P<ver>[\d\.]+)",
    ]),
    ("foobar2000", [
        r"foobar2000\s+v?(?P<ver>[\d\.]+)",
    ]),
    # MP3 encoders. LAME signs the file via the XING header which mutagen
    # surfaces in info.encoder_info or encodedby tag.
    ("LAME", [
        r"\bLAME\s*(?P<ver>[\d\.]+)",
        r"\bLAME3\.(?P<ver>\d+)",   # older style — LAME3.100 etc.
    ]),
    ("Nero AAC", [
        r"Nero\s+AAC\s+(?P<ver>[\d\.]+)",
        r"\bNero\s+AAC\s+codec\b",
    ]),
    ("FAAC", [
        r"\bFAAC\b",
        r"libfaac",
    ]),
    ("QAAC", [
        r"\bqaac\b",
        r"CoreAudio AAC",
    ]),
    ("FFmpeg", [
        r"\bLavf(?:\d+\.\d+\.\d+)?\b",
        r"\bLavc\d+\.\d+\.\d+\b",
        r"FFmpeg(?:\s+(?P<ver>\S+))?",
    ]),
    ("SoX", [
        r"\bSoX\s+(?P<ver>[\d\.]+)",
    ]),
    ("libFLAC", [
        # FLAC vendor string is "reference libFLAC <version>"
        r"reference\s+libFLAC\s+(?P<ver>[\d\.]+)",
        r"libFLAC\s+(?P<ver>[\d\.]+)",
    ]),
    ("Sound Forge", [
        r"Sound Forge\s+(?:Pro\s+)?(?P<ver>[\d\.]+)",
    ]),
    ("Audacity", [
        r"Audacity\s+(?P<ver>[\d\.]+)",
    ]),
    ("WavePack", [
        r"WavPack\s+(?P<ver>[\d\.]+)",
    ]),
    # MP3 from old/dubious encoders. We detect these so the user knows
    # the rip is potentially poor quality.
    ("Xing", [
        r"\bXing\b",
    ]),
    ("Fraunhofer", [
        r"Fraunhofer",
    ]),
]


# Whether each software typically produces lossless or lossy output.
# Used downstream — if the rip software is e.g. dBpoweramp and the
# codec is FLAC, we can be more confident it's a legit lossless rip.
LOSSLESS_SOFTWARE = {
    "Exact Audio Copy", "X Lossless Decoder", "dBpoweramp", "CUERipper",
    "Whipper", "libFLAC", "WavPack",
}
LOSSY_SOFTWARE = {
    "LAME", "Nero AAC", "FAAC", "QAAC", "Xing", "Fraunhofer",
}


@dataclass
class RipDetection:
    """Result of identifying how an audio file was produced."""
    software: str = ""           # canonical name, e.g. "Exact Audio Copy"
    version: str = ""            # raw version string from the signature
    settings: str = ""           # encoder settings string if found
    source: str = ""             # which signal hit: 'log_file' / 'encodedby_tag' / etc.
    confidence: float = 0.0      # 0..1
    is_lossless_software: bool | None = None
    raw_evidence: str = ""       # the actual matched string (for debugging)


def _read_log_head(folder: Path, max_bytes: int = 8192) -> str:
    """Read the head of any .log file in the given folder. Returns the
    concatenation of all .log file heads (rare to have more than one)."""
    if not folder.exists():
        return ""
    out = []
    try:
        for entry in folder.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".log":
                try:
                    out.append(entry.read_text(encoding="utf-8",
                                               errors="replace")[:max_bytes])
                except OSError:
                    pass
    except OSError:
        pass
    return "\n".join(out)


def _match_signatures(haystack: str, source_label: str,
                       confidence_boost: float = 0.0) -> RipDetection | None:
    """Run every signature regex against `haystack`. Return the first
    hit. confidence_boost added to base 0.7 confidence — log-file hits
    get +0.25 (high confidence), tag hits get base, weaker signals get
    less."""
    if not haystack:
        return None
    for canonical_name, patterns in SIGNATURES:
        for pat in patterns:
            m = re.search(pat, haystack, flags=re.IGNORECASE)
            if not m:
                continue
            try:
                ver = m.groupdict().get("ver", "") or ""
            except (IndexError, AttributeError):
                ver = ""
            return RipDetection(
                software=canonical_name,
                version=str(ver).strip(),
                source=source_label,
                confidence=min(1.0, 0.7 + confidence_boost),
                is_lossless_software=(
                    True if canonical_name in LOSSLESS_SOFTWARE else
                    False if canonical_name in LOSSY_SOFTWARE else
                    None
                ),
                raw_evidence=m.group(0)[:120],
            )
    return None


def detect_rip_software(
    audio_path: Path | str,
    *,
    existing_tags: dict[str, Any] | None = None,
) -> RipDetection:
    """
    Identify the software that produced an audio rip.

    existing_tags: dict of tag values already in the DB row. Optional;
    if not provided, we read from the file via mutagen.

    Returns RipDetection with `software=""` if nothing matched.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        return RipDetection()

    # ----- 1. log file in the album folder -------------------------------
    log_text = _read_log_head(audio_path.parent)
    if log_text:
        hit = _match_signatures(log_text, source_label="log_file",
                                confidence_boost=0.25)
        if hit:
            return hit

    # ----- 2. tag-based signals ------------------------------------------
    # Build a haystack of every plausible tag value, in priority order.
    # We dedupe per source to log accurately which tag carried the signal.
    if existing_tags is None:
        try:
            from mutagen import File as MutagenFile
            f = MutagenFile(str(audio_path))
            if f is None:
                existing_tags = {}
            else:
                # Different formats expose tags differently
                tags = {}
                # FLAC / OGG / Opus: dict-like Vorbis comments
                for k in ("encodedby", "encoder", "encodersettings",
                          "comment", "tool", "ENCODEDBY", "ENCODER",
                          "ENCODERSETTINGS", "COMMENT", "TOOL"):
                    try:
                        v = f.get(k)
                        if v:
                            tags[k.lower()] = "; ".join(str(x) for x in v) \
                                if isinstance(v, list) else str(v)
                    except Exception:
                        pass
                # Mutagen exposes file-info.encoder_info for some formats
                info = getattr(f, "info", None)
                if info is not None:
                    for attr in ("encoder_info", "encoder_settings"):
                        try:
                            v = getattr(info, attr, "")
                            if v:
                                tags[attr] = str(v)
                        except Exception:
                            pass
                # Vendor string (FLAC: "reference libFLAC 1.3.4")
                try:
                    if hasattr(f, "tags") and f.tags and hasattr(f.tags, "vendor"):
                        tags["vendor"] = f.tags.vendor or ""
                except Exception:
                    pass
                # ID3 TXXX:Encoder, TENC
                if hasattr(f, "tags") and f.tags is not None:
                    for tname in ("TENC", "TSSE"):
                        try:
                            frame = f.tags.get(tname)
                            if frame and frame.text:
                                tags[tname.lower()] = str(frame.text[0])
                        except Exception:
                            pass
                existing_tags = tags
        except Exception:
            existing_tags = {}

    sources_to_try = [
        ("encodedby_tag",    existing_tags.get("encodedby") or existing_tags.get("tenc")),
        ("encoder_info",     existing_tags.get("encoder_info")),
        ("encodersettings",  existing_tags.get("encodersettings")
                              or existing_tags.get("encoder_settings")
                              or existing_tags.get("tsse")),
        ("comment_tag",      existing_tags.get("comment")),
        ("vendor_string",    existing_tags.get("vendor")),
        ("encoder_tag",      existing_tags.get("encoder")),
        ("tool_tag",         existing_tags.get("tool")),
    ]
    for source_label, content in sources_to_try:
        if content:
            hit = _match_signatures(str(content), source_label=source_label,
                                    confidence_boost=0.0)
            if hit:
                # Tag-based signals are less authoritative than logs;
                # we already set base confidence 0.7 in _match_signatures.
                # Encoder settings from the file vendor string is more
                # reliable than a free-text comment though.
                if source_label in ("vendor_string", "encoder_info"):
                    hit.confidence = min(1.0, hit.confidence + 0.1)
                elif source_label == "comment_tag":
                    hit.confidence = max(0.5, hit.confidence - 0.1)
                # Try to pull out encoder settings too
                settings = (existing_tags.get("encodersettings")
                            or existing_tags.get("encoder_settings")
                            or existing_tags.get("tsse") or "")
                if settings:
                    hit.settings = str(settings)[:200]
                return hit

    return RipDetection()


def detection_to_dict(d: RipDetection) -> dict[str, Any]:
    """Convert a RipDetection into a flat dict suitable for db.upsert_file."""
    return {
        "ripper_software":   d.software,
        "ripper_version":    d.version,
        "ripper_settings":   d.settings,
        "ripper_confidence": float(d.confidence),
    }
