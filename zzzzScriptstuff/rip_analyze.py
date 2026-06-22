"""
rip_analyze.py
==============

Audio file inspection that goes beyond log-file detection (which lives
in `rip_detection.py`). Two layers, both fast:

  1. Header inspection — codec, bit depth, sample rate, duration. Read
     via mutagen. No audio decode. Catches the easy cases.

  2. Bottom-bit silence check — for files that CLAIM 24-bit, decode
     5 seconds and look at the bottom 8 bits of each sample. If they're
     all zero, the file was upsampled from 16-bit. Fake hi-res.

  3. Ripper-software identification — read FLAC vendor tag and scan
     for proprietary tag fingerprints (ACCURATERIPRESULT, XLD_*, etc.)
     to guess which ripping software produced the file.

What this module DELIBERATELY DOESN'T DO:
  ✗ Full spectral analysis to detect lossy-source-in-FLAC. The naïve
    ffmpeg-volumedetect-with-highpass approach we tried is unreliable
    (filter rolloff leaks energy across the cutoff, average-RMS can't
    distinguish "no content above 18 kHz" from "very quiet content").
    The existing `fake_flac.py` + vamp plugin chain (option 5, "Verify
    rip authenticity") IS the right tool for that. We don't duplicate
    it here.

  ✗ Vinyl/tape source detection. Possible in principle (noise floor
    characteristics differ) but too many false positives in practice.

  ✗ Whether AccurateRip would match a checksum. Requires the entire
    file decoded + a network call to the AccurateRip DB. Out of scope.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AudioAnalysis:
    """Result of analysing one audio file."""
    path: str = ""
    codec: str = ""                     # 'flac', 'mp3', etc.
    bit_depth: int = 0                  # nominal (from header)
    sample_rate: int = 0                # Hz
    duration_seconds: float = 0.0
    channel_count: int = 0
    # Findings — each is a (label, severity, detail) tuple. severity ∈ {info, warn, alarm}
    findings: list[tuple[str, str, str]] = field(default_factory=list)
    # Quick verdicts pulled out of findings for easy filtering
    likely_fake_hires: bool = False
    likely_lossy_source: bool = False    # (header check only — see fake_flac.py)
    likely_genuine_cd_rip: bool = False
    # Ripper identification
    ripper_guess: str = ""               # "EAC", "XLD", "dBpoweramp", etc.
    ripper_confidence: str = ""          # "high", "medium", "low", or ""
    ripper_evidence: list[str] = field(default_factory=list)
    # If we couldn't run any audio-domain checks, this stays True
    analysis_skipped: bool = True
    skip_reason: str = ""


# =============================================================================
# Tool detection
# =============================================================================

_FFMPEG: str | None = None
_FFMPEG_CHECKED = False

def _have_ffmpeg() -> str | None:
    """Return path to ffmpeg binary, or None. Cached."""
    global _FFMPEG, _FFMPEG_CHECKED
    if _FFMPEG_CHECKED:
        return _FFMPEG
    _FFMPEG = shutil.which("ffmpeg")
    _FFMPEG_CHECKED = True
    return _FFMPEG


# =============================================================================
# Header parsing (fast — no audio decode)
# =============================================================================

def _read_header(path: Path) -> tuple[str, int, int, float, int]:
    """Return (codec, bit_depth, sample_rate, duration_seconds, channels).
    Uses mutagen — fast, no actual decode. Returns zero defaults on
    failure (rather than raising) so analysis can continue partially."""
    try:
        from mutagen import File as MutagenFile
        m = MutagenFile(str(path))
        if m is None:
            return "", 0, 0, 0.0, 0
        info = getattr(m, "info", None)
        if info is None:
            return "", 0, 0, 0.0, 0
        codec = m.mime[0].split("/")[-1] if m.mime else ""
        sr = int(getattr(info, "sample_rate", 0) or 0)
        bd = int(getattr(info, "bits_per_sample", 0) or 0)
        dur = float(getattr(info, "length", 0.0) or 0.0)
        ch = int(getattr(info, "channels", 0) or 0)
        return codec, bd, sr, dur, ch
    except Exception:
        return "", 0, 0, 0.0, 0



# =============================================================================
# Fake hi-res detection (sample-level)
# =============================================================================

def _check_fake_hires(path: Path, declared_bd: int, sr: int,
                      timeout_s: float = 30) -> tuple[bool, str]:
    """
    For files that CLAIM 24-bit, check whether the bottom 8 bits actually
    carry signal. If they're all zero (or constant), the file was
    upsampled from 16-bit — fake hi-res.

    Approach: decode a few seconds to raw PCM via ffmpeg, examine the
    bottom 8 bits of each sample. If >99% are zero or repeated, it's
    fake hi-res.

    Returns (is_fake_hires, reason_string).
    """
    if declared_bd < 24:
        return False, "not claimed as 24-bit"
    ffmpeg = _have_ffmpeg()
    if not ffmpeg:
        return False, "ffmpeg not available"

    # Decode 5 seconds of mono signed-24-bit PCM to a tempfile
    with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as f:
        pcm_path = f.name
    try:
        cmd = [
            ffmpeg, "-v", "error",
            "-ss", "5", "-t", "5",            # 5s starting from 5s in
            "-i", str(path),
            "-ac", "1",                       # mono
            "-f", "s24le",                    # signed 24-bit little-endian
            "-y", pcm_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout_s)
        if proc.returncode != 0:
            return False, f"ffmpeg decode failed: {proc.stderr[:80]}"

        with open(pcm_path, "rb") as f:
            data = f.read()

        if len(data) < 3 * 1000:    # need at least 1000 samples
            return False, "too little data decoded"

        # Each sample is 3 bytes little-endian. The LOW byte is the
        # bottom 8 bits. Count how many samples have a low byte of 0.
        n_samples = len(data) // 3
        zero_low_count = 0
        for i in range(n_samples):
            if data[i * 3] == 0:
                zero_low_count += 1

        zero_pct = zero_low_count / n_samples
        if zero_pct >= 0.95:
            return True, (f"{zero_pct:.1%} of samples have a zero low byte — "
                          f"bottom 8 bits are silent, file is 16-bit upsampled")
        elif zero_pct >= 0.50:
            return False, (f"{zero_pct:.1%} zero low bytes — suspicious but "
                           f"inconclusive (could be quiet passages)")
        return False, f"only {zero_pct:.1%} zero low bytes — genuine 24-bit content"
    finally:
        try:
            os.unlink(pcm_path)
        except OSError:
            pass


# =============================================================================
# Ripper-software identification
# =============================================================================
#
# Reads the FLAC vendor string and scans for proprietary tags that
# specific ripping software leaves behind. Confidence levels:
#
#   high   — a distinctive ripper-specific tag is present (e.g.
#            ACCURATERIPRESULT, XLD_ESTIMATED_DURATION). These are
#            essentially fingerprints.
#   medium — vendor string narrows to a small range AND no
#            contradicting evidence. e.g. "reference libFLAC 1.4.3"
#            during 2023, combined with the absence of ripper-specific
#            tags, suggests command-line flac or a recent ripper.
#   low    — vendor string is present but reference-libFLAC (used by
#            everything) and no other markers.
#
# Honest limits this routine accepts:
#   • Post-rip tag editing (Picard, foobar) destroys most of these
#     fingerprints. If a file's been re-tagged, we'll often guess wrong.
#   • Drive-offset / pregap / AccurateRip checks not done — would need
#     a network call and full decode.
#   • The "metadata block layout" + "ReplayGain casing" heuristics from
#     Gemini's checklist are technically possible but VERY destructible
#     by re-tagging. We don't ship them.

_RIPPER_FINGERPRINT_TAGS = [
    # (lowercased tag name pattern, ripper, confidence)
    # dBpoweramp's most distinctive tag — added by every CD rip
    ("accurateripresult",          "dBpoweramp",       "high"),
    ("accurateripdiscid",          "dBpoweramp",       "high"),
    # XLD (Mac CD ripper) — embeds a large extraction log + duration tag
    ("xld_estimated_duration",     "XLD",              "high"),
    # foobar2000 typically writes itself into the ENCODER tag
    # — caught by the encoder-string parser below, not here
    # EAC (Exact Audio Copy)
    ("encodercomment",             "EAC (possibly)",   "medium"),
    # CUERipper (foobar/cmd-line variants)
    ("cuesheet",                   "CUE-based ripper", "low"),
]


def identify_ripper(path: Path | str) -> tuple[str, str, list[str]]:
    """
    Inspect a FLAC file (or other Vorbis-comment-bearing file) and
    return (ripper_name, confidence, evidence_list).

    Returns ("", "", []) if nothing identifiable.

    Confidence: "high" / "medium" / "low" / "".
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        return "", "", []

    evidence: list[str] = []
    candidates: list[tuple[str, str]] = []   # (ripper, confidence)

    try:
        from mutagen.flac import FLAC
        from mutagen.id3 import ID3
        from mutagen import File as MutagenFile
        m = MutagenFile(str(p))
        if m is None:
            return "", "", []
    except Exception:
        return "", "", []

    # Step 1: vendor string (FLAC streaminfo's vendor field)
    vendor = ""
    try:
        if isinstance(m, FLAC):
            # mutagen exposes the vendor string on m.tags.vendor for FLAC
            vendor = getattr(m.tags, "vendor", "") or ""
    except Exception:
        pass
    if vendor:
        evidence.append(f"vendor: {vendor}")
        v_low = vendor.lower()
        # Look for distinctive vendor strings
        if "xld" in v_low:
            candidates.append(("XLD", "high"))
        elif "dbpoweramp" in v_low or "dbpower" in v_low:
            candidates.append(("dBpoweramp", "high"))
        elif "eac" in v_low or "exact audio copy" in v_low:
            candidates.append(("EAC", "high"))
        elif "flake" in v_low:
            candidates.append(("Flake encoder", "high"))
        elif "reference libflac" in v_low:
            # This is the common path — almost everything that uses the
            # reference libFLAC library writes this. Not enough to
            # identify the ripper by itself.
            candidates.append(("(libFLAC-based ripper)", "low"))

    # Step 2: scan tags for ripper-specific fingerprints
    try:
        # m.tags can be a VComment, ID3, etc. Iterate all keys.
        tag_keys: list[str] = []
        if hasattr(m, "tags") and m.tags is not None:
            try:
                tag_keys = list(m.tags.keys())
            except Exception:
                pass
        tag_keys_low = [k.lower() for k in tag_keys]

        for pattern, ripper, confidence in _RIPPER_FINGERPRINT_TAGS:
            for tk_low in tag_keys_low:
                if pattern in tk_low:
                    candidates.append((ripper, confidence))
                    evidence.append(f"tag '{tk_low}' present (→ {ripper})")
                    break

        # Encoder tag — check for software names
        encoder_val = ""
        for tk in tag_keys:
            if tk.lower() == "encoder":
                try:
                    val = m.tags[tk]
                    encoder_val = (val[0] if isinstance(val, list) else val) or ""
                except Exception:
                    pass
                break
        if encoder_val:
            evidence.append(f"encoder: {encoder_val}")
            e_low = str(encoder_val).lower()
            if "foobar" in e_low:
                candidates.append(("foobar2000", "high"))
            elif "dbpoweramp" in e_low or "dbpower" in e_low:
                candidates.append(("dBpoweramp", "high"))
            elif "xld" in e_low:
                candidates.append(("XLD", "high"))
            elif "eac" in e_low or "exact audio copy" in e_low:
                candidates.append(("EAC", "high"))
            elif "rubyripper" in e_low:
                candidates.append(("Rubyripper", "high"))
            elif "morituri" in e_low:
                candidates.append(("Morituri", "high"))
            elif "whipper" in e_low:
                candidates.append(("Whipper", "high"))

    except Exception:
        pass

    if not candidates:
        return "", "", evidence

    # Pick the highest-confidence candidate. If multiple at same level,
    # prefer the one with the most evidence.
    confidence_rank = {"high": 3, "medium": 2, "low": 1, "": 0}
    candidates.sort(key=lambda x: confidence_rank.get(x[1], 0), reverse=True)
    best_ripper, best_conf = candidates[0]
    return best_ripper, best_conf, evidence


