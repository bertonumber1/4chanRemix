#!/usr/bin/env python3
"""Unit tests for cd_tools.py (Direct tab's "CD Tools").

    python3 tests/cd_tools_test.py

Runs entirely against synthetic folders in a temp dir plus a fake Discogs
client (no network, no Bit Music archive, no real token needed) — matches
the rest of tests/, which take under a second and never touch a real
library. build_plan/scan_duplicates/apply_plan only ever look at FILENAMES,
never read audio tags, so plain placeholder-byte files stand in for real
audio there, same as label_ref_test.py's touch() helper. tag_release/
get_artwork DO read tags, so those two sections use real (tiny, synthetic)
WAV files via soundfile, same fixture style tag_writer_test.py uses.
"""
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "zzzzScriptstuff"))

import cd_tools as C                                          # noqa: E402
import label_panel as P                                       # noqa: E402

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


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * 16)


class FakeDiscogs:
    """Duck-typed stand-in for label_ref.Discogs — no network. Only
    implements what cd_tools.py actually calls."""

    def __init__(self, release=None, tracklist_by_disc=None, images=None):
        self._release = release or {}
        self._tbd = tracklist_by_disc or {}
        self._images = images or []

    def release(self, release_id):
        return self._release

    def tracklist_by_disc(self, release_id):
        return self._tbd

    def release_images(self, release_id):
        return self._images

    def _get(self, path, params=None, tries=5):
        if path.startswith("/masters/"):
            return {"main_release": 999999}
        raise RuntimeError("unexpected _get call in test: " + path)


tmp = tempfile.mkdtemp(prefix="cdtools_test_")


# ═══════════════════════════════════════════════════════════════════════════
print("[resolve_release_id]")
eq(C.resolve_release_id("123456", None), ("123456", ""), "bare numeric id")
eq(C.resolve_release_id("https://www.discogs.com/release/249504-Some-Title", None),
   ("249504", ""), "release URL with trailing slug")
eq(C.resolve_release_id("release/249504", None), ("249504", ""), "bare release/<id> form")
eq(C.resolve_release_id("", None), ("", ""), "empty input is not an error — just nothing given")
rid, err = C.resolve_release_id("https://www.discogs.com/master/1000-Some-Title", FakeDiscogs())
eq(rid, "999999", "master URL resolves to main_release via the fake client")
eq(err, "", "master resolution reports no error")
rid, err = C.resolve_release_id("https://www.discogs.com/master/1000", None)
eq(rid, "", "master URL with no discogs client available cannot resolve")
check("no Discogs token" in err, "master-without-client error explains why", err)
rid, err = C.resolve_release_id("not a url at all", None)
eq(rid, "", "garbage input resolves to nothing")
check(bool(err), "garbage input reports an error", err)


# ═══════════════════════════════════════════════════════════════════════════
print("\n[scan_duplicates]")
dupe_root = os.path.join(tmp, "dupe_release")
touch(os.path.join(dupe_root, "01 - Alpha.flac"))
touch(os.path.join(dupe_root, "02 - Beta.flac"))
touch(os.path.join(dupe_root, "CD2", "01 - Alpha.flac"))   # same normalised name as CD1's 01
dupes = C.scan_duplicates(dupe_root)
eq(len(dupes), 1, "exactly one duplicate key found (the Alpha track)")
check("alpha" in dupes, "the duplicate key is the normalised 'alpha' title", str(dupes.keys()))
eq(len(list(dupes.values())[0]), 2, "both copies of the duplicate are listed")

clean_root = os.path.join(tmp, "clean_release")
touch(os.path.join(clean_root, "01 - Alpha.flac"))
touch(os.path.join(clean_root, "02 - Beta.flac"))
eq(C.scan_duplicates(clean_root), {}, "no false positive on a release with no repeated names")


