#!/usr/bin/env python3
"""Unit tests for the Labels-tab authenticity wrapper around fake-FLAC/spectral.

    python3 tests/label_authenticity_test.py

fake_flac.analyse() is monkeypatched throughout — no real audio decode, no
numpy/soundfile dependency needed to run this suite. What's under test is the
sampling/escalation policy, the condemn rule, and the (path, mtime, size)
cache — not the spectral analyser itself, which has its own domain.
"""
import os
import sys
import tempfile
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "zzzzScriptstuff"))

import label_authenticity as A                               # noqa: E402

fails = []
ran = 0


def check(ok, label, detail=""):
    global ran
    ran += 1
    print(("  ok    " if ok else "  FAIL  ") + label + ((" " + detail) if detail else ""))
    if not ok:
        fails.append(label)


def eq(got, want, label):
    check(got == want, label, "" if got == want else "got %r, want %r" % (got, want))


def touch(path, size=16):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)


def fake_analysis(verdict, confidence=80.0, notes="synthetic"):
    return types.SimpleNamespace(verdict=verdict, confidence=confidence, notes=notes)


print("[_sample_indices]")
eq(A._sample_indices(0), [], "zero files, nothing to sample")
eq(A._sample_indices(1), [0], "one file — check it")
eq(A._sample_indices(2), [0, 1], "two files — check both")
eq(A._sample_indices(3), [0, 1, 2], "three files — check all, not just a sample")
eq(A._sample_indices(5), [0, 2, 4], "more than three — first, middle, last")
eq(A._sample_indices(10), [0, 5, 9], "spread across the whole release, not clustered at the start")

print("\n[condemn rule]")
for v in ("lossy", "padded", "upsampled", "lossy_format"):
    eq(A._to_verdict(fake_analysis(v))["condemned"], True,
       "%s is condemned" % v)
for v in ("suspect", "window-only", "clean"):
    eq(A._to_verdict(fake_analysis(v))["condemned"], False,
       "%s is NEVER condemned on its own — one signal is not enough" % v)
eq(A._to_verdict(None)["verdict"], "unreadable", "no analyser result reads as unreadable")
eq(A._to_verdict(None)["condemned"], False, "…and unreadable is not condemned either")

print("\n[AuthenticityCache: cache-first, never re-analyses an unchanged file]")
with tempfile.TemporaryDirectory() as tmp:
    p = os.path.join(tmp, "01 - t.flac")
    touch(p)
    cache = A.AuthenticityCache(tmp)
    calls = {"n": 0}
    real_analyse = A.fake_flac.analyse
    try:
        def counting_analyse(path):
            calls["n"] += 1
            return fake_analysis("clean")
        A.fake_flac.analyse = counting_analyse
        first = A.analyse_file(p, cache)
        eq(first["verdict"], "clean", "first call actually analyses the file")
        eq(calls["n"], 1, "and calls the analyser exactly once")
        second = A.analyse_file(p, cache)
        eq(second["verdict"], "clean", "second call returns the same verdict")
        eq(calls["n"], 1, "…without calling the analyser again — the cache was used")

        # A fresh AuthenticityCache reading the same directory must see it too —
        # proves the cache actually persists to disk, not just in-memory.
        cache.save()
        cache2 = A.AuthenticityCache(tmp)

        def raising_analyse(path):
            raise AssertionError("must not be called — the file is unchanged")
        A.fake_flac.analyse = raising_analyse
        third = A.analyse_file(p, cache2)
        eq(third["verdict"], "clean", "a reloaded cache still hits, across process-equivalent instances")

        # Change the file: must invalidate.
        touch(p, size=32)
        A.fake_flac.analyse = counting_analyse
        calls["n"] = 0
        fourth = A.analyse_file(p, cache2)
        eq(calls["n"], 1, "a changed file (different size/mtime) is re-analysed, not stale-cached")
    finally:
        A.fake_flac.analyse = real_analyse

