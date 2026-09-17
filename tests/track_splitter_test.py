#!/usr/bin/env python3
"""Unit tests for track_splitter.py (splitting one continuous rip into
individual tracks, from a .cue sheet or from Discogs' own track lengths).

    python3 tests/track_splitter_test.py

Cue-parsing and split-point math are pure functions, tested here with no
audio at all. The actual cut (split_file(), which needs ffmpeg) is
exercised in tests/cd_tools_test.py instead, alongside cd_tools.py's own
plan_split_cue()/plan_split_discogs()/apply_split() — that's where real
synthetic WAV fixtures already live for this kind of thing.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

import track_splitter as ts                                   # noqa: E402

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


def write_cue(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


print("[cue_to_seconds]")
eq(ts.cue_to_seconds("3", "32", "15"), 3 * 60 + 32 + 15 / 75.0, "MM:SS:FF, FF is 75ths not 100ths")
eq(ts.cue_to_seconds("0", "0", "0"), 0.0, "zero timestamp")

tmp = tempfile.mkdtemp(prefix="track_splitter_test_")

print("\n[parse_continuous_cue — real continuous-rip shape (one FILE, many TRACKs)]")
cont = os.path.join(tmp, "continuous.cue")
write_cue(cont, """PERFORMER "Test Artist"
TITLE "Test Side A"
FILE "side_a.wav" WAVE
  TRACK 01 AUDIO
    TITLE "First Track"
    PERFORMER "Test Artist"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second Track"
    PERFORMER "Test Artist"
    INDEX 01 03:32:15
  TRACK 03 AUDIO
    TITLE "Third Track"
    PERFORMER "Test Artist"
    INDEX 01 07:10:50
""")
r = ts.parse_continuous_cue(cont)
check(r["ok"], "parses ok")
eq(r["source_file"], "side_a.wav", "reads the single FILE name")
eq(len(r["tracks"]), 3, "three tracks parsed")
eq(r["tracks"][0]["start"], 0.0, "track 1 starts at 0")
eq(round(r["tracks"][1]["start"], 3), 212.2, "track 2 start (3:32:15)")
eq(r["tracks"][2]["title"], "Third Track", "track title read")

print("\n[parse_continuous_cue — already-split archive shape (many FILEs) is REFUSED]")
already_split = os.path.join(tmp, "already_split.cue")
write_cue(already_split, """TITLE "Some Release"
FILE "01 - Artist - Song A.wav" WAVE
  TRACK 01 AUDIO
    TITLE "Song A"
    INDEX 01 00:00:00
FILE "02 - Artist - Song B.wav" WAVE
  TRACK 02 AUDIO
    TITLE "Song B"
    INDEX 01 00:00:00
""")
r2 = ts.parse_continuous_cue(already_split)
check(not r2["ok"], "refuses a cue that already names 2+ separate files")
check("2 file" in r2["reason"], "reason explains why", r2["reason"])

print("\n[parse_continuous_cue — missing INDEX 01]")
noindex = os.path.join(tmp, "noindex.cue")
write_cue(noindex, """FILE "side.wav" WAVE
  TRACK 01 AUDIO
    TITLE "A"
  TRACK 02 AUDIO
    TITLE "B"
    INDEX 01 00:05:00
""")
r3 = ts.parse_continuous_cue(noindex)
check(not r3["ok"], "refuses when a track has no INDEX 01")
eq(r3["source_file"], "side.wav", "still reports which file it was")

print("\n[split_points]")
pts = ts.split_points(r["tracks"], total_duration=620.0)
eq(len(pts), 3, "one point per track")
eq(pts[0]["end"], pts[1]["start"], "track 1 ends exactly where track 2 starts")
eq(pts[2]["end"], 620.0, "last track ends at the file's total duration")

print("\n[durations_to_tracks — refuses a gap rather than guessing past it]")
gap = ts.durations_to_tracks([
    {"title": "A", "duration": 180, "artist": "X"},
    {"title": "B", "duration": None, "artist": "Y"},
    {"title": "C", "duration": 150, "artist": "Z"},
])
check(not gap["ok"], "refuses when any track is missing a duration")
check("[2]" in gap["reason"], "names the missing track (1-indexed)", gap["reason"])

print("\n[durations_to_tracks — full durations sum into cumulative starts]")
full = ts.durations_to_tracks([
    {"title": "A", "duration": 180, "artist": "X"},
    {"title": "B", "duration": 200, "artist": "Y"},
    {"title": "C", "duration": 150, "artist": "Z"},
])
check(full["ok"], "computes ok with every duration present")
eq(full["tracks"][0]["start"], 0.0, "track 1 starts at 0")
eq(full["tracks"][1]["start"], 180.0, "track 2 starts where track 1's stated length ends")
eq(full["tracks"][2]["start"], 380.0, "track 3 starts after track 1 + track 2")

print(f"\n{ran} checks, {len(fails)} failed")
if fails:
    print("FAILED:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("PASS")