# ═══════════════════════════════════════════════════════════════════════════
print("\n[build_plan — cue-sourced]")
plan_root = os.path.join(tmp, "cue_release")
touch(os.path.join(plan_root, "CD1", "01 - Alpha.flac"))
touch(os.path.join(plan_root, "CD1", "02 - Beta.flac"))
touch(os.path.join(plan_root, "CD2", "01 - Gamma.flac"))
# A duplicate of Beta leaked into CD2, and a duplicate of Gamma leaked into CD1 —
# the exact "jumbled" shape this whole tool exists to fix.
touch(os.path.join(plan_root, "CD2", "02 - Beta.flac"))
touch(os.path.join(plan_root, "CD1", "03 - Gamma.flac"))
with open(os.path.join(plan_root, "CD1", "album.cue"), "w", encoding="utf-8") as fh:
    fh.write('TRACK 01 AUDIO\n  TITLE "Alpha"\n'
             'TRACK 02 AUDIO\n  TITLE "Beta"\n')
with open(os.path.join(plan_root, "CD2", "album.cue"), "w", encoding="utf-8") as fh:
    fh.write('TRACK 01 AUDIO\n  TITLE "Gamma"\n')

plan = C.build_plan(plan_root, "", None)
eq(plan["status"], "auto_fixable", "clean cue coverage on both discs resolves without Discogs")
eq(sorted(plan["discs"]), [1, 2], "both CD1 and CD2 detected")
eq(len(plan["disc_plan"].get(1, [])), 2, "CD1 plan has 2 tracks (Alpha, Beta)")
eq(len(plan["disc_plan"].get(2, [])), 1, "CD2 plan has 1 track (Gamma)")
cd1_titles = {e["expected_title"] for e in plan["disc_plan"][1]}
eq(cd1_titles, {"Alpha", "Beta"}, "CD1 winners match the cue's expected titles")
beta_entry = next(e for e in plan["disc_plan"][1] if e["expected_title"] == "Beta")
check(os.path.basename(beta_entry["winner"]) == "02 - Beta.flac"
     and os.path.dirname(beta_entry["winner"]) == os.path.join(plan_root, "CD1"),
     "Beta's winner is the copy already sitting in CD1, not the CD2 duplicate")
eq(len(beta_entry["losers"]), 1, "Beta's CD2 duplicate is flagged as a loser")
gamma_entry = plan["disc_plan"][2][0]
check(os.path.dirname(gamma_entry["winner"]) == os.path.join(plan_root, "CD2"),
     "Gamma's winner is the copy already sitting in CD2")
eq(len(gamma_entry["losers"]), 1, "Gamma's CD1 duplicate is flagged as a loser")


# ═══════════════════════════════════════════════════════════════════════════
print("\n[build_plan — discogs-sourced, no cue at all]")
dg_root = os.path.join(tmp, "discogs_release")
touch(os.path.join(dg_root, "CD1", "01 - Solo.flac"))
touch(os.path.join(dg_root, "CD2", "01 - Duet.flac"))
fake = FakeDiscogs(tracklist_by_disc={
    1: [{"title": "Solo", "duration": None}],
    2: [{"title": "Duet", "duration": None}],
})
plan2 = C.build_plan(dg_root, "555", fake)
eq(plan2["status"], "auto_fixable", "no cue at all, but the given Discogs release covers both discs")
eq(plan2["sources"], {1: "discogs", 2: "discogs"}, "both discs correctly report their source as discogs")
eq(plan2["missing"], [], "nothing missing on a fully auto_fixable plan")

print("\n[build_plan — missing list carries structured title+artist for Soulseek search]")
miss_root = os.path.join(tmp, "missing_release")
touch(os.path.join(miss_root, "CD1", "01 - Solo.flac"))
touch(os.path.join(miss_root, "CD2", "dummy.flac"))   # CD2 exists but has nothing matching "Duet"
fake_artist = FakeDiscogs(tracklist_by_disc={
    1: [{"title": "Solo", "duration": None, "artist": "Solo Artist"}],
    2: [{"title": "Duet", "duration": None, "artist": "Duet Artist"}],
})
plan_miss = C.build_plan(miss_root, "555", fake_artist)
eq(plan_miss["status"], "needs_review", "CD2's track genuinely has no matching file")
eq(plan_miss["missing"], [{"disc": 2, "title": "Duet", "artist": "Duet Artist"}],
  "the missing list names exactly the unresolved track, with its artist for a Soulseek search")