print("\n[authenticate_folder: sample then escalate only on condemn]")
with tempfile.TemporaryDirectory() as tmp:
    rel = os.path.join(tmp, "release")
    for i in range(5):
        touch(os.path.join(rel, "%02d - t.flac" % (i + 1)))
    cache = A.AuthenticityCache(tmp)
    real_analyse = A.fake_flac.analyse
    try:
        # All clean: never escalates.
        seen = []
        A.fake_flac.analyse = lambda path: (seen.append(path), fake_analysis("clean"))[1]
        out = A.authenticate_folder(rel, cache, log=lambda m: None)
        check(out["checked"], "a folder with lossless audio is checked")
        eq(out["sampled"], 3, "5 files -> sample of 3 (first/middle/last)")
        eq(out["total"], 5, "total reflects every lossless file in the folder")
        eq(out["escalated"], False, "an all-clean sample never escalates")
        eq(out["condemned"], False, "and is not condemned")
        eq(len(seen), 3, "only the sampled 3 files were actually analysed")

        # One condemned file in the sample: must check every remaining file too.
        cache2 = A.AuthenticityCache(tmp)
        seen2 = []
        def mixed_analyse(path):
            seen2.append(path)
            # the middle of the 5-file sample (index 2) comes back lossy
            return fake_analysis("lossy" if path.endswith("03 - t.flac") else "clean")
        A.fake_flac.analyse = mixed_analyse
        out2 = A.authenticate_folder(rel, cache2, log=lambda m: None)
        eq(out2["escalated"], True, "a condemned sample escalates to the whole folder")
        eq(out2["condemned"], True, "and the folder-level verdict is condemned")
        eq(len(out2["per_file"]), 5, "every file in the folder was analysed after escalation")
        eq(len(seen2), 5, "including the two that were not in the original sample")

        # Only "suspect" in the sample: must NOT escalate — one signal is not enough.
        cache3 = A.AuthenticityCache(tmp)
        seen3 = []
        A.fake_flac.analyse = lambda path: (seen3.append(path), fake_analysis("suspect"))[1]
        out3 = A.authenticate_folder(rel, cache3, log=lambda m: None)
        eq(out3["escalated"], False, "a merely-suspect sample does not escalate")
        eq(out3["condemned"], False, "and is not condemned — suspect alone never condemns")
        eq(len(seen3), 3, "still only the sample was analysed")
    finally:
        A.fake_flac.analyse = real_analyse

print("\n[authenticate_folder: unavailable / empty / stopped]")
with tempfile.TemporaryDirectory() as tmp:
    rel = os.path.join(tmp, "release")
    touch(os.path.join(rel, "01 - t.flac"))
    cache = A.AuthenticityCache(tmp)

    real_available = A.available
    try:
        A.available = lambda: False
        out = A.authenticate_folder(rel, cache)
        eq(out["checked"], False, "unavailable deps -> unchecked, never treated as clean")
        eq(out["reason"], "numpy/soundfile not installed", "and says why")
    finally:
        A.available = real_available

    empty = os.path.join(tmp, "empty-release")
    os.makedirs(empty, exist_ok=True)
    out2 = A.authenticate_folder(empty, cache)
    eq(out2["checked"], False, "a folder with no lossless audio is unchecked, not crashed on")

    real_analyse = A.fake_flac.analyse
    try:
        A.fake_flac.analyse = lambda path: fake_analysis("clean")
        out3 = A.authenticate_folder(rel, cache, should_stop=lambda: True)
        eq(out3["checked"], False, "should_stop honoured before the first file is analysed")
        eq(out3["reason"], "stopped", "and says why")
    finally:
        A.fake_flac.analyse = real_analyse

print("\n%d checks, %d failed" % (ran, len(fails)))
print("PASS" if not fails else "FAILED:\n  - " + "\n  - ".join(fails))
sys.exit(1 if fails else 0)