# =============================================================================
# MAIN: analyze one file
# =============================================================================

def analyze_audio_origin(path: str | Path,
                          *, deep: bool = True,
                          timeout_s: float = 60) -> AudioAnalysis:
    """
    Inspect one audio file. Returns an AudioAnalysis with structured
    findings.

    `deep=False`: only read the file header (fast, no audio decode).
        Catches obvious problems like declared-24-bit when only 16
        bits are used.

    `deep=True`: also run ffmpeg-based audio-domain checks. Slower
        (~5-30 seconds per file).
    """
    p = Path(path)
    r = AudioAnalysis(path=str(p))

    codec, bd, sr, dur, ch = _read_header(p)
    r.codec = codec
    r.bit_depth = bd
    r.sample_rate = sr
    r.duration_seconds = dur
    r.channel_count = ch

    if codec in ("", None):
        r.skip_reason = "couldn't read header (corrupt or unsupported format?)"
        r.findings.append(("unreadable", "warn", r.skip_reason))
        return r

    # Header-only finding: claimed-24 with declared format... we can only
    # know for sure with audio-domain check below.
    if bd == 16 and sr == 44100:
        r.findings.append((
            "cd_format",
            "info",
            "16-bit 44.1 kHz — standard CD format",
        ))
    elif bd >= 24 and sr >= 88200:
        r.findings.append((
            "hires_claim",
            "info",
            f"{bd}-bit {sr/1000:.1f} kHz — claims hi-resolution audio "
            f"(verify with deep analysis)",
        ))

    # Bail here if not doing deep analysis
    if not deep:
        r.analysis_skipped = True
        r.skip_reason = "deep analysis disabled (header-only mode)"
        return r

    if not _have_ffmpeg():
        r.analysis_skipped = True
        r.skip_reason = "ffmpeg not installed — header-only mode"
        r.findings.append((
            "no_ffmpeg",
            "warn",
            "install ffmpeg for spectral / sample-level analysis",
        ))
        return r

    r.analysis_skipped = False

    # Identify the ripping software. This works WITHOUT ffmpeg —
    # purely tag inspection — so we do it even if ffmpeg is missing.
    # (But that branch already returned above, so we're definitely
    # post-ffmpeg-check here. Could be moved up earlier if header-only
    # mode should also try ripper-ID.)
    try:
        ripper, confidence, evidence = identify_ripper(p)
        if ripper:
            r.ripper_guess = ripper
            r.ripper_confidence = confidence
            r.ripper_evidence = list(evidence)
            r.findings.append((
                "ripper_id",
                "info",
                f"{ripper} ({confidence} confidence)",
            ))
    except Exception:
        pass

    # The bottom-bit silence check for fake hi-res. This one DOES work
    # reliably — it reads actual sample bytes, no filter rolloff to
    # confuse the result.
    if bd >= 24:
        is_fake, reason = _check_fake_hires(p, bd, sr, timeout_s=timeout_s)
        if is_fake:
            r.likely_fake_hires = True
            r.findings.append((
                "fake_hires_lowbits",
                "alarm",
                reason,
            ))
        else:
            r.findings.append((
                "bit_depth_check",
                "info",
                reason,
            ))

    return r