print("\n[build_plan — an ambiguous tie is never reported as 'missing']")
tie_root = os.path.join(tmp, "tie_release")
os.makedirs(os.path.join(tie_root, "CD1"), exist_ok=True)
# Two equally-good candidates, NEITHER sitting in CD1's own folder — the
# "exactly one already lives in this disc's folder" tie-break needs exactly
# one match to settle it, so leaving both loose keeps this genuinely tied.
touch(os.path.join(tie_root, "02 - Track.flac"))
touch(os.path.join(tie_root, "03 - Track.flac"))
os.makedirs(os.path.join(tie_root, "CD2"), exist_ok=True)
fake_tie = FakeDiscogs(tracklist_by_disc={
    1: [{"title": "Track", "duration": None, "artist": "A"}],
    2: [],
})
plan_tie = C.build_plan(tie_root, "555", fake_tie)
check(any("equally good files" in r for r in plan_tie["reasons"]),
     "an ambiguous tie still gets its own specific reason", str(plan_tie["reasons"]))
eq(plan_tie["missing"], [],
  "but it is NEVER listed as 'missing' — there are too many files, not zero; nothing to search Soulseek for")

print("\n[build_plan — no cue, no Discogs release given]")
plan3 = C.build_plan(dg_root, "", None)
eq(plan3["status"], "needs_review", "with neither a cue nor a Discogs release, nothing can be verified")
check(any("no Discogs release given" in r for r in plan3["reasons"]),
     "the reason names the actual gap", str(plan3["reasons"]))

print("\n[build_plan — single-folder release]")
single_root = os.path.join(tmp, "single_release")
touch(os.path.join(single_root, "01 - Track.flac"))
plan4 = C.build_plan(single_root, "", None)
eq(plan4["status"], "not_multidisc", "fewer than 2 CDn folders reports not_multidisc, not an error")


