#!/usr/bin/env python3
"""Label reference engine — match what is on disk against a label's Discogs catalogue.

Answers the three questions a collector actually has about a label:

    what do we have, what do we NOT have, and what is still missing from the
    releases we only half-hold.

PROVENANCE.  The catalogue-number and track-matching rules here are ported from
`music-tools/label2lossless` (`labelkit.py` and `label_tracklists.py`), where each
one was paid for with a real mis-file.  They are copied rather than imported for
two reasons: `labelkit` inserts its own `vendor/` directory at `sys.path[0]` on
import, which would shadow this app's mutagen; and music-organiser has to run on
Windows and in Docker, where `music-tools` does not exist.  The comments below
name the bug each rule fixes, so neither copy can be "tidied" back into being
wrong.  If you change a rule here, change it there too.

Nothing in this module writes to disk outside its own cache directory.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher

# Audio we consider LOSSLESS.  .aif/.aiff count: some vinyl and CD rips arrive as
# AIFF and are archived as-is, and ignoring the extension once scored a folder
# holding five AIFF tracks as though they were not there.
LOSSLESS_EXT = (".flac", ".wav", ".aif", ".aiff", ".ape", ".wv", ".alac")
# Lossy files are NOT ownership.  They are counted only so a folder that holds
# nothing but MP3s can be labelled for what it is instead of looking empty.
LOSSY_EXT = (".mp3", ".m4a", ".ogg", ".opus", ".wma", ".aac")
AUDIO_EXT = LOSSLESS_EXT + LOSSY_EXT
# A WAV is lossless and therefore satisfies a track, but it is not what the
# archive is made of — the owner's call is "owned, flagged convert".
NEEDS_CONVERT_EXT = (".wav", ".aif", ".aiff")


# ─── catalogue numbers ────────────────────────────────────────────────────────
# The zero-stripped form is an EXTRA key, never a replacement, and it is only used
# for within-label lookups (do we own this / which folder is this release).  It is
# deliberately not used to search arbitrary folder text: loosening that is how
# "(AR-009)" once matched inside "BAR009".

_CATNO_SPLIT = re.compile(r"[,/]")
# pressing decorations Discogs appends to a variant: (N), (CD), (n), " CD", " (2xCD)"
_CATNO_DECOR = re.compile(r"\s*\((?:n|cd|vinyl|lp|promo|\d+\s*x\s*cd)\)\s*$", re.I)
_CATNO_TRAIL = re.compile(r"\s+(?:cd|vinyl|lp|promo)\s*$", re.I)


def folder_catno(name: str) -> str:
    """The catalogue number a release folder announces, as written.

    Reads the leading "(...)" or "[...]" by walking bracket DEPTH, so a nested
    bracket inside the catno is part of the catno instead of ending it.  The
    naive "stop at the first )" version truncated every catno that contained one.
    Returns "" when the folder does not lead with a bracket.
    """
    s = (name or "").strip()
    if not s or s[0] not in "([":
        return _catno_in_brackets(s)
    close = {"(": ")", "[": "]"}[s[0]]
    depth = 0
    for i, ch in enumerate(s):
        if ch == s[0]:
            depth += 1
        elif ch == close:
            depth -= 1
            if depth == 0:
                return s[1:i].strip()
    return ""


# A second convention used in this collection puts the catalogue number in a
# bracket LATER in the name, next to the label:
#     90's Mix (1999) [Bit Music - 32-706] - WAV
#     '01 (2007) [Bit Music - 37-847] OK
# folder_catno() reads only a LEADING bracket, so these returned "" and the
# folders were invisible to the catalogue — 10 of the 31 unmatched folders in
# the Bit Music archive were this shape.
#
# The separator is required deliberately. Reading digits out of ANY bracket
# would take the year from "Album (1999)" and, worse, loosening catalogue
# matching is exactly how "(AR-009)" once matched inside "BAR009". A bracket
# containing "<something> - <something with digits>" is specific enough to mean
# what it looks like.
_BRACKETED = re.compile(r"[\[(]([^\[\]()]+)[\])]")


def _catno_in_brackets(name: str) -> str:
    for inner in _BRACKETED.findall(name or ""):
        if " - " not in inner:
            continue
        cand = inner.rsplit(" - ", 1)[1].strip()
        if not re.search(r"\d", cand):
            continue
        if re.fullmatch(r"(?:19|20)\d\d", cand):      # a year is not a catno
            continue
        return cand
    return ""


def folder_title(name: str) -> str:
    """What is left of a release folder name after its catno and trailing year."""
    s = (name or "").strip()
    cat = folder_catno(s)
    if cat:
        s = s[s.index(cat) + len(cat) + 1:].strip() if cat in s else s
        s = s.lstrip(")]").strip()
    # placeholder markers are plain .txt files named exactly like the folder they
    # stand in for — the extension is not part of the title
    s = re.sub(r"\.txt$", "", s, flags=re.I)
    return re.sub(r"\s*\((?:19|20)\d\d\)\s*$", "", s).strip()


_YEAR_LED = re.compile(r"^\((?:19|20)\d{2}\)\s*(.+)$")
_TRAILING_FORMAT = re.compile(r"\s+(WAV|FLAC|MP3|AIFF|ALAC|OGG)\s*\Z", re.I)
# ZERO WIDTH SPACE/NON-JOINER/NON-BREAK, LEFT-TO-RIGHT MARK and the other
# Unicode formatting marks in that block, plus the BOM -- invisible, but
# they sit right next to a dash often enough in this specific archive to
# silently break a plain " - " split (real incident: "MC Hair ‎-
# Jewels E.P." would not split at all). Built from explicit \u escapes,
# never pasted as literal glyphs, so the codepoints stay auditable instead
# of invisible in a diff.
_INVISIBLE = re.compile(
    "[​-‏‪-‮﻿]")


def _split_artist_title(s: str) -> tuple[str, str]:
    """"Artist - Title" -> (artist, title), invisible formatting marks
    stripped first. artist is "" when there is no " - " to split on."""
    rest = _INVISIBLE.sub("", s or "")
    parts = re.split(r"\s+-\s+", rest.strip(), maxsplit=1)
    if len(parts) == 2 and parts[0].strip():
        return parts[0].strip(), parts[1].strip()
    return "", rest.strip()


def year_led_artist_title(name: str) -> tuple[str, str] | None:
    """For a folder named "(YEAR) Artist - Title FORMAT" -- a different
    archive's convention (year leads, not a catalogue number) from this
    one's own "(CATNO) Title (Year)" -- the (artist, title) pair to match
    against Discogs. None when the folder isn't shaped like this at all (no
    leading 4-digit-year bracket), so this never fires against this
    archive's own folders and can't regress their matching.

    artist is "" when there is no " - " to split on; the caller still gets
    a usable title either way.
    """
    m = _YEAR_LED.match((name or "").strip())
    if not m:
        return None
    rest = _TRAILING_FORMAT.sub("", m.group(1)).strip()
    return _split_artist_title(rest)


def search_result_to_row(sr: dict) -> dict:
    """One live search_release() hit, reshaped into a catalogue.json-style
    row (separate artist/title, not search's combined "Artist - Title") --
    the same shape label_releases() rows already have, so a release found
    this way slots into the existing catalogue cache and every function
    that reads it (attach_folders, tag_incoming, price/artwork lookups)
    needs no special case for where the row came from.
    """
    artist, title = _split_artist_title(sr.get("title") or "")
    return {"id": sr.get("id"), "catno": sr.get("catno") or "",
            "title": title, "artist": artist,
            "year": str(sr.get("year") or ""), "format": ""}


def ncat(s: str) -> str:
    """The ONE catalogue-number key: punctuation is not information here."""
    return re.sub(r"\W", "", (s or "")).lower()


def _dezero(key: str) -> str:
    """Drop leading zeros from every digit run: 'parmx001' -> 'parmx1'."""
    return re.sub(r"0+(\d)", r"\1", key)


def catno_keys(raw) -> set:
    """Every normalised key one catalogue-number field can legitimately mean.

    Discogs packs all of a release's pressings into a single field —
    "MXCD 1562 (CD), MXCD1562(CD), MXCD 1562 CD" — so matching the whole joined
    string never hit a folder key and multi-catno releases could not be owned at
    all.  Split the variants, undecorate, normalise, and add the zero-stripped
    form of each, because Discogs writes "PARMX 01" where the disk says
    "(PARMX 001)"; comparing only the padded forms reported releases we hold as
    missing, forever.
    """
    out = set()
    for part in _CATNO_SPLIT.split(str(raw or "")):
        part = _CATNO_TRAIL.sub("", _CATNO_DECOR.sub("", part.strip()))
        k = ncat(part)
        if not k or not re.search(r"\d", k):
            # "B & C" falls out of splitting "13-110 (A, B & C)" and is not a catno
            continue
        out.add(k)
        out.add(_dezero(k))
    whole = ncat(raw)
    if whole and re.search(r"\d", whole):
        out.add(whole)
        out.add(_dezero(whole))
    return {k for k in out if k}


def catno_match(a, b) -> bool:
    """Do two catalogue-number fields name the same release?"""
    ka, kb = catno_keys(a), catno_keys(b)
    return bool(ka and kb and (ka & kb))


# ─── titles ───────────────────────────────────────────────────────────────────
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()


def strip_mix(s: str) -> str:
    """Drop bracketed mix/remix info, for a looser title compare."""
    return norm(re.sub(r"[\(\[].*?[\)\]]", " ", s or ""))


_NUM = re.compile(r"\d+")


def num_conflict(a: str, b: str) -> bool:
    """Do two titles disagree on their NUMBERS?

    This label is built on numbered series, and 'Limite 11' vs 'Limite 12' scores
    0.875 against a 0.86 bar — so holding one volume silently marked every
    neighbouring volume owned, and they were never asked for again.  Measured on
    the Bit Music catalogue, 35 entries were "owned" on nothing but a
    number-conflicting title, including Wanted Nº 0001-4 and Top 2002-2013.
    """
    na, nb = _NUM.findall(a or ""), _NUM.findall(b or "")
    if not na or not nb:
        return False
    return {int(x) for x in na} != {int(x) for x in nb}


def fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def title_match(a: str, b: str, bar: float = 0.86) -> bool:
    """A title match must also AGREE ON ITS NUMBERS."""
    return fuzzy(a, b) >= bar and not num_conflict(a, b)


# ─── what is in a folder ──────────────────────────────────────────────────────
def audio_files(path: str) -> list:
    """Every audio file under a release folder, INCLUDING subfolders.

    Releases that ship as continuous DJ sessions get a subfolder per session, and
    a flat listdir() saw only the loose files — scoring a 20-file folder 7/57 and
    putting 45 phantom gaps on the chase list.
    """
    out = []
    for dp, _dirs, fs in os.walk(path):
        for f in fs:
            if f.lower().endswith(AUDIO_EXT):
                out.append(os.path.join(dp, f))
    return out


def _readn(fh, n: int) -> bytes:
    """Read exactly n bytes from a RAW file object, or as many as exist.

    An unbuffered read is allowed to come back short, so every fixed-length read
    has to loop. Buffered files hide this; buffering is what we are avoiding.
    """
    out = b""
    while len(out) < n:
        chunk = fh.read(n - len(out))
        if not chunk:
            break
        out += chunk
    return out


_WANT_TAGS = {b"title", b"artist", b"album", b"tracknumber", b"date", b"year",
             b"catalognumber", b"label"}


def _parse_comment_block(blob: bytes) -> dict:
    """Every wanted VORBIS_COMMENT key=value pair in one pass over bytes
    already read for the title — a superset of what the old title-only parser
    collected, same single read, same cost."""
    out: dict = {}
    try:
        pos = 0
        vlen = int.from_bytes(blob[pos:pos + 4], "little")
        pos += 4 + vlen                          # skip the vendor string
        count = int.from_bytes(blob[pos:pos + 4], "little")
        pos += 4
        for _ in range(min(count, 512)):
            clen = int.from_bytes(blob[pos:pos + 4], "little")
            pos += 4
            key, _sep, val = blob[pos:pos + clen].partition(b"=")
            pos += clen
            k = key.lower()
            if k in _WANT_TAGS and k not in out:
                out[k.decode("ascii", "replace")] = val.decode("utf-8", "replace")
    except Exception:
        pass
    return out


def _flac_tags(full: str) -> dict:
    """artist/album/title/tracknumber/date/catalognumber/label out of a FLAC,
    plus has_picture: bool — WITHOUT ever reading the cover art's bytes.

    `mutagen.flac.FLAC` parses every metadata block, and in this collection most
    files carry embedded artwork — so reading a one-word title pulled a
    multi-megabyte JPEG through the CIFS mount, per file, and then handed the
    buffer back to a per-thread malloc arena that never returned it to the OS.
    A 1,275-folder scan reached 5.5 GB resident and moved at 1.8s a folder.

    FLAC metadata is a chain of blocks, each with a 4-byte header giving its
    type and length, so PICTURE blocks (type 6) can be SEEKED OVER and never
    read — this walks the WHOLE chain (not just up to the first VORBIS_COMMENT,
    type 4) so has_picture is known even when the picture comes after it, which
    it usually does. Falls back to mutagen on anything unexpected — this parser
    only has to be right about the common case, not about every file in the
    world.
    """
    try:
        tags: dict = {}
        has_picture = False
        pic_dims = None
        # buffering=0 on purpose. A buffered reader sizes itself from the
        # filesystem's st_blksize, which on this CIFS mount is 1 MB — so asking
        # for 64 KB quietly issued a 1 MB read and the whole point was lost
        # (measured: 1,024 KB per file either way). Raw reads mean the numbers
        # below are the bytes that actually cross the wire.
        with open(full, "rb", buffering=0) as fh:
            # One 64 KB read covers the whole metadata chain for almost every
            # file, because VORBIS_COMMENT is written before PICTURE. That makes
            # the common case a SINGLE round trip over the network mount, where
            # a seek-per-block would be one each. Files that put the picture
            # first (they exist here) fall through to the seek loop below, which
            # still never reads a PICTURE block's IMAGE bytes.
            data = _readn(fh, 65536)
            if not data.startswith(b"fLaC"):
                return {}
            off = 4
            in_phase1 = True
            while in_phase1 and off + 4 <= len(data):
                last = data[off] & 0x80
                btype = data[off] & 0x7F
                length = int.from_bytes(data[off + 1:off + 4], "big")
                off += 4
                if btype == 6:                       # PICTURE
                    has_picture = True
                    fh.seek(off)
                    pic_dims = _picture_dims(fh, length)
                elif btype == 4 and not tags and length <= (1 << 20):
                    if off + length <= len(data):
                        tags = _parse_comment_block(data[off:off + length])
                    else:
                        fh.seek(off)
                        tags = _parse_comment_block(_readn(fh, length))
                        in_phase1 = False             # fh position now authoritative
                if last:
                    return _flac_tags_result(tags, has_picture, pic_dims)
                off += length
            if in_phase1:
                # The chain ran past the first read — carry on by seeking, still
                # never reading a PICTURE block's IMAGE bytes.
                fh.seek(off)
            while True:
                head = _readn(fh, 4)
                if len(head) < 4:
                    return _flac_tags_result(tags, has_picture, pic_dims)
                last = head[0] & 0x80
                btype = head[0] & 0x7F
                length = int.from_bytes(head[1:4], "big")
                if btype == 6:
                    has_picture = True
                    pic_dims = _picture_dims(fh, length)
                elif btype == 4 and not tags and length <= (1 << 20):
                    tags = _parse_comment_block(_readn(fh, length))
                else:
                    fh.seek(length, 1)
                if last:
                    return _flac_tags_result(tags, has_picture, pic_dims)
    except Exception:
        pass

    try:                                             # last resort
        from mutagen.flac import FLAC
        f = FLAC(full)
        out = {}
        for k in ("title", "artist", "album", "tracknumber", "date", "catalognumber", "label"):
            v = f.get(k) or [""]
            if v[0]:
                out[k] = v[0]
        out["has_picture"] = bool(f.pictures)
        if f.pictures:
            out["picture_width"] = f.pictures[0].width
            out["picture_height"] = f.pictures[0].height
        return out
    except Exception:
        return {}


def _flac_tags_result(tags: dict, has_picture: bool, pic_dims) -> dict:
    out = {**tags, "has_picture": has_picture}
    if pic_dims is not None:
        out["picture_width"], out["picture_height"] = pic_dims
    return out


def _picture_dims(fh, block_length: int):
    """The PICTURE block's own DECLARED width/height — two fixed 32-bit
    fields the FLAC spec puts right after the MIME type and description
    strings, well before the actual image bytes. A broken tagger writes the
    real image correctly but leaves these two numeric fields at zero (the
    "0x0 artwork" bug — hit 1,044 of 2,012 files in this archive). Reading
    them needs only a small bounded prefix of the block, nowhere near the
    image payload itself (often hundreds of KB to several MB) — this is the
    one place _flac_tags reads INTO a PICTURE block at all, and it still
    never touches the image bytes.

    Returns (width, height), or None if the block couldn't be parsed (also
    still skips the rest of the block either way — the caller must not have
    to worry about fh's position after this call).
    """
    cap = min(block_length, 4096)
    blob = _readn(fh, cap)
    left = block_length - len(blob)
    if left > 0:
        fh.seek(left, 1)
    try:
        pos = 4                                       # picture type (unused)
        mlen = int.from_bytes(blob[pos:pos + 4], "big"); pos += 4 + mlen
        dlen = int.from_bytes(blob[pos:pos + 4], "big"); pos += 4 + dlen
        w = int.from_bytes(blob[pos:pos + 4], "big"); pos += 4
        h = int.from_bytes(blob[pos:pos + 4], "big")
        return w, h
    except Exception:
        return None


def _flac_title(full: str) -> str:
    return _flac_tags(full).get("title", "")


def _read_title(full: str) -> str:
    """The TITLE tag, where the format carries one.  Never raises."""
    low = full.lower()
    try:
        if low.endswith(".flac"):
            return _flac_title(full)
        if low.endswith((".wav", ".aif", ".aiff")):
            # WAVs usually carry no tags at all — that is why 110 of the 141
            # "broken" rows in library.db are .wav.  Try, accept nothing.
            import mutagen
            m = mutagen.File(full)
            if m:
                for k in ("title", "TIT2", "INAM"):
                    v = m.get(k)
                    if v:
                        return str(v[0] if isinstance(v, list) else v)
    except Exception:
        pass
    return ""


def folder_tracks(path: str) -> dict:
    """Everything we need to know about one release folder on disk.

    Returns track identities as 4-tuples (title tag, mix-stripped title,
    filename, mix-stripped filename) — the shape match_tracks() scores against —
    plus the format facts that decide whether this folder is archive-grade.

    Also returns `tags` (one dict per lossless file — artist/album/title/
    tracknumber/date/catalognumber/label, whatever was present) and
    `has_artwork`, for the Discogs cross-check. Both ride along on the SAME
    read as the title lookup (_flac_tags supersedes _flac_title) so cross-check
    costs nothing extra over what completeness matching already paid for, and
    both are cached automatically inside FolderCache's per-folder `info` dict.
    """
    files = audio_files(path)
    ids, tags_list, lossless_paths, lossless, lossy, convert, bytes_ = \
        [], [], [], 0, 0, 0, 0
    has_artwork = False
    for full in files:
        f = os.path.basename(full)
        low = f.lower()
        try:
            bytes_ += os.path.getsize(full)
        except OSError:
            pass
        if low.endswith(LOSSY_EXT):
            lossy += 1
            continue                      # lossy files are not ownership
        lossless += 1
        if low.endswith(NEEDS_CONVERT_EXT):
            convert += 1
        if low.endswith(".flac"):
            file_tags = _flac_tags(full)
        else:
            t = _read_title(full)
            file_tags = {"title": t} if t else {}
        if file_tags.get("has_picture"):
            has_artwork = True
        tags_list.append(file_tags)
        lossless_paths.append(full)
        title = file_tags.get("title", "")
        # filename minus a leading "NN -", "NN." or vinyl position "A1."/"B2."
        fn = re.sub(r"^\s*[A-Da-d]?\d+\s*[-.]?\s*", "", os.path.splitext(f)[0])
        ids.append((norm(title), strip_mix(title), norm(fn), strip_mix(fn)))
    if not has_artwork:
        # A cover image sitting loose in the folder (not embedded) counts too.
        # Top level only, not recursive — cheap, and the common case ("folder
        # .jpg" beside the tracks) lives at the top of a release folder anyway.
        try:
            for name in os.listdir(path):
                if name.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                    has_artwork = True
                    break
        except OSError:
            pass
    return {"ids": ids, "lossless": lossless, "lossy": lossy,
            "needs_convert": convert, "bytes": bytes_, "files": len(files),
            "tags": tags_list, "paths": lossless_paths, "has_artwork": has_artwork}


# CD1 / Disc 2 / Disco 3 — a disc is PART of a release, never a release.
# The number may be followed by a description: real folders in this archive are
# named "CD1 (Session by Skudero)" and "Cd 2 (Session By Marti El Nen)", and an
# anchored "^CD\d+$" refused all of them — each disc then became its own
# unmatched folder while the release it belonged to read as missing.
_DISC_DIR = re.compile(r"^(cd|disc|disco|disk|dvd|vol|volume|part|parte)"
                       r"\s*_?-?\s*\d+\b", re.I)


def is_disc_dir(name: str) -> bool:
    return bool(_DISC_DIR.match((name or "").strip()))


def _holds_audio(path: str) -> bool:
    """Is there any audio anywhere under here?  Stops at the first one found."""
    for _dp, _dirs, fs in os.walk(path):
        for f in fs:
            if f.lower().endswith(AUDIO_EXT):
                return True
    return False


def release_folders(root: str, max_depth: int = 4) -> list:
    """Directories under `root` that are one RELEASE each.

    Two rules, both learned from the archive:

    - A release is the shallowest directory holding audio.  Descending past it
      splits a continuous-mix release into its sessions, each of which then
      looks like a half-empty release of its own.
    - A directory whose subfolders are all discs (CD1, CD2, Disco 3) IS the
      release, even though it holds no audio itself.  Without this, a 2xCD
      compilation is reported as two unknown folders called "CD1" and "CD2" —
      they match nothing in the catalogue, and the release they belong to reads
      as missing.  Measured on 80 folders of the Bit Music archive: 29 of the
      29 "not in catalogue" rows were disc subfolders.
    """
    out = []
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return out

    def visit(d: str, depth: int):
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        subdirs = [e for e in entries
                   if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
        has_audio = any(e.is_file(follow_symlinks=False)
                        and e.name.lower().endswith(AUDIO_EXT) for e in entries)
        if has_audio:
            out.append(d)
            return
        # A disc-like subfolder makes this the release — but only when every
        # OTHER subfolder holds no audio. Requiring them ALL to be discs failed
        # on the real archive ("(32-819) Top 2000" keeps its artwork in a
        # sibling called "Caratulas", Top 98/99 use "Covers"), while accepting
        # ANY disc-like subfolder is dangerous in the other direction: one
        # release folder called "CD1" directly under a scan root would make the
        # ENTIRE ROOT read as a single release of 13,000 tracks. Artwork folders
        # contain no audio and release folders do, which separates the two
        # cases exactly, and costs a walk only when a disc folder is present.
        discs = [e for e in subdirs if is_disc_dir(e.name)]
        if discs and all(not _holds_audio(e.path)
                         for e in subdirs if not is_disc_dir(e.name)):
            out.append(d)
            return
        if depth >= max_depth:
            return
        for e in subdirs:
            visit(e.path, depth + 1)

    visit(root, 0)
    # The root counts as a release only when it is plainly ONE folder of music —
    # audio directly inside and nothing but disc folders under it.  Counting a
    # root that merely contains releases would read the whole archive as a single
    # 13,000-track release.
    out = [d for d in out if d != root]
    try:
        entries = list(os.scandir(root))
        subdirs = [e for e in entries
                   if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
        if any(e.is_file(follow_symlinks=False)
               and e.name.lower().endswith(AUDIO_EXT) for e in entries) \
                and all(is_disc_dir(e.name) for e in subdirs):   # noqa: E129
            out.append(root)
    except OSError:
        pass
    return sorted(out)


# ─── track-to-file assignment ─────────────────────────────────────────────────
def _pair_score(track: str, hv) -> float:
    """How strongly one file answers for one track, 0 = not at all.

    Tiered so the STRONGEST evidence wins the file when several tracks want it:
    the title tag beats the filename, an exact containment beats a mix-stripped
    one, and a fuzzy match is the last resort.
    """
    nt, st = norm(track), strip_mix(track)
    htitle, hstitle, hfn, hsfn = hv
    best = 0.0
    for c, tier in ((htitle, 100.0), (hfn, 90.0)):
        if c and nt and (nt in c or c in nt):
            best = max(best, tier)
    for c, tier in ((hstitle, 70.0), (hsfn, 65.0)):
        if c and st and len(st) >= 4 and (st in c or c in st):
            best = max(best, tier)
    if not best:
        for c in (htitle, hfn):
            if not c or not nt:
                continue
            ratio = SequenceMatcher(None, nt, c).ratio()
            if ratio >= 0.7:
                best = max(best, 50.0 + ratio * 10.0)
    return best


def match_tracks(expected: list, have: list) -> dict:
    """Assign files to tracks ONE-TO-ONE.  Returns {track_index: file_index}.

    Testing every track against the whole folder independently let a single file
    answer for several tracks, so a folder holding half a release reported as
    complete.  The case that exposed it: "White Flag Remix" is two tracks,
    "White Flag ... Makina" and "White Flag ... Hardcore"; strip the bracketed
    mix and both reduce to "white flag", so one file satisfied both and a release
    we owned half of read as fully held.

    A file is a physical thing: it can be exactly one track.  Score every pair,
    then take them strongest first, retiring both the track and the file.

    The return value doubles as the set of matched track indices for every
    existing caller (`len(match_tracks(...))`, `i in match_tracks(...)`  — both
    work identically against a dict's keys as they did against a set), while
    also exposing WHICH file answered which track — the cross-check tag
    comparison needs that mapping and must never re-derive its own guess at it.
    """
    pairs = []
    for ti, t in enumerate(expected):
        for fi, hv in enumerate(have):
            sc = _pair_score(t, hv)
            if sc:
                pairs.append((sc, ti, fi))
    pairs.sort(key=lambda x: (-x[0], x[1], x[2]))   # ties by position = deterministic
    used_t, used_f = set(), set()
    assignment = {}
    for _sc, ti, fi in pairs:
        if ti in used_t or fi in used_f:
            continue
        used_t.add(ti)
        used_f.add(fi)
        assignment[ti] = fi
    return assignment


# ─── Discogs ──────────────────────────────────────────────────────────────────
class DiscogsError(RuntimeError):
    pass


class _RateBudget:
    """Process-lifetime Discogs pacing state, shared by every Discogs instance.

    fetch_catalogue() and fetch_tracklists() each build a fresh Discogs(...), so
    without a module-level budget a job started right after a previous job nearly
    used up the 60/min window would still pace as if it had a full one. Only one
    job runs at a time (web_ui.py's /api/job/{kind} refuses a second with 409),
    so this needs no cross-process locking — the thread lock is just defensive.

    Verified 2026-09-11 against a real authenticated response: Discogs sends
    X-Discogs-Ratelimit / X-Discogs-Ratelimit-Remaining / X-Discogs-Ratelimit-Used
    (header names are case-insensitive per HTTP, and case-insensitive lookup via
    HTTPMessage.get() either way).
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.remaining: int | None = None
        self.limit: int = 60

    def note_response(self, headers) -> None:
        remaining = headers.get("X-Discogs-Ratelimit-Remaining")
        limit = headers.get("X-Discogs-Ratelimit")
        with self.lock:
            if remaining is not None:
                try:
                    self.remaining = int(remaining)
                except ValueError:
                    pass
            if limit is not None:
                try:
                    self.limit = int(limit)
                except ValueError:
                    pass

    def note_429(self) -> None:
        with self.lock:
            self.remaining = 0

    def extra_gap(self) -> float:
        with self.lock:
            remaining = self.remaining
        # Low budget: add a flat buffer so the run slows itself down instead of
        # racing into the next 429. 3 is a conservative floor — plenty of slack
        # under a 60/min window, cheap insurance against clock skew between us
        # and Discogs' own counter.
        return 12.0 if remaining is not None and remaining <= 3 else 0.0


_BUDGET = _RateBudget()


class Discogs:
    """Just enough Discogs for a label catalogue and its tracklists.

    Rate limit is 60 requests/minute authenticated; this paces at one every 1.2s
    baseline, backs off further when _BUDGET reports the window is nearly spent,
    and backs off hard on 429 rather than racing into a ban.
    """

    UA = "music-organiser/label-ref +personal-archival"

    def __init__(self, token: str, log=print):
        self.token = (token or "").strip()
        self.log = log
        self._last = 0.0

    def _get(self, path: str, params: dict | None = None, tries: int = 5):
        if not self.token:
            raise DiscogsError("no Discogs token — set one in the Pipeline tab")
        p = dict(params or {})
        p["token"] = self.token
        url = "https://api.discogs.com" + path + "?" + urllib.parse.urlencode(p)
        for attempt in range(tries):
            extra = _BUDGET.extra_gap()
            if extra:
                self.log(f"[discogs] {_BUDGET.remaining} requests left this window — pacing down")
            gap = 1.2 - (time.time() - self._last) + extra
            if gap > 0:
                time.sleep(gap)
            self._last = time.time()
            req = urllib.request.Request(url, headers={"User-Agent": self.UA})
            try:
                with urllib.request.urlopen(req, timeout=30) as fh:
                    _BUDGET.note_response(fh.headers)
                    return json.load(fh)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise DiscogsError("not found: " + path)
                if e.code == 429:
                    _BUDGET.note_429()
                if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                    wait = 5 * (attempt + 1)
                    self.log(f"[discogs] {e.code} — waiting {wait}s")
                    time.sleep(wait)
                    continue
                raise DiscogsError(f"HTTP {e.code} on {path}")
            except Exception as e:
                if attempt < tries - 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise DiscogsError(str(e))
        raise DiscogsError("gave up on " + path)

    def search_label(self, name: str) -> list:
        d = self._get("/database/search", {"q": name, "type": "label", "per_page": 25})
        return [{"id": r.get("id"), "title": r.get("title", ""),
                 "url": r.get("uri", "")} for r in d.get("results", [])]

    def label_releases(self, label_id: int, progress=None) -> list:
        """Every release Discogs files under this label."""
        out, page, pages = [], 1, 1
        while page <= pages:
            d = self._get(f"/labels/{int(label_id)}/releases",
                          {"page": page, "per_page": 100})
            pages = d.get("pagination", {}).get("pages", page)
            for r in d.get("releases", []):
                out.append({
                    "id": r.get("id"),
                    "catno": (r.get("catno") or "").strip(),
                    "title": (r.get("title") or "").strip(),
                    "artist": (r.get("artist") or "").strip(),
                    "year": r.get("year", "") or "",
                    "format": (r.get("format") or "").strip(),
                })
            if progress:
                progress(page, pages, len(out))
            page += 1
        return out

    def tracklist(self, release_id: int) -> list:
        d = self._get(f"/releases/{int(release_id)}")
        out = []
        for t in d.get("tracklist", []):
            # Headings have no duration and no position; they are not tracks.
            if (t.get("type_") or "track") != "track":
                continue
            title = (t.get("title") or "").strip()
            if title:
                out.append(title)
        return out

    def release_price(self, release_id: int, curr_abbr: str = "") -> dict:
        """Marketplace stats for one release — SAME /releases/{id} endpoint
        tracklist() and release_images() already call, just with a currency
        param and reading different fields off the same response. No new
        endpoint, no new rate-limit concern.

        `lowest_price` and `num_for_sale` only appear at all once curr_abbr
        is passed (Discogs' own behaviour, not a choice made here) and
        EXCLUDE postage — the price on the label is not the price in your
        basket. `community` (have/want) is always present and is the
        better rarity signal anyway: a release with zero copies for sale
        this week is not necessarily rare, but one only 3 people on Discogs
        have ever logged owning is.
        """
        d = self._get(f"/releases/{int(release_id)}",
                      {"curr_abbr": curr_abbr} if curr_abbr else None)
        community = d.get("community") or {}
        return {
            "lowest_price": d.get("lowest_price"),
            "num_for_sale": d.get("num_for_sale"),
            "have": community.get("have"),
            "want": community.get("want"),
        }

    def search_release(self, query: str, label: str = "") -> list:
        """Live Discogs search, for a folder the local catalogue cache has
        no answer for at all — a DIFFERENT source of truth than
        label_releases() (which only ever lists what was already fetched
        for this label), so this is the only way to find a release the
        catalogue fetch missed or that was added to Discogs since.

        `label` narrows results to this label by name (a text filter, not
        an id — Discogs' own search API has no id-scoped label filter), cheap
        insurance against matching a same-titled release on someone else's
        catalogue. Results carry catno and a combined "title" ("Artist -
        Title") straight from search, not the separate artist/title fields
        label_releases() returns — the caller splits it the same way a
        year-led folder name gets split.
        """
        params = {"type": "release", "q": query}
        if label:
            params["label"] = label
        d = self._get("/database/search", params)
        return d.get("results", []) or []

    def release_images(self, release_id: int) -> list:
        """A release's own images, straight off the SAME /releases/{id}
        response tracklist() already calls — no new endpoint, just a field
        that call used to throw away. Each entry carries at least "type"
        ("primary" or "secondary") and "uri" (full-size); the caller
        prefers "primary" and falls back to whatever is there."""
        d = self._get(f"/releases/{int(release_id)}")
        return d.get("images", []) or []


# ─── caches ───────────────────────────────────────────────────────────────────
def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


class LabelCache:
    """Per-label Discogs data on disk: the catalogue, and tracklists by catno key.

    Seeds itself from music-tools/label2lossless when that tool has already paid
    for the API calls — 2,952 Bit Music releases and 2,400 tracklists were
    fetched there, and re-fetching them would take the better part of an hour for
    no new information.
    """

    # A convenience on the Linux box only: label2lossless has already paid for
    # these API calls. Absent on Windows and in Docker, where seed() says so and
    # the catalogue is fetched from Discogs instead — which is not a fallback,
    # it is the normal path everywhere else.
    SEED_DIR = os.environ.get("LABEL2LOSSLESS_DIR",
                              "/home/media/music-tools/label2lossless")

    def __init__(self, root: str, label_id: int):
        self.label_id = int(label_id)
        self.dir = os.path.join(root, str(self.label_id))
        self.cat_path = os.path.join(self.dir, "catalogue.json")
        self.tl_path = os.path.join(self.dir, "tracklists.json")
        self.pr_path = os.path.join(self.dir, "prices.json")
        self.fo_path = os.path.join(self.dir, "folder_overrides.json")

    # -- catalogue --
    def catalogue(self) -> list:
        return _read_json(self.cat_path, [])

    def save_catalogue(self, rows: list):
        _write_json(self.cat_path, rows)

    # -- tracklists, keyed by every catno key the release answers to --
    def tracklists(self) -> dict:
        return _read_json(self.tl_path, {})

    def save_tracklists(self, d: dict):
        _write_json(self.tl_path, d)

    # -- marketplace price/rarity, keyed by every catno key too (same shape
    # as tracklists, so callers that already know how to look one up know
    # how to look up the other) --
    def prices(self) -> dict:
        return _read_json(self.pr_path, {})

    def save_prices(self, d: dict):
        _write_json(self.pr_path, d)

    # -- folder path -> Discogs release id, for a specific folder search_orphans()
    # has already confirmed the answer for by name, keyed on path rather than
    # any catno/title key because the whole reason it exists is that this ONE
    # folder's name doesn't agree with attach_folders()' own matching tiers --
    # even though the release itself is (or now is) in the catalogue.
    def folder_overrides(self) -> dict:
        return _read_json(self.fo_path, {})

    def save_folder_overrides(self, d: dict):
        _write_json(self.fo_path, d)

    def seed(self, log=print) -> str:
        """Import label2lossless's caches for this label, if they exist.

        Returns a human sentence about what happened.  Read-only on the source:
        this never writes into the other tool's directory.
        """
        if not os.path.isdir(self.SEED_DIR):
            return "no label2lossless install to seed from"
        got = []
        if not os.path.exists(self.cat_path):
            for name in (f"discogs_catalog_{self.label_id}.json",
                         "discogs_bitmusic_catalog.json" if self.label_id == 10663 else None):
                if not name:
                    continue
                p = os.path.join(self.SEED_DIR, name)
                rows = _read_json(p, None)
                if rows:
                    self.save_catalogue(rows)
                    got.append(f"{len(rows)} catalogue entries")
                    break
        if not os.path.exists(self.tl_path):
            for name in (f"discogs_tracklists_{self.label_id}.json",
                         "discogs_tracklists.json" if self.label_id == 10663 else None):
                if not name:
                    continue
                p = os.path.join(self.SEED_DIR, name)
                d = _read_json(p, None)
                if d:
                    # label2lossless keys on ncat(catno) and stores {id,title,tracks}
                    self.save_tracklists(d)
                    got.append(f"{len(d)} tracklists")
                    break
        return ("seeded " + " and ".join(got) + " from label2lossless") if got \
            else "nothing to seed (caches already present or absent)"


# ─── the scan ─────────────────────────────────────────────────────────────────
class FolderCache:
    """Remembered per-folder reads, so a rescan is cheap.

    Reading tags is the entire cost of a scan: usb-a is a CIFS mount of the
    Windows box, so every FLAC header is a round trip and 60 folders measured at
    ~8 seconds each.  A folder is re-read only when its directory mtime or its
    file count changes, which turns a three-hour rescan into a walk.
    """

    def __init__(self, path: str):
        self.path = path
        self.data = _read_json(path, {})
        self.hits = self.misses = 0

    @staticmethod
    def _stamp(d: str) -> str:
        newest, n = 0.0, 0
        for dp, _dirs, fs in os.walk(d):
            try:
                newest = max(newest, os.path.getmtime(dp))
            except OSError:
                pass
            n += len(fs)
        return f"{int(newest)}:{n}"

    def get(self, d: str) -> dict:
        stamp = self._stamp(d)
        hit = self.data.get(d)
        if hit and hit.get("stamp") == stamp:
            self.hits += 1
            # JSON turns the 4-tuples into lists; scoring needs them indexable
            # only, so this is free — but make it a tuple so the types match.
            return {**hit["info"], "ids": [tuple(x) for x in hit["info"]["ids"]]}
        self.misses += 1
        info = folder_tracks(d)
        self.data[d] = {"stamp": stamp, "info": info}
        return info

    def save(self):
        # prune folders that no longer exist, so the file cannot grow forever
        self.data = {k: v for k, v in self.data.items() if os.path.isdir(k)}
        _write_json(self.path, self.data)


def index_roots(roots: list, log, cache_path: str, workers: int = 8,
                should_stop=None) -> list:
    """Read every release folder under every root, once.

    `roots` is a list of {path, role} where role is "owned" or "incoming".
    Tag reading is I/O against a network mount, not CPU, so it is done on a
    small thread pool — the wall clock is round trips, and eight in flight is
    roughly eight times less waiting without stressing the link.
    """
    from concurrent.futures import ThreadPoolExecutor

    # cache_path has NO default on purpose. It used to, and the one caller that
    # mattered forgot to pass it — so the cache never existed and every scan
    # re-read every folder's tags off the network mount for half an hour. A
    # parameter whose absence costs that much should not be silently omittable;
    # pass "" to mean "really, no cache".
    fc = FolderCache(cache_path) if cache_path else None
    if fc is None:
        log("[label] no folder cache — every folder will be re-read")
    out = []
    for r in roots:
        path, role = r.get("path") or "", (r.get("role") or "owned")
        if not path or not os.path.isdir(path):
            log(f"[label] root missing, skipped: {path or '(blank)'}")
            continue
        log(f"[label] {role}: listing {path} …")
        folders = release_folders(path)
        log(f"[label] {role}: {len(folders)} release folders under {path}")
        done = 0
        read = (lambda d: fc.get(d)) if fc else folder_tracks
        # Work in bounded chunks rather than one pool.map over everything.
        # map() submits every task up front and leaving the `with` block calls
        # shutdown(wait=True), so pressing Stop had to wait for all 1,265 reads
        # to drain — minutes of "stopping…" during which the button looked
        # broken. A chunk is the longest Stop can now take, and it also caps how
        # many finished results sit in memory waiting to be consumed.
        CHUNK = 64
        stopped = False
        pool = ThreadPoolExecutor(max_workers=max(1, workers))
        try:
            for start in range(0, len(folders), CHUNK):
                if should_stop and should_stop():
                    stopped = True
                    break
                batch = folders[start:start + CHUNK]
                for d, info in zip(batch, pool.map(read, batch)):
                    name = os.path.basename(d)
                    out.append({
                        "path": d, "name": name, "root": path, "role": role,
                        "catno": folder_catno(name), "title": folder_title(name),
                        **info,
                    })
                    done += 1
                    if done % 100 == 0 or done == len(folders):
                        log(f"[label]   … {done}/{len(folders)}")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if stopped:
            log("[label] stopped after %d folders" % done)
            if fc:
                fc.save()          # keep what was read; it is all still valid
            return out
    if fc:
        log(f"[label] folder cache: {fc.hits} unchanged, {fc.misses} read")
        fc.save()
    return out


def _catalogue_index(catalogue: list) -> dict:
    """catno key -> catalogue row.  One release answers to several keys."""
    idx = {}
    for r in catalogue:
        for k in catno_keys(r.get("catno")):
            idx.setdefault(k, r)
    return idx


def attach_folders(catalogue: list, folders: list, folder_overrides: dict | None = None) -> tuple:
    """Decide, for every folder, which catalogue release it is.

    Returns (by_release, orphans) where by_release maps the release's identity
    key to the folders claiming it, and orphans are folders that matched nothing
    in this label's catalogue.

    `folder_overrides` ({path: release_id}) is checked FIRST, ahead of every
    other tier — it exists for exactly the folder this function's own tiers
    cannot place: search_orphans() found and confirmed the release by name,
    sometimes one already sitting in the catalogue under a title-wording
    that never would have cleared title_match()'s bar. An explicit answer
    for THIS folder outranks every generic rule this function has.
    """
    idx = _catalogue_index(catalogue)
    id_idx = {r["id"]: r for r in catalogue if r.get("id")}
    by_release, orphans = {}, []
    for f in folders:
        row = None
        override_id = (folder_overrides or {}).get(f.get("path"))
        if override_id is not None and override_id in id_idx:
            row = id_idx[override_id]
            f["matched_by"] = "override"
        keys = catno_keys(f["catno"]) if row is None and f["catno"] else set()
        for k in keys:
            if k in idx:
                row = idx[k]
                f["matched_by"] = "catno"
                break
        if row is None and f["title"]:
            # No catno on the folder (his maxi singles have none) — fall back to
            # the title, but only with the number guard, never title alone.
            for r in catalogue:
                if title_match(r.get("title", ""), f["title"]):
                    row = r
                    f["matched_by"] = "title"
                    break
        if row is None:
            # A DIFFERENT archive's naming convention: "(YEAR) Artist - Title
            # FORMAT" rather than this one's "(CATNO) Title (Year)". Without
            # this, folder_catno() reads the year as a catalogue number (it
            # matches nothing real, correctly), and the title-only fallback
            # above compares "Artist - Title" against the catalogue's BARE
            # title field, which rarely scores high enough — 264 of 272
            # folders from one such share went unmatched before this tier
            # existed, on files with no embedded tags to fall back to either.
            alt = year_led_artist_title(f.get("name") or "")
            if alt is not None:
                alt_artist, alt_title = alt
                for r in catalogue:
                    if not title_match(r.get("title", ""), alt_title):
                        continue
                    cat_artist = (r.get("artist") or "").strip().lower()
                    if alt_artist and cat_artist not in ("", "various", "various artists",
                                                         "va", "v/a"):
                        if fuzzy(cat_artist, alt_artist.lower()) < 0.55:
                            continue
                    row = r
                    f["matched_by"] = "year-led-title"
                    break
        if row is None:
            orphans.append(f)
            continue
        key = release_key(row)
        by_release.setdefault(key, []).append(f)
    return by_release, orphans


def release_key(row: dict) -> str:
    """A stable identity for a catalogue row: its first catno key, else its title."""
    ks = sorted(catno_keys(row.get("catno")))
    return ks[0] if ks else "t:" + norm(row.get("title", ""))


def classify_rarity(price: dict | None) -> str:
    """"cheap and common" vs "rare — long hunt", from cached release_price()
    data. "" when nothing has been fetched for this release yet — never
    guessed, never defaulted to either bucket.

    num_for_sale is the primary signal, not lowest_price: a copy sitting at
    any price is a copy you can actually go buy today, while a price alone
    says nothing about whether one is available RIGHT NOW. `have` backs it
    up for the edge case num_for_sale can't see — a release with plenty of
    copies logged as owned but none currently listed is still a common
    record having a quiet week, not a rare one.
    """
    if not price:
        return ""
    n = price.get("num_for_sale")
    have = price.get("have")
    if n is None and have is None:
        return ""
    if (n or 0) >= 2 or (have or 0) >= 15:
        return "cheap and common"
    return "rare — long hunt"


def tracklist_for(row: dict, tls: dict) -> list:
    """The expected tracklist for a catalogue row, from cache.  [] when unknown."""
    for k in sorted(catno_keys(row.get("catno"))):
        got = tls.get(k)
        if got:
            return list(got.get("tracks") or []) if isinstance(got, dict) else list(got)
    return []


def price_for(row: dict, prices: dict) -> dict | None:
    """The cached release_price() data for a catalogue row. None when
    unknown — same lookup-by-every-catno-key shape as tracklist_for()."""
    for k in sorted(catno_keys(row.get("catno"))):
        got = prices.get(k)
        if got:
            return got
    return None


def assess(catalogue: list, folders: list, tls: dict, overrides=None,
          folder_overrides=None) -> dict:
    """The whole picture: one row per catalogue release, plus the orphan folders.

    Completeness is computed WITHIN A SINGLE FOLDER, never across the library.
    Counting scattered track matches anywhere under a root is what made 146 Bit
    Music compilations read as owned when no folder held them — they were never
    hunted for again.

    `overrides` and `folder_overrides` are unrelated despite the name: the
    first is catnos the owner has declared finished regardless of track
    count; the second is {folder path: release id}, search_orphans()'
    per-folder answer for a name attach_folders()'s own tiers can't place.
    """
    overrides = {ncat(o) for o in (overrides or [])}
    by_release, orphans = attach_folders(catalogue, folders, folder_overrides)

    # One row per RELEASE, not per catalogue entry.  Discogs lists every
    # pressing separately — the CD and the 2xCD reissue of "Bit Music: 10 Años
    # De Exitos" are two entries sharing one catalogue number — and emitting a
    # row each showed the same release twice, with identical counts, as though
    # we were missing it twice over.  Keep the entry with the fullest tracklist,
    # since that is the one completeness should be judged against, and record
    # how many pressings collapsed into it.
    merged, pressings = {}, {}
    for r in catalogue:
        key = release_key(r)
        pressings[key] = pressings.get(key, 0) + 1
        cur = merged.get(key)
        if cur is None or len(tracklist_for(r, tls)) > len(tracklist_for(cur, tls)):
            merged[key] = r

    rows = []
    for key, r in merged.items():
        mine = by_release.get(key, [])
        expected = tracklist_for(r, tls)
        owned = [f for f in mine if f["role"] == "owned"]
        incoming = [f for f in mine if f["role"] == "incoming"]

        def cover(f):
            if not expected:
                # No tracklist known: fall back to "does it hold any lossless
                # audio", which is honest about being a weaker answer.
                return f["lossless"], max(f["lossless"], 0)
            return len(match_tracks(expected, f["ids"])), len(expected)

        best_owned, best_folder = (0, len(expected)), None
        for f in owned:
            got, exp = cover(f)
            if got > best_owned[0]:
                best_owned, best_folder = (got, exp), f
        best_in, best_in_folder = (0, len(expected)), None
        for f in incoming:
            got, exp = cover(f)
            if got > best_in[0]:
                best_in, best_in_folder = (got, exp), f

        # A folder that answers for NO track never wins the "best" slot, because
        # nothing beats zero. The row then pointed at no folder at all and lost
        # its format flags — so a release held only as MP3s showed as merely
        # incomplete, with nothing to say WHY, which is the one case where the
        # reason is the whole answer. Fall back to the first folder we have.
        if best_folder is None and owned:
            best_folder = owned[0]
        if best_in_folder is None and incoming:
            best_in_folder = incoming[0]

        have, exp_n = best_owned
        overridden = bool(catno_keys(r.get("catno")) & overrides)

        if overridden:
            status = "complete"
        elif not owned:
            status = "missing"
        elif not expected:
            status = "held"                 # we have a folder, tracklist unknown
        elif have >= exp_n:
            status = "complete"
        else:
            status = "partial"

        missing_titles = []
        if expected and status == "partial":
            if best_folder:
                got = match_tracks(expected, best_folder["ids"])
                missing_titles = [t for i, t in enumerate(expected)
                                  if i not in got]
            else:
                # We hold a folder for it, but not one track in it answers to
                # the tracklist. Everything is outstanding, and saying nothing
                # is outstanding would be the worst possible answer.
                missing_titles = list(expected)
        elif expected and status == "missing":
            missing_titles = list(expected)

        # What an incoming folder would do for us.
        verdict = ""
        if incoming:
            if not owned:
                verdict = "new"
            elif best_in[0] > have:
                verdict = "upgrade"
            else:
                verdict = "duplicate"

        rows.append({
            "catno": r.get("catno", ""), "title": r.get("title", ""),
            "artist": r.get("artist", ""), "year": r.get("year", ""),
            "id": r.get("id"), "key": key, "pressings": pressings.get(key, 1),
            "status": status, "have": have, "expected": exp_n,
            "known_tracklist": bool(expected),
            "override": overridden,
            "folder": best_folder["path"] if best_folder else "",
            "folder_name": best_folder["name"] if best_folder else "",
            "needs_convert": best_folder["needs_convert"] if best_folder else 0,
            "lossy_only": bool(best_folder and best_folder["lossless"] == 0
                               and best_folder["lossy"] > 0),
            "incoming": [f["path"] for f in incoming],
            "incoming_have": best_in[0],
            "incoming_folder": best_in_folder["path"] if best_in_folder else "",
            "verdict": verdict,
            "missing_tracks": missing_titles,
        })

    return {"rows": rows, "orphans": orphans}


def summarise(rows: list) -> dict:
    s = {"total": len(rows), "complete": 0, "partial": 0, "missing": 0,
         "held": 0, "held_fake": 0, "tracks_missing": 0, "needs_convert": 0,
         "new": 0, "upgrade": 0, "duplicate": 0, "genuine_complete": 0}
    for r in rows:
        s[r["status"]] = s.get(r["status"], 0) + 1
        s["tracks_missing"] += len(r["missing_tracks"])
        if r["needs_convert"]:
            s["needs_convert"] += 1
        if r["verdict"]:
            s[r["verdict"]] = s.get(r["verdict"], 0) + 1
        # A row only counts as genuinely complete once it has actually been
        # spectrally checked — a "complete" release nobody has sampled yet does
        # NOT count, so the gap between complete and genuine_complete is a
        # visible measure of how much of the archive still needs the
        # authenticity backfill (label_panel.authenticity_backfill).
        a = r.get("authenticity") or {}
        if r["status"] == "complete" and a.get("checked") and not a.get("condemned"):
            s["genuine_complete"] += 1
    return s


# ─── series / volume grouping ──────────────────────────────────────────────────
# This catalogue is built on numbered series — Limite Vol I-XIII, Top 98/99/
# 2000, 100% Numeros Uno. "2,480 unrelated rows" is not how a collector sees a
# gap: "Limite — 9 of 13 volumes, missing 3, 7, 11" is. Pure aggregation over
# rows assess() already produced — no new I/O, no new Discogs calls.

# Ported from label2lossless's label_sorter.py: a fixed lookup, not a general
# roman-numeral parser, on purpose — a real release title never uses a roman
# numeral above what a collector would recognise on sight, and a bad/unusual
# roman ("Vol. VIV") should be left alone rather than guessed at.
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
         "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15,
         "xvi": 16, "xvii": 17, "xviii": 18, "xix": 19, "xx": 20}
_SERIES_ROMAN_RE = re.compile(r"\b(vol\.?|volume|parte?|cd)\s*\.?\s*([ivx]+)\b",
                              re.IGNORECASE)


def _normalise_series_number(title: str) -> str:
    """'Vol.II' -> 'Vol. 2' — a roman numeral is just another way of writing
    the volume number, and grouping must not treat "Vol. II" and "Vol. 2" as
    two different series."""
    def repl(m):
        try:
            n = _ROMAN[m.group(2).lower()]
        except KeyError:
            return m.group(0)
        return f"{m.group(1)} {n}"
    return _SERIES_ROMAN_RE.sub(repl, title or "")


def _series_stem(title: str) -> str:
    """The title with every number removed and the punctuation that leaves
    behind tidied up — "Dance Collection 2003 - 2007 - Vol.2" strips to
    "Dance Collection  -  - Vol." before this, which is a legible label for
    nobody. Collapse the dash-shaped gaps a stripped number leaves, then
    collapse whitespace, then trim leftover edge punctuation.
    """
    stem = _NUM.sub("", title)
    stem = re.sub(r"\s*-\s*", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" -.")
    return stem


def series_key(row: dict) -> tuple[str, int] | None:
    """(series identity, this release's number in it), or None when the
    title doesn't look like part of a numbered series. Conservative on
    purpose: requires a real number AND at least a few characters of shared
    title left over after stripping it — a bare number match between two
    otherwise-unrelated titles is not a series, it's a coincidence.
    """
    title = _normalise_series_number(row.get("title", ""))
    nums = _NUM.findall(title)
    if not nums:
        return None
    stem = _series_stem(title)
    if len(norm(stem)) < 3:
        return None
    # The LAST number in the title is the volume/edition number — a leading
    # year range ("Dance Collection 2003 - 2007 - Vol.2") is context, not the
    # series index.
    return norm(stem), int(nums[-1])


def group_series(rows: list) -> list:
    """Bucket rows into numbered series.  Returns
    [{"series": display name, "have": [numbers], "missing": [numbers],
      "total": int}, ...], sorted by series name.  A release counts as
    "have" the moment it is anything other than status "missing" — this is
    about whether you hold the release AT ALL, not how complete it is; a
    partial/held release's own completeness is already shown elsewhere.
    Singletons (a "series" of one release) are dropped — that isn't a gap a
    collector needs pointed out.
    """
    groups: dict = {}
    for r in rows:
        sk = series_key(r)
        if sk is None:
            continue
        key, n = sk
        title = _normalise_series_number(r.get("title", ""))
        stem = _series_stem(title)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"series": stem, "numbers": {}}
        elif len(stem) < len(g["series"]):
            g["series"] = stem  # prefer the shortest/cleanest label seen
        g["numbers"][n] = r["status"] != "missing"

    out = []
    for g in groups.values():
        nums = sorted(g["numbers"])
        if len(nums) < 2:
            continue
        out.append({
            "series": g["series"],
            "have": [n for n in nums if g["numbers"][n]],
            "missing": [n for n in nums if not g["numbers"][n]],
            "total": len(nums),
        })
    out.sort(key=lambda g: g["series"].lower())
    return out


# ─── Discogs cross-check (report-only) ─────────────────────────────────────────
# Naming/tagging/artwork vs the Discogs catalogue. Read-only in this batch: it
# only ever REPORTS what disagrees, it never renames a folder or writes a tag.
# cross_check()'s 'action' is always "none" here — a stub the UI can render
# disabled — because any action that writes to files must be opt-in per row,
# same rule move_folders() already follows.

def canonical_name(row: dict) -> str:
    """The '(catno) Title (Year)' string a folder SHOULD be named."""
    name = f"({row.get('catno', '')}) {row.get('title', '')}"
    if row.get("year"):
        name += f" ({row['year']})"
    return name


def safe_folder_name(name: str) -> str:
    """canonical_name()'s output, made safe to actually create on disk.

    A Discogs title routinely carries "?", ":" or "/" — a remix credit
    alone can hold two of them — and every one of those is illegal in a
    Windows filename. canonical_name() itself stays a pure display/compare
    string (naming_check() diffs it against a folder's ACTUAL name, where
    sanitising would just make real mismatches invisible); this is the
    separate step for the one caller that renames something for real.
    """
    cleaned = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name)
    return cleaned.strip(" .") or "untitled"


