"""Check in a REAL browser that the config-doctor bar obeys `hidden`.

    python3 tests/brave_doctor_bar_check.py

The bug this guards: `#doctor-bar{display:flex}` is specificity 1-0-0 and
outranks the browser's own `[hidden]{display:none}` at 0-1-0, so the element
ignored `el.hidden = true` entirely. It sat across the top of the page, blank
(nothing had filled it) and its "dismiss" button did nothing.

Computed style is the only honest way to test that: the attribute is set
either way, and reading the DOM would have shown a pass while the user was
looking at the bar.

⚠ `chromium-browser` on this box is a dead wrapper — Brave is the browser
  that actually runs headless here.
"""
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

import websockets

URL = os.environ.get("MO_URL", "http://127.0.0.1:8082/")
SHOTS = os.environ.get("SHOT_DIR", "/tmp/mo-doctor-check")
PROFILE = SHOTS + "/brave-profile"
PORT = 9227

os.makedirs(SHOTS, exist_ok=True)

# Own session, so the whole process group can be killed at the end: terminate()
# on the parent alone leaves headless children fighting over the profile dir,
# and the next run silently never loads.
proc = subprocess.Popen([
    "/usr/bin/brave-browser", "--headless=new", "--disable-gpu", "--no-sandbox",
    "--remote-debugging-port=%d" % PORT, "--user-data-dir=%s" % PROFILE,
    "--window-size=1500,950", "--no-first-run", "--disable-brave-update",
    "about:blank",
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def targets():
    for _ in range(40):
        try:
            return json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/json" % PORT, timeout=2).read())
        except Exception:
            time.sleep(0.5)
    raise SystemExit("Brave never opened its debug port")


class CDP:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def send(self, method, **params):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method,
                                       "params": params}))
        while True:
            m = json.loads(await self.ws.recv())
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError("%s: %s" % (method, m["error"]))
                return m.get("result", {})

    async def js(self, expr):
        r = await self.send("Runtime.evaluate", expression=expr,
                            awaitPromise=True, returnByValue=True)
        return r.get("result", {}).get("value")


failures = []


def check(ok, label, detail=""):
    print(("  ok    " if ok else "  FAIL  ") + label + (" " + detail if detail else ""))
    if not ok:
        failures.append(label + " " + detail)


async def main():
    page = [t for t in targets() if t["type"] == "page"][0]
    async with websockets.connect(page["webSocketDebuggerUrl"],
                                  max_size=40 * 1024 * 1024) as ws:
        cdp = CDP(ws)
        await cdp.send("Page.enable")
        await cdp.send("Runtime.enable")
        await cdp.send("Page.navigate", url=URL)
        # checkDoctor() runs on load and fetches /api/doctor; give it room.
        await asyncio.sleep(6)

        title = await cdp.js("document.title")
        print("loaded: %r" % title)

        doctor = await cdp.js("""(()=>{
          const b=document.getElementById('doctor-bar');
          if(!b) return null;
          const cs=getComputedStyle(b);
          return {hidden:b.hidden, display:cs.display, h:b.offsetHeight,
                  text:(document.getElementById('doctor-text')||{}).textContent||''};
        })()""")
        api_ok = await cdp.js(
            "fetch('/api/doctor').then(r=>r.json()).then(d=>!!d.ok)")

        print("\n[config doctor]  /api/doctor ok=%s" % api_ok)
        check(doctor is not None, "#doctor-bar exists")
        if doctor:
            print("  attr hidden=%s  computed display=%s  height=%dpx  text=%r"
                  % (doctor["hidden"], doctor["display"], doctor["h"],
                     doctor["text"][:60]))
            if api_ok:
                # Nothing wrong with the config, so the bar must not be there.
                check(doctor["hidden"] is True, "bar is marked hidden")
                check(doctor["display"] == "none",
                      "bar is actually not displayed", "(display=%s)" % doctor["display"])
                check(doctor["h"] == 0, "bar takes up no height",
                      "(%dpx)" % doctor["h"])
            else:
                check(doctor["h"] > 0, "bar is shown when there IS a problem")
                check(doctor["text"].strip() != "",
                      "bar is not blank when shown")

        # A bar that is shown must still dismiss. Force it visible, click, look.
        dismissed = await cdp.js("""(()=>{
          const b=document.getElementById('doctor-bar');
          document.getElementById('doctor-text').textContent='test problem';
          b.hidden=false;
          const shownH=b.offsetHeight;
          document.querySelector('#doctor-bar .doc-x').click();
          return {shownH:shownH, afterH:b.offsetHeight,
                  display:getComputedStyle(b).display};
        })()""")
        print("\n[dismiss]")
        check(dismissed["shownH"] > 0, "bar can be shown at all",
              "(%dpx)" % dismissed["shownH"])
        check(dismissed["afterH"] == 0, "dismiss actually hides it",
              "(still %dpx, display=%s)" % (dismissed["afterH"], dismissed["display"]))

        # ...and stay dismissed when the 60s re-check fires.
        await cdp.js("checkDoctor()")
        await asyncio.sleep(1.5)
        after = await cdp.js(
            "document.getElementById('doctor-bar').offsetHeight")
        check(after == 0, "stays dismissed after a re-check", "(%dpx)" % after)

        # The other element that had the same trap.
        spec = await cdp.js("""(()=>{
          const i=document.getElementById('ff-spec-img');
          if(!i) return null;
          i.hidden=true;
          return {display:getComputedStyle(i).display, h:i.offsetHeight};
        })()""")
        print("\n[ff-spec-img, same trap]")
        if spec is None:
            check(False, "#ff-spec-img exists")
        else:
            check(spec["display"] == "none", "obeys hidden",
                  "(display=%s)" % spec["display"])

        shot = await cdp.send("Page.captureScreenshot", format="png")
        path = os.path.join(SHOTS, "doctor-bar.png")
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(shot["data"]))
        print("\nscreenshot -> %s" % path)

    print("\n%s" % ("PASS" if not failures else "FAILED:\n  - " +
                    "\n  - ".join(failures)))
    return 1 if failures else 0


try:
    rc = asyncio.run(main())
finally:
    try:
        os.killpg(os.getpgid(proc.pid), 15)
    except Exception:
        pass
sys.exit(rc)
