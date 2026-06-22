"""
rip_audio.py
============

Optional audio-analysis confirmation of suspected lossy transcodes,
using the Vamp lossy-encoding-detector plugin by Chris Cannam.

Why this is gated to a subset:
------------------------------
The Vamp plugin runs a small CNN over a spectrogram, similar to the
Hennequin et al ICASSP 2017 method. It's about 98% accurate on first
exposure. But it's slow:

  - full detector:  ~80 seconds per file
  - quick detector: ~3 seconds per file (one second of audio at 30s mark)

For a 207k-file library:
  full:  207,000 × 80s   = ~190 days of CPU time
  quick: 207,000 × 3s    = ~7 days

Neither is reasonable as a library-wide first-pass. fake_flac.py's
spectral-cutoff approach is far cheaper (sub-second per file) and
catches the obvious transcodes via the frequency-cliff heuristic.

So the intended flow is:

  1. fake_flac.py screens the whole library cheaply -> marks
     `transcode_suspected = 1` on files that look fishy.
  2. THIS module is run as a confirm-on-suspects pass on only those
     files. Same algorithm class but more accurate. For most libraries
     that's 1-5% of files, dropping cost to hours not weeks.

Dependencies (must be installed separately by the user):
  - The vamp-lossy-encoding-detector plugin compiled and installed.
    Their README has Meson/Ninja instructions; alternative is the
    debian package `vamp-plugin-lossy-encoding-detector` on some distros.
  - `sonic-annotator` from the Vamp project (`apt install
    sonic-annotator` on Debian/Ubuntu, `paru -S sonic-annotator` on Arch).

If either is missing, this module reports a clear "not installed"
message rather than failing silently.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


logger = logging.getLogger("music-organiser")


# Plugin identifiers as exposed by sonic-annotator / vamp-simple-host.
# Full: bigger CNN over the whole file. Quick: 1s sample.
PLUGIN_FULL  = "vamp:vamp-lossy-encoding-detector:lossydetector:lossy"
PLUGIN_QUICK = "vamp:vamp-lossy-encoding-detector:quicklossydetector:lossy"


@dataclass
class LossyAudioCheck:
    """Result of one vamp-plugin run on one file."""
    path: str = ""
    verdict: str = ""        # 'lossy' / 'original' / 'unknown' / ''
    confidence: float = 0.0  # 0..1 (plugin's own confidence)
    raw_output: str = ""     # for debugging
    error: str = ""


def find_sonic_annotator() -> str | None:
    """Return path to sonic-annotator binary, or None if not installed."""
    return shutil.which("sonic-annotator")


def is_plugin_installed(quick: bool = True) -> bool | None:
    """Quickly check whether the vamp-lossy-encoding-detector plugin is
    discoverable. Returns True/False, or None if sonic-annotator itself
    is missing (so caller knows which error to surface)."""
    sa = find_sonic_annotator()
    if sa is None:
        return None
    try:
        r = subprocess.run(
            [sa, "-l"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    plugin_name = PLUGIN_QUICK if quick else PLUGIN_FULL
    return plugin_name in r.stdout


def check_file(audio_path: Path | str, *, quick: bool = True,
               timeout_sec: int = 120) -> LossyAudioCheck:
    """
    Run the Vamp plugin on one audio file. Returns a LossyAudioCheck.

    Uses sonic-annotator under the hood. The plugin emits a single line
    of output like "0.0, 80.05: 1 Lossy" or "0.0, 80.05: 0 Original".
    """
    out = LossyAudioCheck(path=str(audio_path))
    sa = find_sonic_annotator()
    if not sa:
        out.error = "sonic-annotator not installed"
        return out

    plugin = PLUGIN_QUICK if quick else PLUGIN_FULL
    try:
        # -d <plugin> -w csv --csv-stdout <file>
        r = subprocess.run(
            [sa, "-d", plugin, "-w", "csv", "--csv-stdout", str(audio_path)],
            capture_output=True, text=True, timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        out.error = f"timed out after {timeout_sec}s"
        return out
    except OSError as e:
        out.error = f"sonic-annotator failed to launch: {e}"
        return out

    out.raw_output = (r.stdout + "\n" + r.stderr)[:2000]
    if r.returncode != 0:
        out.error = f"sonic-annotator exit {r.returncode}: {r.stderr[:200]}"
        return out

    # Parse: CSV is "path,start,duration,value,label"
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # very last column is the label
        # Examples:
        # filename,0.000000000,80.050793651,1.000000000,Lossy
        # filename,0.000000000,80.050793651,0.000000000,Original
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            confidence = float(parts[-2])
        except ValueError:
            continue
        label = parts[-1].strip().lower()
        if "lossy" in label:
            out.verdict = "lossy"
            out.confidence = float(confidence)
        elif "original" in label or "lossless" in label:
            out.verdict = "original"
            # plugin emits 0 for original, so flip the meaning
            out.confidence = 1.0 - float(confidence)
        return out

    out.verdict = "unknown"
    return out


def install_hint() -> str:
    """Multi-line user-facing instructions for installing the dependencies."""
    return (
        "  To use Vamp audio-based detection you need:\n"
        "    1. The Vamp plugin SDK + sonic-annotator host program.\n"
        "       Arch:   sudo pacman -S sonic-annotator\n"
        "       Debian: sudo apt install sonic-annotator\n"
        "    2. The vamp-lossy-encoding-detector plugin itself.\n"
        "       Source: https://github.com/cannam/vamp-lossy-encoding-detector\n"
        "       Build:  ./repoint install && meson setup build && \n"
        "               ninja -C build && sudo ninja -C build install\n"
        "    3. After install, verify by running:\n"
        "       sonic-annotator -l | grep lossy\n"
        "       (should list 'vamp-lossy-encoding-detector:lossydetector' etc)\n"
    )


def run_on_suspects(db, *, quick: bool = True,
                     limit: int = 0, log_cb=None) -> dict[str, int]:
    """
    Walk the DB, find files marked transcode_suspected=1 by fake_flac.py,
    run the Vamp plugin on each, write `transcode_audio_verdict` back to
    the DB row. Returns {checked, confirmed_lossy, confirmed_lossless, skipped}.

    quick=True uses the quick detector (~3s/file); False uses full
    (~80s/file).
    limit: stop after N files (0 = no limit). Useful for sanity-checking.
    """
    stats = {"checked": 0, "confirmed_lossy": 0, "confirmed_lossless": 0,
             "errors": 0, "skipped": 0}

    def emit(level: str, msg: str) -> None:
        if log_cb:
            log_cb(level, msg)

    if not find_sonic_annotator():
        emit("warning", "sonic-annotator not installed")
        stats["skipped"] = -1
        return stats
    if not is_plugin_installed(quick=quick):
        emit("warning", "vamp-lossy-encoding-detector plugin not installed")
        stats["skipped"] = -1
        return stats

    cursor = db.conn.execute(
        "SELECT path, codec FROM files "
        "WHERE transcode_suspected = 1 "
        "ORDER BY path"
    )
    n = 0
    for row in cursor:
        if limit and n >= limit:
            break
        path = row["path"]
        if not Path(path).exists():
            stats["skipped"] += 1
            continue
        stats["checked"] += 1
        n += 1
        emit("info", f"vamp: checking {Path(path).name}")
        result = check_file(path, quick=quick)
        if result.error:
            stats["errors"] += 1
            emit("broken", f"vamp error: {result.error}")
            continue
        # Update DB row with the audio verdict — we don't overwrite the
        # cheaper-screen `transcode_suspected` because that's how the user
        # found these files in the first place. We add a separate column.
        try:
            db.conn.execute(
                "UPDATE files SET transcode_notes = ? WHERE path = ?",
                (f"vamp:{result.verdict}:{result.confidence:.3f}", path),
            )
            db.conn.commit()
        except Exception as e:
            stats["errors"] += 1
            emit("broken", f"db update failed: {e}")
            continue
        if result.verdict == "lossy":
            stats["confirmed_lossy"] += 1
            emit("warning", f"CONFIRMED LOSSY ({result.confidence:.2f}): "
                            f"{Path(path).name}")
        elif result.verdict == "original":
            stats["confirmed_lossless"] += 1
            emit("info", f"confirmed original ({result.confidence:.2f}): "
                        f"{Path(path).name}")
    return stats