def format_analysis(r: AudioAnalysis, *, verbose: bool = False) -> str:
    """Render an AudioAnalysis as human-readable lines."""
    lines = [
        f"  {r.path}",
        f"    codec={r.codec}  {r.bit_depth}-bit {r.sample_rate/1000:.1f} kHz  "
        f"{r.duration_seconds:.0f}s  {r.channel_count}ch",
    ]
    if r.ripper_guess:
        lines.append(f"    ripper: {r.ripper_guess}  ({r.ripper_confidence} confidence)")
    if r.analysis_skipped:
        lines.append(f"    (deep analysis skipped: {r.skip_reason})")
        return "\n".join(lines)

    # Verdict summary
    verdicts = []
    if r.likely_fake_hires:
        verdicts.append("✗ FAKE HI-RES")
    if verdicts:
        lines.append(f"    verdict: {'; '.join(verdicts)}")

    if verbose:
        for label, sev, detail in r.findings:
            mark = {"info": "i", "warn": "!", "alarm": "✗"}.get(sev, " ")
            lines.append(f"      [{mark}] {label}: {detail}")
        if r.ripper_evidence and verbose:
            for ev in r.ripper_evidence:
                lines.append(f"      [i] {ev}")

    return "\n".join(lines)

    return "\n".join(lines)