def naming_check(folder_name: str, row: dict) -> dict:
    """Compare a folder's actual name against the canonical one — fuzzy, not
    brittle exact-string, since real names carry trailing noise ("- WAV",
    pressing notes) that is not a naming PROBLEM worth flagging. Reuses the
    same catno/title parsing and title_match() bar (0.86) used everywhere else
    a folder name is read, rather than inventing a second way to compare.
    """
    actual = folder_name or ""
    matches = False
    if actual:
        actual_catno = folder_catno(actual)
        if actual_catno and row.get("catno"):
            matches = bool(catno_keys(actual_catno) & catno_keys(row["catno"]))
        if not matches:
            matches = title_match(folder_title(actual), row.get("title", ""))
    return {"expected": canonical_name(row), "actual": actual, "matches": matches}


def tag_check(folder_tags: list, expected: list, row: dict, matched: dict) -> dict:
    """Tag presence/contradiction vs the Discogs tracklist, for tracks that
    match_tracks() already assigned to a specific file — this never re-derives
    its own idea of which file is which track, so cross-check can never
    disagree with completeness about matching.

    `folder_tags` is folder_tracks()'s 'tags' list (parallel-indexed to the
    file iteration that produced 'ids'); `matched` is match_tracks()'s
    {track_index: file_index}.  Comparisons use fuzzy()'s 0.86 bar, not
    exact-string, so tag casing/punctuation differences are not reported as
    contradictions.
    """
    missing_artist, missing_tracknumber = [], []
    contradicts_artist, contradicts_catno = [], []
    checked = 0
    row_artist = (row.get("artist") or "").strip()
    row_catno = row.get("catno") or ""
    various = row_artist.lower() in ("", "various", "various artists", "va", "v/a")
    for ti, track_title in enumerate(expected):
        fi = matched.get(ti)
        if fi is None or fi < 0 or fi >= len(folder_tags):
            continue
        tags = folder_tags[fi] or {}
        checked += 1
        if not various:
            file_artist = (tags.get("artist") or "").strip()
            if not file_artist:
                missing_artist.append(track_title)
            elif fuzzy(file_artist, row_artist) < 0.86:
                contradicts_artist.append({"track": track_title, "tag": file_artist,
                                           "catalogue": row_artist})
        if not (tags.get("tracknumber") or "").strip():
            missing_tracknumber.append(track_title)
        file_catno = (tags.get("catalognumber") or "").strip()
        if row_catno and file_catno and not (catno_keys(file_catno) & catno_keys(row_catno)):
            contradicts_catno.append({"track": track_title, "tag": file_catno,
                                      "catalogue": row_catno})
    return {"missing_artist": missing_artist, "missing_tracknumber": missing_tracknumber,
            "contradicts_artist": contradicts_artist, "contradicts_catno": contradicts_catno,
            "checked_tracks": checked}