# ═══════════════════════════════════════════════════════════════════════════
# Cross-disc duplicate recovery, duration-verified. Found 2026-09-16 on a
# real archive release ((32-717) Limite The Sound): a title Discogs lists on
# BOTH discs used to always report "missing" for the second disc, because
# the first disc's occurrence consumed every physical copy matching that
# title — including spare duplicates about to be filed away as junk, one of
# which might genuinely be the second disc's own copy. Needs real audio (not
# placeholder bytes) since duration is exactly what has to be checked.
try:
    import numpy as np
    import soundfile as sf

    def wav_of(path, seconds):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        sf.write(path, np.zeros(int(seconds * 44100), dtype="float32"), 44100, format="WAV")

    print("\n[build_plan — cross-disc duplicate recovery, duration matches]")
    rec_root = os.path.join(tmp, "recover_release")
    wav_of(os.path.join(rec_root, "CD1", "01 - SharedSong.wav"), 100.0)
    os.makedirs(os.path.join(rec_root, "CD2"), exist_ok=True)   # disc_numbers() needs the folder to exist
    # the spare sitting loose is the SAME length as CD1's accepted copy —
    # a strong signal it is genuinely the same recording, safe to reuse.
    wav_of(os.path.join(rec_root, "10 - SharedSong.wav"), 100.2)
    fake_rec = FakeDiscogs(tracklist_by_disc={
        1: [{"title": "SharedSong", "duration": None}],
        2: [{"title": "SharedSong", "duration": None}],
    })
    plan_rec = C.build_plan(rec_root, "77", fake_rec)
    eq(plan_rec["status"], "auto_fixable",
      "a same-duration spare recovers the second disc's legitimate copy")
    cd2_entry = plan_rec["disc_plan"][2][0]
    check(cd2_entry.get("recovered_from_duplicate") is True,
         "the recovered entry is explicitly marked as such, not indistinguishable from a normal match")
    check(os.path.basename(cd2_entry["winner"]) == "10 - SharedSong.wav",
         "the spare file became CD2's own winner")

    print("\n[build_plan — cross-disc duplicate recovery REFUSES a duration mismatch]")
    # This is the actual real-archive case: same title, but the "spare" is a
    # completely different length recording (161s vs 388s on the real
    # release) — must NOT be silently treated as the same song.
    mismatch_root = os.path.join(tmp, "mismatch_release")
    wav_of(os.path.join(mismatch_root, "CD1", "01 - SharedSong.wav"), 100.0)
    os.makedirs(os.path.join(mismatch_root, "CD2"), exist_ok=True)
    wav_of(os.path.join(mismatch_root, "10 - SharedSong.wav"), 240.0)   # different edit/length
    fake_mismatch = FakeDiscogs(tracklist_by_disc={
        1: [{"title": "SharedSong", "duration": None}],
        2: [{"title": "SharedSong", "duration": None}],
    })
    plan_mismatch = C.build_plan(mismatch_root, "77", fake_mismatch)
    eq(plan_mismatch["status"], "needs_review",
      "a duration mismatch is correctly refused, not silently accepted")
    eq(plan_mismatch["disc_plan"].get(2, []), [],
      "CD2 has no entry at all — nothing was guessed")
    check(any('no file found for "SharedSong"' in r for r in plan_mismatch["reasons"]),
         "the reason honestly says the track could not be found",
         str(plan_mismatch["reasons"]))

    print("\n[build_plan — cross-disc recovery with NO spare at all]")
    nospare_root = os.path.join(tmp, "nospare_release")
    wav_of(os.path.join(nospare_root, "CD1", "01 - SharedSong.wav"), 100.0)
    os.makedirs(os.path.join(nospare_root, "CD2"), exist_ok=True)
    # CD2 has nothing at all matching the title — genuinely absent, not a
    # misfiled duplicate. Must report missing, not invent a file.
    fake_nospare = FakeDiscogs(tracklist_by_disc={
        1: [{"title": "SharedSong", "duration": None}],
        2: [{"title": "SharedSong", "duration": None}],
    })
    plan_nospare = C.build_plan(nospare_root, "77", fake_nospare)
    eq(plan_nospare["status"], "needs_review", "nothing to recover from — correctly stays needs_review")
    check(any('no file found for "SharedSong"' in r for r in plan_nospare["reasons"]),
         "reports the track as missing rather than crashing on an empty losers list")

    print("\n[build_plan — Discogs-stated duration is checked, not just the sibling's own]")
    dgdur_root = os.path.join(tmp, "dgdur_release")
    wav_of(os.path.join(dgdur_root, "CD1", "01 - SharedSong.wav"), 100.0)
    os.makedirs(os.path.join(dgdur_root, "CD2"), exist_ok=True)
    # This spare does NOT match CD1's own winner (100.0s) — but DOES match
    # what Discogs itself says CD2's copy should be (150.0s). If recovery
    # only ever compared against the sibling, this would wrongly stay
    # unresolved; it should succeed because Discogs' own duration is
    # consulted first.
    wav_of(os.path.join(dgdur_root, "10 - SharedSong.wav"), 150.0)
    fake_dgdur = FakeDiscogs(tracklist_by_disc={
        1: [{"title": "SharedSong", "duration": None}],
        2: [{"title": "SharedSong", "duration": 150.0}],
    })
    plan_dgdur = C.build_plan(dgdur_root, "77", fake_dgdur)
    eq(plan_dgdur["status"], "auto_fixable",
      "recovered using Discogs' OWN stated duration for this occurrence, not the sibling's")
except ImportError as exc:
    print("  SKIP  cross-disc duplicate recovery section — %s not installed" % exc)


# ═══════════════════════════════════════════════════════════════════════════
print("\n[apply_plan]")
apply_root = os.path.join(tmp, "apply_release")
touch(os.path.join(apply_root, "01 - Alpha.flac"))          # misplaced: sitting loose at top level
touch(os.path.join(apply_root, "CD1", "02 - Beta.flac"))
touch(os.path.join(apply_root, "CD2", "02 - Beta.flac"))   # duplicate, leaked from CD1
touch(os.path.join(apply_root, "CD2", "01 - Gamma.flac"))
with open(os.path.join(apply_root, "CD1", "a.cue"), "w", encoding="utf-8") as fh:
    fh.write('TRACK 01 AUDIO\n  TITLE "Alpha"\nTRACK 02 AUDIO\n  TITLE "Beta"\n')
with open(os.path.join(apply_root, "CD2", "a.cue"), "w", encoding="utf-8") as fh:
    fh.write('TRACK 01 AUDIO\n  TITLE "Gamma"\n')

plan_a = C.build_plan(apply_root, "", None)
eq(plan_a["status"], "auto_fixable", "setup plan for apply_plan test is auto_fixable")

dry = C.apply_plan(apply_root, plan_a, dry_run=True)
eq(dry["ok"], True, "dry run reports ok")
check(os.path.exists(os.path.join(apply_root, "CD2", "02 - Beta.flac")),
     "dry run does not actually move the duplicate")
