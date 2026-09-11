#!/usr/bin/env python3
"""Boot the web UI on a spare port and assert the page and API still work.

Run it after EVERY change to web_ui.py, templates/ or static/:

    python3 smoke_test.py            # boots its own copy on port 8099
    python3 smoke_test.py --port 8082 --no-boot   # test an already-running one

It is deliberately blunt. It does not care whether the answers are correct,
only that the app boots, serves every tab, serves its own CSS and JS, and
answers every read-only endpoint without a 500. That is exactly the class of
mistake a blind string substitution into a 4,000-line file produces.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent

# Every tab in the nav bar. A rename that misses one shows up here.
TABS = ["Pipeline", "Direct", "Session", "Library", "Tools", "Telegram", "Labels",
        "Spek-tro"]

# Element ids the JavaScript reaches for on load. If one goes missing the page
# still renders and then silently does nothing, which is the worst failure.
ELEMENT_IDS = [
    "prov-list", "direct-prov-list", "doctor-text", "tool-log",
    # Labels tab
    "lb-roots", "lb-rows", "lb-summary", "lb-log", "lb-cat-status",
    # language selector
    "lang-sel",
]

# Read-only endpoints: safe to call repeatedly, must never 500.
ENDPOINTS = [
    "/api/health",
    "/api/doctor",
    "/api/config",
    "/api/naming",
    "/api/fs/places",
    "/api/library/stats",
    "/api/session/files",
    "/api/fakeflac/suspects",
    "/api/tg/status",
    "/api/label/status",
    # These answer {ok:false,...} with HTTP 200 before a scan exists, which is
    # the point: a tab must be able to say "nothing yet" without a 500.
    "/api/label/rows",
    "/api/label/orphans",
    "/api/label/moves",
]

ASSETS = ["/static/app.css", "/static/app.js",
          "/static/i18n.js", "/static/i18n-lang.js"]

failures = []
checks = 0


def check(ok, label, detail=""):
    global checks
    checks += 1
    if ok:
        print("  ok    %s" % label)
    else:
        print("  FAIL  %s %s" % (label, detail))
        failures.append("%s %s" % (label, detail))


def get(base, path, timeout=30):
    """Return (status, body-bytes, content-type). Never raises for HTTP codes."""
    req = urllib.request.Request(base + path, headers={"User-Agent": "smoke"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")
    except Exception as e:                        # connection refused, timeout
        return 0, str(e).encode(), ""


def wait_until_up(base, proc, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        if get(base, "/api/health", timeout=3)[0] == 200:
            return True
        time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0,
                    help="0 (the default) asks the OS for a free port")
    ap.add_argument("--no-boot", action="store_true",
                    help="test a server that is already running on --port")
    args = ap.parse_args()
    if not args.port:
        # Never hardcode a port: 8099 is the radio dashboard, 8082 the real
        # instance, and a clash reads as "the server never came up".
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        args.port = s.getsockname()[1]
        s.close()
    base = "http://127.0.0.1:%d" % args.port

    proc = None
    log = None
    if not args.no_boot:
        # A spare port and a throwaway log, so this never disturbs the real one.
        log = open(HERE / "smoke_test.log", "w")
        proc = subprocess.Popen(
            [sys.executable, str(HERE / "web_ui.py"),
             "--host", "127.0.0.1", "--port", str(args.port)],
            stdout=log, stderr=subprocess.STDOUT, cwd=str(HERE),
            env=dict(os.environ, MUSIC_ORGANISER_SMOKE="1"))
        print("booting on port %d (pid %d)" % (args.port, proc.pid))

    try:
        if not wait_until_up(base, proc):
            print("SERVER NEVER CAME UP — smoke_test.log:")
            if log:
                log.flush()
                print((HERE / "smoke_test.log").read_text()[-4000:])
            return 1

        print("\n[page]")
        status, body, _ = get(base, "/")
        html = body.decode("utf-8", "replace")
        check(status == 200, "GET /", "-> %s" % status)
        check(len(html) > 20000, "page is not a stub", "%d bytes" % len(html))
        for tab in TABS:
            check(">%s<" % tab in html or ">%s<" % tab.upper() in html
                  or tab in html, "tab %r present" % tab)
        for eid in ELEMENT_IDS:
            check('id="%s"' % eid in html or "id='%s'" % eid in html,
                  "element id %r present" % eid)
        check("/static/app.css" in html, "links its stylesheet")
        check("/static/app.js" in html, "links its script")
        check("__ASSETV__" not in html, "cache-buster was substituted")
        check("<style>" not in html, "no inline <style> left behind")

        print("\n[assets]")
        for a in ASSETS:
            status, body, ctype = get(base, a)
            check(status == 200, "GET %s" % a, "-> %s" % status)
            check(len(body) > 1000, "%s is not empty" % a, "%d bytes" % len(body))
        status, body, ctype = get(base, "/static/app.js")
        check("javascript" in ctype, "app.js served as JavaScript", ctype)

        # An id rule with a display: property silently beats [hidden], and the
        # element then ignores el.hidden=true forever. That put a blank,
        # undismissable error bar across the top of the page. The global rule
        # is what stops it, so guard the rule itself.
        css = get(base, "/static/app.css")[1].decode("utf-8", "replace")
        check("[hidden]{display:none!important}" in css.replace(" ", ""),
              "[hidden] is forced, so el.hidden works")
        # tests/brave_doctor_bar_check.py proves it in a real browser;
        # computed style is the only honest check and needs one.

        # The tab buttons must carry data-tab. switchTab() used to find the
        # active button by comparing its TEXT to a hard-coded English map,
        # which a translation breaks completely (and which had already gone
        # stale for Telegram and SPEK-TRO).
        check(html.count('data-tab="') >= 8,
              "every tab button has a data-tab key",
              "%d found" % html.count('data-tab="'))
        check("textContent===labels[name]" not in
              get(base, "/static/app.js")[1].decode("utf-8", "replace"),
              "switchTab no longer matches tabs by their text")

        print("\n[i18n]")
        langjs = get(base, "/static/i18n-lang.js")[1].decode("utf-8", "replace")
        check("window.I18N_LANGS" in langjs, "dictionaries are exported")
        for word in ('"Pipeline"', '"Labels"', '"Scan folders"'):
            check(word in langjs, "es dictionary has %s" % word)
        # A duplicate key silently loses one translation.
        import re as _re
        keys = _re.findall(r'^\s{4}"((?:[^"\\]|\\.)*)":', langjs, _re.M)
        dupes = {k for k in keys if keys.count(k) > 1}
        check(not dupes, "no duplicate dictionary keys",
              ", ".join(sorted(dupes))[:80])
        check(len(keys) > 300, "dictionary is populated", "%d keys" % len(keys))

        print("\n[api]")
        for ep in ENDPOINTS:
            status, body, ctype = get(base, ep)
            check(status == 200, "GET %s" % ep, "-> %s" % status)
            if status == 200:
                try:
                    json.loads(body.decode("utf-8", "replace"))
                    check(True, "%s is valid JSON" % ep)
                except Exception as e:
                    check(False, "%s is valid JSON" % ep, str(e))

    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        if log:
            log.close()

    print("\n%d checks, %d failed" % (checks, len(failures)))
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  - %s" % f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
