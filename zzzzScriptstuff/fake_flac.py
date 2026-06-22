"""
fake_flac.py
============

Detect FLACs that were transcoded from a lossy source (MP3/AAC/etc).

Background:
- Real lossless audio from a CD master has energy across the full
  20 Hz – 22.05 kHz spectrum (Nyquist of 44.1 kHz).
- Lossy codecs discard high-frequency content above a bitrate-dependent
  cutoff:  MP3 128 kbps cuts ~16 kHz, 192 ~18 kHz, 256/320 ~19-20 kHz.
  AAC and OGG have similar but slightly higher cutoffs.
- If a "FLAC" has a hard cutoff well below 22 kHz, it was almost certainly
  encoded from a lossy source. The FLAC encoding step is lossless, so the
  cutoff from the original lossy encode survives.

This module:
1. Reads a sample of audio in the middle of the track (where the song
   is more likely to have wideband energy — intros and outros are
   often quieter).
2. Computes the FFT magnitude spectrum.
3. Walks the spectrum from Nyquist downward, looking for the highest
   frequency where energy crosses a noise-floor threshold.
4. Returns the cutoff plus a confidence value:
     cutoff_hz < 17000        -> very suspicious  (confidence ~0.9)
     cutoff_hz < 19000        -> suspicious       (confidence ~0.6)
     cutoff_hz < 20500        -> borderline       (confidence ~0.3)
     cutoff_hz >= 20500       -> probably real lossless

Limitations & honesty:
- Some legitimate sources are inherently band-limited (e.g. vinyl rips,
  spoken-word recordings, classical music with no high-freq content).
  These will *look* like fake lossless. The `notes` field flags this case
  when energy across the whole spectrum is unusually low.
- Modern lossy codecs (Opus, AAC-HE) preserve more high-freq than old
  MP3 and can pass this test. They're rarer in "lossless" libraries
  though so practical false-positive rate is low.
- We only analyse FLAC and similar lossless containers. MP3 files
  obviously have lossy cutoffs by definition — no point checking.

We don't decode the whole file. Reading 5-10 seconds in the middle is
enough for the FFT to be reliable and keeps per-file analysis to ~50-100ms.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# numpy is required. soundfile is preferred; audioread is a fallback.
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore
    NUMPY_AVAILABLE = False

try:
    import soundfile as sf
    SOUNDFILE_AVAILABLE = True
except ImportError:
    sf = None  # type: ignore
    SOUNDFILE_AVAILABLE = False


# How many seconds of audio to analyse. More is more reliable but slower.
ANALYSIS_DURATION_SECONDS = 8.0


@dataclass
class TranscodeAnalysis:
    """Result of analysing one file for lossy-source signatures."""
    suspected: bool
    cutoff_hz: float
    confidence: float
    notes: str

    def to_db_columns(self) -> dict[str, Any]:
        """Format as DB column updates."""
        return {
            "transcode_checked": 1,
            "transcode_suspected": 1 if self.suspected else 0,
            "transcode_cutoff_hz": float(self.cutoff_hz),
            "transcode_confidence": float(self.confidence),
            "transcode_notes": self.notes,
        }


def dependencies_available() -> bool:
    """True if we have everything we need to run analysis."""
    return NUMPY_AVAILABLE and SOUNDFILE_AVAILABLE


def missing_dependencies() -> list[str]:
    miss = []
    if not NUMPY_AVAILABLE:
        miss.append("numpy")
    if not SOUNDFILE_AVAILABLE:
        miss.append("soundfile")
    return miss


def _read_middle_window(
    path: Path,
    seconds: float = ANALYSIS_DURATION_SECONDS,
) -> tuple[Any, int] | None:
    """
    Read `seconds` of audio from the middle of the file. Returns
    (samples, sample_rate) or None on failure.

    Samples are returned as float, mono (mixed if multichannel) to keep
    the FFT simple — phase between channels doesn't matter for cutoff
    detection.
    """
    if not SOUNDFILE_AVAILABLE:
        return None

    try:
        with sf.SoundFile(str(path)) as f:
            total_frames = len(f)
            sample_rate = f.samplerate
            if total_frames <= 0 or sample_rate <= 0:
                return None

            window_frames = int(seconds * sample_rate)
            if window_frames > total_frames:
                window_frames = total_frames
                start = 0
            else:
                # Start in the middle, then back up by half the window.
                start = max(0, (total_frames // 2) - (window_frames // 2))

            f.seek(start)
            data = f.read(frames=window_frames, dtype="float32", always_2d=True)

        # Mix to mono.
        if data.ndim == 2:
            mono = data.mean(axis=1)
        else:
            mono = data
        return mono, sample_rate
    except Exception:
        return None


def _find_cutoff_hz(
    samples: Any,
    sample_rate: int,
    *,
    noise_floor_db: float = -80.0,
) -> tuple[float, float]:
    """
    Find the highest frequency with significant energy in `samples`.

    Returns (cutoff_hz, mean_energy_db).

    Implementation:
      - FFT the whole window, take magnitude (we don't care about phase).
      - Convert to dB relative to the spectral peak.
      - Walk from Nyquist downward; the first bin above `noise_floor_db`
        is the cutoff.

    The Nyquist frequency is sample_rate / 2. For a 44.1 kHz FLAC,
    Nyquist = 22050 Hz. For 48 kHz, Nyquist = 24000 Hz. Higher-rate files
    (88.2, 96, 192 kHz) have higher Nyquists but the same lossy-cutoff
    indicators are still present in the audible band.
    """
    if not NUMPY_AVAILABLE:
        raise RuntimeError("numpy required")

    # Apply a Hann window to reduce spectral leakage.
    n = len(samples)
    if n < 1024:
        return 0.0, -120.0
    window = np.hanning(n)
    windowed = samples * window

    spectrum = np.fft.rfft(windowed)
    magnitude = np.abs(spectrum)
    # Avoid log(0)
    magnitude = np.maximum(magnitude, 1e-12)

    # Convert to dB relative to peak.
    peak = magnitude.max()
    db = 20.0 * np.log10(magnitude / peak)

    # Mean energy in dB across the full spectrum — used to detect very
    # quiet windows (silence) where the cutoff measurement is unreliable.
    mean_db = float(db.mean())

    # Frequencies for each bin
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)

    # Walk from the top of the spectrum down — find the highest freq
    # whose energy exceeds noise_floor_db.
    above = db > noise_floor_db
    if not above.any():
        return 0.0, mean_db

    # np.where returns indices in ascending order; we want the max.
    idx = int(np.where(above)[0].max())
    cutoff_hz = float(freqs[idx])
    return cutoff_hz, mean_db


def _classify(cutoff_hz: float, sample_rate: int, mean_db: float) -> tuple[bool, float, str]:
    """
    Given a measured cutoff and overall energy, classify the file.

    Returns (suspected, confidence, notes).
    """
    nyquist = sample_rate / 2.0

    # Sanity / silence guard. If mean energy is very low across the whole
    # spectrum, the file is probably mostly silent or we got an unlucky
    # window — don't accuse it of being fake.
    if mean_db < -90.0:
        return False, 0.0, "window too quiet for reliable analysis"

    # If the file is high-sample-rate (>48 kHz), the Nyquist is much
    # higher than 22 kHz. The MP3-style cutoff signature stays in the
    # 16-20 kHz range regardless, so we compare against fixed thresholds
    # not Nyquist.

    # Distance from Nyquist for confidence scaling on real lossless.
    # On standard 44.1 kHz files, Nyquist = 22050. Real lossless rolls off
    # gently near Nyquist (the encoder's anti-alias filter); a cutoff above
    # 21 kHz is normal. The lossy signature is a hard cutoff WELL below.

    if cutoff_hz < 14000:
        return True, 0.95, f"hard cutoff at {cutoff_hz:.0f} Hz — likely MP3 ≤96 kbps source"
    if cutoff_hz < 16500:
        return True, 0.90, f"hard cutoff at {cutoff_hz:.0f} Hz — likely MP3 128 kbps source"
    if cutoff_hz < 18500:
        return True, 0.75, f"cutoff at {cutoff_hz:.0f} Hz — likely MP3 160-192 kbps source"
    if cutoff_hz < 19800:
        return True, 0.55, f"cutoff at {cutoff_hz:.0f} Hz — possible MP3 224-256 kbps or AAC source"
    if cutoff_hz < 20500 and nyquist > 21000:
        # Borderline. Could be old vinyl rip or a 320 kbps transcode.
        return True, 0.30, f"borderline cutoff at {cutoff_hz:.0f} Hz — possible 320 kbps source or band-limited master"

    # cutoff within a normal range of Nyquist
    if nyquist > 21000:
        # 44.1 or 48 kHz — expect cutoff close to Nyquist for real lossless
        return False, 0.05, f"cutoff at {cutoff_hz:.0f} Hz — consistent with lossless"
    else:
        # Unusual sample rate; report but don't flag
        return False, 0.0, f"cutoff at {cutoff_hz:.0f} Hz (sample rate {sample_rate} Hz)"


def analyse(path: str | Path) -> TranscodeAnalysis | None:
    """
    Run the fake-FLAC check on a single file.

    Returns None if dependencies are missing or the file is unreadable.
    Caller is responsible for filtering inputs to lossless containers —
    running this on an MP3 will correctly identify the MP3 cutoff but
    that's not useful information.
    """
    if not dependencies_available():
        return None

    p = Path(path)
    if not p.exists():
        return None

    read = _read_middle_window(p)
    if read is None:
        return TranscodeAnalysis(
            suspected=False,
            cutoff_hz=0.0,
            confidence=0.0,
            notes="could not read audio data",
        )

    samples, sample_rate = read
    if len(samples) < 4096:
        return TranscodeAnalysis(
            suspected=False,
            cutoff_hz=0.0,
            confidence=0.0,
            notes=f"file too short ({len(samples)} samples) for reliable FFT",
        )

    cutoff_hz, mean_db = _find_cutoff_hz(samples, sample_rate)
    suspected, confidence, notes = _classify(cutoff_hz, sample_rate, mean_db)
    return TranscodeAnalysis(
        suspected=suspected,
        cutoff_hz=cutoff_hz,
        confidence=confidence,
        notes=notes,
    )


# =============================================================================
# BATCH RUNNER
# =============================================================================

from dataclasses import field as _field


@dataclass
class VerifyStats:
    files_checked: int = 0
    files_suspect: int = 0
    files_clean: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    errors: list[str] = _field(default_factory=list)

    def summary(self) -> str:
        return (
            f"checked: {self.files_checked}\n"
            f"suspect (transcoded?): {self.files_suspect}\n"
            f"clean: {self.files_clean}\n"
            f"skipped (already checked): {self.files_skipped}\n"
            f"failed: {self.files_failed}"
        )


def verify_lossless_in_db(
    db,
    *,
    ui: Any = None,
    force: bool = False,
    only_codecs: tuple[str, ...] = ("flac", "alac", "ape", "wv",
                                    "wave", "aiff", "trueaudio"),
) -> VerifyStats:
    """
    Walk every row in the DB that looks lossless, run analyse() on each,
    save results back into the row.

    `force` re-checks files that have already been analysed.
    `ui` is an optional ui.LiveImportUI / PlainImportUI.
    """
    stats = VerifyStats()
    if not dependencies_available():
        miss = ", ".join(missing_dependencies())
        raise RuntimeError(
            f"fake-flac detection needs: {miss}. "
            f"Install with: pacman -S python-numpy python-soundfile, "
            f"or: pip install --user numpy soundfile"
        )

    # Build the list of files to check.
    placeholders = ", ".join("?" for _ in only_codecs)
    if force:
        sql = (
            f"SELECT path, codec, size_bytes FROM files "
            f"WHERE codec IN ({placeholders}) "
            f"  AND status != 'broken'"
        )
        params = list(only_codecs)
    else:
        sql = (
            f"SELECT path, codec, size_bytes FROM files "
            f"WHERE codec IN ({placeholders}) "
            f"  AND status != 'broken' "
            f"  AND (transcode_checked IS NULL OR transcode_checked = 0)"
        )
        params = list(only_codecs)

    rows = db.conn.execute(sql, params).fetchall()
    total = len(rows)

    if ui is not None:
        ui.set_total(total)
        ui.log("info", f"checking {total} lossless files for fake-FLAC signatures")

    # Already-checked count
    if not force:
        already = db.conn.execute(
            "SELECT COUNT(*) FROM files WHERE transcode_checked = 1 AND codec IN "
            f"({placeholders})", params,
        ).fetchone()[0]
        stats.files_skipped = already

    for row in rows:
        path = row["path"] if hasattr(row, "keys") else row[0]
        codec = row["codec"] if hasattr(row, "keys") else row[1]
        size_b = row["size_bytes"] if hasattr(row, "keys") else row[2]

        if ui is not None:
            ui.update(
                current_file=path,
                file_size_bytes=size_b or 0,
                file_codec=codec or "",
            )

        try:
            result = analyse(path)
        except Exception as e:
            stats.files_failed += 1
            stats.errors.append(f"{path}: {e}")
            if ui is not None:
                ui.advance(broken=True)
                ui.log("broken", f"{Path(path).name}  ({e})")
            continue

        stats.files_checked += 1
        if result is None:
            stats.files_failed += 1
            if ui is not None:
                ui.advance(broken=True)
            continue

        update = result.to_db_columns()
        update["path"] = path
        with db.transaction():
            db.upsert_file(update)

        if result.suspected:
            stats.files_suspect += 1
            kind = "broken"   # red — most noticeable
            msg = f"{Path(path).name}  → {result.notes}"
        else:
            stats.files_clean += 1
            kind = "imported"
            msg = f"{Path(path).name}  ✓ {result.notes}"

        if ui is not None:
            ui.advance(imported=not result.suspected, broken=result.suspected,
                       size_bytes=size_b or 0)
            ui.log(kind, msg)
            # Show the result in the grabbing panel so you can see live what's happening
            ui.set_grabbing({
                "cutoff_hz":  f"{result.cutoff_hz:.0f} Hz",
                "confidence": f"{result.confidence:.0%}",
                "verdict":    "SUSPECT" if result.suspected else "clean",
                "notes":      result.notes,
            })

    return stats