def artwork_check(folder_info: dict) -> dict:
    """Presence, source, and the 0x0-dimension bug — read-only, report-only.
    The Discogs image fetch/compare half stays deferred: it is a genuinely
    new API surface, out of scope for report-only cross-check.
    """
    tags = folder_info.get("tags") or []
    present = bool(folder_info.get("has_artwork"))
    embedded_tags = [t for t in tags if t.get("has_picture")]
    source = "embedded" if embedded_tags else ("folder-image" if present else "none")
    # A broken tagger writes the real image correctly but leaves its
    # declared width/height at zero — hit 1,044 of 2,012 files in this
    # archive. dimension_ok is only meaningful when there's an embedded
    # picture to have dimensions at all; True (nothing to flag) otherwise.
    dimension_ok = True
    for t in embedded_tags:
        w, h = t.get("picture_width"), t.get("picture_height")
        if w is not None and h is not None and (w <= 0 or h <= 0):
            dimension_ok = False
            break
    return {
        "present": present, "source": source, "dimension_ok": dimension_ok,
        "deferred": [
            "Discogs image PIXEL compare (does the art actually match the "
            "release) — label_panel.get_artwork() can now FETCH an image "
            "when none is present, but comparing one already here against "
            "Discogs' own stays out of report-only cross-check's scope",
        ],
    }


