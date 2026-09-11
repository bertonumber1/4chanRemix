#!/usr/bin/env python3
"""Authenticity checking for the Labels tab — the ONE sanctioned crossing from
Labels into the SPEK-TRO / fake-FLAC checker, read-only, as a library call.

This module calls `fake_flac.analyse(path)` directly on a file path. It does
NOT go through fake_flac's DB-row batch pipeline (`verify_lossless_in_db`) —
that pipeline is shaped around library.db rows, and Labels tracks folders on
disk, not imported files. Nothing in here writes to library.db, and nothing
in the main pipeline (importer, organiser_core, library.db, the other tabs)
reads anything this module writes — see label_panel.py's own rule that the
rest of the app never reads label_state.json/label-cache/.

Must not import label_ref or label_panel — the crossing is one-directional.
"""
from __future__ import annotations

import json
import os
import sys
import time

# label_authenticity.py sits in zzzzScriptstuff/ so it can plainly `import
# fake_flac`/`import spectral` as siblings. Bootstrap sys.path with our own
# directory rather than assuming a caller already did it — web_ui.py adds
# zzzzScriptstuff to sys.path at process start, but label_panel_test.py
# imports label_panel directly and never runs that bootstrap.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_flac  # noqa: E402

# The house rule (already enforced inside fake_flac._spectral_analyse, kept
# here too so this module's own contract is explicit and testable on its
# own): only these verdicts count as a confident transcode. "suspect" is one
# signal only — vinyl rips and deliberately dull masters look band-limited
# too — and must never, on its own, take a release out of "complete".
CONDEMN_VERDICTS = {"lossy", "padded", "upsampled", "lossy_format"}

# Worst-first, for picking one representative verdict/confidence out of a
# folder's several checked files.
_VERDICT_PRIORITY = ["lossy", "lossy_format", "padded", "upsampled", "suspect",
                     "window-only", "clean", "unreadable"]


def available() -> bool:
    """True if fake_flac's own dependencies (numpy, soundfile) import.
    Callers must treat False as 'unchecked', never as 'clean'."""
    try:
        return bool(fake_flac.dependencies_available())
    except Exception:
        return False


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


class AuthenticityCache:
    """Per-file transcode verdicts, keyed (path, mtime_ns, size) — the same
    stamp granularity spectral.py already uses for its own spectrogram-PNG
    cache, just per-file instead of FolderCache's per-folder (mtime,
    filecount) stamp, because a single track can be re-ripped in place
    without its folder's file count changing.
    """

    def __init__(self, cache_dir: str):
        self.path = os.path.join(cache_dir, "authenticity.json")
        self._data = _read_json(self.path, {})
        self._dirty = False

    @staticmethod
    def _stamp(path: str):
        try:
            st = os.stat(path)
        except OSError:
            return None
        return "%d:%d" % (st.st_mtime_ns, st.st_size)

    def get(self, path: str) -> dict | None:
        """The cached verdict for this exact file at its current mtime/size,
        or None on a miss (changed, or never checked)."""
        stamp = self._stamp(path)
        if stamp is None:
            return None
        entry = self._data.get(path)
        if entry and entry.get("stamp") == stamp:
            return dict(entry.get("verdict") or {})
        return None

    def put(self, path: str, verdict: dict) -> None:
        stamp = self._stamp(path)
        if stamp is None:
            return
        self._data[path] = {"stamp": stamp, "verdict": verdict}
        self._dirty = True

    def save(self) -> None:
        if self._dirty:
            _write_json(self.path, self._data)
            self._dirty = False


def _unreadable(reason: str) -> dict:
    return {"verdict": "unreadable", "condemned": False, "confidence": 0.0,
            "notes": reason, "checked_at": time.time()}


def _to_verdict(analysis) -> dict:
    """fake_flac.TranscodeAnalysis -> this module's verdict shape.  Kept
    independent of fake_flac's dataclass so this module has no hard
    dependency on its internals beyond .analyse()'s return."""
    if analysis is None:
        return _unreadable("no result from the analyser")
    verdict = analysis.verdict or "unreadable"
    return {
        "verdict": verdict,
        "condemned": verdict in CONDEMN_VERDICTS,
        "confidence": float(getattr(analysis, "confidence", 0.0) or 0.0),
        "notes": (getattr(analysis, "notes", "") or "")[:600],
        "checked_at": time.time(),
    }


