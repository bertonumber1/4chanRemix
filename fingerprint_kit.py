"""Acoustic fingerprint comparison — "are these two files the same recording?"

Ported from music-tools/label2lossless's fpkit.py + bitmusic_fingerprint.py
(a separate, already-proven toolkit built against this same Bit Music
archive), generalised so any part of this app can ask the question, not
just the multi-CD dedupe path that motivated porting it in.

A chromaprint is one 32-bit word per ~0.124s of audio. Two encodings of the
SAME recording agree in almost every bit; two different recordings agree in
about half, which is what noise looks like. Rips do not always start at the
same instant, so comparison slides one fingerprint against the other and
keeps the best alignment — without that, two seconds of extra leading
silence looks like a different track.

Audio similarity is NEVER trusted alone here: a release routinely carries an
Original Mix, an Extended Mix and a Radio Edit of the same musical material,
and over a long window those score well above SAME because they genuinely
share the same audio. What separates them is LENGTH, so callers making a
"same file" decision must also check duration agreement — see
multicd_dedupe.py's build_fix_plan() for the reference pattern.

Two things have to exist for this module to do anything:
  - fpcalc (fingerprint GENERATION): found on PATH, or at the well-known
    MusicBrainz Picard install location on Windows, or pointed at directly
    via the FPCALC env var. Never bundled — it's a common tool, more likely
    to already be on a box that also runs Picard/beets than not.
  - libchromaprint (fingerprint COMPARISON, i.e. decoding a fingerprint back
    into bits): on Linux this is normally an apt/pip-installable shared
    library and the `chromaprint` ctypes wrapper just finds it. On Windows
    there is no standalone redistributable — fpcalc.exe statically links
    it — so a copy built from the official chromaprint source (see
    vendor/chromaprint-windows-x64/BUILD.md) ships in this repo.

Neither is required for the app to run: AVAILABLE is False and every
function here returns its "nothing to say" value when either is missing,
the same self-disabling pattern the Fake-FLAC tab already uses for
sonic-annotator.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
_VENDORED_DLL_DIR = os.path.join(HERE, "vendor", "chromaprint-windows-x64")
_VENDORED_PY_DIR = os.path.join(HERE, "vendor", "pyacoustid-chromaprint")
_WINDOWS_PICARD_FPCALC = r"C:\Program Files\MusicBrainz Picard\fpcalc.exe"

CACHE = os.path.expanduser(os.path.join(
    "~", ".local", "share", "music-organiser", "fingerprints.json"))

FPLEN = 120             # seconds of audio to fingerprint; plenty to identify a track
SAME = 0.90             # above this, and only WITH duration agreement, the same recording
MAX_OFFSET = 30         # frames of slide either way (~3.7s)
MIN_OVERLAP = 80        # frames that must overlap for a verdict (~10s)
MAX_DUR_DELTA = 2.5     # seconds two copies of one recording may differ by

_POPCOUNT = np.unpackbits(np.arange(256, dtype=np.uint8)[:, None], axis=1).sum(1)


def _find_fpcalc() -> str:
    env = os.environ.get("FPCALC")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("fpcalc")
    if found:
        return found
    if sys.platform == "win32" and os.path.isfile(_WINDOWS_PICARD_FPCALC):
        return _WINDOWS_PICARD_FPCALC
    return ""


FPCALC_PATH = _find_fpcalc()

if sys.platform == "win32" and os.path.isdir(_VENDORED_DLL_DIR):
    os.environ["PATH"] = _VENDORED_DLL_DIR + os.pathsep + os.environ.get("PATH", "")

# The ctypes wrapper is vendored, not pip-installed: PyPI's own "chromaprint"
# package is an unrelated project that happens to squat the name (see
# vendor/pyacoustid-chromaprint/README.md) — importing it bare here would
# silently pick up whichever one happens to be installed.
if _VENDORED_PY_DIR not in sys.path:
    sys.path.insert(0, _VENDORED_PY_DIR)

try:
    import chromaprint as _cp
    _CHROMAPRINT_ERROR = ""
except Exception as _exc:                                    # pragma: no cover
    _cp = None
    _CHROMAPRINT_ERROR = str(_exc)

AVAILABLE = bool(FPCALC_PATH and _cp is not None)


# ─── generation ──────────────────────────────────────────────────────────────
def fingerprint_file(path: str, length: int = FPLEN) -> tuple[float, str]:
    """(duration, base64 fingerprint), or (0.0, "") on anything unreadable.

    Never raises — a corrupt or missing file is data, not an exception to
    handle at every call site.
    """
    if not FPCALC_PATH:
        return 0.0, ""
    try:
        p = subprocess.run([FPCALC_PATH, "-length", str(length), path],
                           capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return 0.0, ""
    dur, fp = 0.0, ""
    for line in p.stdout.splitlines():
        if line.startswith("DURATION="):
            try:
                dur = float(line[9:])
            except ValueError:
                pass
        elif line.startswith("FINGERPRINT="):
            fp = line[12:]
    return (dur, fp) if fp else (0.0, "")


# ─── cache (path -> {size, mtime, dur, fp}, invalidated on size/mtime) ────────
def load_cache(path: str = CACHE) -> dict:
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cache(cache: dict, path: str = CACHE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    os.replace(tmp, path)


def fingerprint_cached(path: str, cache: dict) -> tuple[float, str]:
    """Same return shape as fingerprint_file(), but reuses `cache` (as
    returned by load_cache()) keyed on (size, mtime) and mutates it in
    place — the caller decides when to save_cache()."""
    try:
        st = os.stat(path)
    except OSError:
        return 0.0, ""
    rec = cache.get(path)
    if rec and rec.get("size") == st.st_size and abs(rec.get("mtime", 0) - st.st_mtime) < 2:
        return rec.get("dur") or 0.0, rec.get("fp") or ""
    dur, fp = fingerprint_file(path)
    cache[path] = {"size": st.st_size, "mtime": st.st_mtime, "dur": dur, "fp": fp}
    return dur, fp


# ─── comparison ────────────────────────────────────────────────────────────
_decoded: dict = {}


def decode(b64: str):
    """base64 chromaprint -> uint32 numpy array, or None. Memoised —
    decoding is the expensive part when comparing many pairs."""
    if not b64 or _cp is None:
        return None
    got = _decoded.get(b64)
    if got is None:
        try:
            raw, _ = _cp.decode_fingerprint(b64.encode())
            got = np.asarray(raw, dtype=np.uint32)
        except Exception:
            got = None
        _decoded[b64] = got
    return got


def _score(a, b, off):
    if off >= 0:
        x, y = a[off:], b
    else:
        x, y = a, b[-off:]
    n = min(len(x), len(y))
    if n < MIN_OVERLAP:
        return None
    diff = np.bitwise_xor(x[:n], y[:n])
    bits = int(_POPCOUNT[diff.view(np.uint8)].sum())
    return 1.0 - bits / (n * 32.0)


def _slide(a, b, k):
    n = min(len(a), len(b)) - k
    if n < MIN_OVERLAP:
        n = min(len(a), len(b))
        k = 0
        if n < MIN_OVERLAP:
            return 0.0
    win = np.lib.stride_tricks.sliding_window_view(a[:n + k], n)[:k + 1]
    diff = np.bitwise_xor(win, b[:n])
    bits = _POPCOUNT[diff.view(np.uint8)].reshape(diff.shape[0], -1).sum(1)
    return float(1.0 - bits.min() / (n * 32.0))


def similarity(fa, fb) -> float:
    """Best bit agreement between two decoded fingerprints over the slide
    window. 0.0 when either is missing/empty — never raises."""
    if fa is None or fb is None or len(fa) == 0 or len(fb) == 0:
        return 0.0
    s = _score(fa, fb, 0)
    if s is not None and s >= 0.98:
        return s
    best = s or 0.0
    return max(best, _slide(fa, fb, MAX_OFFSET), _slide(fb, fa, MAX_OFFSET))


def same(fa, fb, bar: float = SAME) -> bool:
    return similarity(fa, fb) >= bar


def same_recording(path_a: str, path_b: str, cache: dict,
                    bar: float = SAME, dur_delta: float = MAX_DUR_DELTA) -> bool:
    """The full, corroborated check a caller should actually use: same
    audio AND same length. Never true when fingerprinting isn't available
    on this machine — callers fall back to their own duration-only logic
    in that case, they don't skip the check silently."""
    if not AVAILABLE:
        return False
    dur_a, fp_a = fingerprint_cached(path_a, cache)
    dur_b, fp_b = fingerprint_cached(path_b, cache)
    if dur_a and dur_b and abs(dur_a - dur_b) > dur_delta:
        return False
    return same(decode(fp_a), decode(fp_b), bar)
