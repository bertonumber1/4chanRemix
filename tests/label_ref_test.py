#!/usr/bin/env python3
"""Unit tests for the label matching rules.

    python3 tests/label_ref_test.py

Every case here is a bug that actually happened to the archive, not a made-up
example. They run against synthetic folders in a temp dir, so they take under a
second and need neither the network nor the USB mount — which matters, because
the real scan reads 1,287 folders over a CIFS share and takes tens of minutes.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import label_ref as L                                        # noqa: E402

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


def touch(path, name):
    os.makedirs(path, exist_ok=True)
    open(os.path.join(path, name), "wb").write(b"\0" * 16)


print("[catalogue numbers]")
eq(L.folder_catno("(12-414) Esto es... Makina (1997)"), "12-414", "plain catno")
eq(L.folder_catno("(044, 72-100) The Look Of Love (2003)"), "044, 72-100",
   "multi-catno folder")
# The naive "stop at the first )" reader truncated every catno with a bracket.
eq(L.folder_catno("(PARMX 003 (promo)) Psy Man (2004)"), "PARMX 003 (promo)",
   "nested bracket stays inside the catno")
eq(L.folder_catno("No Brackets Here"), "", "no catno reads as empty")

# A second convention in this collection puts the catno in a LATER bracket
# beside the label. Ten of the archive's unmatched folders were this shape.
eq(L.folder_catno("90's Mix (1999) [Bit Music - 32-706] - WAV"), "32-706",
   "catno read from a bracket later in the name")
eq(L.folder_catno("'01 (2007) [Bit Music - 37-847] OK"), "37-847",
   "…even with a leading apostrophe and trailing noise")
# and the things that must NOT be mistaken for one
eq(L.folder_catno("Some Album (1999)"), "", "a bare year is not a catno")
eq(L.folder_catno("Album (Remastered Edition)"), "", "prose in brackets is not a catno")
eq(L.folder_catno("Artist - Title (2004)"), "",
   "a dash in the NAME does not make the year a catno")
eq(L.folder_catno("Live Set (Recorded Live - 2004)"), "",
   "a dash inside the bracket still does not make a year a catno")
eq(L.folder_title("(12-414) Esto es... Makina (1997)"), "Esto es... Makina",
   "title drops catno and year")

# Discogs packs every pressing into one field; matching the joined string
# never hit a folder key, so multi-catno releases could not be owned at all.
check("mxcd1562" in L.catno_keys("MXCD 1562 (CD), MXCD1562(CD), MXCD 1562 CD"),
      "pressing variants split into keys")
# Discogs writes "PARMX 01" where the disk says "(PARMX 001)".
check(L.catno_match("PARMX 01", "PARMX 001"), "zero-padding is not a difference")
check(not L.catno_match("12-414", "12-415"), "different catnos do not match")
check(not L.catno_keys("B & C"), "a non-catno fragment yields no key")

print("\n[title guard]")
# 'Limite 11' vs 'Limite 12' scores 0.875 against a 0.86 bar, so holding one
# volume silently marked every neighbouring volume owned.
check(L.fuzzy("Limite 11", "Limite 12") >= 0.86, "the fuzzy score really is that close")
check(L.num_conflict("Limite 11", "Limite 12"), "numbers disagree")
check(not L.title_match("Limite 11", "Limite 12"), "so the titles do NOT match")
check(L.title_match("Esto es... Makina", "Esto es Makina"), "punctuation still matches")
check(not L.num_conflict("Toma!! Makina", "Toma Makina"), "no numbers, no conflict")

print("\n[one file answers for one track]")
# "White Flag Remix" is two tracks: "White Flag ... Makina" and
# "White Flag ... Hardcore". Strip the bracketed mix and both reduce to
# "white flag", so one file satisfied both and a half-held release read as
# complete — and was never hunted for again.
expected = ["White Flag (Makina Mix)", "White Flag (Hardcore Mix)"]
have = [(L.norm("White Flag (Makina Mix)"), L.strip_mix("White Flag (Makina Mix)"),
         L.norm("01 White Flag"), L.strip_mix("01 White Flag"))]
got = L.match_tracks(expected, have)
eq(len(got), 1, "one file cannot satisfy two tracks")
have2 = have + [(L.norm("White Flag (Hardcore Mix)"),
                 L.strip_mix("White Flag (Hardcore Mix)"),
                 L.norm("02 White Flag"), L.strip_mix("02 White Flag"))]
eq(len(L.match_tracks(expected, have2)), 2, "two files satisfy both")

print("\n[a disc is not a release]")
with tempfile.TemporaryDirectory() as tmp:
    root = os.path.join(tmp, "archive")
    # multi-disc release whose artwork sits in a non-disc sibling — this is the
    # shape that leaked CD1/CD2 out as unknown folders
    rel = os.path.join(root, "(32-819) Top 2000 (1999)")
    touch(os.path.join(rel, "CD1"), "01 - A.flac")
    touch(os.path.join(rel, "CD2"), "01 - B.flac")
    os.makedirs(os.path.join(rel, "Caratulas"), exist_ok=True)
    open(os.path.join(rel, "Caratulas", "front.jpg"), "wb").write(b"x")
    # ordinary single-folder release
    flat = os.path.join(root, "(10-076) Greatest Hits (2012)")
    touch(flat, "01 - C.flac")

    found = L.release_folders(root)
    eq(sorted(os.path.basename(f) for f in found),
       ["(10-076) Greatest Hits (2012)", "(32-819) Top 2000 (1999)"],
       "the release is the parent, not its discs")
    eq(len(L.audio_files(rel)), 2, "both discs' audio belongs to the release")

    # Real disc folders in this archive carry a description after the number.
    # An anchored ^CD\d+$ refused every one of them, and 44 discs came back as
    # unmatched folders while their releases read as missing.
    for name in ("CD1 (Session by Skudero)", "Cd 2 (Session By Marti El Nen)",
                 "CD3 (Best Tracks)", "Disco 1"):
        check(L.is_disc_dir(name), "disc folder recognised: %s" % name)
    for name in ("Covers", "Caratulas", "Scans", "CD"):
        check(not L.is_disc_dir(name), "not a disc folder: %s" % name)

    # its own root, so it cannot pollute the catalogue fixtures below
    with tempfile.TemporaryDirectory() as described:
        named = os.path.join(described, "(22-937) Limite Vol.III (2000)")
        touch(os.path.join(named, "CD1 (Session by Skudero)"), "01 - X.flac")
        touch(os.path.join(named, "CD2 (Session by Xavi Metralla)"), "01 - Y.flac")
        got = [os.path.basename(f) for f in L.release_folders(described)]
        eq(got, ["(22-937) Limite Vol.III (2000)"],
           "described discs collapse into their release, and are not releases")

    # The other direction, and far worse: accepting ANY disc-like subfolder
    # would make a scan root holding one release called "CD1" read as a single
    # release of the whole archive. Artwork folders hold no audio; release
    # folders do, which is what separates the two.
    with tempfile.TemporaryDirectory() as hazard:
        touch(os.path.join(hazard, "CD1"), "01 - trap.flac")
        touch(os.path.join(hazard, "(11-111) A Real Release (1999)"), "01 - A.flac")
        touch(os.path.join(hazard, "(22-222) Another (2000)"), "01 - B.flac")
        got = [os.path.basename(f) for f in L.release_folders(hazard)]
        eq(sorted(got), ["(11-111) A Real Release (1999)", "(22-222) Another (2000)",
                         "CD1"],
           "a root is never swallowed by a folder that looks like a disc")

    print("\n[assess]")
    catalogue = [
        {"id": 1, "catno": "32-819", "title": "Top 2000", "artist": "Various", "year": 1999},
        # a second pressing of the SAME release — two rows used to be emitted
        {"id": 2, "catno": "32-819", "title": "Top 2000", "artist": "Various", "year": 1999},
        {"id": 3, "catno": "10-076", "title": "Greatest Hits", "artist": "Various", "year": 2012},
        {"id": 4, "catno": "99-999", "title": "Never Owned", "artist": "Someone", "year": 2001},
    ]
    tls = {"32819": {"id": 1, "title": "Top 2000", "tracks": ["A", "B"]},
           "10076": {"id": 3, "title": "Greatest Hits", "tracks": ["C", "D"]},
           "99999": {"id": 4, "title": "Never Owned", "tracks": ["E"]}}
    folders = L.index_roots([{"path": root, "role": "owned"}], lambda m: None, "")
    res = L.assess(catalogue, folders, tls)
    rows = {r["catno"]: r for r in res["rows"]}
    eq(len(res["rows"]), 3, "one row per RELEASE, not per pressing")
    eq(rows["32-819"]["pressings"], 2, "the collapsed pressings are counted")
    eq(rows["32-819"]["status"], "complete", "both discs make it complete")
    eq(rows["10-076"]["status"], "partial", "one of two tracks is partial")
    eq(rows["10-076"]["missing_tracks"], ["D"], "and it names the missing one")
    eq(rows["99-999"]["status"], "missing", "a release with no folder is missing")
    eq(rows["99-999"]["missing_tracks"], ["E"], "a missing release lists every track")
    eq(res["orphans"], [], "nothing is orphaned")

    print("\n[formats]")
    wav = os.path.join(root, "(44-444) Wav Release (2000)")
    touch(wav, "01 - W.wav")
    mp3 = os.path.join(root, "(55-555) Lossy Release (2000)")
    touch(mp3, "01 - M.mp3")
    catalogue += [
        {"id": 5, "catno": "44-444", "title": "Wav Release", "artist": "X", "year": 2000},
        {"id": 6, "catno": "55-555", "title": "Lossy Release", "artist": "Y", "year": 2000},
    ]
    tls["44444"] = {"id": 5, "title": "Wav Release", "tracks": ["W"]}
    tls["55555"] = {"id": 6, "title": "Lossy Release", "tracks": ["M"]}
    folders = L.index_roots([{"path": root, "role": "owned"}], lambda m: None, "")
    rows = {r["catno"]: r for r in L.assess(catalogue, folders, tls)["rows"]}
    # A WAV is lossless: it closes the gap, but it is flagged for conversion.
    eq(rows["44-444"]["status"], "complete", "a WAV satisfies the track")
    eq(rows["44-444"]["needs_convert"], 1, "and is flagged for conversion")
    # Lossy is NOT ownership.
    eq(rows["55-555"]["status"], "partial", "an MP3 does not satisfy the track")
    check(rows["55-555"]["lossy_only"], "and the folder is flagged lossy")

    print("\n[incoming verdicts]")
    inc = os.path.join(tmp, "incoming")
    # a release we do not own at all
    touch(os.path.join(inc, "(99-999) Never Owned (2001)"), "01 - E.flac")
    # a more complete copy of one we half-hold
    up = os.path.join(inc, "(10-076) Greatest Hits (2012)")
    touch(up, "01 - C.flac")
    touch(up, "02 - D.flac")
    # a copy that adds nothing
    touch(os.path.join(inc, "(32-819) Top 2000 (1999)"), "01 - A.flac")
    folders = L.index_roots([{"path": root, "role": "owned"},
                             {"path": inc, "role": "incoming"}], lambda m: None, "")
    rows = {r["catno"]: r for r in L.assess(catalogue, folders, tls)["rows"]}
    eq(rows["99-999"]["verdict"], "new", "a release we lack is NEW")
    eq(rows["10-076"]["verdict"], "upgrade", "a fuller copy is an UPGRADE")
    eq(rows["32-819"]["verdict"], "duplicate", "a lesser copy is a DUPLICATE")

    print("\n[orphans]")
    touch(os.path.join(inc, "(XX-000) Some Other Label (1999)"), "01 - Z.flac")
    folders = L.index_roots([{"path": inc, "role": "incoming"}], lambda m: None, "")
    res = L.assess(catalogue, folders, tls)
    names = [o["name"] for o in res["orphans"]]
    eq(names, ["(XX-000) Some Other Label (1999)"],
       "a folder outside the catalogue is an orphan, not a silent drop")

print("\n[folder cache and Stop]")
with tempfile.TemporaryDirectory() as tmp:
    root = os.path.join(tmp, "many")
    for i in range(300):
        touch(os.path.join(root, "(%03d) Release %d (2000)" % (i, i)), "01 - t.flac")
    cache = os.path.join(tmp, "folders.json")

    # Stop must not have to drain every queued read first. pool.map() submits
    # the whole list and shutdown(wait=True) waits for all of it, so Stop once
    # sat for minutes while the button said "stopping…".
    seen = {"n": 0}
    def stopper():
        seen["n"] += 1
        return seen["n"] > 2
    part = L.index_roots([{"path": root, "role": "owned"}], lambda m: None,
                         cache, should_stop=stopper)
    check(0 < len(part) < 300, "Stop halts partway, not at the end",
          "read %d of 300" % len(part))
    check(os.path.exists(cache), "work done before Stop is still cached")

    # The cache is what makes a rescan cheap. It had a default of "" and the one
    # caller that mattered omitted it, so it never existed and every scan
    # re-read every folder off the network mount.
    L.index_roots([{"path": root, "role": "owned"}], lambda m: None, cache)
    msgs = []
    again = L.index_roots([{"path": root, "role": "owned"}], msgs.append, cache)
    line = [m for m in msgs if "folder cache" in m]
    check(bool(line) and "300 unchanged, 0 read" in line[0],
          "a rescan reads nothing", line[0] if line else "no cache line logged")
    check(len(again) == 300, "and still returns every folder", str(len(again)))

    # Omitting the cache path must be impossible to do by accident.
    try:
        L.index_roots([{"path": root, "role": "owned"}], lambda m: None)
        check(False, "index_roots refuses to run without a cache_path")
    except TypeError:
        check(True, "index_roots refuses to run without a cache_path")

print("\n[Discogs pacing]")
# _RateBudget is process-lifetime state shared by every Discogs instance —
# tested directly, not through a mocked urlopen, since the budget object is
# the actual unit that decides pacing and a plain dict already satisfies the
# .get(key, default) interface real HTTP headers expose.
b = L._RateBudget()
check(b.remaining is None, "budget starts unknown")
check(b.extra_gap() == 0.0, "and paces at baseline until told otherwise")
b.note_response({"X-Discogs-Ratelimit-Remaining": "56", "X-Discogs-Ratelimit": "60"})
eq(b.remaining, 56, "remaining parsed from a real response's headers")
eq(b.limit, 60, "limit parsed from a real response's headers")
eq(b.extra_gap(), 0.0, "plenty left — no extra pacing")
b.note_response({"X-Discogs-Ratelimit-Remaining": "2"})
eq(b.remaining, 2, "remaining updates on every response")
check(b.extra_gap() > 0, "low budget — extra pacing kicks in")
b.note_response({})
eq(b.remaining, 2, "a response with no header leaves remaining unchanged — a momentary miss must not erase good information")

b2 = L._RateBudget()
b2.note_429()
eq(b2.remaining, 0, "a 429 sets remaining to 0 immediately, without waiting for a header")
check(b2.extra_gap() > 0, "…and that alone is enough to trigger extra pacing")

print("\n[_flac_tags: fuller tag set + artwork detection]")
# A hand-built FLAC byte stream — real header structure, fake/absent payload
# where it doesn't matter (the picture block's bytes are declared but never
# need to be valid image data, since the parser must never read them).
def _vorbis_body(tags):
    vendor = b"testenc"
    body = len(vendor).to_bytes(4, "little") + vendor
    entries = [("%s=%s" % (k.upper(), v)).encode("utf-8") for k, v in tags.items()]
    body += len(entries).to_bytes(4, "little")
    for e in entries:
        body += len(e).to_bytes(4, "little") + e
    return body


def _block_header(btype, length, last=False):
    b0 = (0x80 if last else 0) | (btype & 0x7F)
    return bytes([b0]) + length.to_bytes(3, "big")


def _write_flac(path, blocks):
    # blocks: [(block_type, body_bytes, is_last), ...]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"fLaC")
        for btype, body, last in blocks:
            fh.write(_block_header(btype, len(body), last))
            fh.write(body)


with tempfile.TemporaryDirectory() as tmp:
    vc = _vorbis_body({"title": "Test Title", "artist": "Test Artist",
                       "tracknumber": "3", "catalognumber": "TB-001"})
    # VORBIS_COMMENT (not last) -> PICTURE with declared-but-unvalidated bytes
    # (not last) -> a real terminating PADDING block (last). If the picture
    # block's declared length were skipped by the wrong amount, the PADDING
    # header below would be misread and everything after it would come out
    # wrong or raise — so a correct result here IS the proof it was skipped
    # correctly, not read.
    p = os.path.join(tmp, "01 - t.flac")
    _write_flac(p, [(4, vc, False), (6, b"\xff" * 32, False), (1, b"", True)])
    tags = L._flac_tags(p)
    eq(tags.get("title"), "Test Title", "title extracted from a synthetic FLAC")
    eq(tags.get("artist"), "Test Artist", "artist extracted too — not just title")
    eq(tags.get("catalognumber"), "TB-001", "catalogue number tag extracted")
    check(tags.get("has_picture") is True,
          "a picture block is detected without ever supplying valid image bytes")
    eq(L._flac_title(p), "Test Title", "_flac_title wrapper still returns just the title")

    p2 = os.path.join(tmp, "02 - t.flac")
    _write_flac(p2, [(4, vc, True)])
    tags2 = L._flac_tags(p2)
    check(tags2.get("has_picture") is False, "no picture block — has_picture is False")

    def _picture_body(width, height, mime=b"image/jpeg", desc=b"", data=b"\xff" * 200):
        body = (3).to_bytes(4, "big")                      # picture type: front cover
        body += len(mime).to_bytes(4, "big") + mime
        body += len(desc).to_bytes(4, "big") + desc
        body += width.to_bytes(4, "big") + height.to_bytes(4, "big")
        body += (24).to_bytes(4, "big")                     # colour depth (unused here)
        body += (0).to_bytes(4, "big")                      # indexed colours (unused here)
        body += len(data).to_bytes(4, "big") + data
        return body

    p3 = os.path.join(tmp, "03 - t.flac")
    _write_flac(p3, [(4, vc, False), (6, _picture_body(0, 0), True)])
    tags3 = L._flac_tags(p3)
    eq(tags3.get("picture_width"), 0, "a declared 0 width is read from the PICTURE block header")
    eq(tags3.get("picture_height"), 0, "…and 0 height too — this IS the 0x0-artwork bug")

    p4 = os.path.join(tmp, "04 - t.flac")
    _write_flac(p4, [(4, vc, False), (6, _picture_body(600, 600), True)])
    tags4 = L._flac_tags(p4)
    eq((tags4.get("picture_width"), tags4.get("picture_height")), (600, 600),
       "a real declared width/height is read correctly, without decoding the image bytes")

print("\n[folder_tracks: tags + artwork]")
with tempfile.TemporaryDirectory() as tmp:
    rel = os.path.join(tmp, "release")
    _write_flac(os.path.join(rel, "01 - t.flac"),
               [(4, _vorbis_body({"title": "One", "artist": "A"}), True)])
    info = L.folder_tracks(rel)
    eq(len(info["tags"]), 1, "one tags dict per lossless file")
    eq(info["tags"][0].get("title"), "One", "and it carries the real tag data, not just the title")
    check(info["has_artwork"] is False, "no embedded picture, no loose image — no artwork")

    open(os.path.join(rel, "cover.jpg"), "wb").write(b"\xff\xd8\xff")
    info2 = L.folder_tracks(rel)
    check(info2["has_artwork"] is True, "a loose cover.jpg counts as artwork too")

print("\n[cross-check: naming]")
row = {"catno": "12-414", "title": "Esto es... Makina", "year": "1997", "artist": "Various"}
eq(L.canonical_name(row), "(12-414) Esto es... Makina (1997)", "canonical name is built the same way it is parsed")
check(L.naming_check("(12-414) Esto es... Makina (1997)", row)["matches"],
      "an exact canonical name matches")
check(L.naming_check("(12-414) Esto es... Makina (1997) - WAV", row)["matches"],
      "trailing noise like '- WAV' is not a naming problem")
check(not L.naming_check("(99-999) Something Else (2004)", row)["matches"],
      "a genuinely different release does not match")
eq(L.naming_check("", row)["matches"], False, "an empty folder name never matches")

print("\n[cross-check: tags]")
expected = ["Track One", "Track Two", "Track Three"]
row2 = {"catno": "12-414", "artist": "DJ Test"}
folder_tags = [
    {"artist": "DJ Test", "tracknumber": "1", "catalognumber": "12-414"},   # track 0
    {"tracknumber": "2"},                                                   # track 1 — no artist
    {"artist": "Someone Else", "tracknumber": "3", "catalognumber": "99-999"},  # track 2
]
matched = {0: 0, 1: 1, 2: 2}                       # match_tracks()'s {track_index: file_index}
tc = L.tag_check(folder_tags, expected, row2, matched)
eq(tc["checked_tracks"], 3, "every matched track is checked")
eq(tc["missing_artist"], ["Track Two"], "a matched file with no artist tag is flagged missing")
check(len(tc["contradicts_artist"]) == 1 and tc["contradicts_artist"][0]["track"] == "Track Three",
      "an artist tag that disagrees with the catalogue is flagged, fuzzy not exact")
check(len(tc["contradicts_catno"]) == 1 and tc["contradicts_catno"][0]["track"] == "Track Three",
      "a catalogue-number tag that disagrees is flagged too")

various_row = {"catno": "12-414", "artist": "Various Artists"}
tc_va = L.tag_check(folder_tags, expected, various_row, matched)
eq(tc_va["missing_artist"], [], "Various Artists rows are never checked for artist agreement")

no_match = L.tag_check(folder_tags, expected, row2, {})
eq(no_match["checked_tracks"], 0, "an unmatched track is never checked — cross-check never "
   "disagrees with completeness about which file is which track")

print("\n[cross-check: artwork]")
check(L.artwork_check({"has_artwork": False, "tags": []})["source"] == "none",
      "no artwork at all reads as 'none'")
check(L.artwork_check({"has_artwork": True, "tags": [{"has_picture": True}]})["source"] == "embedded",
      "an embedded picture block reads as 'embedded'")
check(L.artwork_check({"has_artwork": True, "tags": [{}]})["source"] == "folder-image",
      "artwork present but nothing embedded reads as a loose folder image")
eq(len(L.artwork_check({"has_artwork": False, "tags": []})["deferred"]), 1,
   "the Discogs image fetch is named as deferred, never silently dropped")
check(L.artwork_check({"has_artwork": False, "tags": []})["dimension_ok"] is True,
      "no embedded picture at all — nothing to flag, dimension_ok defaults true")
check(L.artwork_check({"has_artwork": True,
                       "tags": [{"has_picture": True, "picture_width": 500,
                                "picture_height": 500}]})["dimension_ok"] is True,
      "a real declared width/height passes")
check(L.artwork_check({"has_artwork": True,
                       "tags": [{"has_picture": True, "picture_width": 0,
                                "picture_height": 0}]})["dimension_ok"] is False,
      "a declared 0x0 is caught — the actual bug this checks for")
check(L.artwork_check({"has_artwork": True,
                       "tags": [{"has_picture": False}]})["dimension_ok"] is True,
      "no width/height fields at all (not embedded) — nothing to flag")

print("\n[cross-check: combined]")
cc = L.cross_check(row2, {"tags": folder_tags, "has_artwork": False, "folder_name": ""}, expected, matched)
eq(cc["action"], "none", "no write action in this batch — report only")
check("naming" in cc and "tags" in cc and "artwork" in cc, "all three checks are present")

print("\n[series grouping]")
eq(L._normalise_series_number("Limite Vol.II"), "Limite Vol. 2",
   "a roman numeral becomes a plain one")
eq(L._normalise_series_number("Vol. VIV Something"), "Vol. VIV Something",
   "a malformed roman is left alone, not guessed at")
eq(L.series_key({"title": "Just A Title"}), None,
   "no number at all — not a series")
eq(L.series_key({"title": "12"}), None,
   "a number with nothing else is not a series identity")
check(L.series_key({"title": "Limite 11"})[0] == L.series_key({"title": "Limite 12"})[0],
      "two volumes of the same series share the same series identity")
check(L.series_key({"title": "Limite 11"})[1] == 11 and
      L.series_key({"title": "Limite Vol. XII"})[1] == 12,
      "the trailing number (roman or not) is read as the volume number")
check(L.series_key({"title": "Top 98"}) != L.series_key({"title": "Kilos De Mix 98"}),
      "a shared number alone does not make two different series the same one")


def row(title, status):
    return {"title": title, "status": status}


rows = [
    row("Limite 9", "complete"), row("Limite 11", "partial"),
    row("Limite 12", "missing"), row("Limite 13", "missing"),
    # a real pair from this catalogue: same umbrella series, different year
    # ranges in the title, distinguished only by the trailing Vol.N
    row("Dance Collection 2003 - 2007 - Vol.2", "complete"),
    row("Dance Collection 1993 - 1997 - Vol.3", "missing"),
    row("A Totally Unrelated Release", "complete"),   # no number, ignored
    row("Top 98", "complete"),                        # a series of one — dropped
]
groups = {g["series"]: g for g in L.group_series(rows)}
check("limite" in [k.lower() for k in groups], "Limite forms a group",
      str(list(groups)))
g = groups.get("Limite") or groups.get("limite") or next(
    (v for k, v in groups.items() if k.lower() == "limite"), None)
eq(g["total"], 4, "four volumes seen")
eq(g["have"], [9, 11], "9 and 11 are held (partial still counts as held)")
eq(g["missing"], [12, 13], "12 and 13 are outright missing")

dc = next((v for k, v in groups.items() if "dance collection" in k.lower()), None)
check(dc is not None, "the two Dance Collection entries share a group despite "
      "different year ranges in the title")
if dc:
    eq(sorted(dc["have"] + dc["missing"]), [2, 3],
       "grouped on the trailing Vol.N, not the year range")

check(not any("top" in k.lower() for k in groups),
      "a series with only one release seen is not reported as a gap")
check(not any("unrelated" in k.lower() for k in groups),
      "a title with no number never forms a group")

print("\n[match_against_catalogues]")
cat_a = [{"id": 1, "catno": "10-001", "title": "First Release"}]
cat_b = [{"id": 2, "catno": "20-002", "title": "Second Release"}]
catalogues = {100: cat_a, 200: cat_b}

folder_a = {"catno": "10-001", "title": ""}
m = L.match_against_catalogues(folder_a, catalogues)
check(100 in m and 200 not in m, "a catno matches only the label that owns it")
eq(m[100]["matched_by"], "catno", "matched by catalogue number")
eq(m[100]["row"]["title"], "First Release", "the matched catalogue row is returned")

folder_none = {"catno": "99-999", "title": "Nothing Like Either"}
check(L.match_against_catalogues(folder_none, catalogues) == {},
      "a folder matching nobody's catalogue returns nothing")

folder_title_only = {"catno": "", "title": "Second Release"}
m2 = L.match_against_catalogues(folder_title_only, catalogues)
check(200 in m2 and m2[200]["matched_by"] == "title",
      "no catno on the folder falls back to title match, same as attach_folders()")

print("\n[classify_rarity]")
eq(L.classify_rarity(None), "", "no price data at all: unclassified, not guessed")
eq(L.classify_rarity({}), "", "an empty price dict: also unclassified")
eq(L.classify_rarity({"num_for_sale": None, "have": None}), "",
   "num_for_sale and have both unknown: unclassified")
eq(L.classify_rarity({"num_for_sale": 5, "have": 2}), "cheap and common",
   "copies for sale right now: cheap and common, regardless of a low have count")
eq(L.classify_rarity({"num_for_sale": 0, "have": 40}), "cheap and common",
   "nothing for sale THIS WEEK, but plenty of copies logged as owned: still common")
eq(L.classify_rarity({"num_for_sale": 0, "have": 3}), "rare — long hunt",
   "nothing for sale and almost nobody owns it: the actual rare case")
eq(L.classify_rarity({"num_for_sale": None, "have": 3}), "rare — long hunt",
   "num_for_sale missing (no curr_abbr was passed) but have is low: still classifiable")
price_a = L.price_for({"catno": "10-001"}, {})
check(price_a is None, "price_for with an empty cache returns None, not KeyError")
price_b = L.price_for({"catno": "10-001"}, {"10001": {"num_for_sale": 1}})
eq(price_b, {"num_for_sale": 1}, "price_for looks the release up by its normalised catno key")

print("\n%d checks, %d failed" % (ran, len(fails)))
print("PASS" if not fails else "FAILED:\n  - " + "\n  - ".join(fails))
sys.exit(1 if fails else 0)