def extract_local_picture(path: str) -> tuple[bytes, str] | None:
    """The full bytes of one file's embedded picture, if it has one.

    Deliberately NOT the fast scan path: _flac_tags() above seeks OVER a
    PICTURE block's image bytes on purpose (the 5.5 GB incident in that
    function's own docstring), because a bulk scan reads every file in a
    folder. This reads exactly one file, in full, only when a human has
    already clicked something — the cost this avoids during scan is fine to
    pay once, opt-in.

    Covers FLAC's own picture blocks and the APIC frame formats (MP3, and
    WAV via the RIFF 'id3 ' chunk tag_writer's WAVE fix already knows how to
    open) — whatever mutagen.File() can actually open. None on anything
    else, or on a file with no embedded picture at all.
    """
    try:
        import mutagen
        f = mutagen.File(path)
    except Exception:
        return None
    if f is None:
        return None
    pics = getattr(f, "pictures", None)
    if pics:
        return pics[0].data, (pics[0].mime or "image/jpeg")
    tags = getattr(f, "tags", None)
    if tags is not None and hasattr(tags, "getall"):
        apics = tags.getall("APIC")
        if apics:
            return apics[0].data, (apics[0].mime or "image/jpeg")
    return None


def download_image(url: str, timeout: float = 30.0) -> tuple[bytes, str] | None:
    """Plain HTTP GET for an image URL — Discogs' own CDN, not the API, so
    no token and no rate pacing needed. None on any failure; the caller
    treats "no artwork found" and "network hiccup" the same way (nothing to
    write), rather than surfacing a distinction the UI has no use for."""
    req = urllib.request.Request(url, headers={"User-Agent": Discogs.UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), (r.headers.get("Content-Type") or "image/jpeg")
    except Exception:
        return None


def cross_check(row: dict, folder_info: dict, tracklist: list, matched: dict) -> dict:
    """Combine naming/tag/artwork checks for one release.  Always returns
    action: "none" in this batch — see the module-level note above."""
    naming = naming_check(row.get("folder_name", ""), row)
    tags = tag_check(folder_info.get("tags") or [], tracklist, row, matched)
    artwork = artwork_check(folder_info)
    return {"naming": naming, "tags": tags, "artwork": artwork, "action": "none"}


def match_against_catalogues(folder: dict, catalogues: dict) -> dict:
    """Which of several labels' catalogues this ONE folder matches, and how.

    `catalogues` is {label_id: catalogue_rows}. Reuses attach_folders()'s own
    matching rules (catno first, then title WITH the number guard) so a
    cross-label match can never disagree with what a same-label scan would
    have found for the same folder. Returns {label_id: {"row": ..., "matched_by":
    "catno"|"title"}} — usually zero or one entry, but a folder with an
    ambiguous catalogue number could legitimately match more than one label.
    """
    out = {}
    keys = catno_keys(folder.get("catno")) if folder.get("catno") else set()
    for label_id, catalogue in catalogues.items():
        idx = _catalogue_index(catalogue)
        row, matched_by = None, None
        for k in keys:
            if k in idx:
                row, matched_by = idx[k], "catno"
                break
        if row is None and folder.get("title"):
            for r in catalogue:
                if title_match(r.get("title", ""), folder["title"]):
                    row, matched_by = r, "title"
                    break
        if row is not None:
            out[label_id] = {"row": row, "matched_by": matched_by}
    return out