def analyse_file(path: str, cache: AuthenticityCache) -> dict:
    """Cache-first: returns the cached verdict if the file is unchanged, else
    calls fake_flac.analyse(path), stores the result, returns it.  Never
    raises — a per-file analysis failure returns an 'unreadable' verdict
    rather than aborting the folder.
    """
    cached = cache.get(path)
    if cached is not None:
        return cached
    try:
        result = fake_flac.analyse(path)
    except Exception as exc:
        verdict = _unreadable("analysis failed: %s" % exc)
    else:
        verdict = _to_verdict(result)
    cache.put(path, verdict)
    return verdict


def _sample_indices(n: int) -> list:
    """Which track indices to check FIRST.  n<=3: every track.  Otherwise
    first/middle/last — spread across the release rather than clustered at
    the start, since a transcode is a property of the SOURCE the whole
    release was ripped from, and a spread sample is more likely to catch a
    release that is only partly re-encoded."""
    if n <= 0:
        return []
    if n <= 3:
        return list(range(n))
    return sorted({0, n // 2, n - 1})


def _worst(results: list) -> dict:
    if not results:
        return {"verdict": "unreadable", "confidence": 0.0, "notes": ""}

    def rank(r):
        try:
            return _VERDICT_PRIORITY.index(r.get("verdict", "unreadable"))
        except ValueError:
            return len(_VERDICT_PRIORITY)

    return min(results, key=rank)


def authenticate_folder(folder_path: str, cache: AuthenticityCache, log=print,
                        should_stop=None) -> dict:
    """Sample 2-3 tracks; escalate to every track ONLY if the sample condemns
    one.  Never raises.  Returns:
        {"checked": True, "sampled": int, "total": int, "escalated": bool,
         "condemned": bool, "worst_verdict": str, "confidence": float,
         "notes": str, "per_file": [{"path": ..., **verdict}, ...]}
    or {"checked": False, "reason": "..."} when unavailable, empty, or
    stopped before a single file was analysed.
    """
    if not available():
        return {"checked": False, "reason": "numpy/soundfile not installed"}
    try:
        import label_ref as L                      # the one narrow crossing
        files = [f for f in L.audio_files(folder_path)
                if f.lower().endswith(L.LOSSLESS_EXT)]
    except Exception as exc:
        return {"checked": False, "reason": "could not list folder: %s" % exc}
    if not files:
        return {"checked": False, "reason": "no lossless audio"}

    def check_all(paths):
        out = []
        for p in paths:
            if should_stop and should_stop():
                return None
            out.append({"path": p, **analyse_file(p, cache)})
        return out

    sample_paths = [files[i] for i in _sample_indices(len(files))]
    sample_results = check_all(sample_paths)
    if sample_results is None:
        return {"checked": False, "reason": "stopped"}

    condemned_sample = any(r["condemned"] for r in sample_results)
    if not condemned_sample:
        worst = _worst(sample_results)
        return {"checked": True, "sampled": len(sample_results), "total": len(files),
                "escalated": False, "condemned": False,
                "worst_verdict": worst["verdict"], "confidence": worst["confidence"],
                "notes": worst.get("notes", ""), "per_file": sample_results}

    sampled_set = set(sample_paths)
    remaining = [f for f in files if f not in sampled_set]
    rest_results = check_all(remaining)
    if rest_results is None:
        worst = _worst(sample_results)
        return {"checked": True, "sampled": len(sample_results), "total": len(files),
                "escalated": True, "condemned": True,
                "worst_verdict": worst["verdict"], "confidence": worst["confidence"],
                "notes": "stopped mid-escalation", "per_file": sample_results}

    all_results = sample_results + rest_results
    worst = _worst(all_results)
    return {"checked": True, "sampled": len(sample_results), "total": len(files),
            "escalated": True, "condemned": any(r["condemned"] for r in all_results),
            "worst_verdict": worst["verdict"], "confidence": worst["confidence"],
            "notes": worst.get("notes", ""), "per_file": all_results}
