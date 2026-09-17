#!/usr/bin/env python3
"""Unit tests for organiser_core.build_destination_path's Direct-mode /
multi-disc behaviour.

    python3 tests/organiser_core_test.py

Regression tests for two real bugs found and fixed 2026-09-16:
  1. Direct mode's organise_cfg["artist_led_folder"] = True was silently
     dead — build_destination_path checks folder_scheme FIRST, and the
     shipped default ("artist_release_track_mix_year") always won, so
     every track of a release landed in its own folder no matter what
     artist_led_folder said. Fixed by Direct mode also setting
     folder_scheme="release".
  2. A source file's disc-folder parent (CD1/CD2/...) was thrown away by
     build_destination_path, so two discs' same-numbered tracks (the
     normal case, not an edge case — most multi-disc rips restart track
     numbering per disc) collided into one folder as "05 - ....ext" /
     "05 - ... (2).ext", hiding that they are two different songs from
     two different discs. Fixed by carrying a CDn subfolder through to
     the destination when the source sits under one.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "zzzzScriptstuff"))

from organiser_core import build_destination_path             # noqa: E402

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


def record(path, title, track, **extra):
    r = {
        "path": path, "primary_artist": "DJ Test", "albumartist": "DJ Test",
        "artist": "DJ Test", "album": "Test Album", "title": title,
        "year": "2001", "track_number": track, "catalog_number": "12-345",
    }
    r.update(extra)
    return r


print("[Direct mode: artist_led_folder actually takes effect]")
direct_cfg = {"artist_led_folder": True, "folder_scheme": "release"}
p1 = build_destination_path(record("/src/Rel/01.flac", "Track One", "1"),
                            "solo", destination_root="/Out", organise_cfg=direct_cfg)
p2 = build_destination_path(record("/src/Rel/02.flac", "Track Two", "2"),
                            "solo", destination_root="/Out", organise_cfg=direct_cfg)
eq(p1.parent, p2.parent, "two tracks of the same release land in the SAME folder")
check("Track One" not in str(p1.parent) and "Track Two" not in str(p1.parent),
     "the shared folder is the RELEASE folder, not a per-track folder")

print("\n[the old default silently ignored artist_led_folder — documenting the bug, not the fix]")
broken_cfg = {"artist_led_folder": True}  # folder_scheme left at its config.default.toml default
p3 = build_destination_path(record("/src/Rel/01.flac", "Track One", "1"),
                            "solo", destination_root="/Out", organise_cfg=broken_cfg)
p4 = build_destination_path(record("/src/Rel/02.flac", "Track Two", "2"),
                            "solo", destination_root="/Out", organise_cfg=broken_cfg)
check(p3.parent != p4.parent,
     "without folder_scheme='release', the pre-fix code path is confirmed to still split "
     "tracks into separate folders — proves the fix is what changed the behaviour above, "
     "not some other default")


print("\n[multi-disc: same track number on two discs no longer collides]")
r_cd1 = record("/src/Comp/CD1/05.flac", "Song A", "5")
r_cd2 = record("/src/Comp/CD2/05.flac", "Song B", "5")
q1 = build_destination_path(r_cd1, "solo", destination_root="/Out", organise_cfg=direct_cfg)
q2 = build_destination_path(r_cd2, "solo", destination_root="/Out", organise_cfg=direct_cfg)
check(q1 != q2, "CD1 track 5 and CD2 track 5 get DIFFERENT destination paths")
eq(q1.parent.parent, q2.parent.parent, "...but still share the same release folder one level up")
eq(q1.parent.name, "CD1", "CD1's file lands under a 'CD1' subfolder")
eq(q2.parent.name, "CD2", "CD2's file lands under a 'CD2' subfolder")

print("\n[the actual collision case: same track number AND same/blank title]")
# The artist_led filename includes the title, which is why q1/q2 above already
# differ even without the CDn fix. The real collision this fix exists for is a
# release with a blank/unreadable title tag on both discs' track 5 — the
# filename then collapses to a bare "05 - .ext" on both, and only the disc
# subfolder still tells them apart.
b_cd1 = record("/src/Comp2/CD1/05.flac", "", "5")
b_cd2 = record("/src/Comp2/CD2/05.flac", "", "5")
bq1 = build_destination_path(b_cd1, "solo", destination_root="/Out", organise_cfg=direct_cfg)
bq2 = build_destination_path(b_cd2, "solo", destination_root="/Out", organise_cfg=direct_cfg)
eq(bq1.name, bq2.name, "with blank titles the filenames DO collapse to the same string")
check(bq1 != bq2, "...but the full destination path still differs, because of the CDn subfolder")
check(bq1.parent != bq2.parent, "the CDn subfolder is what actually prevents the collision here")

print("\n[disc-folder naming variants all normalise to 'CDn']")
for src_disc, want in (("CD1", "CD1"), ("Disc 2", "CD2"), ("disc03", "CD3"), ("DVD 1", "CD1")):
    r = record(f"/src/Rel/{src_disc}/01.flac", "T", "1")
    q = build_destination_path(r, "solo", destination_root="/Out", organise_cfg=direct_cfg)
    eq(q.parent.name, want, f"source folder '{src_disc}' normalises to '{want}'")

print("\n[single-disc release is unaffected — no spurious CDn folder]")
single = record("/src/Rel/05.flac", "Solo Track", "5")
qs = build_destination_path(single, "solo", destination_root="/Out", organise_cfg=direct_cfg)
check(not any(part.upper().startswith("CD") for part in qs.parts[-2:-1]),
     "no CDn component appears when the source has no disc-folder parent")

print("\n[the plain (non artist_led) fallback branch gets the same disc handling]")
plain_cfg = {"folder_scheme": "release"}
r_plain1 = record("/src/Comp/CD1/05.flac", "Song A", "5", primary_artist="", albumartist="", artist="")
r_plain2 = record("/src/Comp/CD2/05.flac", "Song B", "5", primary_artist="", albumartist="", artist="")
p5 = build_destination_path(r_plain1, "mix", destination_root="/Out", organise_cfg=plain_cfg)
p6 = build_destination_path(r_plain2, "mix", destination_root="/Out", organise_cfg=plain_cfg)
check(p5 != p6, "the plain fallback branch also avoids the collision")
eq(p5.parent.name, "CD1", "plain branch: CD1 subfolder present")
eq(p6.parent.name, "CD2", "plain branch: CD2 subfolder present")


print("\n[plain 'release' scheme: folder now includes artist for a solo release]")
solo_plain = record("/src/SoloRel/01.flac", "Track One", "1")
sp = build_destination_path(solo_plain, "solo", destination_root="/Out", organise_cfg=plain_cfg)
eq(sp.parent.name, "(12-345) DJ Test - Test Album (2001)",
  "folder is '(catno) Artist - Title (Year)', in that order")

print("\n[plain 'release' scheme: a VA/mix release keeps NO per-track artist in the shared folder]")
va1 = record("/src/VARel/01.flac", "Song A", "1", primary_artist="Artist One",
            albumartist="Artist One", artist="Artist One")
va2 = record("/src/VARel/02.flac", "Song B", "2", primary_artist="Artist Two",
            albumartist="Artist Two", artist="Artist Two")
vp1 = build_destination_path(va1, "mix", destination_root="/Out", organise_cfg=plain_cfg)
vp2 = build_destination_path(va2, "mix", destination_root="/Out", organise_cfg=plain_cfg)
eq(vp1.parent, vp2.parent, "both VA tracks still land in the SAME shared release folder")
eq(vp1.parent.name, "(12-345) Test Album (2001)",
  "folder has no artist at all — neither track's own name, since a VA comp has no single one")

print("\n[folder_scheme='custom': user template reorders/reformats the folder freely]")
custom_cfg = {"folder_scheme": "custom", "folder_name_template": "{artist} - {title} [{catno}] ({year})"}
cp = build_destination_path(solo_plain, "solo", destination_root="/Out", organise_cfg=custom_cfg)
eq(cp.parent.name, "DJ Test - Test Album [12-345] (2001)", "folder matches the custom template exactly")

print("\n[folder_scheme='custom': VA/mix still gets no per-track artist, same guard as the plain scheme]")
cva1 = build_destination_path(va1, "mix", destination_root="/Out", organise_cfg=custom_cfg)
cva2 = build_destination_path(va2, "mix", destination_root="/Out", organise_cfg=custom_cfg)
eq(cva1.parent, cva2.parent, "both VA tracks still share one folder under a custom template too")
check("Artist One" not in cva1.parent.name and "Artist Two" not in cva1.parent.name,
     "{artist} resolved to blank, not either track's own name")

print("\n[folder_scheme='custom': a missing token value is NOT smart-cleaned, by design]")
no_catno = dict(solo_plain); no_catno["catalog_number"] = ""
ncp = build_destination_path(no_catno, "solo", destination_root="/Out",
                             organise_cfg={"folder_scheme": "custom",
                                          "folder_name_template": "({catno}) {artist} - {title} ({year})"})
eq(ncp.parent.name, "() DJ Test - Test Album (2001)",
  "empty {catno} leaves a bare '()' rather than guessing what to drop")

print("\n%d checks, %d failed" % (ran, len(fails)))
print("PASS" if not fails else "FAILED:\n  - " + "\n  - ".join(fails))
sys.exit(1 if fails else 0)
