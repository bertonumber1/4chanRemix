#!/usr/bin/env python3
"""End-to-end tests for the part of the Labels tab that TOUCHES FILES.

    python3 tests/label_panel_test.py

`label_panel` moves music. Until this existed, not one line of that path had
ever executed — the move log did not exist on disk. Everything here runs against
throwaway folders in a temp dir; the real archive is never named, never opened,
and the module's own state/cache/log paths are redirected before it is used.
"""
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import label_panel as P                                      # noqa: E402

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


def touch(path, name, size=16):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, name), "wb") as fh:
        fh.write(b"\0" * size)


def release(root, name, tracks=3):
    d = os.path.join(root, name)
    for i in range(1, tracks + 1):
        touch(d, "%02d - track.flac" % i)
    return d


def main():
    tmp = tempfile.mkdtemp(prefix="label-panel-test-")
    try:
        # Redirect every path the module writes to. Doing this BEFORE any call
        # is the whole reason this test is safe to run on the live machine.
        P.STATE_PATH = os.path.join(tmp, "label_state.json")
        P.CACHE_DIR = os.path.join(tmp, "label-cache")
        P.MOVELOG = os.path.join(tmp, "label_moves.tsv")

        incoming = os.path.join(tmp, "incoming")
        archive = os.path.join(tmp, "archive")
        outside = os.path.join(tmp, "somewhere-else")
        os.makedirs(archive)
        rel = release(incoming, "(11-111) A Release (1999)")
        stray = release(outside, "(99-999) Not In Any Root (2000)")

        print("[state]")
        ok, msg = P.add_root(incoming, "incoming")
        check(ok, "a root can be added", msg)
        ok, msg = P.set_archive(archive)
        check(ok, "an archive can be set", msg)
        ok, msg = P.add_root(os.path.join(tmp, "does-not-exist"), "owned")
        check(not ok, "a missing folder is refused, not stored", msg)

        print("\n[refusals — each of these would lose or wreck files]")
        # An unauthenticated LAN endpoint that moves ANY directory is not a
        # feature. Only folders under a configured root may be moved.
        r = P.move_folders([stray])
        eq(r["results"][0]["ok"], False, "a folder outside every root is refused")
        check("outside every configured folder" in r["results"][0]["reason"],
              "and says why", r["results"][0]["reason"])
        check(os.path.isdir(stray), "and it is still where it was")

        r = P.move_folders([os.path.join(incoming, "no-such-folder")])
        eq(r["results"][0]["reason"], "source folder is gone",
           "a vanished folder is reported, not crashed on")

        link = os.path.join(incoming, "(22-222) A Link (2000)")
        os.symlink(rel, link)
        r = P.move_folders([link])
        eq(r["results"][0]["ok"], False, "a symlinked source is refused")
        os.unlink(link)

        # The archive living inside the folder being moved would move a tree
        # into itself.
        nested_archive = os.path.join(rel, "archive-inside")
        os.makedirs(nested_archive)
        r = P.move_folders([rel], dest_dir=nested_archive)
        eq(r["results"][0]["ok"], False, "moving a folder into itself is refused")
        check("destroy" in r["results"][0]["reason"], "and says so plainly",
              r["results"][0]["reason"])
        shutil.rmtree(nested_archive)

        print("\n[dry run]")
        r = P.move_folders([rel], dry_run=True)
        check(r["results"][0]["ok"], "a dry run reports success")
        check("would move" in r["results"][0]["reason"], "and says it would move",
              r["results"][0]["reason"])
        check(os.path.isdir(rel), "and moves NOTHING")
        check(not os.path.exists(P.MOVELOG), "and writes nothing to the move log")

        print("\n[the real move]")
        r = P.move_folders([rel])
        row = r["results"][0]
        check(row["ok"], "the move succeeds", row["reason"])
        eq(row["files"], 3, "and counts the audio files that arrived")
        check(not os.path.exists(rel), "the source is gone")
        dest = row["dest"]
        check(os.path.isdir(dest), "the destination exists")
        eq(sorted(os.listdir(dest)),
           ["01 - track.flac", "02 - track.flac", "03 - track.flac"],
           "with every file intact")

        print("\n[the move log and undo]")
        log = P.move_log()
        eq(len(log), 1, "the move is logged")
        eq(log[0]["action"], "move", "as a move")
        check(log[0]["undoable"], "and is marked undoable")
        ok, msg = P.undo_move(dest)
        check(ok, "undo puts it back", msg)
        check(os.path.isdir(rel), "the folder is at its ORIGINAL path again")
        eq(sorted(os.listdir(rel)),
           ["01 - track.flac", "02 - track.flac", "03 - track.flac"],
           "with every file still intact")
        check(not os.path.exists(dest), "and is no longer in the archive")
        eq(len(P.move_log()), 2, "the undo is logged too")
        ok, msg = P.undo_move(dest)
        check(not ok, "undoing twice is refused, not repeated", msg)

        print("\n[name collisions]")
        # Two different releases can legitimately share a folder name; the
        # second must not overwrite the first.
        a = release(incoming, "(33-333) Same Name (2001)", tracks=2)
        P.move_folders([a])
        b = release(incoming, "(33-333) Same Name (2001)", tracks=5)
        r = P.move_folders([b])
        check(r["results"][0]["ok"], "a same-named folder still moves")
        check(r["results"][0]["dest"].endswith("(2)"),
              "to a new name, not over the top", r["results"][0]["dest"])
        eq(len(os.listdir(os.path.join(archive, "(33-333) Same Name (2001)"))), 2,
           "the first folder is untouched")

        print("\n[per-folder reporting]")
        good = release(incoming, "(44-444) Fine (2002)")
        r = P.move_folders([good, stray, os.path.join(incoming, "ghost")])
        eq(len(r["results"]), 3, "one result per folder asked for")
        eq([x["ok"] for x in r["results"]], [True, False, False],
           "each judged on its own")
        eq(r["ok"], False, "and the batch is not called a success")

        print("\n[exports without a scan]")
        try:
            P.export("outstanding_tracks")
            check(False, "exporting before a scan raises, rather than writing an empty file")
        except Exception:
            check(True, "exporting before a scan raises, rather than writing an empty file")

        print("\n[exports with a scan]")
        os.makedirs(os.path.dirname(P._scan_path(P.load_state()["label_id"])), exist_ok=True)
        scan = {"when": 0, "label_id": 1, "label_name": "Test Label", "roots": [],
                "orphans": [], "summary": {},
                "rows": [
                    {"catno": "11-111", "title": "Held Whole", "artist": "A",
                     "year": 1999, "id": 1, "key": "11111", "status": "complete",
                     "have": 2, "expected": 2, "known_tracklist": True,
                     "override": False, "folder": "/x", "folder_name": "x",
                     "needs_convert": 0, "lossy_only": False, "incoming": [],
                     "incoming_have": 0, "incoming_folder": "", "verdict": "",
                     "missing_tracks": [], "pressings": 1},
                    {"catno": "22-222", "title": "Half Held", "artist": "B",
                     "year": 2000, "id": 2, "key": "22222", "status": "partial",
                     "have": 1, "expected": 2, "known_tracklist": True,
                     "override": False, "folder": "/y", "folder_name": "y",
                     "needs_convert": 1, "lossy_only": False, "incoming": [],
                     "incoming_have": 0, "incoming_folder": "", "verdict": "",
                     "missing_tracks": ["Lost Track"], "pressings": 1},
                    {"catno": "33-333", "title": "Comp", "artist": "Various",
                     "year": 2001, "id": 3, "key": "33333", "status": "partial",
                     "have": 1, "expected": 2, "known_tracklist": True,
                     "override": False, "folder": "/z", "folder_name": "z",
                     "needs_convert": 0, "lossy_only": False, "incoming": [],
                     "incoming_have": 0, "incoming_folder": "", "verdict": "",
                     "missing_tracks": ["Comp Track"], "pressings": 1},
                    {"catno": "44-444", "title": "Never Had It", "artist": "C",
                     "year": 2002, "id": 4, "key": "44444", "status": "missing",
                     "have": 0, "expected": 3, "known_tracklist": True,
                     "override": False, "folder": "", "folder_name": "",
                     "needs_convert": 0, "lossy_only": False, "incoming": [],
                     "incoming_have": 0, "incoming_folder": "", "verdict": "",
                     "missing_tracks": ["T1", "T2", "T3"], "pressings": 1},
                ]}
        with open(P._scan_path(P.load_state()["label_id"]), "w", encoding="utf-8") as fh:
            json.dump(scan, fh)

        for kind in ("outstanding_tracks", "missing_releases", "partial",
                     "search_terms", "have", "orphans"):
            name, text = P.export(kind)
            check(name.endswith((".csv", ".txt")), "export %s has a filename" % kind, name)
            check(isinstance(text, str), "export %s returns text" % kind)

        name, text = P.export("outstanding_tracks")
        check("Lost Track" in text and "T1" in text,
              "outstanding tracks lists both partial gaps and missing releases")
        check("Held Whole" not in text, "and does not list what we hold whole")

        name, text = P.export("missing_releases")
        check("44-444" in text and "11-111" not in text,
              "missing releases lists only what is missing")

        name, text = P.export("search_terms")
        check("C Never Had It" in text, "a named artist gets artist + title")
        check("Comp Track" in text and "Various Comp Track" not in text,
              "a Various Artists placeholder is never used as a search term")

        print("\n[path comparison is platform-correct]")
        # Windows is the primary target. There "D:\\Music" and "d:/music" are one
        # folder; on Linux "Music" and "music" are two. os.path.normcase draws
        # exactly that line, so these assertions read differently per platform
        # ON PURPOSE.
        import os.path as _op
        same_on_windows = (os.name == "nt")
        before = len(P.load_state()["roots"])
        P.add_root(incoming.upper() if same_on_windows else incoming, "incoming")
        after = len(P.load_state()["roots"])
        eq(after, before, "re-adding the same folder does not duplicate it")
        check(_op.normcase("/A/B") == _op.normcase("/a/b") if same_on_windows
              else _op.normcase("/A/B") != _op.normcase("/a/b"),
              "case folding matches the platform")
        # Whatever the platform, a folder under a root must be recognised as
        # under it — this is what gates every move.
        check(P._under(os.path.join(incoming, "x", "y"), incoming),
              "a nested path is recognised as inside its root")
        check(not P._under(outside, incoming),
              "and an unrelated path is not")

        print("\n[stale results]")
        # Everything shown, and every export, is read from the LAST scan.
        # Changing the setup without rescanning must say so rather than keep
        # presenting numbers for a configuration that no longer exists.
        st = P.load_state()
        fresh = dict(scan)
        fresh["roots"] = st["roots"]
        fresh["label_id"] = st["label_id"]
        with open(P._scan_path(st["label_id"]), "w", encoding="utf-8") as fh:
            json.dump(fresh, fh)
        eq(P.status({})["scan"]["stale"], "",
           "a scan matching the current setup is not stale")

        another = os.path.join(tmp, "another")
        os.makedirs(another, exist_ok=True)
        P.add_root(another, "owned")
        check("folders have changed" in (P.status({})["scan"]["stale"] or ""),
              "adding a folder marks the results stale",
              P.status({})["scan"]["stale"])

        P.remove_root(another)
        eq(P.status({})["scan"]["stale"], "", "removing it again clears the warning")

        # Per-label scan storage (Batch 2): switching to a label that has
        # never been scanned shows NOTHING, not the previous label's stale
        # numbers relabelled — the old single shared last_scan.json used to
        # leak exactly that, mixing one label's results into another's view.
        P.set_label(99999, "Someone Else")
        eq(P.status({})["scan"], None,
           "switching to an unscanned label shows nothing scanned, never "
           "another label's stale results")

        P.set_label(fresh["label_id"], "Test Label")
        eq(P.status({})["scan"]["stale"], "",
           "switching back restores that label's own scan, untouched")

        print("\n[status]")
        s = P.status({})
        eq(s["archive"], archive, "status reports the archive")
        check(s["moves"] > 0, "and how many moves have happened")

        print("\n[fetch_tracklists time budget]")
        # A Discogs stand-in that would fail the test if fetch_tracklists ever
        # reaches it after the time budget/should_stop check should have exited
        # first — proves the early-exit path, not just that it returns quickly.
        class _NeverCalled:
            def __init__(self, token, log):
                pass
            def tracklist(self, release_id):
                raise AssertionError("tracklist() must not be called once the "
                                     "time budget/should_stop check has fired")

        class _Fake:
            def __init__(self, token, log):
                self.log = log
            def tracklist(self, release_id):
                return ["Track %d" % release_id]

        P.set_label(10663, "Test Label")
        c = P.cache()
        c.save_catalogue([{"id": 1, "catno": "TB-001", "title": "One"},
                          {"id": 2, "catno": "TB-002", "title": "Two"}])
        c.save_tracklists({})

        real_discogs, real_time = P.L.Discogs, P.time.time
        try:
            # time_budget_s=0 and a clock that has already moved on by the time
            # the loop's first check runs: the budget check must fire before any
            # release is fetched.
            # only_missing_releases=False: this test's synthetic catalogue has
            # nothing to do with whatever last_scan.json earlier sections left
            # behind, so it must not be filtered by that staleness heuristic —
            # that filter is separately-existing behaviour, not what's under
            # test here.
            P.L.Discogs = _NeverCalled
            ticks = [1000.0, 1000.5]
            P.time.time = lambda: ticks.pop(0) if len(ticks) > 1 else ticks[0]
            out = P.fetch_tracklists({}, log=lambda m: None, time_budget_s=0,
                                     only_missing_releases=False)
            eq(out["added"], 0, "a spent time budget stops before the first fetch")

            P.time.time = real_time
            out = P.fetch_tracklists({}, log=lambda m: None,
                                     should_stop=lambda: True,
                                     only_missing_releases=False)
            eq(out["added"], 0, "should_stop is honoured before the first fetch too")

            # Now let it actually run: plenty of time budget, no stop signal.
            P.L.Discogs = _Fake
            out = P.fetch_tracklists({}, log=lambda m: None, time_budget_s=60,
                                     only_missing_releases=False)
            eq(out["added"], 2, "with budget and no stop signal, it completes normally")
            saved = c.tracklists()
            expected_keys = P.L.catno_keys("TB-001") | P.L.catno_keys("TB-002")
            check(expected_keys and expected_keys.issubset(saved),
                  "and the tracklists actually land in the cache",
                  "keys=%r saved=%r" % (expected_keys, sorted(saved)))
        finally:
            P.L.Discogs = real_discogs
            P.time.time = real_time

        print("\n[cross_check_release]")
        row3 = {"catno": "12-414", "title": "Esto es Makina", "artist": "Various",
               "folder_name": "(12-414) Esto es Makina (1997)"}
        tls3 = {k: {"id": 1, "title": "Esto es Makina",
                    "tracks": ["Track One", "Track Two"]}
               for k in P.L.catno_keys("12-414")}
        folder_info3 = {
            "ids": [(P.L.norm("Track One"), P.L.strip_mix("Track One"),
                    P.L.norm("01 - t"), P.L.strip_mix("01 - t")),
                   (P.L.norm("Track Two"), P.L.strip_mix("Track Two"),
                    P.L.norm("02 - t"), P.L.strip_mix("02 - t"))],
            "tags": [{"tracknumber": "1"}, {"tracknumber": "2"}],
            "has_artwork": False,
        }
        result = P.cross_check_release(row3, folder_info3, tls3)
        eq(row3["crosscheck"], result, "the result is attached to the row in place")
        check(result["naming"]["matches"],
              "naming check runs against the real tracklist-bearing row")
        eq(result["tags"]["checked_tracks"], 2, "both tracks matched and checked")
        eq(result["action"], "none", "no write action in this batch")

        no_folder = P.cross_check_release(dict(row3), None, tls3)
        eq(no_folder["tags"]["checked_tracks"], 0,
           "a missing folder is handled without crashing")

        print("\n[_apply_authenticity_status]")
        r = {"status": "complete", "authenticity": {"checked": True, "condemned": True}}
        P._apply_authenticity_status(r)
        eq(r["status"], "held_fake", "complete + condemned flips to held_fake")

        r2 = {"status": "complete", "authenticity": {"checked": True, "condemned": False}}
        P._apply_authenticity_status(r2)
        eq(r2["status"], "complete", "complete + genuinely clean stays complete")

        r3 = {"status": "partial", "authenticity": {"checked": True, "condemned": True}}
        P._apply_authenticity_status(r3)
        eq(r3["status"], "partial",
           "a not-yet-complete release is never relabelled held_fake — that bucket "
           "means 'held in full but fake', not 'incomplete and also fake'")

        r4 = {"status": "complete", "authenticity": {"checked": False}}
        P._apply_authenticity_status(r4)
        eq(r4["status"], "complete", "an unchecked release is never assumed fake")

        print("\n[authenticate_release]")
        import types as _types
        fake_cache = _types.SimpleNamespace(saved=False)
        fake_cache.save = lambda: setattr(fake_cache, "saved", True)
        fake_auth = _types.SimpleNamespace(
            available=lambda: True,
            AuthenticityCache=lambda cache_dir: fake_cache,
            authenticate_folder=lambda path, cache, log, should_stop:
                {"checked": True, "condemned": False, "sampled": 1, "total": 1,
                 "escalated": False, "worst_verdict": "clean", "confidence": 0.0,
                 "notes": "", "per_file": []},
        )
        real_import = P._import_label_authenticity
        try:
            P._import_label_authenticity = lambda: fake_auth
            row5 = {"catno": "12-414", "folder": os.path.join(tmp, "somewhere"), "status": "complete"}
            result5 = P.authenticate_release(row5, log=lambda m: None)
            eq(row5["authenticity"], result5, "the result is attached to the row in place")
            eq(result5["checked"], True, "and the (mocked) authenticator actually ran")
            check(fake_cache.saved, "the cache is saved after a check")

            row_no_folder = {"catno": "12-414", "status": "missing"}
            r_nf = P.authenticate_release(row_no_folder, log=lambda m: None)
            eq(r_nf["reason"], "no folder", "a row with no folder is never sent to the analyser")

            P._import_label_authenticity = lambda: (_ for _ in ()).throw(ImportError("nope"))
            r_broken = P.authenticate_release(dict(row5), log=lambda m: None)
            eq(r_broken["checked"], False,
               "a broken/missing SPEK-TRO dependency degrades to unchecked, never crashes")
            check("unavailable" in r_broken["reason"], "and says why", r_broken["reason"])
        finally:
            P._import_label_authenticity = real_import

        print("\n[authenticity_backfill]")
        backfill_scan = {
            "when": 0, "label_id": st["label_id"], "label_name": "Test Label", "roots": [],
            "orphans": [],
            "rows": [
                {"key": "a", "status": "complete", "folder": "/x/a", "catno": "A",
                 "missing_tracks": [], "needs_convert": 0, "verdict": ""},
                {"key": "b", "status": "partial", "folder": "/x/b", "catno": "B",
                 "missing_tracks": [], "needs_convert": 0, "verdict": ""},
                {"key": "c", "status": "missing", "folder": "", "catno": "C",
                 "missing_tracks": [], "needs_convert": 0, "verdict": ""},
                {"key": "d", "status": "complete", "folder": "/x/d", "catno": "D",
                 "missing_tracks": [], "needs_convert": 0, "verdict": "",
                 "authenticity": {"checked": True, "condemned": False}},
            ],
        }
        backfill_scan["summary"] = P.L.summarise(backfill_scan["rows"])
        with open(P._scan_path(st["label_id"]), "w", encoding="utf-8") as fh:
            json.dump(backfill_scan, fh)

        checked_paths = []
        def fake_authenticate_release(row, log=print, should_stop=None):
            checked_paths.append(row["folder"])
            result = {"checked": True, "condemned": row["folder"] == "/x/a",
                     "sampled": 1, "total": 1, "escalated": False,
                     "worst_verdict": "clean", "confidence": 0.0, "notes": ""}
            row["authenticity"] = result
            return result

        real_authenticate = P.authenticate_release
        try:
            P.authenticate_release = fake_authenticate_release
            out_bf = P.authenticity_backfill({}, log=lambda m: None)
        finally:
            P.authenticate_release = real_authenticate

        eq(sorted(checked_paths), ["/x/a", "/x/b"],
           "only owned, held-or-better, not-yet-checked rows are backfilled — "
           "row c has no folder, row d was already checked")
        eq(out_bf["checked"], 2, "and it reports how many it did")

        reloaded = json.loads(open(P._scan_path(st["label_id"]), encoding="utf-8").read())
        rows_by_key = {r["key"]: r for r in reloaded["rows"]}
        eq(rows_by_key["a"]["status"], "held_fake",
           "the condemned complete row is relabelled in the saved scan")
        eq(rows_by_key["b"]["status"], "partial",
           "a partial row's status is untouched even though it was checked")
        check(rows_by_key["a"]["authenticity"]["checked"] and rows_by_key["b"]["authenticity"]["checked"],
              "both checked rows have their authenticity result persisted")

        again = P.authenticity_backfill({}, log=lambda m: None)
        eq(again["checked"], 0, "a second run finds nothing left to check — every eligible row is now checked")

        print("\n[onboard_root]")
        P.set_label(20100, "Onboard Test Label")
        oc = P.cache()
        oc.save_catalogue([{"id": 501, "catno": "20-100", "title": "Test Release",
                            "artist": "DJ Test", "year": "2001"}])
        oc.save_tracklists({})            # unknown tracklist -> status "held"

        new_root = os.path.join(tmp, "onboard-owned")
        release(new_root, "(20-100) Test Release (2001)", tracks=2)

        calls = []
        def fake_authenticate(row, log=print, should_stop=None):
            calls.append(row["folder"])
            result = {"checked": True, "condemned": True, "sampled": 1, "total": 2,
                     "escalated": False, "worst_verdict": "lossy", "confidence": 0.9,
                     "notes": ""}
            row["authenticity"] = result
            return result

        real_authenticate = P.authenticate_release
        try:
            P.authenticate_release = fake_authenticate
            res = P.onboard_root({}, new_root, "owned", log=lambda m: None)
        finally:
            P.authenticate_release = real_authenticate

        check(res["ok"], "onboard_root succeeds against a real synthetic folder")
        eq(res["onboarded"], 1, "exactly one release was authenticity-checked")
        eq(len(calls), 1, "authenticate_release was called exactly once, for that release")

        row_out = next(r for r in res["scan"]["rows"] if r["catno"] == "20-100")
        eq(row_out["status"], "held",
           "an unknown-tracklist 'held' row stays held even when condemned — "
           "held_fake means COMPLETE and fake, not merely held and fake")
        check(row_out["authenticity"]["condemned"], "…but the authenticity result is still attached")
        check("crosscheck" in row_out, "cross-check ran too, as part of the same scan()")

        res_inc = P.onboard_root({}, new_root, "incoming", log=lambda m: None)
        eq(res_inc["onboarded"], 0, "an incoming folder gets no authenticity check at all")

        bad = P.onboard_root({}, os.path.join(tmp, "does-not-exist"), "owned", log=lambda m: None)
        eq(bad["ok"], False, "onboarding a nonexistent path is refused, not crashed on")

        print("\n[multi-label: tracking + overview]")
        before_ids = {t["id"] for t in P.load_state()["tracked_labels"]}
        check(30001 not in before_ids, "a fresh label id starts untracked")

        P.set_label(30001, "Series Test Label")
        tracked_ids = {t["id"] for t in P.load_state()["tracked_labels"]}
        check(20100 in tracked_ids, "switching away from a label auto-tracks it")
        check(30001 not in tracked_ids,
              "the label just switched TO is not itself tracked — it's the active one")

        c2 = P.cache()
        c2.save_catalogue([{"id": 601, "catno": "30-001", "title": "Solo Release"}])
        c2.save_tracklists({})
        new_root2 = os.path.join(tmp, "ml-owned")
        release(new_root2, "(30-001) Solo Release (2001)")
        P.onboard_root({}, new_root2, "owned", log=lambda m: None)

        ov = {o["label_id"]: o for o in P.overview()}
        check(30001 in ov, "the active label appears in the overview")
        check(20100 in ov, "a tracked (non-active) label appears too")
        eq(ov[30001]["active"], True, "the active label is flagged active")
        eq(ov[20100]["active"], False, "a tracked label is not flagged active")
        check(ov[30001]["summary"] is not None,
              "the active label's overview carries its own scan summary")
        check(ov[20100]["summary"] is not None,
              "a tracked label's overview reads ITS OWN cached scan — not the active label's")
        eq(ov[20100]["summary"]["total"], 1,
           "…and it really is that label's own earlier data, not a copy of the active one's")

        P.untrack_label(20100)
        ov2 = {o["label_id"]: o for o in P.overview()}
        check(20100 not in ov2, "untrack_label removes it from the overview")
        check(30001 in ov2, "the active label stays present regardless of tracking")

        print("\n[cross_label_check]")
        P.track_label(40001, "Other Label")
        P.L.LabelCache(P.CACHE_DIR, 40001).save_catalogue(
            [{"id": 701, "catno": "40-001", "title": "Foreign Release"}])

        incoming2 = os.path.join(tmp, "cross-label-incoming")
        release(incoming2, "(40-001) Foreign Release (2005)")
        P.add_root(incoming2, "incoming")

        res_cl = P.cross_label_check("incoming")
        matches = {r["label_id"]: r for r in res_cl["rows"]}
        check(40001 in matches,
              "an incoming folder matching a TRACKED (non-active) label's "
              "catalogue is surfaced")
        eq(matches[40001]["catno"], "40-001", "with the right catalogue number")
        eq(matches[40001]["matched_by"], "catno", "matched by catalogue number")
        check(30001 not in matches,
              "it is never reported against the currently active label — "
              "that folder is already visible in the normal Incoming view")

        res_cl2 = P.cross_label_check("owned")
        eq(res_cl2["rows"], [],
           "checking a role with no matching roots returns nothing, not an error")

        print("\n[fix_tags]")
        import numpy as np
        import soundfile as sf
        from mutagen.flac import FLAC

        def flac_track(path, title, artist=None, tracknumber=None):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            sf.write(path, np.zeros(4410, dtype="float32"), 44100, format="FLAC")
            f = FLAC(path)
            f["title"] = title
            if artist is not None:
                f["artist"] = artist
            if tracknumber is not None:
                f["tracknumber"] = tracknumber
            f.save()

        ft_root = os.path.join(tmp, "fix-tags-incoming")
        ft_release = os.path.join(ft_root, "(FT-001) Fix Tags Release (2001)")
        # Track 1: no artist, no tracknumber — both should get filled in.
        flac_track(os.path.join(ft_release, "01 - Alpha.flac"), "Alpha")
        # Track 2: already tagged correctly — only_missing must leave it alone.
        flac_track(os.path.join(ft_release, "02 - Beta.flac"), "Beta",
                   artist="Right Artist", tracknumber="2")
        P.add_root(ft_root, "owned")

        ft_catno = "FT-001"
        ft_key = sorted(P.L.catno_keys(ft_catno))[0]
        P.L.LabelCache(P.CACHE_DIR, P.load_state()["label_id"]).save_tracklists(
            {ft_key: {"tracks": ["Alpha", "Beta"]}})
        P._write_scan({"label_id": P.load_state()["label_id"], "rows": [
            {"folder": ft_release, "artist": "Right Artist", "catno": ft_catno,
             "title": "Fix Tags Release"},
        ]})

        r_dry = P.fix_tags([ft_release], dry_run=True)
        eq(r_dry["results"][0]["fixed_files"], 1,
           "dry run reports exactly the one file that needs fixing")
        eq(FLAC(os.path.join(ft_release, "01 - Alpha.flac")).get("artist"), None,
           "dry run writes nothing to disk")

        r_fix = P.fix_tags([ft_release])
        check(r_fix["ok"], "fix_tags reports ok", str(r_fix))
        eq(r_fix["results"][0]["fixed_files"], 1, "exactly one file actually needed a write")

        tags1 = FLAC(os.path.join(ft_release, "01 - Alpha.flac"))
        eq(tags1.get("artist"), ["Right Artist"],
           "the missing artist was filled in from the catalogue")
        eq(tags1.get("tracknumber"), ["1"],
           "the missing track number was filled in from tracklist position")

        tags2 = FLAC(os.path.join(ft_release, "02 - Beta.flac"))
        eq(tags2.get("artist"), ["Right Artist"],
           "track 2's already-correct artist tag is unchanged")
        eq(tags2.get("tracknumber"), ["2"],
           "track 2's already-correct track number is unchanged")

        r_again = P.fix_tags([ft_release])
        eq(r_again["results"][0]["fixed_files"], 0,
           "run again: nothing left to fix, reported as such — not re-written")

        outside_ft = os.path.join(tmp, "somewhere-else-ft")
        os.makedirs(outside_ft, exist_ok=True)
        r_outside = P.fix_tags([outside_ft])
        check("outside every configured folder" in r_outside["results"][0]["reason"],
              "a folder outside every root is refused, same as move_folders")

        print("\n[get_artwork]")
        from mutagen.flac import FLAC, Picture

        def flac_with_picture(path, jpeg_bytes):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            sf.write(path, np.zeros(4410, dtype="float32"), 44100, format="FLAC")
            f = FLAC(path)
            pic = Picture()
            pic.data = jpeg_bytes
            pic.type = 3
            pic.mime = "image/jpeg"
            f.add_picture(pic)
            f["title"] = "Has Art"
            f.save()

        # A minimal-but-real JPEG (SOI + EOI markers) -- good enough for
        # mutagen's Picture to carry and for the written file to round-trip.
        FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 32 + b"\xff\xd9"

        art_root = os.path.join(tmp, "artwork-incoming")
        art_release = os.path.join(art_root, "(ART-001) Has Art Somewhere (2001)")
        flac_track(os.path.join(art_release, "01 - Bare.flac"), "Bare")
        flac_with_picture(os.path.join(art_release, "02 - Pictured.flac"), FAKE_JPEG)
        P.add_root(art_root, "owned")
        P._write_scan({"label_id": P.load_state()["label_id"], "rows": [
            {"folder": art_release, "artist": "Someone", "catno": "ART-001",
             "title": "Has Art Somewhere", "id": None},
        ]})

        r_art = P.get_artwork([art_release], cfg={})
        row_art = r_art["results"][0]
        check(row_art["ok"], "get_artwork reports ok", str(r_art))
        eq(row_art["source"], "local",
           "found the picture embedded in the OTHER file in the folder")
        saved = os.path.join(art_release, "folder.jpg")
        check(os.path.isfile(saved), "folder.jpg was actually written")
        with open(saved, "rb") as fh:
            eq(fh.read(), FAKE_JPEG, "…with exactly the extracted picture's bytes")

        r_art_again = P.get_artwork([art_release], cfg={})
        row_again = r_art_again["results"][0]
        check(row_again["ok"] and row_again["source"] == "folder-image",
              "run again: the folder.jpg just written now counts as already-present")

        no_art_root = os.path.join(tmp, "no-art-incoming")
        no_art_release = release(no_art_root, "(NOART-001) Nothing Here (2001)", tracks=1)
        P.add_root(no_art_root, "owned")
        P._write_scan({"label_id": P.load_state()["label_id"], "rows": [
            {"folder": art_release, "artist": "Someone", "catno": "ART-001",
             "title": "Has Art Somewhere", "id": None},
            {"folder": no_art_release, "artist": "Nobody", "catno": "NOART-001",
             "title": "Nothing Here", "id": None},
        ]})
        r_no_art = P.get_artwork([no_art_release], cfg={})
        check("no Discogs release id" in r_no_art["results"][0]["reason"],
              "no local picture and no Discogs id: refused, not a network call")

        print("\n[fetch_prices]")
        label_id = P.load_state()["label_id"]

        class FakeDiscogs:
            """Stands in for label_ref.Discogs — no network, records every
            call so the test can assert exactly which releases were fetched
            and which were correctly skipped."""
            calls = []

            def __init__(self, token, log=print):
                pass

            def release_price(self, release_id, curr_abbr=""):
                FakeDiscogs.calls.append((release_id, curr_abbr))
                return {"lowest_price": 4.0 + release_id, "num_for_sale": 3,
                        "have": 20, "want": 5}

        real_discogs = P.L.Discogs
        P.L.Discogs = FakeDiscogs
        try:
            P.L.LabelCache(P.CACHE_DIR, label_id).save_catalogue([
                {"id": 9001, "catno": "PR-001", "title": "Missing One",
                 "artist": "A", "year": 2001},
                {"id": 9002, "catno": "PR-002", "title": "Missing Two",
                 "artist": "B", "year": 2002},
                {"id": 9003, "catno": "PR-003", "title": "Already Have It",
                 "artist": "C", "year": 2003},
            ])
            P._write_scan({"label_id": label_id, "rows": [
                {"folder": "", "catno": "PR-001", "artist": "A", "title": "Missing One",
                 "year": 2001, "id": 9001, "key": "pr001", "status": "missing", "expected": 2},
                {"folder": "", "catno": "PR-002", "artist": "B", "title": "Missing Two",
                 "year": 2002, "id": 9002, "key": "pr002", "status": "missing", "expected": 3},
                {"folder": "/somewhere", "catno": "PR-003", "artist": "C",
                 "title": "Already Have It", "year": 2003, "id": 9003, "key": "pr003",
                 "status": "complete", "expected": 4},
            ]})

            r_fp = P.fetch_prices(cfg={})
            eq(r_fp["added"], 2, "fetched prices for exactly the two MISSING releases")
            eq(sorted(c[0] for c in FakeDiscogs.calls), [9001, 9002],
               "the already-complete release 9003 was never called for — no signal it needs")
            eq(FakeDiscogs.calls[0][1], "USD", "priced in USD")

            cached = P.L.LabelCache(P.CACHE_DIR, label_id).prices()
            price_1 = P.L.price_for({"catno": "PR-001"}, cached)
            check(price_1 is not None and price_1["lowest_price"] == 9005.0,
                  "the fetched price actually landed in the per-label cache")

            FakeDiscogs.calls = []
            r_fp_again = P.fetch_prices(cfg={})
            eq(r_fp_again["added"], 0,
               "run again: both missing releases already have a cached price — no re-fetch")
            eq(FakeDiscogs.calls, [], "…and no Discogs call was made at all")
        finally:
            P.L.Discogs = real_discogs

        print("\n[tag_incoming]")
        from mutagen.flac import FLAC as _FLAC2

        def untagged_flac(path, title=None):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            sf.write(path, np.zeros(4410, dtype="float32"), 44100, format="FLAC")
            if title is not None:
                f = _FLAC2(path)
                f["title"] = title
                f.save()

        ti_root = os.path.join(tmp, "tag-incoming")
        ti_release = os.path.join(ti_root, "(1994) Test Artist - Test Release WAV")
        t1 = os.path.join(ti_release, "01 - Track One.flac")
        t2 = os.path.join(ti_release, "02 - Track Two.flac")
        untagged_flac(t1, title="Track One")
        untagged_flac(t2, title="Track Two")
        P.add_root(ti_root, "incoming")

        ti_catno = "TI-001"
        ti_key = sorted(P.L.catno_keys(ti_catno))[0]
        P.L.LabelCache(P.CACHE_DIR, label_id).save_tracklists(
            {ti_key: {"tracks": ["Track One", "Track Two"]}})
        P._write_scan({"label_id": label_id, "rows": [
            {"folder": "", "incoming_folder": ti_release, "catno": ti_catno,
             "artist": "Test Artist", "title": "Test Release", "id": 888,
             "verdict": "new"},
        ]})

        r_dry_ti = P.tag_incoming([ti_release], dry_run=True)
        eq(r_dry_ti["results"][0]["tagged_files"], 2,
           "dry run: both untagged files would get written")
        eq(_FLAC2(t1).get("artist"), None, "dry run writes nothing to disk")

        r_ti = P.tag_incoming([ti_release])
        check(r_ti["ok"], "tag_incoming reports ok", str(r_ti))
        eq(r_ti["results"][0]["tagged_files"], 2, "both files actually got written")

        tags_t1 = _FLAC2(t1)
        eq(tags_t1.get("artist"), ["Test Artist"], "release artist written")
        eq(tags_t1.get("album"), ["Test Release"], "release title written as album")
        eq(tags_t1.get("catalognumber"), ["TI-001"], "catalogue number written")
        eq(tags_t1.get("title"), ["Track One"],
           "per-track title from the tracklist match — untouched since it was already there")
        eq(tags_t1.get("tracknumber"), ["1"], "per-track number from tracklist position")

        tags_t2 = _FLAC2(t2)
        eq(tags_t2.get("tracknumber"), ["2"], "track two got position 2")

        r_ti_again = P.tag_incoming([ti_release])
        eq(r_ti_again["results"][0]["tagged_files"], 0,
           "run again: every field already has a value — nothing re-written")

        # A "Various" catalogue artist must never get stamped onto a specific
        # folder's files — that would be actively wrong metadata.
        va_root = os.path.join(tmp, "tag-incoming-va")
        va_release = os.path.join(va_root, "(2000) Anyone - Comp Track WAV")
        untagged_flac(os.path.join(va_release, "01 - Comp Track.flac"))
        P.add_root(va_root, "incoming")
        P._write_scan({"label_id": label_id, "rows": [
            {"folder": "", "incoming_folder": ti_release, "catno": ti_catno,
             "artist": "Test Artist", "title": "Test Release", "id": 888, "verdict": "new"},
            {"folder": "", "incoming_folder": va_release, "catno": "VA-001",
             "artist": "Various", "title": "Some Compilation", "id": 889, "verdict": "new"},
        ]})
        r_va = P.tag_incoming([va_release])
        va_file = os.path.join(va_release, "01 - Comp Track.flac")
        eq(_FLAC2(va_file).get("artist"), None,
           "catalogue artist 'Various' is never written as a specific file's artist")
        eq(_FLAC2(va_file).get("album"), ["Some Compilation"],
           "…but the album/catno still get written")

        r_ti_outside = P.tag_incoming([os.path.join(tmp, "not-a-root")])
        check("outside every configured folder" in r_ti_outside["results"][0]["reason"],
              "a folder outside every root is refused, same as fix_tags")

        print("\n[search_orphans]")

        class FakeDiscogsSearch:
            calls = []

            def __init__(self, token, log=print):
                pass

            def search_release(self, query, label=""):
                FakeDiscogsSearch.calls.append((query, label))
                if "Gitana" in query:
                    # Title agrees closely with the folder's own "Mi Gitana"
                    # — this is the clean "added" path; title_match()'s own
                    # bar (and near-miss behaviour) is covered in label_ref_test.py.
                    return [{"id": 9101, "catno": "71-109",
                             "title": "Analogue Gipsy Dance - Mi Gitana", "year": "1996"}]
                if "Nothing Findable" in query:
                    return []
                if "Wrong Match" in query:
                    # A result comes back, but its title does not agree with
                    # ours closely enough — must be reported ambiguous, not added.
                    return [{"id": 9102, "catno": "99-999",
                            "title": "Completely Different Artist - Totally Unrelated Song",
                            "year": "2000"}]
                if "Already Catalogued" in query:
                    # Title agrees closely enough to pass — this checks the
                    # SEPARATE "already have this id" short-circuit, not the
                    # title-agreement bar.
                    return [{"id": 9001, "catno": "PR-001",
                            "title": "Someone - Track", "year": "2001"}]
                return []

        real_discogs2 = P.L.Discogs
        P.L.Discogs = FakeDiscogsSearch
        try:
            so_root = os.path.join(tmp, "search-orphans-incoming")
            os.makedirs(so_root, exist_ok=True)
            P.add_root(so_root, "incoming")
            # An OWNED orphan must never be searched — orphans on that side
            # are a different problem (a stray/wrong root), not "find this".
            owned_orphan_root = os.path.join(tmp, "search-orphans-owned")
            os.makedirs(owned_orphan_root, exist_ok=True)
            P.add_root(owned_orphan_root, "owned")

            P._write_scan({"label_id": label_id, "rows": [
                {"folder": "", "catno": "PR-001", "artist": "A", "title": "Missing One",
                 "year": 2001, "id": 9001, "key": "pr001", "status": "missing", "expected": 2},
            ], "orphans": [
                {"path": os.path.join(so_root, "(1996) Analogue Gipsy Dance - Mi Gitana FLAC"),
                 "name": "(1996) Analogue Gipsy Dance - Mi Gitana FLAC", "role": "incoming",
                 "catno": "1996", "title": "Analogue Gipsy Dance - Mi Gitana FLAC"},
                {"path": os.path.join(so_root, "(2000) Nothing Findable - Track WAV"),
                 "name": "(2000) Nothing Findable - Track WAV", "role": "incoming",
                 "catno": "2000", "title": "Nothing Findable - Track WAV"},
                {"path": os.path.join(so_root, "(2001) Wrong Match - Track WAV"),
                 "name": "(2001) Wrong Match - Track WAV", "role": "incoming",
                 "catno": "2001", "title": "Wrong Match - Track WAV"},
                {"path": os.path.join(so_root, "(2001) Already Catalogued - Track WAV"),
                 "name": "(2001) Already Catalogued - Track WAV", "role": "incoming",
                 "catno": "2001", "title": "Already Catalogued - Track WAV"},
                {"path": os.path.join(owned_orphan_root, "Some Owned Stray"),
                 "name": "Some Owned Stray", "role": "owned", "catno": "", "title": "Some Owned Stray"},
            ]})

            r_so = P.search_orphans(cfg={})
            eq(r_so, {"added": 1, "assigned": 2, "ambiguous": 1, "no_match": 1}, str(r_so))
            queried = [q for q, lbl_ in FakeDiscogsSearch.calls]
            check(not any("Owned Stray" in q for q in queried),
                  "an OWNED-side orphan is never searched at all")

            cat_after = P.L.LabelCache(P.CACHE_DIR, label_id).catalogue()
            new_row = next((r for r in cat_after if r.get("id") == 9101), None)
            check(new_row is not None, "the confident NEW match was added to the catalogue cache")
            eq(new_row, {"id": 9101, "catno": "71-109", "title": "Mi Gitana",
                        "artist": "Analogue Gipsy Dance", "year": "1996", "format": ""},
               "…in label_releases()'s own row shape")

            overrides_after = P.L.LabelCache(P.CACHE_DIR, label_id).folder_overrides()
            gitana_path = os.path.join(so_root, "(1996) Analogue Gipsy Dance - Mi Gitana FLAC")
            already_cat_path = os.path.join(so_root, "(2001) Already Catalogued - Track WAV")
            eq(overrides_after.get(gitana_path), 9101,
               "the newly-catalogued release is ALSO recorded as this folder's override")
            eq(overrides_after.get(already_cat_path), 9001,
               "the already-catalogued release (title-wording gap) is assigned to its folder")

            FakeDiscogsSearch.calls = []
            r_so_again = P.search_orphans(cfg={})
            # Re-running still finds the same 4 incoming orphans (the scan
            # itself is not re-run by this function) — but the Gitana one
            # is now catalogued, so it should report as "already catalogued"
            # (still assigned, just no second catalogue entry).
            eq(r_so_again["added"], 0,
               "run again without a rescan: the already-added release is not re-added")
            eq(r_so_again["assigned"], 2,
               "…but both folders are still (re-)assigned their override — idempotent")
        finally:
            P.L.Discogs = real_discogs2

        print("\n[rename_incoming]")
        ri_root = os.path.join(tmp, "rename-incoming")
        ri_release = os.path.join(ri_root, "(1994) Test Artist - Test Release WAV")
        untagged_flac(os.path.join(ri_release, "01 - Track.flac"))
        P.add_root(ri_root, "incoming")
        P._write_scan({"label_id": label_id, "rows": [
            {"folder": "", "incoming_folder": ri_release, "catno": "RI-001?",
             "artist": "Test Artist", "title": "Weird : Title / Here", "year": 1994,
             "id": 999, "verdict": "new"},
        ]})

        r_ri_dry = P.rename_incoming([ri_release], dry_run=True)
        check(r_ri_dry["results"][0]["ok"] and os.path.isdir(ri_release),
              "dry run reports ok and does not touch the filesystem")

        r_ri = P.rename_incoming([ri_release])
        check(r_ri["ok"], "rename_incoming reports ok", str(r_ri))
        new_path = r_ri["results"][0]["new_path"]
        check(os.path.isdir(new_path) and not os.path.isdir(ri_release),
              "the folder actually moved to the new name")
        check(all(c not in os.path.basename(new_path) for c in '<>:"/\\|?*'),
              "illegal Windows filename characters (from the catno/title) were stripped")

        r_ri_again = P.rename_incoming([new_path])
        eq(r_ri_again["results"][0]["reason"], "not matched to a catalogue release in the last scan — rescan first",
           "the OLD path is gone from the cached scan now — renaming again needs a fresh scan first")

        collide_root = os.path.join(tmp, "rename-collide")
        collide_release = os.path.join(collide_root, "(1995) Someone - Another WAV")
        untagged_flac(os.path.join(collide_release, "01 - Track.flac"))
        os.makedirs(os.path.join(collide_root, "(RI-002) Blocking Name (1995)"), exist_ok=True)
        P.add_root(collide_root, "incoming")
        P._write_scan({"label_id": label_id, "rows": [
            {"folder": "", "incoming_folder": collide_release, "catno": "RI-002",
             "artist": "Someone", "title": "Blocking Name", "year": 1995,
             "id": 998, "verdict": "new"},
        ]})
        r_ri_collide = P.rename_incoming([collide_release])
        check("already exists at the target name" in r_ri_collide["results"][0]["reason"],
              "a name collision is refused, not overwritten or silently numbered")
        check(os.path.isdir(collide_release), "…and the source folder is untouched")

        print("\n[missing_releases export: rarity sort]")
        P._write_scan({"label_id": label_id, "rows": [
            {"catno": "R-1", "artist": "X", "title": "Rare One", "year": 2000, "id": 1,
             "expected": 2, "status": "missing", "rarity": "rare — long hunt",
             "price": {"lowest_price": 50.0}},
            {"catno": "C-1", "artist": "Y", "title": "Cheap Two", "year": 2000, "id": 2,
             "expected": 2, "status": "missing", "rarity": "cheap and common",
             "price": {"lowest_price": 8.0}},
            {"catno": "C-2", "artist": "Z", "title": "Cheap One", "year": 2000, "id": 3,
             "expected": 2, "status": "missing", "rarity": "cheap and common",
             "price": {"lowest_price": 3.0}},
            {"catno": "U-1", "artist": "W", "title": "Unpriced", "year": 2000, "id": 4,
             "expected": 2, "status": "missing", "rarity": "", "price": {}},
            {"catno": "H-1", "artist": "V", "title": "Not Missing", "year": 2000, "id": 5,
             "expected": 2, "status": "complete", "rarity": "cheap and common",
             "price": {"lowest_price": 1.0}},
        ]})
        _fn, text = P.export("missing_releases")
        import csv as _csv, io as _io
        csv_rows = list(_csv.reader(_io.StringIO(text)))
        eq(csv_rows[0][:2], ["catno", "artist"], "header row is as expected")
        titles = [row[2] for row in csv_rows[1:]]
        eq(titles, ["Cheap One", "Cheap Two", "Rare One", "Unpriced"],
           "cheap-and-common first (cheapest first within it), then rare, "
           "then never-priced last — and the COMPLETE release is not in the export at all")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d checks, %d failed" % (ran, len(fails)))
    print("PASS" if not fails else "FAILED:\n  - " + "\n  - ".join(fails))
    return 1 if fails else 0


sys.exit(main())
