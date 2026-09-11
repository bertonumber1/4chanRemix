#!/usr/bin/env python3
"""Unit tests for tag_writer.py's per-format writeback.

    python3 tests/tag_writer_test.py

Every case here is a bug that actually happened, not a made-up example. Runs
against synthetic audio in a temp dir — no real library files touched.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "zzzzScriptstuff"))

import numpy as np
import soundfile as sf
from mutagen.id3 import ID3, TPUB
from mutagen.wave import WAVE

from tag_writer import write_tags_to_file                    # noqa: E402

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


def wav(path, tagged=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sf.write(path, np.zeros(4410, dtype="float32"), 44100, format="WAV")
    if tagged:
        # A WAV that already carries an ID3 chunk (e.g. label/catalogue
        # number written by an earlier pass) but no artist tag yet -- the
        # real shape found in the archive that surfaced this bug.
        f = WAVE(path)
        f.add_tags()
        f.tags.add(TPUB(encoding=3, text=["Some Label"]))
        f.save()


def main():
    tmp = tempfile.mkdtemp(prefix="tag-writer-test-")
    try:
        print("[WAV writeback]")
        # Real bug: write_tags_to_file() opened every WAV via
        # EasyID3(path), which expects a bare ID3 stream starting at byte 0.
        # A WAV always starts with "RIFF....WAVE" instead, so EasyID3 raised
        # "doesn't start with an ID3 tag" on EVERY WAV -- tagged or not --
        # and Labels' new per-row "Fix tags" button failed on 100% of the
        # archive's WAV releases with "2 error(s) writing tags".
        untagged = os.path.join(tmp, "untagged.wav")
        wav(untagged)
        r = write_tags_to_file(untagged, {"artist": "Test Artist", "track_number": "1"},
                               only_missing=True)
        eq(r.error, "", "a WAV with no ID3 chunk at all gets one created, not an error")
        eq(sorted(r.written_fields), ["artist", "track_number"],
           "both fields actually got written")
        tags = WAVE(untagged).tags
        eq(str(tags.get("TPE1")), "Test Artist", "artist landed in the real TPE1 frame")
        eq(str(tags.get("TRCK")), "1", "track number landed in the real TRCK frame")

        already_tagged = os.path.join(tmp, "already-tagged.wav")
        wav(already_tagged, tagged=True)
        r2 = write_tags_to_file(already_tagged, {"artist": "New Artist"}, only_missing=True)
        eq(r2.error, "", "a WAV with an existing (different-field) ID3 chunk also works")
        eq(r2.written_fields, ["artist"], "the missing artist field is written")
        tags2 = WAVE(already_tagged).tags
        eq(str(tags2.get("TPUB")), "Some Label",
           "the pre-existing publisher tag survives untouched")
        eq(str(tags2.get("TPE1")), "New Artist", "the new artist tag is present")

        r3 = write_tags_to_file(already_tagged, {"artist": "Overwrite Attempt"},
                                only_missing=True)
        eq(r3.written_fields, [], "only_missing=True never overwrites the artist just written")
        eq(str(WAVE(already_tagged).tags.get("TPE1")), "New Artist",
           "…and the file on disk still says so")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d checks, %d failed" % (ran, len(fails)))
    print("PASS" if not fails else "FAILED:\n  - " + "\n  - ".join(fails))
    return 1 if fails else 0


sys.exit(main())