check(not os.path.isdir(C._review_root(apply_root)) or not os.listdir(C._review_root(apply_root)),
     "dry run creates no real review-folder contents")

real = C.apply_plan(apply_root, plan_a, dry_run=False)
eq(real["ok"], True, "real apply reports ok")
check(not os.path.exists(os.path.join(apply_root, "CD2", "02 - Beta.flac")),
     "the duplicate is gone from CD2")
check(os.path.exists(os.path.join(apply_root, "CD1", "01 - Alpha.flac")),
     "the misplaced Alpha winner was actually moved into CD1")
check(not os.path.exists(os.path.join(apply_root, "01 - Alpha.flac")),
     "Alpha is gone from its old top-level location")
review_root = C._review_root(apply_root)
found_in_review = False
for dp, _dirs, fs in os.walk(review_root):
    if "02 - Beta.flac" in fs:
        found_in_review = True
eq(found_in_review, True, "the duplicate was moved into the review folder, not deleted")
eq(real["verify"], {}, "post-apply verification finds no count mismatch")

# Undo: the WINNER moves are logged as plain "move" — same action name
# move_log()/undo_move() key undo-eligibility on — so a file move (not
# just a folder move) shows up as undoable. This was a real gap: the
# pre-existing os.path.isdir(dest) check in move_log()/undo_move() only
# ever matched directories, so no individual-file move (not just this
# tool's — multicd_dedupe.py's too) ever actually showed as undoable.
moves = P.move_log(limit=20)
winner_rows = [r for r in moves if r["action"] == "move" and r["src"].endswith("Alpha.flac")]
check(bool(winner_rows), "the Alpha winner move was logged with action='move'")
if winner_rows:
    check(winner_rows[0]["undoable"], "a single-FILE move now shows as undoable (was broken before)")
    ok_undo, msg = P.undo_move(winner_rows[0]["dest"])
    eq(ok_undo, True, "undo_move actually succeeds on a single-file move now: " + msg)
    check(os.path.exists(os.path.join(apply_root, "01 - Alpha.flac")),
         "Alpha is back at its original (pre-apply) location after undo")
    check(not os.path.exists(os.path.join(apply_root, "CD1", "01 - Alpha.flac")),
         "Alpha is no longer at the moved-to location after undo")

not_fixable = C.apply_plan(apply_root, {"status": "needs_review", "reasons": ["x"]}, dry_run=True)
eq(not_fixable["ok"], False, "apply_plan refuses a plan that was never auto_fixable")


# ═══════════════════════════════════════════════════════════════════════════
print("\n[remove_file / delete_review_files]")
rm_root = os.path.join(tmp, "remove_release")
victim = os.path.join(rm_root, "CD1", "03 - Extra.flac")
touch(victim)
res = C.remove_file(rm_root, victim, dry_run=False)
eq(res["ok"], True, "remove_file moves the file successfully")
check(not os.path.exists(victim), "the original file is gone from the working folder")
moved_to = res["dest"]
check(P._under(moved_to, C._review_root(rm_root)), "the file landed inside the review folder")

outside = os.path.join(tmp, "not_the_review_folder", "x.flac")
touch(outside)
del_res = C.delete_review_files(rm_root, [outside])
eq(del_res["ok"], False, "delete_review_files refuses a path outside the review folder")
check(os.path.exists(outside), "the refused file was NOT deleted")

del_res2 = C.delete_review_files(rm_root, [moved_to])
eq(del_res2["ok"], True, "delete_review_files succeeds on a file actually inside the review folder")
check(not os.path.exists(moved_to), "the file inside the review folder really is deleted")


# ═══════════════════════════════════════════════════════════════════════════
print("\n[move_folder / copy_folder]")
mv_src = os.path.join(tmp, "mv_source_release")
touch(os.path.join(mv_src, "01 - Track.flac"))
mv_dest_dir = os.path.join(tmp, "mv_dest")
os.makedirs(mv_dest_dir, exist_ok=True)

cp = C.copy_folder(mv_src, mv_dest_dir, dry_run=False)
eq(cp["ok"], True, "copy_folder succeeds")
check(os.path.exists(mv_src), "copy_folder leaves the source in place")
check(os.path.isdir(cp["dest"]), "the copy landed at the reported destination")

mv = C.move_folder(mv_src, mv_dest_dir, dry_run=False)
eq(mv["ok"], True, "move_folder succeeds")
check(not os.path.exists(mv_src), "move_folder removes the source")
check(os.path.isdir(mv["dest"]), "the move landed at the reported destination")

nested = C.move_folder(mv_dest_dir, mv_dest_dir, dry_run=True)
eq(nested["ok"], False, "move_folder refuses when the destination is inside the source")

missing_src = os.path.join(tmp, "does_not_exist")
bad = C.move_folder(missing_src, mv_dest_dir, dry_run=True)
eq(bad["ok"], False, "move_folder refuses a source folder that does not exist")


# ═══════════════════════════════════════════════════════════════════════════
print("\n[tag_release / get_artwork — real WAV fixtures]")
try:
    import numpy as np
    import soundfile as sf

    def wav(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        sf.write(path, np.zeros(4410, dtype="float32"), 44100, format="WAV")

    tag_root = os.path.join(tmp, "tag_release")
    wav(os.path.join(tag_root, "01 - track one.wav"))
    wav(os.path.join(tag_root, "02 - track two.wav"))
    rel = {
        "artists": [{"name": "Test Artist"}],
        "title": "Test Album",
        "year": 2001,
        "labels": [{"catno": "12-345"}],
        "tracklist": [{"type_": "track", "title": "Track One"},
                     {"type_": "track", "title": "Track Two"}],
    }
    fake2 = FakeDiscogs(release=rel)
    tr_dry = C.tag_release(tag_root, "1", fake2, dry_run=True)
    eq(tr_dry["ok"], True, "tag_release dry run reports ok")
    check(tr_dry["tagged_files"] > 0, "dry run reports files it WOULD tag")

    tr_real = C.tag_release(tag_root, "1", fake2, dry_run=False)
    eq(tr_real["ok"], True, "tag_release real run reports ok")
    eq(tr_real["tagged_files"], 2, "both files actually got tagged")

    from mutagen.wave import WAVE
    w = WAVE(os.path.join(tag_root, "01 - track one.wav"))
    id3 = w.tags
    check(id3 is not None and str(id3.get("TALB", "")).strip() == "Test Album",
         "album tag was actually written to disk")
    check(id3 is not None and str(id3.get("TPE1", "")).strip() == "Test Artist",
         "artist tag was actually written to disk")

    tr_again = C.tag_release(tag_root, "1", fake2, dry_run=False)
    eq(tr_again["tagged_files"], 0,
      "only_missing=True means a second run touches nothing already tagged")

    no_release = C.tag_release(tag_root, "", fake2, dry_run=True)
    eq(no_release["ok"], False, "tag_release refuses when no release id is given")

    art_root = os.path.join(tmp, "artwork_release")
    wav(os.path.join(art_root, "01 - track.wav"))
    fake3 = FakeDiscogs(images=[{"type": "primary", "uri": "http://example.invalid/cover.jpg"}])
    # No network in tests — this only exercises the "no local art, no loose
    # art, falls through to a Discogs fetch attempt" path and expects a
    # clean failure reason, not a crash, when that fetch can't reach anything.
    art = C.get_artwork(art_root, "1", fake3, dry_run=True)
    check(not art["ok"], "no local artwork and an unreachable Discogs URL correctly fails, not crashes")
    check(bool(art["reason"]), "the failure has a reason")

    loose_root = os.path.join(tmp, "loose_artwork_release")
    wav(os.path.join(loose_root, "01 - track.wav"))
    touch(os.path.join(loose_root, "folder.jpg"))
    art2 = C.get_artwork(loose_root, "", None, dry_run=True)
    eq(art2["ok"], True, "a folder that already has loose art short-circuits cleanly")
    eq(art2["source"], "folder-image", "reports the existing art as the source")
except ImportError as exc:
    print("  SKIP  tag_release/get_artwork section — %s not installed" % exc)


shutil.rmtree(tmp, ignore_errors=True)

print("\n%d checks, %d failed" % (ran, len(fails)))
print("PASS" if not fails else "FAILED:\n  - " + "\n  - ".join(fails))
sys.exit(1 if fails else 0)
